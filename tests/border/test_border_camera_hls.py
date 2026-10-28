"""The stamped HLS reader: playlists, MPEG-TS timestamps, and frames timed by the stream's clock.

The end-to-end cases generate a short HLS stream with ffmpeg and serve it from a local HTTP server,
once with PROGRAM-DATE-TIME (as YouTube does) and once with only Last-Modified (as the City of El
Paso does). They check that each frame's capture time comes from that clock, not from arrival.
"""
from __future__ import annotations

import os
import queue
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
from types import SimpleNamespace
from unittest import mock

import httpx
import pytest

pytest.importorskip("cv2")

from scraper.border.camera import hls, stream
from scraper.border.camera.hls import (
    WRAP,
    Clock,
    Decoder,
    Gone,
    HlsReader,
    Segment,
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

    def test_a_program_time_is_not_carried_across_a_discontinuity(self):
        # After a cut the old timeline's time is wrong by the whole outage: c.ts has no time
        # until a tag gives one. A tag before or after the cut's tag both date the segment.
        later = T0 + timedelta(hours=1)
        text = ("#EXTM3U\n#EXT-X-TARGETDURATION:5\n#EXT-X-MEDIA-SEQUENCE:0\n"
                f"#EXT-X-PROGRAM-DATE-TIME:{T0.isoformat()}\n"
                "#EXTINF:5,\na.ts\n#EXTINF:5,\nb.ts\n#EXT-X-DISCONTINUITY\n#EXTINF:5,\nc.ts\n"
                f"#EXT-X-PROGRAM-DATE-TIME:{later.isoformat()}\n#EXT-X-DISCONTINUITY\n"
                "#EXTINF:5,\nd.ts\n#EXTINF:5,\ne.ts\n")
        times = [s.program_time for s in parse_playlist(text, "https://e.org/").segments]
        self.assertEqual(times, [T0, T0 + timedelta(seconds=5), None, later,
                                 later + timedelta(seconds=5)])


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

    def test_a_start_before_the_last_one_is_a_cut_not_a_wrap(self):
        # A restart 0.5 s back, within the pump's tolerance for 1 s segments. Taken as the
        # 33-bit wrap it moved the timeline 26.5 h and stamped frames about 13 h off.
        clock = Clock()
        clock.add(90_000 * 100, 90_000 * 101, T0)
        with self.assertRaises(TimelineCut):
            clock.add(round(90_000 * 99.5), round(90_000 * 100.5), T0 + timedelta(seconds=1))
        self.assertEqual(clock.wall(90_000 * 100 + 18_000), T0 + timedelta(seconds=0.2))

    def test_a_segment_without_its_own_wall_time_is_placed_by_the_others(self):
        clock = Clock()
        clock.add(0, 90_000 * 4, T0)
        clock.add(90_000 * 5, 90_000 * 9, None)               # no Last-Modified, no PDT
        clock.add(90_000 * 10, 90_000 * 14, T0 + timedelta(seconds=10))
        self.assertEqual(clock.wall(90_000 * 8), T0 + timedelta(seconds=8))


class LastModified(unittest.TestCase):
    def test_a_minus_0000_zone_is_utc_not_this_pcs_local_time(self):
        reader = HlsReader(lambda: "", "last-modified", (160, 120), 2.0, "chisme-test")
        self.addCleanup(reader.http.close)
        seg = Segment(0, "https://e.org/x.ts", 5.0, None)
        for stamp in ("Sun, 01 Nov 2026 08:30:00 GMT", "Sun, 01 Nov 2026 08:30:00 -0000"):
            resp = httpx.Response(200, headers={"Last-Modified": stamp})
            # Written at 08:30:00 (+0.5 s for the truncation), its last frame 4.5 s into it.
            self.assertEqual(reader._segment_wall(seg, resp, 4.5),
                             datetime(2026, 11, 1, 8, 29, 56, tzinfo=UTC), stamp)

    def test_a_malformed_stamp_is_an_unknown_time_not_a_failed_read(self):
        # parsedate_to_datetime raised ValueError in the pump, which ended the read. A segment
        # with no time of its own is placed by the others, as one with no header is.
        reader = HlsReader(lambda: "", "last-modified", (160, 120), 2.0, "chisme-test")
        self.addCleanup(reader.http.close)
        seg = Segment(0, "https://e.org/x.ts", 5.0, None)
        for stamp in ("not a date", "Sun, 32 Nov 2026 08:30:00 GMT"):
            resp = httpx.Response(200, headers={"Last-Modified": stamp})
            self.assertIsNone(reader._segment_wall(seg, resp, 4.5), stamp)


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

    SOURCE = "testsrc=size=320x240:rate=10"
    LOSSLESS = False

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
                        self.SOURCE, "-t", "12", "-c:v", "libx264", "-g", "10",
                        *(["-qp", "0"] if self.LOSSLESS else []),
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

    def _stamp_files(self) -> None:
        """Each segment file stamped as written when its last frame was captured."""
        start = T0
        for seg in self.segments:
            end = start + timedelta(seconds=seg.duration)
            path = os.path.join(self.dir, os.path.basename(seg.uri))
            os.utime(path, (end.timestamp(), end.timestamp()))
            start = end

    def test_frames_are_timed_by_the_program_date_time(self):
        _, got = self._read(self._with_pdt(), "pdt", 6)
        self.assertGreaterEqual(len(got), 12)
        self.assertLess(abs((got[0].at - T0).total_seconds()), 0.15)
        spacing = [(b.at - a.at).total_seconds() for a, b in pairwise(got)]
        self.assertTrue(all(abs(s - 0.5) < 0.06 for s in spacing), spacing)

    def test_without_program_time_last_modified_sets_the_clock(self):
        self._stamp_files()
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


class Holes(EndToEnd):
    """Stream time never seen is sized by the timestamps on either side of it."""

    def test_a_sequence_jump_is_sized_by_the_timestamps_not_the_target_duration(self):
        # TARGETDURATION bounds a segment's length, it does not give it: here 3 over 2 s
        # segments. Four missed were taken as 12 s, so the 8 s really skipped read as a cut.
        pairs = [p for p in pairwise(self.vod.splitlines()) if p[0].startswith("#EXTINF")]

        def playlist(first: int, last: int, tail: list[str]) -> str:
            pdt = (T0 + timedelta(seconds=2 * first)).isoformat()
            return "\n".join(["#EXTM3U", "#EXT-X-VERSION:3", "#EXT-X-TARGETDURATION:3",
                              f"#EXT-X-MEDIA-SEQUENCE:{first}",
                              f"#EXT-X-PROGRAM-DATE-TIME:{pdt}",
                              *[x for p in pairs[first:last] for x in p], *tail]) + "\n"

        later = playlist(5, 6, ["#EXT-X-ENDLIST"])            # segments 1-4 never listed
        served = iter([playlist(0, 1, []), later])

        class Changing(Quiet):
            def do_GET(self):
                if self.path.endswith("jump.m3u8"):
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
        url = f"http://127.0.0.1:{server.server_address[1]}/jump.m3u8"
        reader = HlsReader(lambda: url, "pdt", (160, 120), 2.0, "chisme-test")
        self.addCleanup(reader.http.close)
        got = []
        with self.assertRaises(StreamLost) as caught:         # the stream ends after the jump
            got.extend(reader.frames(None))
        self.assertNotIsInstance(caught.exception, TimelineCut)
        self.assertEqual([g.reason for g in reader.gaps],
                         ["4 segment(s) expired before they were read"])
        self.assertAlmostEqual(reader.gaps[0].seconds, 8.0, delta=0.2)
        self.assertGreaterEqual(max(f.at for f in got), T0 + timedelta(seconds=10))

    def test_a_segment_without_last_modified_is_placed_by_the_others(self):
        # Its frames were placed only up to 1 s past the segment before; the rest were dropped
        # and no Gap said so.
        self._stamp_files()
        bare = os.path.basename(self.segments[2].uri)

        class Unstamped(Quiet):
            def send_header(self, keyword, value):
                if not (keyword == "Last-Modified" and self.path.endswith(bare)):
                    super().send_header(keyword, value)

        server = _serve(self.dir, Unstamped)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self._write("lm.m3u8", self.vod)
        reader, got = self._read(f"http://127.0.0.1:{server.server_address[1]}/lm.m3u8",
                                 "last-modified", 8)
        spacing = [(b.at - a.at).total_seconds() for a, b in pairwise(got)]
        self.assertGreaterEqual(len(got), 16)
        self.assertTrue(all(abs(s - 0.5) < 0.06 for s in spacing), spacing)
        self.assertEqual(reader.gaps, [])

    test_frames_are_timed_by_the_program_date_time = None
    test_without_program_time_last_modified_sets_the_clock = None
    test_the_segment_timestamps_are_read_from_the_transport_stream = None
    test_a_jump_in_the_sequence_is_reported_as_a_gap = None


@unittest.skipUnless(FFMPEG, "needs the ffmpeg command-line tool")
class DecodedBacklog(unittest.TestCase):
    def test_the_backlog_is_bounded_in_bytes_not_frames(self):
        # 256 frames of the City's 1280x720 were 708 MB held for a slow consumer.
        for w, h in [(1280, 720), (854, 480), (160, 120)]:
            decoder = Decoder((w, h), 2.0)
            decoder.close()
            self.assertLessEqual(decoder.frames.maxsize * w * h * 3, 64_000_000, (w, h))
            self.assertGreaterEqual(decoder.frames.maxsize, 8, (w, h))


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

    def _joined_text(self, tag: bool, dated: bool = True) -> str:
        tail = [ln if ln.startswith("#") else "b/" + ln for ln in self.later]
        cut = ["#EXT-X-DISCONTINUITY"] if tag else []
        # The new timeline's own time, as a server dates it after a restart: it follows on.
        if dated:
            cut.append(f"#EXT-X-PROGRAM-DATE-TIME:{(T0 + timedelta(seconds=12)).isoformat()}")
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

    def test_a_timeline_after_a_cut_with_no_time_of_its_own_is_never_stamped(self):
        # The time carried over the cut would stamp every frame early by the whole outage.
        from unittest import mock

        from scraper.border.camera import hls

        url = self._write("undated.m3u8", self._joined_text(tag=True, dated=False))
        reader = HlsReader(lambda: url, "pdt", (160, 120), 2.0, "chisme-test")
        self.addCleanup(reader.http.close)
        got = []
        with mock.patch.object(hls, "STAMP_WATCHDOG_S", 0.0), \
                self.assertRaises(StreamLost) as caught:
            got.extend(reader.frames(None))
        self.assertEqual(got, [])
        self.assertIn("could be stamped", str(caught.exception))

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

    def test_a_whole_scene_read_faster_than_2_fps_is_refused_before_anything_is_fetched(self):
        # Its frozen check takes exact repeats for a stalled encoder: pairs closer than 0.42 s
        # repeat exactly on a live City picture.
        resolved = []
        with mock.patch.object(hls, "Decoder") as decoder:
            reader = HlsReader(lambda: resolved.append(1) or "https://example.org/x.m3u8",
                               "last-modified", (160, 120), 4.0, "chisme-test")
            self.addCleanup(reader.http.close)
            with self.assertRaises(ValueError):
                next(reader.frames(5))
        self.assertEqual(resolved, [])
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


@unittest.skipUnless(FFMPEG, "needs the ffmpeg command-line tool")
class Motion(EndToEnd):
    """Each frame says whether the picture moved, for the runner's no-motion gap."""

    def test_a_reads_first_frame_has_not_moved_and_the_rest_are_compared(self):
        reader, got = self._read(self._with_pdt(), "pdt", 6)
        # testsrc's counter and sweep change the picture between frames 0.5 s apart.
        self.assertEqual([f.moved for f in got], [False] + [True] * (len(got) - 1))
        again = list(reader.frames(2))                      # a fresh read has no pair yet
        self.assertIs(again[0].moved, False)

    test_frames_are_timed_by_the_program_date_time = None
    test_without_program_time_last_modified_sets_the_clock = None
    test_the_segment_timestamps_are_read_from_the_transport_stream = None
    test_a_jump_in_the_sequence_is_reported_as_a_gap = None


def native_line(n: int, pts: int, checksum: str) -> str:
    """One frame's line from the native showinfo, as ffmpeg 9 prints it."""
    return (f"[showinfo@native @ 0000021c] n:{n:4d} pts:{pts:7d} pts_time:{pts / 90000:<7g}"
            f" duration:   3000 duration_time:0.0333333 fmt:yuv420p cl:left sar:1/1 s:1280x720 "
            f"i:P iskey:0 type:P checksum:{checksum} plane_checksum:[{checksum} 0 0] "
            f"mean:[90 128 128] stdev:[40.1 2.0 2.0]\n")


class NativeRepeatTimes(unittest.TestCase):
    """How long the native picture stays bit-identical (the still tier's margin, Q-32), from the
    checksum the native showinfo prints for every decoded frame."""

    def test_a_run_lasts_from_the_frame_that_brought_a_picture_to_the_one_that_changed_it(self):
        runs = hls.NativeRepeats()
        runs.line("[showinfo@native @ 0] config in time_base: 1/90000, frame_rate: 30/1\n")
        runs.line("[showinfo@native @ 0] config out time_base: 0/0, frame_rate: 0/0\n")
        self.assertIsNone(runs.take())                        # no frame yet
        for n, s in enumerate("AABCCCCD"):                    # C shows for 4 frames at 30 fps
            runs.line(native_line(n, 3000 * n, s * 8))
            runs.line("[showinfo@native @ 0] color_range:tv color_space:bt709\n")
        self.assertEqual(runs.take(), round(4 * 3000 / 90000, 3))
        self.assertEqual(runs.take(), 0.0)                    # since: D, one frame so far
        for n in range(8, 38):                                # D for a second: a freeze
            runs.line(native_line(n, 3000 * n, "D" * 8))
        self.assertEqual(runs.take(), 1.0)                    # shown while it lasts, as a floor

    def test_only_a_native_decoder_measures(self):
        self.assertIsNone(Decoder.native_repeat(SimpleNamespace(native=None)))


@unittest.skipUnless(FFMPEG, "needs the ffmpeg command-line tool")
class NativeRepeatsRead(EndToEnd):
    """A whole-scene read carries the longest native repeat and what the frozen check judged;
    a read with a clock region measures no native repeat and counts no exact repeats."""

    SOURCE = "testsrc=size=320x240:rate=2,fps=10"           # each picture for 5 frames: 0.5 s
    # Lossless, so a repeated picture decodes bit-identical: at its default quality x264 kept
    # refining one still picture, and no two of its first 55 frames matched (2026-10-28).
    LOSSLESS = True

    def test_a_whole_scene_read_carries_the_longest_native_repeat(self):
        _, got = self._read(self._with_pdt(), "pdt", 6)
        native = [f.native_repeat_s for f in got if f.native_repeat_s is not None]
        self.assertGreater(len(native), len(got) // 2, native)
        self.assertEqual(max(native), 0.5)
        self.assertIsNone(got[0].repeats)                     # no pair yet
        self.assertEqual({f.repeats for f in got[1:]}, {0})   # picked 0.5 s apart: no repeat
        self.assertTrue(all(f.grey is not None for f in got))

    def test_a_read_with_a_clock_region_measures_no_native_repeat(self):
        url = self._with_pdt()
        reader = HlsReader(lambda: url, "pdt", (160, 120), 2.0, "chisme-test",
                           live_region=(0.0, 0.0, 0.25, 0.1))
        self.addCleanup(reader.http.close)
        got = list(reader.frames(4))
        self.assertTrue(got)
        self.assertEqual({(f.native_repeat_s, f.repeats) for f in got}, {(None, None)})
        self.assertTrue(all(f.grey is not None for f in got))

    test_frames_are_timed_by_the_program_date_time = None
    test_without_program_time_last_modified_sets_the_clock = None
    test_the_segment_timestamps_are_read_from_the_transport_stream = None
    test_a_jump_in_the_sequence_is_reported_as_a_gap = None


class EmptyDecoder:
    """Stands in for ffmpeg: decodes nothing, and its frames end when its input is closed."""

    def __init__(self, size, fps, native=False):
        self.frames: queue.Queue = queue.Queue()
        self.proc = mock.Mock()
        self.proc.stdin.close.side_effect = lambda: self.frames.put(None)

    def feed(self, data: bytes) -> None:
        pass

    def close(self) -> None:
        pass


class RestartedStream(unittest.TestCase):
    """A read whose playlist is gone (404/410) or ended drops the cached channel listing its URL
    came from (stream.forget), so a YouTube stream restarted under a new video id is found on
    the next resolve, not after up to LISTING_TTL of the old id."""

    ENDED = "#EXTM3U\n#EXT-X-TARGETDURATION:5\n#EXT-X-MEDIA-SEQUENCE:7\n#EXT-X-ENDLIST\n"

    def setUp(self):
        self.live_id, self.listings = "old", 0

        def ytdlp(*args, timeout=120):
            if "--flat-playlist" in args:
                self.listings += 1
                return mock.Mock(returncode=0, stderr="",
                                 stdout=f"{self.live_id}\tis_live\tPuente Zaragoza (Sur)\n")
            video = args[-1].rsplit("=", 1)[1]
            return mock.Mock(returncode=0, stderr="", stdout=f"https://hls.example/{video}.m3u8\n")

        for target, name, value in ((stream, "_listing", {}), (stream, "_resolved", {}),
                                    (stream, "_ytdlp", ytdlp), (hls, "Decoder", EmptyDecoder)):
            patcher = mock.patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.reader = HlsReader(lambda: stream.resolve("ch", "Zaragoza (Sur)"), "pdt",
                                (160, 120), 2.0, "chisme-test")
        self.addCleanup(self.reader.http.close)

    def test_an_ended_stream_is_listed_afresh_on_the_next_resolve(self):
        with mock.patch.object(self.reader, "_get", return_value=mock.Mock(text=self.ENDED)), \
                self.assertRaisesRegex(StreamLost, "the stream ended"):
            list(self.reader.frames(None))
        self.live_id = "new"
        self.assertEqual(stream.resolve("ch", "Zaragoza (Sur)"), "https://hls.example/new.m3u8")
        self.assertEqual(self.listings, 2)

    def test_a_gone_playlist_is_listed_afresh_within_the_read(self):
        def gone(url):
            self.live_id = "new"                            # restarted; the old URL 404s
            raise Gone(f"HTTP 404 for {url}")

        with mock.patch.object(self.reader, "_get", side_effect=gone) as get, \
                self.assertRaises(Gone):
            list(self.reader.frames(None))
        self.assertEqual([c.args[0] for c in get.call_args_list],
                         ["https://hls.example/old.m3u8", "https://hls.example/new.m3u8"])
        self.assertEqual(self.listings, 2)
        self.assertNotIn("ch", stream._listing)             # the new URL was gone too

    def test_a_city_url_leaves_the_listing_alone(self):
        stream.resolve("ch", "Zaragoza (Sur)")
        reader = HlsReader(lambda: "https://zoocams.example/x.m3u8", "last-modified",
                           (160, 120), 2.0, "chisme-test")
        self.addCleanup(reader.http.close)
        with mock.patch.object(reader, "_get", return_value=mock.Mock(text=self.ENDED)), \
                self.assertRaises(StreamLost):
            list(reader.frames(None))
        self.assertIn("ch", stream._listing)

    def test_a_transient_failure_leaves_the_listing_alone(self):
        def busy(url):
            raise StreamLost(f"HTTP 503 for {url}")          # not Gone: the stream may still be up

        with mock.patch.object(self.reader, "_get", side_effect=busy) as get, \
                self.assertRaisesRegex(StreamLost, "HTTP 503"):
            list(self.reader.frames(None))
        self.assertEqual(get.call_count, 2)                 # retried once, on a fresh resolve
        stream.resolve("ch", "Zaragoza (Sur)")
        self.assertEqual(self.listings, 1)


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
