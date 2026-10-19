"""Finding a bridge's live stream and reading frames from it at a fixed rate.

Streams are found by title on the operator's channel, never by a stored video id: an id
changes every time a stream restarts. A stream can fail three ways, and each is raised,
never swallowed (ADR-0011), so a dead camera reads as dead rather than as an empty bridge:

    StreamDown    the channel lists it, but YouTube will not serve it ("We're experiencing
                  technical difficulties", seen on 7 of 16 FPFCH streams on 2026-10-19)
    StreamFrozen  frames arrive but stopped changing: a stalled encoder repeating its last frame
    StreamLost    frames stopped arriving mid-read

The product owner chose to read these YouTube streams knowing YouTube's terms forbid
automated access (QUESTIONS.md, Q-7). Frames are analysed in memory and never written.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
import unicodedata
from collections import deque
from collections.abc import Iterator
from dataclasses import dataclass

import cv2
import numpy as np

FORMAT = "bestvideo[height<=480]/bestvideo"   # 480p is plenty for metres of queue; less to pull
FROZEN_SECONDS = 20       # an unchanging picture this long is a stalled encoder, not a quiet road
FROZEN_DIFF = 1.0         # mean grey-level change below which two frames are "the same picture"
FROZEN_CHANGED = 0.1      # frozen when at most this share of a window's frames changed: keyframes
LISTING_TTL = 600         # a channel's stream list is re-read at most every ten minutes


class StreamError(RuntimeError):
    pass


class StreamDown(StreamError):
    pass


class StreamFrozen(StreamError):
    pass


class StreamLost(StreamError):
    pass


def _ytdlp(*args: str, timeout: int = 120) -> subprocess.CompletedProcess:
    # Node as the JS runtime: without one YouTube extraction is deprecated and drops formats.
    # UTF-8 forced on both ends: on Windows yt-dlp otherwise prints in the console code page,
    # and the "ó" in "Córdova - Américas" made the whole channel listing undecodable.
    env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
    return subprocess.run([sys.executable, "-m", "yt_dlp", "--encoding", "utf-8",
                           "--js-runtimes", "node", *args],
                          capture_output=True, text=True, encoding="utf-8", errors="replace",
                          env=env, timeout=timeout,
                          check=False)   # a refusal is read from returncode and stderr below


def _plain(text: str) -> str:
    folded = unicodedata.normalize("NFKD", text.casefold())
    return " ".join("".join(c for c in folded if not unicodedata.combining(c)).split())


@dataclass(frozen=True)
class Listed:
    video_id: str
    title: str
    live: bool


_listing: dict[str, tuple[float, list[Listed]]] = {}


def channel_streams(channel: str) -> list[Listed]:
    cached = _listing.get(channel)
    if cached and time.monotonic() - cached[0] < LISTING_TTL:
        return cached[1]
    done = _ytdlp("--flat-playlist", "--print", "%(id)s\t%(live_status)s\t%(title)s", channel)
    if done.returncode != 0:
        raise StreamDown(f"could not list {channel}: {_last(done.stderr)}")
    listed = []
    for line in done.stdout.splitlines():
        parts = line.split("\t", 2)
        if len(parts) == 3:
            listed.append(Listed(parts[0], parts[2], parts[1] == "is_live"))
    _listing[channel] = (time.monotonic(), listed)
    return listed


def _last(stderr: str) -> str:
    lines = [line for line in stderr.strip().splitlines() if line.strip()]
    return lines[-1][:200] if lines else "no output"


def find(channel: str, title: str) -> Listed:
    """The live stream whose title ends with `title`, e.g. "Paso del Norte (Norte)".

    Matched on the end of the title, accents and case folded: every title shares the prefix
    "Transmisión en vivo Puente Internacional", and a substring match for "Zaragoza (Sur)"
    would be ambiguous in spirit with "Zaragoza (Av. Waterfill) - Vista Sur"; the suffix is not."""
    want = _plain(title)
    hits = [s for s in channel_streams(channel) if _plain(s.title).endswith(want)]
    if not hits:
        raise StreamDown(f"no stream titled '…{title}' on {channel}")
    live = [s for s in hits if s.live]
    if not live:
        raise StreamDown(f"'{hits[0].title}' is listed but not live")
    return live[0]


def resolve(channel: str, title: str) -> str:
    stream = find(channel, title)
    done = _ytdlp("-g", "-f", FORMAT, f"https://www.youtube.com/watch?v={stream.video_id}")
    urls = done.stdout.strip().splitlines()
    if done.returncode != 0 or not urls:
        raise StreamDown(f"{stream.title}: {_last(done.stderr)}")
    return urls[0]


def frames(url: str, fps: float, seconds: float,
           live_region: tuple[float, float, float, float] | None = None) -> Iterator[np.ndarray]:
    """Frames at `fps` for `seconds` of stream time, from a live or recorded source.

    Paced by the stream's own frame count, not the wall clock, so a recorded clip and a live
    stream sample the same moments and replaying a recording reproduces a live read.

    `live_region` (x, y, w, h as fractions of the frame) is where the picture must keep
    changing while the stream is alive: the burned-in clock on the bridge cameras. A frozen
    encoder re-sends the same picture, but never byte for byte (it is re-encoded), and an
    empty road at night barely changes either, so "the clock stopped" is the test, measured
    as mean grey-level change. Without a region the whole frame is compared."""
    cap = cv2.VideoCapture(url)
    if not cap.isOpened():
        raise StreamLost(f"could not open {url[:80]}")
    source_fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    step = max(1, round(source_fps / fps))
    wanted = round(seconds * fps)
    frozen_limit = max(2, round(FROZEN_SECONDS * fps))
    changes: deque[bool] = deque(maxlen=frozen_limit)
    last, i, n = None, 0, 0
    try:
        while n < wanted:
            if not cap.grab():
                raise StreamLost(f"stream ended after {n} of {wanted} frames")
            if i % step == 0:
                ok, frame = cap.retrieve()
                if not ok:
                    raise StreamLost(f"frame {n} could not be decoded")
                probe = _live_probe(frame, live_region)
                if last is not None:
                    changes.append(float(np.abs(probe - last).mean()) >= FROZEN_DIFF)
                last = probe
                # Judged over the whole window, not a run of identical frames: a frozen
                # stream is still re-encoded, and every keyframe changes the picture a little.
                if len(changes) == frozen_limit and sum(changes) <= FROZEN_CHANGED * frozen_limit:
                    raise StreamFrozen(f"the picture has not changed for {FROZEN_SECONDS}s")
                n += 1
                yield frame
            i += 1
    finally:
        cap.release()


def _live_probe(frame: np.ndarray, region) -> np.ndarray:
    h, w = frame.shape[:2]
    if region:
        x, y, rw, rh = region
        frame = frame[round(y * h):round((y + rh) * h), round(x * w):round((x + rw) * w)]
    grey = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY) if frame.ndim == 3 else frame
    return cv2.resize(grey, (64, 16), interpolation=cv2.INTER_AREA).astype(np.float32)
