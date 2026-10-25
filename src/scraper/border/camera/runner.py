"""`camera transit`: the crossing time between two cameras, by two methods (ADR-0014).

    python -m scraper.border camera transit [--a pdn-north] [--b city-pdn-north] [--minutes N]

Reads both cameras continuously as stamped frames (hls.py): every frame carries its capture time
from the stream's own clock. Each frame goes to every counter of its camera:
- loop.LoopCounter, deterministic;
- detect.DetectorCounter, YOLOX-S.

Their crossings and gaps are kept in memory. Every `every` seconds each method is measured on
its own (transit.measure), and one record per method is appended to
CAMERA_STATE_DIR/transit-YYYY-MM-DD.jsonl. A record holds derived numbers only: counts, rates,
transit statistics per window, epochs. No frame, signature, single crossing or single transit is
ever written. Crossings, and the signatures in them, are dropped after SIGNATURE_TTL_S. Each
measurement covers at most that long, chained to the one before by the cars-between count
(transit.prior_at).

A line is not seen while:
- its stream is down (between the last frame before and the first frame after), or a segment
  expired before it was read (hls.Gap);
- for the loop method, any of its zones is still learning the empty road.

Those times are gaps at that line, never "no cars". Gaps cut the transit measurement into
epochs, and each epoch needs anchors or a carried count before its transits are trusted.

The loop counters' learnt roads are saved to CAMERA_STATE_DIR/<camera>.loop.npz and seeded on
start, so a restart in a queue does not wait for the lane to clear. Each road is a median of
near-empty moments, the same kind of record as the queue reader's strip references; no frame is
saved.

The City camera at line B is read at the owner's risk and never displayed (ADR-0014): nothing
here serves or shows a picture.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections import deque
from datetime import UTC, datetime, timedelta

import numpy as np

from . import transit
from .calibration import Camera
from .crossing import SIGNATURE_TTL_S, Crossing
from .hls import Gap
from .reader import live_reader, state_dir
from .stream import StreamError

LO_MIN = 0.5             # the fastest believable trip from line A to line B, minutes
HI_MIN = 120.0           # the slowest: FHWA's crossing-time guide drops matches over 120 min
RECONNECT_S = 10.0       # wait before opening a stream again after it failed
SAVE_EVERY_S = 600       # how often the loop counters' learnt roads are saved
METHODS = ("loop", "yolox")

log = logging.getLogger(__name__)


class TransitRunner:
    def __init__(self, cam_a: Camera, cam_b: Camera, methods=METHODS,
                 lo_min: float = LO_MIN, hi_min: float = HI_MIN):
        self.cams = (cam_a, cam_b)
        self.lines = {cam_a.id: _one_line(cam_a), cam_b.id: _one_line(cam_b)}
        self.line_a, self.line_b = self.lines[cam_a.id], self.lines[cam_b.id]
        self.methods = tuple(methods)
        self.lo_min, self.hi_min = lo_min, hi_min
        self.counters = {cam.id: self._counters(cam) for cam in self.cams}
        self.crossings: dict[tuple[str, str], deque[Crossing]] = {
            (m, ln): deque() for m in self.methods for ln in (self.line_a, self.line_b)}
        self.gaps: dict[tuple[str, str], list[Gap]] = {key: [] for key in self.crossings}
        self.learning_since: dict[str, datetime | None] = {}   # camera -> when loop learning began
        self.first_at: dict[str, datetime] = {}
        self.last_at: dict[str, datetime] = {}
        self.previous: dict[str, transit.Measurement] = {}
        self.errors: dict[str, str] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._saved_at = time.monotonic()

    def _counters(self, cam: Camera) -> list:
        out = []
        if "loop" in self.methods:
            from .loop import LoopCounter
            loop = LoopCounter(cam)
            refs = _load_refs(cam)
            if refs:
                still = loop.seed(refs)
                log.info("%s: loop roads seeded; still learning %s", cam.id, still or "none")
            out.append(loop)
        if "yolox" in self.methods:
            from .detect import DetectorCounter
            out.append(DetectorCounter(cam))     # one network per camera: one thread each
        return out

    # -- reading ------------------------------------------------------------------------------

    def start(self) -> None:
        for cam in self.cams:
            threading.Thread(target=self._feed, args=(cam,), daemon=True,
                             name=f"transit-{cam.id}").start()

    def stop(self) -> None:
        self._stop.set()
        self.save_refs()

    def _feed(self, cam: Camera) -> None:
        line = self.lines[cam.id]
        while not self._stop.is_set():
            reader = live_reader(cam)
            seen_gaps = 0
            resumed = True
            try:
                for frame in reader.frames(None):
                    if self._stop.is_set():
                        return
                    if resumed and cam.id in self.last_at:
                        # The stream was down from the last frame before to this one.
                        self._gap(cam, Gap(self.last_at[cam.id],
                                           (frame.at - self.last_at[cam.id]).total_seconds(),
                                           "stream down or reconnecting"))
                    resumed = False
                    while seen_gaps < len(reader.gaps):
                        self._gap(cam, reader.gaps[seen_gaps])
                        seen_gaps += 1
                    self._frame(cam, line, frame)
            except StreamError as exc:
                self.errors[cam.id] = f"{type(exc).__name__}: {exc}"
                log.warning("%s: %s; reconnecting in %.0f s", cam.id, exc, RECONNECT_S)
            except Exception as exc:
                self.errors[cam.id] = f"{type(exc).__name__}: {exc}"
                log.exception("%s failed; reconnecting", cam.id)
            finally:
                reader.http.close()
            self._stop.wait(RECONNECT_S)

    def _gap(self, cam: Camera, gap: Gap) -> None:
        line = self.lines[cam.id]
        for counter in self.counters[cam.id]:
            counter.gap(gap)
        with self._lock:
            for m in self.methods:
                self.gaps[(m, line)].append(gap)

    def _frame(self, cam: Camera, line: str, frame) -> None:
        self.first_at.setdefault(cam.id, frame.at)
        self.last_at[cam.id] = frame.at
        self.errors.pop(cam.id, None)
        for counter in self.counters[cam.id]:
            found = counter.add(frame)
            own = counter.gaps() if hasattr(counter, "gaps") else []
            with self._lock:
                self.crossings[(counter.method, line)].extend(c for c in found if c.line == line)
                self.gaps[(counter.method, line)].extend(own)
            if counter.method == "loop":
                self._learning(cam, line, counter, frame.at)

    def _learning(self, cam: Camera, line: str, counter, at: datetime) -> None:
        """While any loop zone of the line is learning, the line's loop count is short: that
        stretch becomes a gap the moment learning ends (and is added as an open one when
        measuring before then)."""
        learning = any(state == "learning" for (ln, _), state in counter.states().items()
                       if ln == line)
        since = self.learning_since.get(cam.id)
        if learning and since is None:
            self.learning_since[cam.id] = at
        elif not learning and since is not None:
            with self._lock:
                self.gaps[("loop", line)].append(
                    Gap(since, (at - since).total_seconds(), "loop zones learning the empty road"))
            self.learning_since[cam.id] = None

    # -- measuring ----------------------------------------------------------------------------

    def seen_until(self) -> datetime | None:
        """Both lines are observed up to the older of their latest frames."""
        if len(self.last_at) < 2:
            return None
        return min(self.last_at.values())

    def measure(self) -> dict[str, tuple[transit.Measurement, transit.Now | None]]:
        at = self.seen_until()
        if at is None:
            return {}
        start = max(max(self.first_at.values()), at - timedelta(seconds=SIGNATURE_TTL_S))
        out = {}
        with self._lock:
            for m in self.methods:
                self._expire(m, at)
                a = list(self.crossings[(m, self.line_a)])
                b = list(self.crossings[(m, self.line_b)])
                ga = self._gaps_for(m, self.line_a, at)
                gb = self._gaps_for(m, self.line_b, at)
                prev = self.previous.get(m)
                prior = transit.prior_at(prev, start) if prev and prev.start < start else None
                try:
                    measured = transit.measure(a, b, self.lo_min, self.hi_min, gaps_a=ga,
                                               gaps_b=gb, start=start if prior is None else None,
                                               end=at, prior=prior)
                except ValueError as exc:        # e.g. `start` inside a gap
                    log.info("%s: not measurable yet: %s", m, exc)
                    continue
                self.previous[m] = measured
                current = transit.now_from(measured, at) if measured.epochs else None
                out[m] = (measured, current)
        return out

    def _gaps_for(self, method: str, line: str, at: datetime) -> list[Gap]:
        gaps = list(self.gaps[(method, line)])
        if method == "loop":
            for cam in self.cams:
                since = self.learning_since.get(cam.id)
                if self.lines[cam.id] == line and since is not None:
                    gaps.append(Gap(since, (at - since).total_seconds(), "loop zones learning"))
        return gaps

    def _expire(self, method: str, at: datetime) -> None:
        """Per-vehicle data (crossings carry signatures) lives at most SIGNATURE_TTL_S."""
        cutoff = at - timedelta(seconds=SIGNATURE_TTL_S)
        for line in (self.line_a, self.line_b):
            kept = self.crossings[(method, line)]
            while kept and kept[0].at < cutoff:
                kept.popleft()
            self.gaps[(method, line)] = [g for g in self.gaps[(method, line)]
                                         if g.after is None or g.after + timedelta(
                                             seconds=g.seconds) >= cutoff]

    def records(self, measured: dict) -> list[dict]:
        out = []
        for (m, current) in measured.values():
            rec = transit.record(m, transit.windows(m), current)
            rec.update({"written_at": datetime.now(UTC).isoformat(),
                        "seen_until": m.end.isoformat(),
                        "line_a": f"{self.cams[0].id}:{self.line_a}",
                        "line_b": f"{self.cams[1].id}:{self.line_b}"})
            out.append(rec)
        return out

    def write(self, records: list[dict]) -> None:
        if not records:
            return
        state_dir().mkdir(parents=True, exist_ok=True)
        path = state_dir() / f"transit-{datetime.now(UTC):%Y-%m-%d}.jsonl"
        with open(path, "a", encoding="utf-8") as f:
            f.writelines(json.dumps(rec, ensure_ascii=False, default=str) + "\n" for rec in records)

    def status(self, measured: dict) -> str:
        at = self.seen_until()
        parts = []
        for cam in self.cams:
            lag = None if cam.id not in self.last_at else (
                datetime.now(UTC) - self.last_at[cam.id]).total_seconds()
            err = self.errors.get(cam.id)
            parts.append(f"{cam.id}: " + (f"ERROR {err}" if err else
                                          "waiting" if lag is None else f"{lag:.0f} s behind"))
        lines = [" | ".join(parts)]
        for m in self.methods:
            with self._lock:
                a = [c.at for c in self.crossings[(m, self.line_a)]]
                b = [c.at for c in self.crossings[(m, self.line_b)]]
            if at is not None:
                since = at - timedelta(minutes=10)
                rate_a = sum(1 for t in a if t >= since) / 10
                rate_b = sum(1 for t in b if t >= since) / 10
            else:
                rate_a = rate_b = 0.0
            text = f"  {m:5}: A {len(a)} cars ({rate_a:.1f}/min)  B {len(b)} ({rate_b:.1f}/min)"
            if m in measured:
                meas, current = measured[m]
                last = meas.epochs[-1] if meas.epochs else None
                text += f"  epochs {len(meas.epochs)}"
                if last is not None:
                    text += f", offset {last.offset} ({last.source})"
                if current is not None and current.minutes is not None:
                    text += f"  now ~{current.minutes:.0f} min ({current.between} cars between)"
                done = [t.minutes for t in meas.transits if not t.flagged]
                if done:
                    text += f"  median transit {float(np.median(done)):.1f} min (n={len(done)})"
            if m == "loop":
                learning = [cid for cid, s in self.learning_since.items() if s is not None]
                if learning:
                    text += f"  learning: {', '.join(learning)}"
            lines.append(text)
        return "\n".join(lines)

    def save_refs(self) -> None:
        for cam in self.cams:
            for counter in self.counters[cam.id]:
                if counter.method == "loop":
                    _save_refs(cam, counter.references())
        self._saved_at = time.monotonic()

    def maybe_save_refs(self) -> None:
        if time.monotonic() - self._saved_at >= SAVE_EVERY_S:
            self.save_refs()


def _one_line(cam: Camera) -> str:
    lines = sorted({z.line for z in cam.count_zones})
    if len(lines) != 1:
        raise ValueError(f"{cam.id}: needs exactly one counting line, has {lines or 'none'}")
    return lines[0]


def _refs_path(cam: Camera):
    return state_dir() / f"{cam.id}.loop.npz"


def _save_refs(cam: Camera, refs: dict) -> None:
    if not refs:
        return
    state_dir().mkdir(parents=True, exist_ok=True)
    arrays = {}
    for (line, lane), (grey, edges) in refs.items():
        arrays[f"{line}|{lane}|grey"] = grey
        arrays[f"{line}|{lane}|edges"] = edges
    path = _refs_path(cam)
    tmp = path.with_suffix(".tmp")
    with open(tmp, "wb") as f:
        np.savez_compressed(f, **arrays)
    os.replace(tmp, path)


def _load_refs(cam: Camera) -> dict:
    path = _refs_path(cam)
    if not path.exists():
        return {}
    try:
        with np.load(path) as saved:
            keys = {k.rsplit("|", 1)[0] for k in saved.files}
            return {tuple(k.split("|", 1)): (saved[f"{k}|grey"].copy(), saved[f"{k}|edges"].copy())
                    for k in keys}
    except (OSError, ValueError, KeyError) as exc:
        log.warning("%s: unreadable loop roads (%s); learning afresh", cam.id, exc)
        return {}


def cmd_transit(args, cameras) -> int:
    from .worker import AlreadyRunning, hold_lock

    for cid in (args.a, args.b):
        if cid not in cameras:
            raise SystemExit(f"unknown camera {cid}")
    state_dir().mkdir(parents=True, exist_ok=True)
    try:
        held = hold_lock(state_dir() / "transit.lock")
    except AlreadyRunning as exc:
        print(exc)
        return 1
    methods = [m.strip() for m in args.methods.split(",") if m.strip()]
    runner = TransitRunner(cameras[args.a], cameras[args.b], methods)
    runner.start()
    print(f"Crossing time {args.a} -> {args.b}, methods {', '.join(methods)}. "
          f"Records: {state_dir()}\\transit-<date>.jsonl  (Ctrl+C to stop)", flush=True)
    ends = None if args.minutes is None else time.monotonic() + args.minutes * 60
    try:
        while ends is None or time.monotonic() < ends:
            time.sleep(min(args.every, max(1.0, (ends or 1e18) - time.monotonic())))
            measured = runner.measure()
            runner.write(runner.records(measured))
            runner.maybe_save_refs()
            print(f"[{datetime.now(UTC):%H:%M:%S} UTC]\n{runner.status(measured)}", flush=True)
    except KeyboardInterrupt:
        pass
    finally:
        runner.stop()
        held.close()
    return 0
