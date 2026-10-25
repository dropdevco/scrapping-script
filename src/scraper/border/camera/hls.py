"""Frames from a live HLS stream, each stamped with when it was captured (ADR-0014).

A frame's arrival time is the wrong clock. On 2026-10-23, YouTube delivered FPFCH frames 5-19 s
after capture, and OpenCV's reader opened three segments back and then delivered 180 frames in
0.45 s. The City of El Paso streams arrived 2.6-4 s after capture. A crossing time taken from
arrival times would carry all of that. Both kinds of stream carry their own time, so this reader
uses it:

    clock="pdt"            YouTube: the playlist's #EXT-X-PROGRAM-DATE-TIME gives the wall time
                           at which a segment starts. (On 2026-10-23 FPFCH's burned-in clock sat
                           a steady 0.9-1.0 s behind it.)
    clock="last-modified"  The City streams carry no PDT, but the server stamps each 5-second
                           segment with Last-Modified when it is written. That time is to the
                           second, so the offset is the median over many segments.

Either way, a frame's time is its segment's wall time plus its PTS offset into the segment,
through one offset (wall seconds minus PTS seconds) fitted as a running median. This machine's
clock is never used for a frame: it ran 1.46 s slow and unsynced on 2026-10-23.

Segments are fetched into memory by sequence number. A jump in the sequence is a gap: those
seconds were never seen, and it is reported as a `Gap`, never covered over. The bytes are fed to
one ffmpeg process that decodes across segment boundaries. The City segments do not each start
with the decoder's parameter sets, so decoding them one by one loses frames. Frames stay in memory
(QUESTIONS.md Q-7); nothing is written to disk.

Needs the ffmpeg command-line tool on PATH.
"""
from __future__ import annotations

import queue
import re
import shutil
import statistics
import subprocess
import threading
import time
from collections import deque
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from urllib.parse import urljoin

import httpx
import numpy as np

from .stream import FROZEN_SECONDS, StreamFrozen, StreamLost, frozen_check

WRAP = 1 << 33                 # MPEG-TS timestamps are 33-bit, at 90 kHz: they wrap every 26.5 h
CLOCK_HZ = 90_000
OFFSET_SEGMENTS = 30           # the offset is the median over this many recent segments
CLOCK_SEGMENTS = 1024          # segments remembered for placing frames (over 17 min of 1 s ones)
CUT_TOLERANCE_S = 2.0          # a segment starting further than this from where the last ended,
                               # beyond any skipped segments, is a timeline cut
STAMP_WATCHDOG_S = 30.0        # no frame placed in time for this long: the read has failed
LM_ROUNDING_S = 0.5            # Last-Modified truncates to the second: add the mean lost half
POLL_RETRIES = 3
FETCH_TIMEOUT_S = 10


class Gone(StreamLost):
    """HTTP 404/410: the resource is not there (an expired segment, or an expired URL)."""


class Blocked(StreamLost):
    """HTTP 401/403 that a fresh URL did not cure: the server refuses this client. For the City
    feeds that means stop (ADR-0014): the client is never disguised to get past a block."""


class TimelineCut(StreamLost):
    """The stream's timestamps jumped (an encoder or server restart, #EXT-X-DISCONTINUITY). The
    read ends before the cut, with every frame before it stamped right; a fresh read starts a
    fresh clock on the far side."""


@dataclass(frozen=True)
class Stamped:
    image: np.ndarray          # BGR, at the reader's size
    at: datetime               # capture time, UTC, from the stream's own clock
    pts: int                   # 90 kHz, as decoded (unwrapped by ffmpeg)


@dataclass(frozen=True)
class Gap:
    after: datetime | None     # stream time where the unseen stretch starts; None before any
    seconds: float             # how much stream time was never seen
    reason: str


@dataclass(frozen=True)
class Segment:
    seq: int
    uri: str
    duration: float
    program_time: datetime | None   # wall time at the segment's start, if the playlist says
    discontinuity: bool = False     # #EXT-X-DISCONTINUITY before it: its timestamps restart


@dataclass(frozen=True)
class Playlist:
    media_sequence: int
    target_duration: float
    segments: list[Segment]
    ended: bool


def parse_playlist(text: str, base_url: str) -> Playlist:
    """A media playlist. A master playlist (variants) is refused: the caller resolves a variant."""
    if "#EXTM3U" not in text:
        raise StreamLost("not an HLS playlist")
    if "#EXT-X-STREAM-INF" in text:
        raise StreamLost("a master playlist, not a media playlist")
    media_sequence, target, ended = 0, 0.0, False
    segments: list[Segment] = []
    duration: float | None = None
    program: datetime | None = None
    cut = False
    seq = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#EXT-X-MEDIA-SEQUENCE:"):
            media_sequence = int(line.split(":", 1)[1])
        elif line.startswith("#EXT-X-TARGETDURATION:"):
            target = float(line.split(":", 1)[1])
        elif line.startswith("#EXT-X-PROGRAM-DATE-TIME:"):
            program = _iso(line.split(":", 1)[1])
        elif line.startswith("#EXTINF:"):
            duration = float(line.split(":", 1)[1].split(",", 1)[0])
        elif line.startswith("#EXT-X-ENDLIST"):
            ended = True
        elif line == "#EXT-X-DISCONTINUITY":
            cut = True
        elif not line.startswith("#"):
            seq = media_sequence + len(segments)
            segments.append(Segment(seq, urljoin(base_url, line), duration or target, program,
                                    cut))
            # One PDT covers the segments after it until the next tag; carry it forward.
            program = program + timedelta(seconds=duration or target) if program else None
            duration, cut = None, False
    return Playlist(media_sequence, target, segments, ended)


def _iso(text: str) -> datetime:
    value = datetime.fromisoformat(text.strip())
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def pts_range(ts: bytes) -> tuple[int, int] | None:
    """(smallest, largest) video PTS in an MPEG-TS segment, 90 kHz, or None if it has none.

    Reads only PES headers: 188-byte packets, a payload that starts a unit, a video stream id
    (0xE0-0xEF), and the PTS flag. The smallest is used, not the first: with B-frames the first
    packet in decode order is not the earliest picture."""
    low = high = None
    for i in range(0, len(ts) - 187, 188):
        pkt = ts[i:i + 188]
        if pkt[0] != 0x47 or not pkt[1] & 0x40:            # sync byte, payload_unit_start
            continue
        start = 4
        if pkt[3] & 0x20:                                   # adaptation field present
            start += 1 + pkt[4]
        if not pkt[3] & 0x10 or start + 14 > 188:          # no payload, or no room for a PES header
            continue
        pes = pkt[start:]
        if pes[0:3] != b"\x00\x00\x01" or not 0xE0 <= pes[3] <= 0xEF:
            continue
        if not pes[7] & 0x80:                               # PTS_DTS_flags: no PTS
            continue
        p = pes[9:14]
        pts = (((p[0] >> 1) & 0x07) << 30) | (p[1] << 22) | ((p[2] >> 1) << 15) \
            | (p[3] << 7) | (p[4] >> 1)
        if low is None:
            low = high = pts
        else:                                               # compare modulo the wrap
            if (pts - low) % WRAP > WRAP // 2:
                low = pts
            if (pts - high) % WRAP < WRAP // 2:
                high = pts
    return None if low is None else (low, high)


class Clock:
    """Maps a 90 kHz PTS to wall time, from segments whose wall time is known.

    Each segment contributes one offset (wall seconds minus unwrapped PTS seconds), and the
    offset used is the median of the last OFFSET_SEGMENTS. A frame is placed by its PTS
    distance, modulo the wrap, from the start of the segment it belongs to."""

    def __init__(self) -> None:
        self.offsets: deque[float] = deque(maxlen=OFFSET_SEGMENTS)
        # Kept long enough to place a frame the consumer reaches long after its segment was
        # fetched: the decoder can hold 256 frames (two minutes at 2 fps) and more behind them.
        self.segments: deque[tuple[int, float, float]] = deque(maxlen=CLOCK_SEGMENTS)
        self._last_mod: int | None = None
        self._unwrapped = 0
        self._lock = threading.Lock()   # added to by the pump thread, read by the consumer

    def add(self, start_pts: int, end_pts: int, wall_start: datetime) -> None:
        with self._lock:
            self._add(start_pts, end_pts, wall_start)

    def _add(self, start_pts: int, end_pts: int, wall_start: datetime) -> None:
        mod = start_pts % WRAP
        if self._last_mod is None:
            self._unwrapped = mod
        else:
            self._unwrapped += (mod - self._last_mod) % WRAP
        self._last_mod = mod
        u = self._unwrapped / CLOCK_HZ
        span = ((end_pts - start_pts) % WRAP) / CLOCK_HZ
        self.segments.append((mod, span, u))
        self.offsets.append(wall_start.timestamp() - u)

    def wall(self, pts: int) -> datetime | None:
        with self._lock:
            if not self.offsets:
                return None
            offset = statistics.median(self.offsets)
            segments = tuple(self.segments)
        p = pts % WRAP
        for start_mod, span, u in reversed(segments):
            d = ((p - start_mod) % WRAP) / CLOCK_HZ
            if d <= span + 1.0:                      # inside this segment (one frame of slack)
                return datetime.fromtimestamp(u + d + offset, UTC)
        return None


SHOWINFO = re.compile(r"\bn:\s*(\d+)\s+pts:\s*(-?\d+)\s+pts_time:")
TIME_BASE = re.compile(r"time_base:\s*(\d+)/(\d+)")


class Decoder:
    """One ffmpeg process: MPEG-TS bytes in on stdin, raw BGR frames out on stdout, each
    frame's PTS on stderr (showinfo). Frames are picked about every 1/fps seconds of stream
    time, keeping each picked frame's own PTS; no frame is duplicated to fill a rate."""

    def __init__(self, size: tuple[int, int], fps: float):
        if not shutil.which("ffmpeg"):
            raise StreamLost("ffmpeg is not on PATH; the HLS reader needs it")
        self.size = size
        w, h = size
        pick = max(0.0, 1.0 / fps - 0.02)
        vf = f"select='isnan(prev_selected_t)+gte(t-prev_selected_t\\,{pick:.3f})',scale={w}:{h},showinfo"
        self.proc = subprocess.Popen(
            ["ffmpeg", "-hide_banner", "-loglevel", "info", "-copyts", "-f", "mpegts",
             "-i", "pipe:0", "-an", "-sn", "-vf", vf, "-fps_mode", "passthrough",
             "-f", "rawvideo", "-pix_fmt", "bgr24", "pipe:1"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.frames: queue.Queue = queue.Queue(maxsize=256)
        self._pts: queue.Queue = queue.Queue()
        self.tb = 1 / CLOCK_HZ
        self._closed = threading.Event()
        self._threads = [threading.Thread(target=self._read_info, daemon=True),
                         threading.Thread(target=self._read_frames, daemon=True)]
        for t in self._threads:
            t.start()

    def _read_info(self) -> None:
        for raw in iter(self.proc.stderr.readline, b""):
            line = raw.decode("utf-8", "replace")
            if "showinfo" not in line:
                continue
            tb = TIME_BASE.search(line)
            if tb and int(tb.group(1)):
                self.tb = int(tb.group(1)) / int(tb.group(2))
            m = SHOWINFO.search(line)
            if m:
                self._pts.put(int(m.group(2)))

    def _read_frames(self) -> None:
        w, h = self.size
        n = w * h * 3
        while True:
            buf = self.proc.stdout.read(n)
            if len(buf) < n:
                self._put(None)
                return
            try:
                pts = self._pts.get(timeout=5)
            except queue.Empty:
                self._put(None)
                return
            image = np.frombuffer(buf, np.uint8).reshape(h, w, 3)
            self._put((round(pts * self.tb * CLOCK_HZ), image))

    def _put(self, item) -> None:
        # A reader that stopped early leaves the queue full: never block past close().
        while not self._closed.is_set():
            try:
                self.frames.put(item, timeout=0.5)
                return
            except queue.Full:
                continue

    def feed(self, data: bytes) -> None:
        self.proc.stdin.write(data)
        self.proc.stdin.flush()

    def close(self) -> None:
        """Stop now: frames not yet read are not wanted."""
        self._closed.set()
        try:
            self.proc.stdin.close()
        except OSError:
            pass
        if self.proc.poll() is None:
            self.proc.kill()
        self.proc.wait()
        for t in self._threads:
            t.join(timeout=5)
        self.proc.stdout.close()
        self.proc.stderr.close()


class HlsReader:
    """Stamped frames from one live HLS media playlist.

    `playlist_url` is a callable so an expiring URL (YouTube's last about six hours) can be
    resolved again. `user_agent` is our normal, honest one: if a site blocks it, the read fails
    and is reported; the client is never disguised to get past a block (ADR-0014)."""

    def __init__(self, playlist_url: Callable[[], str], clock: str, size: tuple[int, int],
                 fps: float, user_agent: str,
                 live_region: tuple[float, float, float, float] | None = None,
                 http: httpx.Client | None = None):
        if clock not in ("pdt", "last-modified"):
            raise ValueError("clock must be 'pdt' or 'last-modified'")
        self._url_fn, self.clock_kind, self.size, self.fps = playlist_url, clock, size, fps
        self.live_region = live_region
        self.http = http or httpx.Client(headers={"User-Agent": user_agent},
                                         timeout=FETCH_TIMEOUT_S, follow_redirects=True)
        self.clock = Clock()
        self.gaps: list[Gap] = []
        self.last_seq: int | None = None
        self.last_at: datetime | None = None

    def _get(self, url: str) -> httpx.Response:
        for attempt in range(POLL_RETRIES):
            try:
                r = self.http.get(url)
            except httpx.HTTPError as exc:
                if attempt == POLL_RETRIES - 1:
                    raise StreamLost(f"{type(exc).__name__} fetching {url[:80]}") from exc
                time.sleep(1)
                continue
            if r.status_code == 200:
                return r
            if r.status_code in (404, 410):
                raise Gone(f"HTTP {r.status_code} for {url[:80]}")
            if r.status_code in (401, 403):
                raise Blocked(f"HTTP {r.status_code} for {url[:80]}")
            if attempt == POLL_RETRIES - 1:
                raise StreamLost(f"HTTP {r.status_code} for {url[:80]}")
            time.sleep(1)
        raise StreamLost(f"no response for {url[:80]}")

    def _segment_wall(self, seg: Segment, resp: httpx.Response, span: float) -> datetime | None:
        if self.clock_kind == "pdt":
            return seg.program_time
        modified = resp.headers.get("last-modified")
        if not modified:
            return None
        written = parsedate_to_datetime(modified) + timedelta(seconds=LM_ROUNDING_S)
        # Written when complete, so its last frame was captured at about `written`. The lag from
        # capture to write is unknown (0-5 s, ASSUMED) until calibrated against a timed crossing.
        return written - timedelta(seconds=span)

    def frames(self, seconds: float | None = None) -> Iterator[Stamped]:
        """Stamped frames covering `seconds` of capture time (or for ever when None).

        Gaps found on the way are appended to `self.gaps`, placed where they are in stream time
        (where the pump's fed content ended), not where the consumer happens to be. A timeline
        cut ends the read with TimelineCut after the frames before it."""
        url = self._url_fn()                     # before ffmpeg starts: a failure leaks nothing
        check = frozen_check(self.fps, self.live_region)
        self.last_seq = self.last_at = None      # each read starts afresh; gaps are within a read
        self.clock = Clock()
        first: datetime | None = None
        stop = threading.Event()
        errors: list[BaseException] = []
        decoder = Decoder(self.size, self.fps)

        def pump() -> None:
            nonlocal url
            fed_end: datetime | None = None          # stream time at the end of what was fed
            prev: tuple[int, float] | None = None    # (start PTS, duration) of the last fed
            skipped = 0.0                            # seconds skipped since `prev`

            def hole(seconds: float, reason: str) -> None:
                nonlocal fed_end, skipped
                self.gaps.append(Gap(fed_end, seconds, reason))
                if fed_end is not None:
                    fed_end += timedelta(seconds=seconds)
                skipped += seconds

            try:
                while not stop.is_set():
                    try:
                        text = self._get(url).text
                    except StreamLost:
                        url = self._url_fn()         # an expired URL: resolve it once more
                        text = self._get(url).text
                    pl = parse_playlist(text, url)
                    new = [g for g in pl.segments if self.last_seq is None or g.seq > self.last_seq]
                    if self.last_seq is None:
                        cuts = [i for i, g in enumerate(new) if g.discontinuity]
                        new = new[cuts[-1]:] if cuts else new    # start on the newest timeline
                    elif new and new[0].seq > self.last_seq + 1:
                        missed = new[0].seq - self.last_seq - 1
                        hole(missed * pl.target_duration,
                             f"{missed} segment(s) expired before they were read")
                    for seg in new:
                        if seg.discontinuity and prev is not None:
                            raise TimelineCut(f"#EXT-X-DISCONTINUITY at segment {seg.seq}")
                        try:
                            resp = self._get(seg.uri)
                        except Gone:
                            # Listed, then expired before we fetched it (seen on bridgepdn1 on
                            # 2026-10-24): those seconds were never seen. A gap, not a failure.
                            hole(seg.duration, f"segment {seg.seq} expired before it was fetched")
                            self.last_seq = seg.seq
                            continue
                        span = pts_range(resp.content)
                        if span is not None and prev is not None:
                            expected = prev[0] + round((prev[1] + skipped) * CLOCK_HZ)
                            off = (span[0] - expected) % WRAP
                            off = (off - WRAP if off > WRAP // 2 else off) / CLOCK_HZ
                            if abs(off) > max(CUT_TOLERANCE_S, pl.target_duration):
                                raise TimelineCut(f"timestamps jumped {off:+.1f} s at segment "
                                                  f"{seg.seq}")
                        if span is not None:
                            length = ((span[1] - span[0]) % WRAP) / CLOCK_HZ
                            wall = self._segment_wall(seg, resp, length)
                            if wall is not None:
                                self.clock.add(span[0], span[1], wall)
                            end = self.clock.wall(span[1])
                            if end is not None:
                                fed_end = end
                            prev, skipped = (span[0], seg.duration), 0.0
                        decoder.feed(resp.content)
                        self.last_seq = seg.seq
                    if pl.ended:
                        decoder.proc.stdin.close()
                        return
                    stop.wait(max(1.0, pl.target_duration / 2))
            except BaseException as exc:  # noqa: BLE001 - handed to the consumer below
                errors.append(exc)
                try:
                    decoder.proc.stdin.close()
                except OSError:
                    pass

        try:
            threading.Thread(target=pump, daemon=True).start()
            placed = time.monotonic()                # last time a frame was stamped and handed on
            while True:
                try:
                    item = decoder.frames.get(timeout=max(STAMP_WATCHDOG_S, FROZEN_SECONDS))
                except queue.Empty:
                    raise StreamLost("no frame for 30 s") from None
                if item is None:
                    if errors:
                        raise errors[0]
                    raise StreamLost("the stream ended")
                pts, image = item
                at = self.clock.wall(pts)
                if at is None or (self.last_at is not None and at <= self.last_at):
                    # Before any segment's time was known, or a repeated frame. Decoded frames
                    # that are never placed must not look like a live read.
                    if time.monotonic() - placed > STAMP_WATCHDOG_S:
                        raise StreamLost("no frame could be stamped for 30 s")
                    continue
                check(image)                         # raises StreamFrozen
                self.last_at = at
                first = first or at
                yield Stamped(image, at, pts)
                placed = time.monotonic()            # the consumer's own time does not count
                if seconds is not None and (at - first).total_seconds() >= seconds:
                    return
        finally:
            stop.set()
            decoder.close()


__all__ = [
    "Blocked",
    "Gap",
    "Gone",
    "HlsReader",
    "Playlist",
    "Segment",
    "Stamped",
    "StreamFrozen",
    "StreamLost",
    "TimelineCut",
    "parse_playlist",
    "pts_range",
]
