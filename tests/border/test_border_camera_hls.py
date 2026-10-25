"""The stamped HLS reader: playlists, MPEG-TS timestamps, and frames timed by the stream's clock.

The end-to-end cases generate a short HLS stream with ffmpeg and serve it from a local HTTP server,
once with PROGRAM-DATE-TIME (as YouTube does) and once with only Last-Modified (as the City of El
Paso does). They check that each frame's capture time comes from that clock, not from arrival.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import threading
import unittest
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from itertools import pairwise

import pytest

pytest.importorskip("cv2")

from scraper.border.camera.hls import (
    WRAP,
    Clock,
    HlsReader,
    TimelineCut,
    parse_playlist,
    pts_range,
)
from scraper.border.camera.stream import StreamLost

FFMPEG = shutil.which("ffmpeg")
T0 = datetime(2026, 10, 24, 18, 0, 0, tzinfo=UTC)


class Playlists(unittest.TestCase):
    def test_one_program_date_time_carries_forward_to_the_next_segments(self):
        text = ("#EXTM3U\n#EXT-X-TARGETDURATION:5\n#EXT-X-MEDIA-SEQUENCE:100\n"
                "#EXT-X-PROGRAM-DATE-TIME:2026-10-24T18:00:00.500+00:00\n"
                "#EXTINF:5.0,\na.ts\n#EXTINF:5.0,\nb.ts\n#EXTINF:4.0,\nc.ts\n")
        pl = parse_playlist(text, "https://example.org/live/index.m3u8")
        self.assertEqual([s.seq for s in pl.segments], [100, 101, 102])
        self.assertEqual(pl.segments[1].uri, "https://example.org/live/b.ts")
        self.assertEqual(pl.segments[2].program_time, T0 + timedelta(seconds=10.5))

    def test_a_city_playlist_has_no_program_time(self):
        text = ("#EXTM3U\n#EXT-X-TARGETDURATION:5\n#EXT-X-MEDIA-SEQUENCE:7\n"
                "#EXTINF:5,\nhttps://zoocams.example/x-00000007.ts\n")
        pl = parse_playlist(text, "https://zoocams.example/x.m3u8")
        self.assertIsNone(pl.segments[0].program_time)
        self.assertEqual(pl.segments[0].duration, 5.0)

    def test_a_master_playlist_is_refused(self):
        with self.assertRaises(StreamLost):
            parse_playlist("#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1\nv.m3u8\n", "https://e.org/")


class ClockMapping(unittest.TestCase):
    def test_a_frame_is_placed_by_its_distance_into_its_segment(self):
        clock = Clock()
        clock.add(90_000 * 100, 90_000 * 104, T0)            # a 5 s segment starting at T0
        self.assertEqual(clock.wall(90_000 * 102), T0 + timedelta(seconds=2))

    def test_the_33_bit_wrap_does_not_break_the_timeline(self):
        clock = Clock()
        start = WRAP - 90_000 * 2                             # 2 s before the counter wraps
        clock.add(start, (start + 90_000 * 4) % WRAP, T0)
        clock.add((start + 90_000 * 5) % WRAP, (start + 90_000 * 9) % WRAP, T0 + timedelta(seconds=5))
        self.assertEqual(clock.wall((start + 90_000 * 7) % WRAP), T0 + timedelta(seconds=7))

    def test_the_offset_is_a_median_so_one_late_stamp_does_not_move_it(self):
        clock = Clock()
        for k in range(5):
            late = 3 if k == 2 else 0                         # one segment written 3 s late
            clock.add(90_000 * 5 * k, 90_000 * (5 * k + 4),
                      T0 + timedelta(seconds=5 * k + late))
        self.assertEqual(clock.wall(90_000 * 21), T0 + timedelta(seconds=21))


def _serve(directory: str, handler=SimpleHTTPRequestHandler) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(handler, directory=directory))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


class Quiet(SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


@unittest.skipUnless(FFMPEG, "needs the ffmpeg command-line tool")
class EndToEnd(unittest.TestCase):
    """A 12 s stream at 10 fps in 2 s segments, its program time starting at a known instant."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
                        "testsrc=size=320x240:rate=10", "-t", "12", "-c:v", "libx264", "-g", "10",
                        "-pix_fmt", "yuv420p", "-f", "hls", "-hls_time", "2",
                        "-hls_list_size", "0", os.path.join(self.dir, "live.m3u8")],
                       check=True, timeout=60)
        with open(os.path.join(self.dir, "live.m3u8"), encoding="utf-8") as f:
            self.vod = f.read()
        self.segments = parse_playlist(self.vod, "http://x/").segments
        self.server = _serve(self.dir, Quiet)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}/"

    def _write(self, name: str, text: str) -> str:
        with open(os.path.join(self.dir, name), "w", encoding="utf-8") as f:
            f.write(text)
        return self.base + name

    def _with_pdt(self) -> str:
        lines = self.vod.replace("#EXTINF", f"#EXT-X-PROGRAM-DATE-TIME:{T0.isoformat()}\n#EXTINF", 1)
        return self._write("pdt.m3u8", lines)

    def _read(self, url: str, clock: str, seconds: float):
        reader = HlsReader(lambda: url, clock, (160, 120), 2.0, "chisme-test")
        self.addCleanup(reader.http.close)
        return reader, list(reader.frames(seconds))

    def test_frames_are_timed_by_the_program_date_time(self):
        _, got = self._read(self._with_pdt(), "pdt", 6)
        self.assertGreaterEqual(len(got), 12)
        self.assertLess(abs((got[0].at - T0).total_seconds()), 0.15)
        spacing = [(b.at - a.at).total_seconds() for a, b in pairwise(got)]
        self.assertTrue(all(abs(s - 0.5) < 0.06 for s in spacing), spacing)

    def test_without_program_time_last_modified_sets_the_clock(self):
        # Each segment file is stamped as written when its last frame was captured.
        start = T0
        for seg in self.segments:
            end = start + timedelta(seconds=seg.duration)
            path = os.path.join(self.dir, os.path.basename(seg.uri))
            os.utime(path, (end.timestamp(), end.timestamp()))
            start = end
        _, got = self._read(self._write("lm.m3u8", self.vod), "last-modified", 6)
        # Last-Modified is to the second, and a segment's last frame is one frame short of its
        # end, so the fit is good to about a second, not to a frame.
        self.assertLess(abs((got[0].at - T0).total_seconds()), 1.0)
        self.assertEqual(format_datetime(got[0].at, usegmt=True)[:16], format_datetime(T0, usegmt=True)[:16])

    @unittest.skipUnless(shutil.which("ffprobe"), "needs ffprobe")
    def test_the_segment_timestamps_are_read_from_the_transport_stream(self):
        # Checked against ffprobe's own reading of the same segment's video packets.
        path = os.path.join(self.dir, os.path.basename(self.segments[1].uri))
        out = subprocess.run([shutil.which("ffprobe"), "-v", "error", "-select_streams", "v",
                              "-show_entries", "packet=pts", "-of", "csv=p=0", path],
                             capture_output=True, text=True, check=True, timeout=30).stdout
        probed = [int(v.strip(" ,")) for v in out.splitlines() if v.strip(" ,").isdigit()]
        with open(path, "rb") as f:
            self.assertEqual(pts_range(f.read()), (min(probed), max(probed)))

    def test_a_jump_in_the_sequence_is_reported_as_a_gap(self):
        lines = self.vod.splitlines()
        head = [ln for ln in lines if ln.startswith("#EXT-X-") and "ENDLIST" not in ln]
        body = [ln for ln in lines if not ln.startswith("#EXT-X-") and not ln.startswith("#EXTM3U")]
        pairs = list(zip(body[0::2], body[1::2]))             # (#EXTINF, uri) per segment
        first = "\n".join(["#EXTM3U", *head, *[x for p in pairs[:2] for x in p]]) + "\n"
        later = "\n".join(["#EXTM3U", *[h.replace("MEDIA-SEQUENCE:0", "MEDIA-SEQUENCE:4") for h in head],
                           *[x for p in pairs[4:] for x in p], "#EXT-X-ENDLIST"]) + "\n"
        served = iter([first, later])

        class Changing(Quiet):
            def do_GET(self):
                if self.path.endswith("gap.m3u8"):
                    body = next(served, later).encode()
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                super().do_GET()

        server = _serve(self.dir, Changing)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        url = f"http://127.0.0.1:{server.server_address[1]}/gap.m3u8"
        reader = HlsReader(lambda: url, "last-modified", (160, 120), 2.0, "chisme-test")
        self.addCleanup(reader.http.close)
        with self.assertRaises(StreamLost):                   # the stream ends after the jump
            list(reader.frames(None))
        self.assertEqual(len(reader.gaps), 1)
        self.assertIn("2 segment(s)", reader.gaps[0].reason)


class ExpiredSegments(EndToEnd):
    """A segment listed but gone by the time it is fetched is a gap; the read goes on."""

    def test_a_segment_that_404s_is_a_gap_not_a_failure(self):
        gone = os.path.basename(self.segments[2].uri)
        os.remove(os.path.join(self.dir, gone))
        reader, got = self._read(self._with_pdt(), "pdt", 8)
        self.assertGreaterEqual(len(got), 12)          # 8 s of stream, 2 s of it missing
        self.assertEqual([g.reason for g in reader.gaps],
                         [f"segment {self.segments[2].seq} expired before it was fetched"])
        # The frames on either side keep their true times: the seconds of the missing segment
        # are simply absent.
        jumps = [(b.at - a.at).total_seconds() for a, b in pairwise(got)]
        self.assertGreater(max(jumps), 1.5)

    test_frames_are_timed_by_the_program_date_time = None
    test_without_program_time_last_modified_sets_the_clock = None
    test_the_segment_timestamps_are_read_from_the_transport_stream = None
    test_a_jump_in_the_sequence_is_reported_as_a_gap = None


@unittest.skipUnless(FFMPEG, "needs the ffmpeg command-line tool")
class TimelineCuts(EndToEnd):
    """An encoder restart behind a playlist: the read ends at the cut, nothing is stamped wrong."""

    def setUp(self):
        super().setUp()
        later = os.path.join(self.dir, "b")
        os.makedirs(later)
        subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
                        "testsrc=size=320x240:rate=10", "-t", "8", "-c:v", "libx264", "-g", "10",
                        "-pix_fmt", "yuv420p", "-output_ts_offset", "1000", "-f", "hls",
                        "-hls_time", "2", "-hls_list_size", "0",
                        os.path.join(later, "live.m3u8")], check=True, timeout=60)
        with open(os.path.join(later, "live.m3u8"), encoding="utf-8") as f:
            self.later = [ln for ln in f.read().splitlines()
                          if ln.startswith("#EXTINF") or ln.endswith(".ts")]

    def _head(self) -> str:
        head = self.vod.replace("#EXT-X-ENDLIST", "").rstrip("\n")
        return head.replace("#EXTINF", f"#EXT-X-PROGRAM-DATE-TIME:{T0.isoformat()}\n#EXTINF", 1)

    def _joined_text(self, tag: bool) -> str:
        tail = [ln if ln.startswith("#") else "b/" + ln for ln in self.later]
        cut = ["#EXT-X-DISCONTINUITY"] if tag else []
        return "\n".join([self._head(), *cut, *tail, "#EXT-X-ENDLIST"]) + "\n"

    def _joined(self, name: str, tag: bool) -> str:
        return self._write(name, self._joined_text(tag))

    def _read_to_cut(self, url):
        reader = HlsReader(lambda: url, "pdt", (160, 120), 2.0, "chisme-test")
        self.addCleanup(reader.http.close)
        got = []
        with self.assertRaises(TimelineCut):
            got.extend(reader.frames(None))
        return got

    def test_a_cut_that_appears_mid_read_ends_it_with_every_frame_before_it_right(self):
        first, joined = self._head() + "\n", self._joined_text(tag=True)
        served = iter([first, joined])

        class Live(Quiet):
            def do_GET(self):
                if self.path.endswith("live-cut.m3u8"):
                    body = next(served, joined).encode()
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                super().do_GET()

        server = _serve(self.dir, Live)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        got = self._read_to_cut(f"http://127.0.0.1:{server.server_address[1]}/live-cut.m3u8")
        self.assertGreaterEqual(len(got), 20)
        latest = T0 + timedelta(seconds=12.5)
        self.assertTrue(all(T0 - timedelta(seconds=0.2) <= f.at <= latest for f in got))

    def test_a_read_that_starts_after_a_cut_begins_on_the_newest_timeline(self):
        reader = HlsReader(lambda: self._joined("after.m3u8", tag=True), "pdt", (160, 120), 2.0,
                           "chisme-test")
        self.addCleanup(reader.http.close)
        got = []
        with self.assertRaises(StreamLost) as caught:
            got.extend(reader.frames(None))
        self.assertNotIsInstance(caught.exception, TimelineCut)
        self.assertGreaterEqual(len(got), 10)
        self.assertGreaterEqual(min(f.at for f in got), T0 + timedelta(seconds=11.5))

    def test_a_timestamp_jump_without_a_tag_is_caught_too(self):
        got = self._read_to_cut(self._joined("jump.m3u8", tag=False))
        latest = T0 + timedelta(seconds=12.5)
        self.assertTrue(all(f.at <= latest for f in got), [f.at for f in got[-3:]])

    test_frames_are_timed_by_the_program_date_time = None
    test_without_program_time_last_modified_sets_the_clock = None
    test_the_segment_timestamps_are_read_from_the_transport_stream = None
    test_a_jump_in_the_sequence_is_reported_as_a_gap = None


class Failures(unittest.TestCase):
    def test_a_url_that_cannot_be_resolved_starts_no_ffmpeg(self):
        from unittest import mock

        from scraper.border.camera import hls
        from scraper.border.camera.stream import StreamDown

        def down():
            raise StreamDown("not live")
        with mock.patch.object(hls, "Decoder") as decoder:
            reader = HlsReader(down, "pdt", (160, 120), 2.0, "chisme-test")
            self.addCleanup(reader.http.close)
            with self.assertRaises(StreamDown):
                next(reader.frames(5))
        decoder.assert_not_called()

    def test_a_403_is_a_block_not_an_outage(self):
        from unittest import mock

        from scraper.border.camera.hls import Blocked

        reader = HlsReader(lambda: "https://example.org/x.m3u8", "last-modified", (160, 120),
                           2.0, "chisme-test")
        self.addCleanup(reader.http.close)
        refused = mock.Mock(status_code=403)
        with mock.patch.object(reader.http, "get", return_value=refused), \
                self.assertRaises(Blocked):
            reader._get("https://example.org/x.m3u8")


@unittest.skipUnless(FFMPEG, "needs the ffmpeg command-line tool")
class Watchdog(EndToEnd):
    """Frames that decode but can never be placed in time are a failed read, not a quiet one."""

    def test_frames_that_cannot_be_stamped_end_the_read(self):
        from unittest import mock

        from scraper.border.camera import hls

        with mock.patch.object(hls, "STAMP_WATCHDOG_S", 0.0), \
                mock.patch.object(hls.Clock, "wall", lambda self, pts: None):
            reader = HlsReader(lambda: self.base + "live.m3u8", "pdt", (160, 120), 2.0,
                               "chisme-test")
            self.addCleanup(reader.http.close)
            with self.assertRaises(StreamLost) as caught:
                list(reader.frames(None))
        self.assertIn("could be stamped", str(caught.exception))

    test_frames_are_timed_by_the_program_date_time = None
    test_without_program_time_last_modified_sets_the_clock = None
    test_the_segment_timestamps_are_read_from_the_transport_stream = None
    test_a_jump_in_the_sequence_is_reported_as_a_gap = None


class LiveFrames(unittest.TestCase):
    """How a camera's live stream is opened: its own URL and clock, our honest User-Agent."""

    def test_a_city_camera_is_read_at_its_url_with_the_last_modified_clock(self):
        from unittest import mock

        from scraper.border.camera import reader
        from scraper.border.camera.calibration import camera_from_dict
        from scraper.core.config import settings

        cam = camera_from_dict("city", {
            "title": "City bridge camera", "port_number": "240202", "direction": "north",
            "frame_size": [1280, 720], "lanes": [], "display": False,
            "source": {"kind": "hls", "url": "https://zoocams.example/x.m3u8",
                       "clock": "last-modified"}})
        with mock.patch.object(reader, "HlsReader") as made:
            reader.live_frames(cam, 10)
        url_fn, clock, size, fps, agent, region = made.call_args.args
        self.assertEqual((url_fn(), clock, size, fps, agent, region),
                         ("https://zoocams.example/x.m3u8", "last-modified", (1280, 720), 2.0,
                          settings.user_agent, None))
        made.return_value.frames.assert_called_once_with(10)


class TimedPairs(unittest.TestCase):
    """The queue reader times each pair by its stamps and never times across a gap."""

    def test_a_pair_across_a_gap_is_not_timed(self):
        from camera_scene import FRAME, Scene, camera, queue_fronts

        from scraper.border.camera.calibration import Strip
        from scraper.border.camera.measure import LaneWindow, Reference, image_features

        cam = camera()
        strip = Strip(cam, cam.lanes[0], FRAME)
        ref = Reference.from_images(strip, [image_features(Scene().render([]))])
        window = LaneWindow(strip, ref, 0.5)
        scene = Scene()
        for n, t in enumerate([0.0, 0.5, 1.0, 9.0, 9.5]):      # 8 s missing after the third
            window.add(scene.render(queue_fronts(30, 0.3 * t, scene.view_m)), t)
            self.assertLessEqual(len(window.speeds), n)
        self.assertEqual(window.untimed_pairs, 1)
        self.assertEqual(len(window.speeds), 3)


if __name__ == "__main__":
    unittest.main()
