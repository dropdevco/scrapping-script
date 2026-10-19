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
