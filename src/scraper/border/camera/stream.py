"""Finding a bridge's live stream and reading frames from it at a fixed rate.

Streams are found by title on the operator's channel, never by a stored video id: an id
changes every time a stream restarts. A stream can fail three ways, and each is raised,
never swallowed (ADR-0011), so a dead camera reads as dead rather than as an empty bridge:

    StreamDown    the channel lists it, but YouTube will not serve it ("We're experiencing
                  technical difficulties", seen on 7 of 16 FPFCH streams on 2026-10-19)
    StreamFrozen  frames arrive but stopped changing: a stalled encoder repeating its last frame
                  (frozen_check says what that means with and without a burned-in clock)
    StreamLost    frames stopped arriving mid-read

The product owner chose to read these YouTube streams knowing YouTube's terms forbid
automated access (QUESTIONS.md, Q-7). Frames are analysed in memory: the worker and the
viewer never write one. Only an operator's explicit `camera snapshot --out` or
`camera read --debug` writes a single annotated frame, for checking a calibration.
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
FROZEN_SECONDS = 20       # the window each frozen rule judges, and what a StreamFrozen takes back
FROZEN_DIFF = 1.0         # the clock region changed less than this: the clock did not tick
FROZEN_DIFF_SCENE = 0.3   # a whole scene (no clock) that changed this much moved (frozen_check)
BLACK_LEVEL = 12          # mean grey below this is an encoder sending black, not a dark road
FROZEN_CHANGED = 0.1      # with a clock: frozen when at most this share of pairs ticked: keyframes
FROZEN_SAME_SHARE = 1 / 3  # a whole scene is frozen when this share of its pairs repeat exactly
SCENE_MAX_FPS = 2.0       # a whole scene is read no faster: that rule's margin (frozen_check)
LISTING_TTL = 600         # a channel's stream list is re-read at most every ten minutes


class StreamError(RuntimeError):
    pass


class StreamDown(StreamError):
    pass


class StreamFrozen(StreamError):
    """`seconds` is how far back from the last frame the rule that raised it looked, at its
    nominal fps: the stretch a caller takes back, because those frames were already read as a
    quiet road. Frames come a little more than 1/fps apart, so the window itself is longer: the
    City's 40 pairs spanned p50 20.96 s, at most 21.38 s, on 2026-10-27."""

    def __init__(self, message: str, seconds: float = FROZEN_SECONDS):
        super().__init__(message)
        self.seconds = seconds


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
_resolved: dict[tuple[str, str], str] = {}     # (channel, title) -> the URL resolve() last gave


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
        # The cached listing may name a stream the operator has since restarted under a new
        # video id: list the channel afresh next time, not after LISTING_TTL.
        _listing.pop(channel, None)
        raise StreamDown(f"{stream.title}: {_last(done.stderr)}")
    _resolved[(channel, title)] = urls[0]
    return urls[0]


def forget(url: str) -> None:
    """A read of `url` found the stream gone (HTTP 404/410) or ended (#EXT-X-ENDLIST). If
    resolve() gave that URL, its channel's cached listing is dropped, so the next resolve()
    finds a stream restarted under a new video id. Any other URL (a City camera's) is ignored."""
    for key, given in list(_resolved.items()):
        if given == url:
            _resolved.pop(key, None)
            _listing.pop(key[0], None)


def frames(url: str, fps: float, seconds: float,
           live_region: tuple[float, float, float, float] | None = None) -> Iterator[np.ndarray]:
    """Frames at `fps` for `seconds` of a recorded clip's own time (`--source`). Live reads do
    not come through here: hls.HlsReader stamps each frame with its capture time.

    The caller times these frames as exactly 1/fps apart, so each one is the clip's frame
    nearest k/fps by its frame count, never the wall clock. Taking every round(source/fps)-th
    frame instead drifted: from a 25 fps clip every 12th frame is 0.48 s apart, and a rendered
    queue's speed read 3 % low. Now a frame is at most half a source frame from its slot, with
    no drift. A clip slower than `fps` cannot give frames 1/fps apart and is refused.

    `live_region` (x, y, w, h as fractions of the frame) is where the picture must keep
    changing while the stream is alive: the burned-in clock on the bridge cameras. A frozen
    encoder's picture still changes a little at each keyframe (it is re-encoded), and an
    empty road at night barely changes either, so "the clock stopped" is the test, measured
    as mean grey-level change. Without a region the whole frame is compared, and frozen means
    the picture repeats exactly between keyframes (ASSUMPTION, ADR-0016); that needs `fps` of
    SCENE_MAX_FPS or less (frozen_check)."""
    check = frozen_check(fps, live_region)       # refuses a whole scene read too fast
    cap = cv2.VideoCapture(url)
    if not cap.isOpened():
        raise StreamLost(f"could not open {url[:80]}")
    source_fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    if source_fps < fps:
        cap.release()
        raise ValueError(f"{url[:80]} runs at {source_fps:g} fps, slower than the {fps:g} fps "
                         f"asked for")
    wanted = round(seconds * fps)
    i, n = 0, 0
    try:
        while n < wanted:
            if not cap.grab():
                raise StreamLost(f"stream ended after {n} of {wanted} frames")
            if i >= round(n * source_fps / fps):
                ok, frame = cap.retrieve()
                if not ok:
                    raise StreamLost(f"frame {n} could not be decoded")
                check(frame)
                n += 1
                yield frame
            i += 1
    finally:
        cap.release()


def frozen_check(fps: float, live_region: tuple[float, float, float, float] | None):
    """A callable that takes each frame in turn and raises StreamFrozen once the stream has
    stopped, judged over the last FROZEN_SECONDS of frame pairs at `fps`. Otherwise it returns
    whether the frame moved from the one before it: by FROZEN_DIFF in the `live_region` (the
    clock ticked), or by FROZEN_DIFF_SCENE over a whole scene. The first frame has no pair and
    did not move.

    With a `live_region`, frozen means the clock stopped: at most FROZEN_CHANGED of the pairs
    changed it. Without one, frozen means the picture repeats itself: at least
    FROZEN_SAME_SHARE of the pairs are bit-identical at the probe size, which holds only for a
    whole scene read at SCENE_MAX_FPS or slower (refused otherwise)."""
    if not live_region and fps > SCENE_MAX_FPS:
        raise ValueError(f"a whole scene is read at {SCENE_MAX_FPS:g} fps or less, not {fps:g}: "
                         f"closer pairs can repeat exactly on a live picture")
    limit = max(2, round(FROZEN_SECONDS * fps))
    changes: deque[bool] = deque(maxlen=limit)     # with a clock: this pair changed it
    same: deque[bool] = deque(maxlen=limit)        # a whole scene: this pair repeats exactly
    last: np.ndarray | None = None
    # Without a burned-in clock the whole scene is the probe. A quiet live road barely changes:
    # the City's bridgepdn1 at 19:50 local on 2026-10-24 changed by a median 1.14 grey levels a
    # pair (min 0.16) at full size, so it is compared at 320x180, not 64x16. The first rule here
    # (frozen when at most FROZEN_CHANGED of the pairs changed by FROZEN_DIFF_SCENE) raised 17
    # times in 20.2 min on that live picture on 2026-10-27 03:32-03:52 UTC, 50.4 an hour, and
    # each one cut the line's count. What the live picture never did was repeat itself: 0 of
    # 2362 pairs at 2 fps were bit-identical (the smallest change was 0.0095 grey). At the
    # native rate 33.5 % of frames repeated the one before, but the picture changed at least
    # every 0.42 s, and these pairs are at least 0.48 s apart (hls.Decoder picks frames
    # 1/fps - 0.02 s apart): that margin is SCENE_MAX_FPS. It was one night: that the margin
    # holds at dusk and by day is an ASSUMPTION until the runs of Q-32. An encoder fed one
    # unchanging picture, or black, is expected to repeat it exactly between keyframes
    # (ASSUMPTION from how H.264 codes an unchanged picture; no City freeze has been sampled).
    # A third, not all, leaves room for the pairs each keyframe changes; the share is a
    # judgement (ASSUMPTION).
    # The trade-off: a freeze re-encoded with noise, so that no two frames match, is no longer
    # caught here, where the first rule caught it in 20-30 s. The runner's no-motion gap takes
    # such a stretch back instead, from the last frame that moved (hls.Stamped.moved), on its
    # own timeline so that it survives reconnects.
    size, bar = ((64, 16), FROZEN_DIFF) if live_region else ((320, 180), FROZEN_DIFF_SCENE)

    def check(frame: np.ndarray) -> bool:
        nonlocal last
        probe = _live_probe(frame, live_region, size)
        moved = False
        if last is not None:
            diff = float(np.abs(probe - last).mean())
            moved = diff >= bar
            changes.append(moved)
            same.append(diff == 0.0)
        last = probe
        if live_region:
            # Judged over the whole window, not a run of identical frames: a frozen stream is
            # still re-encoded, and every keyframe changes the picture a little.
            stopped = len(changes) == limit and sum(changes) <= FROZEN_CHANGED * limit
        else:
            stopped = len(same) == limit and sum(same) >= FROZEN_SAME_SHARE * limit
        if stopped:
            # Seen live: Zaragoza (Av. Waterfill) came back at 10:16 UTC on 2026-10-19
            # serving solid black for 20 s, then dropped again. Say which it is.
            if float(frame.mean()) < BLACK_LEVEL:
                what = f"is black for {FROZEN_SECONDS}s"
            elif live_region:
                what = f"has not changed for {FROZEN_SECONDS}s"
            else:   # say what was counted: the share that calls it frozen is a judgement
                what = f"repeated exactly in {sum(same)} of the last {limit} pairs"
            raise StreamFrozen(f"the picture {what}", FROZEN_SECONDS)
        return moved

    return check


def _live_probe(frame: np.ndarray, region, size: tuple[int, int] = (64, 16)) -> np.ndarray:
    h, w = frame.shape[:2]
    if region:
        x, y, rw, rh = region
        frame = frame[round(y * h):round((y + rh) * h), round(x * w):round((x + rw) * w)]
    grey = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY) if frame.ndim == 3 else frame
    return cv2.resize(grey, size, interpolation=cv2.INTER_AREA).astype(np.float32)
