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
from .hls import Blocked, Gap
from .reader import live_reader, state_dir
from .stream import FROZEN_SECONDS, StreamError, StreamFrozen

LO_MIN = 0.5             # the fastest believable trip from line A to line B, minutes
# The slowest believable trip, minutes. FHWA's crossing-time guide drops matches over 120 min,
# but an anchor is only trusted when both ends lie inside the measured span with hi_min to
# spare (transit's "enclosed"). Within the 3 h a signature may be kept (ADR-0014), that needs
# 2 x hi_min under 3 h: at 120, no trip shorter than 60 min could ever anchor, and PDN's run
# about 47. So 90, and a trip over 90 min between the lines is never anchored. ASSUMPTION
# that A->B (the middle of the bridge, not the whole wait) stays under that.
HI_MIN = SIGNATURE_TTL_S / 60 / 2
RECONNECT_S = 10.0       # wait before opening a stream again after it failed
SAVE_EVERY_S = 600       # how often the loop counters' learnt roads are saved
METHODS = ("loop", "yolox")

log = logging.getLogger(__name__)


class TransitRunner:
    def __init__(self, cam_a: Camera, cam_b: Camera, methods=METHODS,
                 lo_min: float = LO_MIN, hi_min: float = HI_MIN):
        if 2 * hi_min > SIGNATURE_TTL_S / 60:
            raise ValueError(f"hi_min {hi_min} min: no anchor could be trusted within the "
                             f"{SIGNATURE_TTL_S / 3600:.0f} h signatures are kept")
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
        self.blocked: set[str] = set()      # cameras whose server refused us: never retried
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
            reader = None
            seen_gaps = 0
            resumed = True
            try:
                reader = live_reader(cam)
                for frame in reader.frames(None):
                    if self._stop.is_set():
                        return
                    last = self.last_at.get(cam.id)
                    if last is not None and frame.at <= last:
                        # A fresh reader starts at the playlist's oldest segment: these frames
                        # were counted before the reconnect.
                        continue
                    if resumed and last is not None:
                        # The stream was not read from the last frame before to this one.
                        self._gap(cam, Gap(last, (frame.at - last).total_seconds(),
                                           "stream down or reconnecting"))
                    resumed = False
                    while seen_gaps < len(reader.gaps):
                        self._gap(cam, reader.gaps[seen_gaps])
                        seen_gaps += 1
                    self._frame(cam, line, frame)
            except Blocked as exc:
                self.errors[cam.id] = f"BLOCKED {exc}"
                if cam.source == "hls":
                    # The City refused this client: stop, as ADR-0014 decision 5 says. Never
                    # retried, never disguised.
                    self.blocked.add(cam.id)
                    log.error("%s: blocked by the server (%s); no longer read", cam.id, exc)
                    return
                log.warning("%s: %s; reconnecting in %.0f s", cam.id, exc, RECONNECT_S)
            except StreamFrozen as exc:
                # The frozen frames were already counted as road with no cars: take them back
                # as a gap, so the line's count is not read as zero traffic.
                self.errors[cam.id] = f"StreamFrozen: {exc}"
                last = self.last_at.get(cam.id)
                if last is not None:
                    since = max(last - timedelta(seconds=FROZEN_SECONDS), self.first_at[cam.id])
                    self._gap(cam, Gap(since, (last - since).total_seconds(),
                                       "picture frozen or black"))
                log.warning("%s: %s; reconnecting in %.0f s", cam.id, exc, RECONNECT_S)
            except StreamError as exc:
                self.errors[cam.id] = f"{type(exc).__name__}: {exc}"
                log.warning("%s: %s; reconnecting in %.0f s", cam.id, exc, RECONNECT_S)
            except Exception as exc:
                self.errors[cam.id] = f"{type(exc).__name__}: {exc}"
                log.exception("%s failed; reconnecting", cam.id)
            finally:
                if reader is not None:
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
        if not self.last_at:
            return {}
        # Per-vehicle data goes after SIGNATURE_TTL_S by the newest frame either camera has,
        # even while the other camera is down and nothing can be measured.
        cutoff = max(self.last_at.values()) - timedelta(seconds=SIGNATURE_TTL_S)
        with self._lock:
            for m in self.methods:
                self._expire(m, cutoff)
        at = self.seen_until()
        if at is None or len(self.first_at) < 2:
            return {}
        horizon = max(max(self.first_at.values()), cutoff)
        if horizon >= at:
            return {}                            # one camera is more than the TTL behind
        out = {}
        with self._lock:
            for m in self.methods:
                a = list(self.crossings[(m, self.line_a)])
                b = list(self.crossings[(m, self.line_b)])
                ga = self._gaps_for(m, self.line_a, at)
                gb = self._gaps_for(m, self.line_b, at)
                start, prior = self._start(m, horizon, ga + gb)
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

    def _start(self, method: str, horizon: datetime,
               gaps: list[Gap]) -> tuple[datetime, transit.Prior | None]:
        """Where this measurement starts, and the cars-between count carried to it.

        The start never moves backwards (once an earlier measurement moved it past a gap, it
        stays there), and it is never left just before a gap: the count carried across a gap
        needs RATE_WINDOW_MIN of rate before it, so a start inside that stretch moves to the
        gap's end. Then the count is carried from the previous measurement at that start."""
        prev = self.previous.get(method)
        start = horizon
        if prev is not None and prev.start > start:
            start = prev.start
        spans = transit._merge([(g.after, g.after + timedelta(seconds=g.seconds))
                                for g in gaps if g.after is not None])
        lead = timedelta(minutes=transit.RATE_WINDOW_MIN)
        moved = True
        while moved:
            moved = False
            for g0, g1 in spans:
                if g0 - lead <= start < g1:
                    start, moved = g1, True
        prior = transit.prior_at(prev, start) if prev is not None and prev.start <= start else None
        return start, prior

    def _gaps_for(self, method: str, line: str, at: datetime) -> list[Gap]:
        gaps = list(self.gaps[(method, line)])
        if method == "loop":
            for cam in self.cams:
                since = self.learning_since.get(cam.id)
                if self.lines[cam.id] == line and since is not None:
                    gaps.append(Gap(since, (at - since).total_seconds(), "loop zones learning"))
        return gaps

    def _expire(self, method: str, cutoff: datetime) -> None:
        """Per-vehicle data (crossings carry signatures) lives at most SIGNATURE_TTL_S."""
        for line in (self.line_a, self.line_b):
            kept = self.crossings[(method, line)]
            while kept and kept[0].at < cutoff:
                kept.popleft()
            self.gaps[(method, line)] = [g for g in self.gaps[(method, line)]
                                         if g.after is not None and g.after + timedelta(
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
            if cam.id in self.blocked:
                parts[-1] += " (stopped: blocked)"
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
            try:
                measured = runner.measure()
                runner.write(runner.records(measured))
                runner.maybe_save_refs()
                print(f"[{datetime.now(UTC):%H:%M:%S} UTC]\n{runner.status(measured)}",
                      flush=True)
            except Exception:  # a full disk or a locked file must never stop the counting
                log.exception("transit tick failed; retrying next tick")
    except KeyboardInterrupt:
        pass
    finally:
        try:
            runner.stop()
        except Exception:
            log.exception("saving the loop roads at exit failed")
        held.close()
    return 0
