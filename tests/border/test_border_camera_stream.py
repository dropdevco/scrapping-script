"""Finding streams, reading frames, and the calibration file — no network.

A dead, frozen or truncated stream must raise, never read as an empty bridge, so each
failure is exercised against a small video written to a temporary directory.
"""
from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest import mock

import pytest

pytest.importorskip("cv2")

import cv2
import numpy as np

from scraper.border.camera import stream
from scraper.border.camera.calibration import (
    Strip,
    camera_from_dict,
    load_cameras,
)
from scraper.border.camera.measure import LaneEstimate
from scraper.border.camera.reader import as_signal

PREFIX = "Transmisión en vivo Puente Internacional "


def clip(path: Path, frames: list[np.ndarray], fps: float = 2.0) -> str:
    out = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), fps, (64, 36))
    for f in frames:
        out.write(f)
    out.release()
    return str(path)


def varied(n: int) -> list[np.ndarray]:
    rng = np.random.default_rng(3)
    return [rng.integers(0, 255, (36, 64, 3), dtype=np.uint8) for _ in range(n)]


def numbered(n: int) -> list[np.ndarray]:
    """Frames that carry their own index: sixteens on the left half, units on the right."""
    out = []
    for i in range(n):
        f = np.empty((36, 64, 3), np.uint8)
        f[:, :32], f[:, 32:] = (i // 16) * 16 + 8, (i % 16) * 16 + 8
        out.append(f)
    return out


def number(frame: np.ndarray) -> int:
    return int(frame[:, :32].mean() // 16) * 16 + int(frame[:, 32:].mean() // 16)


def picture(shade: int = 0) -> np.ndarray:
    """One fixed grey scene, BGR, at the whole-scene probe's own size (320x180)."""
    grey = np.random.default_rng(5).integers(40, 200, (180, 320), dtype=np.uint8) + shade
    return np.repeat(grey[:, :, None], 3, axis=2)


def quiet(n: int, shade: int = 0) -> list[np.ndarray]:
    """A live road with nothing moving: each frame is the scene with a fresh 1 % of its pixels
    one grey level up, so a pair differs by about 0.02 on average, never by 0.3 and never by
    nothing (the night of 2026-10-27: 0 of 2362 pairs identical, the smallest 0.0095)."""
    rng = np.random.default_rng(7)
    out = []
    for _ in range(n):
        f = picture(shade)
        f[rng.random((180, 320)) < 0.01] += 1
        out.append(f)
    return out


def ticking(n: int, every: int) -> list[np.ndarray]:
    """Quiet frames whose clock box (the top-left 80x18) changes shade on every `every`-th pair."""
    out = []
    for i, f in enumerate(quiet(n)):
        f[:18, :80] = 60 if (i // every) % 2 else 160
        out.append(f)
    return out


CLOCK = (0.0, 0.0, 0.25, 0.1)          # ticking()'s clock box, as fractions of the frame


def feed(check, frames: list[np.ndarray]) -> list[bool]:
    return [check(f) for f in frames]


class Finding(unittest.TestCase):
    LISTING = (
        stream.Listed("a1", PREFIX + "Zaragoza (Av. Waterfill) - Vista Sur", True),
        stream.Listed("a2", PREFIX + "Zaragoza (Sur)", True),
        stream.Listed("a3", PREFIX + "Córdova - Américas (Norte)", True),
        stream.Listed("a4", PREFIX + "Guadalupe (Norte)", False),
    )

    def setUp(self):
        patcher = mock.patch.object(stream, "channel_streams", return_value=list(self.LISTING))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_the_title_suffix_picks_the_one_camera(self):
        self.assertEqual(stream.find("ch", "Zaragoza (Sur)").video_id, "a2")

    def test_accents_and_case_do_not_matter(self):
        self.assertEqual(stream.find("ch", "cordova - americas (norte)").video_id, "a3")

    def test_listed_but_not_live_is_down(self):
        with self.assertRaises(stream.StreamDown):
            stream.find("ch", "Guadalupe (Norte)")

    def test_unknown_title_is_down(self):
        with self.assertRaises(stream.StreamDown):
            stream.find("ch", "Santa Teresa (Norte)")

    def test_youtubes_own_refusal_is_down_with_its_reason(self):
        refused = mock.Mock(returncode=1, stdout="",
                            stderr="ERROR: [youtube] a2: We're experiencing technical difficulties.")
        with (mock.patch.object(stream, "_ytdlp", return_value=refused),
              self.assertRaisesRegex(stream.StreamDown, "technical difficulties")):
            stream.resolve("ch", "Zaragoza (Sur)")


class FakeChannel:
    """yt-dlp for one channel with one camera, whose operator can restart its stream under a
    new video id. Counts how often the channel is listed."""

    def __init__(self):
        self.live_id, self.listings = "old", 0

    def __call__(self, *args, timeout=120):
        if "--flat-playlist" in args:
            self.listings += 1
            return mock.Mock(returncode=0, stderr="",
                             stdout=f"{self.live_id}\tis_live\t{PREFIX}Zaragoza (Sur)\n")
        video = args[-1].rsplit("=", 1)[1]
        if video != self.live_id:
            return mock.Mock(returncode=1, stdout="",
                             stderr=f"ERROR: [youtube] {video}: This live event has ended.")
        return mock.Mock(returncode=0, stdout=f"https://hls.example/{video}.m3u8\n", stderr="")


class ListingCache(unittest.TestCase):
    """The channel listing is cached for LISTING_TTL, but a stream restarted under a new video
    id must be found on the next try, not after up to 10 min of the old id."""

    def setUp(self):
        self.channel = FakeChannel()
        for name, value in (("_listing", {}), ("_resolved", {}), ("_ytdlp", self.channel)):
            patcher = mock.patch.object(stream, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_a_listed_stream_is_resolved_from_the_cached_listing(self):
        stream.resolve("ch", "Zaragoza (Sur)")
        stream.resolve("ch", "Zaragoza (Sur)")
        self.assertEqual(self.channel.listings, 1)

    def test_a_failed_resolve_lists_the_channel_afresh(self):
        self.assertEqual(stream.resolve("ch", "Zaragoza (Sur)"), "https://hls.example/old.m3u8")
        self.channel.live_id = "new"                          # the operator restarts it
        with self.assertRaisesRegex(stream.StreamDown, "has ended"):
            stream.resolve("ch", "Zaragoza (Sur)")            # the cached listing names "old"
        self.assertEqual(stream.resolve("ch", "Zaragoza (Sur)"), "https://hls.example/new.m3u8")
        self.assertEqual(self.channel.listings, 2)

    def test_a_read_that_found_its_stream_gone_lists_the_channel_afresh(self):
        old = stream.resolve("ch", "Zaragoza (Sur)")
        self.channel.live_id = "new"
        stream.forget(old)
        self.assertEqual(stream.resolve("ch", "Zaragoza (Sur)"), "https://hls.example/new.m3u8")
        self.assertEqual(self.channel.listings, 2)

    def test_a_url_resolve_did_not_give_keeps_the_listing(self):
        stream.resolve("ch", "Zaragoza (Sur)")
        stream.forget("https://zoocams.example/x.m3u8")       # a City camera's own URL
        stream.resolve("ch", "Zaragoza (Sur)")
        self.assertEqual(self.channel.listings, 1)


class Frames(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())

    def test_reads_the_requested_seconds_at_the_requested_rate(self):
        path = clip(self.dir / "ok.mp4", varied(30))
        self.assertEqual(len(list(stream.frames(path, fps=2, seconds=10))), 20)

    def test_a_stream_that_ends_early_is_lost(self):
        path = clip(self.dir / "short.mp4", varied(6))
        with self.assertRaises(stream.StreamLost):
            list(stream.frames(path, fps=2, seconds=10))

    def test_a_picture_that_stops_changing_is_frozen(self):
        still = varied(1)[0]
        path = clip(self.dir / "frozen.mp4", varied(4) + [still] * 150)
        with self.assertRaises(stream.StreamFrozen):
            list(stream.frames(path, fps=2, seconds=70))

    def test_an_unopenable_source_is_lost(self):
        with self.assertRaises(stream.StreamLost):
            list(stream.frames(str(self.dir / "missing.mp4"), fps=2, seconds=1))

    def test_frames_stay_1_over_fps_apart_when_the_clip_rate_is_not_a_multiple(self):
        # The reader times them as 0.5 s apart. Every 12th frame of a 25 fps clip is 0.48 s:
        # 0.38 s off by 10 s in, and speeds 3 % low. Each is now the frame nearest its slot.
        for source in (25, 15, 24):
            path = clip(self.dir / f"rate{source}.mp4", numbered(250), fps=source)
            got = [number(f) for f in stream.frames(path, fps=2, seconds=10)]
            self.assertEqual(len(got), 20)
            off = max(abs(n - k * source / 2) for k, n in enumerate(got))
            self.assertLessEqual(off, 0.5, (source, got))

    def test_a_clip_slower_than_the_rate_is_refused(self):
        # Every frame of it would be timed as 0.5 s apart when they are 1 s apart.
        path = clip(self.dir / "slow.mp4", varied(10), fps=1)
        with self.assertRaises(ValueError):
            next(stream.frames(path, fps=2, seconds=5))

    def test_a_whole_scene_read_faster_than_2_fps_is_refused_before_it_is_opened(self):
        # Refused, not lost: the rate is checked before the (missing) file is opened.
        with self.assertRaises(ValueError):
            next(stream.frames(str(self.dir / "missing.mp4"), fps=4, seconds=1))


class FrozenWholeScene(unittest.TestCase):
    """A City camera has no burned-in clock: frozen means its picture repeats exactly."""

    def test_a_quiet_live_road_is_never_frozen(self):
        # The first whole-scene rule called this frozen after 20 s, 50.4 times an hour on the
        # night of 2026-10-27, and each time cut the line's count.
        check = stream.frozen_check(2.0, None)
        moved = feed(check, quiet(600))                       # 300 s at 2 fps
        self.assertFalse(any(moved))

    def test_a_road_that_changes_by_one_pixel_a_pair_is_never_frozen(self):
        # Compared at 320x180, not 64x16: averaged down, a change this small rounds away and
        # every pair reads as a repeat.
        frames = [picture() for _ in range(120)]              # 60 s at 2 fps
        for i, f in enumerate(frames):
            f[i % 180, (7 * i) % 320] += 1                    # one grey level, one pixel
        check = stream.frozen_check(2.0, None)
        self.assertFalse(any(feed(check, frames)))

    def test_a_third_of_20_s_of_pairs_repeating_exactly_is_frozen(self):
        check = stream.frozen_check(2.0, None)
        fresh = iter(quiet(41))
        frames = [next(fresh)]
        for pair in range(40):                                # 13 repeats among 40 pairs
            frames.append(frames[-1] if pair % 3 == 1 else next(fresh))
        feed(check, frames)                                   # 13 of 40 is not yet frozen
        with self.assertRaises(stream.StreamFrozen) as caught:
            check(frames[-1])                                 # 14 of 40 is
        self.assertEqual(caught.exception.seconds, stream.FROZEN_SECONDS)
        self.assertEqual(caught.exception.seconds, 20)
        self.assertIn("repeated exactly in 14 of the last 40 pairs", str(caught.exception))

    def test_a_black_picture_still_says_it_is_black(self):
        check = stream.frozen_check(2.0, None)
        with self.assertRaises(stream.StreamFrozen) as caught:
            feed(check, [np.zeros((180, 320, 3), np.uint8)] * 41)
        self.assertIn("is black", str(caught.exception))
        self.assertEqual(caught.exception.seconds, stream.FROZEN_SECONDS)

    def test_each_frame_says_whether_it_moved(self):
        check = stream.frozen_check(2.0, None)
        still, nudged = quiet(2)
        brighter = picture(shade=5)                           # 5 grey levels: a car going by
        self.assertEqual(feed(check, [still, nudged, brighter, brighter, still]),
                         [False, False, True, False, True])

    def test_half_a_grey_level_over_the_whole_scene_moved_and_a_fifth_did_not(self):
        # The runner's no-motion hold (QUIET_S) was measured with FROZEN_DIFF_SCENE at 0.3. On
        # the night of 2026-10-27 UTC any bar from 0.2 to 0.5 gave the same lag between moving
        # pairs (p99 8.4 s); at 1.0 it was 37 s, at 4.0 201 s.
        check = stream.frozen_check(2.0, None)
        half, fifth = picture(), picture()
        half[:90] += 1                                        # half its pixels 1 grey level up
        fifth[:36] += 1                                       # a fifth of them
        self.assertEqual(feed(check, [picture(), half, picture(), fifth]),
                         [False, True, True, False])

    def test_a_whole_scene_read_faster_than_2_fps_is_refused(self):
        # On one night native frames changed at least every 0.42 s (dusk and day: Q-32); pairs
        # closer than that can repeat exactly on a live picture.
        with self.assertRaises(ValueError):
            stream.frozen_check(2.5, None)
        stream.frozen_check(stream.SCENE_MAX_FPS, None)
        stream.frozen_check(12.0, CLOCK)                      # a clock camera may go faster

    def test_a_frozen_error_takes_back_20_s_unless_told_otherwise(self):
        self.assertEqual(stream.StreamFrozen("has not changed").seconds, stream.FROZEN_SECONDS)
        self.assertEqual(stream.StreamFrozen("nothing moved", 300).seconds, 300)


class FrozenClock(unittest.TestCase):
    """A camera with a burned-in clock keeps its rule: frozen when the clock stops changing."""

    def test_a_clock_that_stops_is_frozen_though_no_two_frames_repeat(self):
        # A frozen encoder re-encodes its last picture, so the frames still differ a little.
        check = stream.frozen_check(2.0, CLOCK)
        with self.assertRaises(stream.StreamFrozen) as caught:
            feed(check, quiet(41))
        self.assertIn("has not changed", str(caught.exception))
        self.assertEqual(caught.exception.seconds, stream.FROZEN_SECONDS)

    def test_a_tenth_of_the_pairs_changing_the_clock_is_still_frozen(self):
        check = stream.frozen_check(2.0, CLOCK)
        with self.assertRaises(stream.StreamFrozen):
            feed(check, ticking(200, every=10))               # 4 of every 40 pairs

    def test_more_than_a_tenth_is_alive_and_each_tick_is_a_move(self):
        check = stream.frozen_check(2.0, CLOCK)
        moved = feed(check, ticking(200, every=8))            # 5 of every 40 pairs
        self.assertEqual(moved, [i > 0 and i % 8 == 0 for i in range(200)])


class CalibrationFile(unittest.TestCase):
    def test_every_calibration_loads_and_sees_road(self):
        cameras = load_cameras()
        self.assertTrue(cameras)
        for cam in cameras.values():
            for lane in cam.lanes:
                strip = Strip(cam, lane, cam.frame_size)
                self.assertGreater(strip.rows_valid.mean(), 0.5, f"{cam.id}/{lane.lane}")
            for x, y, w, h in cam.mask:
                self.assertLessEqual(x + w, cam.frame_size[0], cam.id)
                self.assertLessEqual(y + h, cam.frame_size[1], cam.id)

    def test_titles_are_real_text_not_mojibake(self):
        # bota-south-approach was once saved as "CÃ³rdova" (UTF-8 read as cp1252): replays
        # from recorded clips never noticed, but the live stream could never have been found.
        for cam in load_cameras().values():
            self.assertNotIn("Ã", cam.title, cam.id)
            self.assertNotIn("Ã", cam.notes, cam.id)

    def test_bad_values_are_refused(self):
        raw = {"title": "t", "port_number": "1", "direction": "north", "frame_size": [10, 10],
               "ground": {"image": [[0, 9], [9, 9], [6, 2], [3, 2]], "width_m": 3, "length_m": 9},
               "lanes": [{"lane": "x", "across": [0, 1], "along": [0, 1], "booth": "middle"}]}
        with self.assertRaises(ValueError):
            camera_from_dict("bad", raw)
        raw["lanes"][0]["booth"] = "far"
        raw["direction"] = "east"
        with self.assertRaises(ValueError):
            camera_from_dict("bad", raw)


class Signals(unittest.TestCase):
    def test_an_unverified_lane_is_never_filed_under_a_cbp_lane(self):
        cam = load_cameras()["pdn-north"]
        est = LaneEstimate(camera=cam.id, lane=cam.lanes[0].lane, port_number=cam.port_number,
                           direction="north", state="queue", frames=120, seconds=60,
                           queue_m=40, speed_m_s=0.5, wait_min=12.4, low_min=10.0, high_min=15.6)
        sig = as_signal(est, cam.lanes[0], datetime(2026, 10, 19, 13, 0, tzinfo=UTC))
        self.assertIsNone(sig["lane"])
        self.assertEqual((sig["kind"], sig["source"], sig["minutes"]), ("measured", "cam:pdn-north", 12))
        self.assertEqual(sig["evidence"]["lane"], cam.lanes[0].lane)


if __name__ == "__main__":
    unittest.main()
