"""`camera transit`: the crossing time between two cameras, by two methods (ADR-0014).

    python -m scraper.border camera transit [--a pdn-north] [--b city-pdn-north] [--minutes N]
                                            [--device auto|gpu|cpu]

Reads both cameras continuously as stamped frames (hls.py): every frame carries its capture time
from the stream's own clock. Each frame goes to every counter of its camera:
- loop.LoopCounter, deterministic;
- detect.DetectorCounter, YOLOX-S.

Their crossings and gaps are kept in memory. Every `every` seconds each method and each lane-type
group (below) is measured on its own (transit.measure), and one record per method and group is
appended to CAMERA_STATE_DIR/transit-YYYY-MM-DD.jsonl, its lane_type the group's. A record holds
derived numbers only: counts, rates, transit statistics per window, epochs, each line's unseen
stretches with why (transit.record), and how many times each line's stream was opened again
since the start. No frame, signature or crossing is written, and no transit
as a record of its own (window statistics over few transits can give their times back, not which
car had which). Crossings, and the signatures in them, are dropped after SIGNATURE_TTL_S (of stream
time, plus the wall time since the newest frame arrived). Each measurement covers at most that
long, chained to the one before by the cars-between count (transit.prior_at) when its start
moves and that count can be checked.

A line is not seen while:
- its stream is down (between the last frame before and the first frame after), or a segment
  expired before it was read (hls.Gap);
- its picture was frozen: the reader's StreamFrozen, taken back over the stretch its rule
  judged (StreamFrozen.seconds) before the stream is opened again. After StreamFrozen after
  StreamFrozen the wait before opening it again doubles, from RECONNECT_S up to
  RECONNECT_MAX_S, until a read gives RESET_AFTER_S of frames;
- nothing moved there for more than QUIET_S, or since before a StreamFrozen: no frame whose
  picture moved (hls.Stamped.moved: the clock ticked, or the whole scene changed) and no car
  counted at the line;
- for the loop method, any of its zones is still learning the empty road.

Those times are gaps at that line, never "no cars". Gaps cut the transit measurement into
epochs. Each epoch needs its own anchors, or a sure count continued from the measurement before
with no gap, before its transits are trusted; a count carried across a gap is flagged until
anchors confirm it.

The reader catches a picture that repeats itself exactly, not one frozen with noise
(stream.frozen_check). So a line counts as seen only up to its last frame that moved or counted a
car (`moved_at`), not up to its latest frame (seen_until): measured to its latest frame, such a
freeze would read as an empty road while it lasted, and a record once written is not corrected.
When something moves again after more than QUIET_S, the stretch since `moved_at` becomes a gap,
"nothing moved for N s", before that frame reaches the counters, so the cars in it are cut out as
at any gap; a shorter stretch is seen again, as a quiet road, unless a StreamFrozen was taken in
it (a fresh read's check raises only on a full window, so the end of a freeze read after a reopen
was never taken back). `moved_at` is the runner's own, not the reader's, so a freeze broken by
reopened streams is still judged from its last moving frame.
The hold is a lag. At line B (the City's whole scene) on 2026-10-27 03:32-03:52 UTC, at 21:30
local traffic, the time from one moving 2-fps pair to the next was p50 0.52 s, p99 8.4 s and at
most 16.5 s (930 intervals). With that night's motion bursts thinned to a half or a quarter
(ASSUMPTION: bursts scale with traffic) p99 was 49 s and 112 s, the longest 216 s and 407 s.
Where nothing at a line moves for minutes, as may happen in a standing queue (ASSUMPTION, not
measured), the line stays that far behind: stale, never an empty road.

What the freeze rule and the hold rest on is logged with each record, per line, numbers only
(QUESTIONS.md Q-32): `most_repeats_*`, the most frame pairs that repeated exactly in any window
of the still tier (40 pairs at 2 fps; it calls 14 frozen) since the record before, whether or
not a StreamFrozen was raised (None at a line with a clock); `native_repeat_s_*`, the longest
the picture stayed bit-identical at the stream's own frame rate (the still tier's margin, once
0.42 s live; None where not measured: a line with a clock); `darkest_grey_*`, the lowest mean
grey of a frame, which the black test compares with stream.BLACK_LEVEL; and `hold_lag_s_*`,
how far the line's latest frame was past where it is seen (`moved_at`) when measured: a
duration, so not cut to the minute as `seen_until` is. They are read in each camera's own
thread, frame by frame and from each StreamFrozen, and kept under the runner's lock until the
records take them.

Lane types (QUESTIONS.md Q-22). The k-th car at A is the (offset + k)-th at B only among cars of
one lane type, so each lane type that zones at both lines carry is a group, measured from its
own lanes' crossings only, with its own chain of measurements. At PDN that is SENTRI (A lanes 4-5,
B column E) and "normal" (the other lanes and columns). Then a lane with no type, or with a type
the other line lacks, is in no group, and its crossings are not measured; a warning names it at
start. A pair whose lines share no lane type (nothing typed, as before Q-22, or one camera
typed and the other not) is measured as before: one group of every lane, "mixed". Gaps,
learning and expiry stay per line: a gap at a line, or any loop zone of it still learning, cuts
every group there.

A zone may name the methods that count it (cameras.json "methods"; every method if it names
none). Each method's counter counts only the zones that name it, and a group with a zone, at
either line, that a method does not count is not measured by that method: its count there would
miss that lane's cars or hold others'. `not_measured` says why, and so do a warning at start and
the status line. At PDN that is the loop's SENTRI: in both daylight City frames a col-4 car
covers column E, and in a rendered pass the loop counted such cars there (line-B review,
2026-10-26). The loop's normal is still measured, although col-1..col-3 cars cover the next
column too and the loop counts them there again: that over-count stays within the group, so it
is drift (the anchors' line takes up part of it), not another group's cars, and the loop's
normal cannot vote until the normal hand count at line B checks it (QUESTIONS.md Q-24). By how
much it over-counts is not measured (ASSUMPTION).

YOLOX runs where `device` says (gpu.py: "auto", "gpu" or "cpu"; None reads $CHISME_YOLOX_DEVICE,
else "auto"), one network per camera. "gpu" asked for and refused at start (gpu.GpuRefused)
stops the command before anything is read. A GPU path that stops mid-run (gpu.GpuFailed, raised
out of a counter's `add`) stops for the whole process (gpu.STATE): every camera whose YOLOX runs
on the GPU drops its counter at its own next frame, for the rest of the run, its line unseen by
YOLOX from its frame before (an open gap, "YOLOX stopped: ..."), never counted on the
CPU in its place, since the same frames give the same counts per path, not across paths. The
loop counter, and the other method's records, go on; the log, the status and each yolox record
say so (`yolox_stopped`, "devices"). Each yolox record carries "devices": per camera the path its
boxes came from, "cpu" or "gpu:<adapter>", with the GPU's running check against the CPU
(cpu_checked, cpu_differed), so records of the two paths are never graded together
(combine.record_device). Each record also carries "anchoring": how far its latest epoch has come
towards anchors (transit.anchoring), which the status shows as a bar.

The loop counters' learnt roads are saved to CAMERA_STATE_DIR/<camera>.loop.npz and seeded on
start, so a restart in a queue does not wait for the lane to clear. Each road is a median of
near-empty moments, the same kind of record as the queue reader's strip references; no frame is
saved. Each is saved with its zone's quad and the camera's frame size, ground rectangle and
masks, and a road whose zone or any of those has changed since is dropped on start and learnt
afresh.

The City camera at line B is read at the owner's risk and never displayed (ADR-0014): nothing
here serves or shows a picture.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
import zipfile
from collections import deque
from datetime import UTC, datetime, timedelta

import numpy as np

from . import transit
from .calibration import Camera
from .crossing import METHODS, SIGNATURE_TTL_S, Crossing, Zone
from .gpu import DEVICE_ENV, GpuFailed, GpuRefused, requested_device
from .hls import Blocked, Gap
from .reader import live_reader, state_dir
from .stream import StreamError, StreamFrozen

LO_MIN = 0.5             # the fastest believable trip from line A to line B, minutes
# The slowest trip an anchor may have, minutes. Anchors set an offset on their own only when both
# their ends lie inside the measured span with hi_min to spare (transit's "enclosed"), so a trip
# of T min needs an epoch longer than 2 x hi_min - T, and no epoch is longer than the 3 h a
# signature may be kept (ADR-0014). Half of that, 90, is the largest hi_min at which every trip
# up to hi_min can be enclosed (at FHWA's 120, no trip under 60 min could). A trip longer than
# hi_min cannot anchor: its true partner is outside the search. Its epoch's anchors are then
# look-alikes that disagree, so the epoch comes out flagged ("few-anchors" or "carried"), not
# anchored wrong. ASSUMPTION backed by simulation only: with trips of 110 and 150 min, 0 of 160
# epochs sure and wrong (40 seeds each at 3 % and 10 % distinctive cars), against 60 before the
# 2026-10-26 audit. ASSUMPTION that A->B (the middle of the bridge, not the whole wait) stays
# under 90 min, until a hand-timed or GPS-timed A->B says.
HI_MIN = SIGNATURE_TTL_S / 60 / 2
RECONNECT_S = 10.0       # wait before opening a stream again after it failed
# After StreamFrozen after StreamFrozen the wait doubles (10, 20, 40 ... s) up to RECONNECT_MAX_S,
# and drops back only once a read has given RESET_AFTER_S of frames. A picture frozen for good
# was otherwise opened again every 30 s or so (the wait plus the 20 s the check needs), and each
# opening of the City stream fetches its playlist's segments again (about 2, 0.26 MB, on
# 2026-10-27). Not reset by a read that ends any other way: a StreamLost from the same stalled
# source would keep the wait at RECONNECT_S. ASSUMPTION: 60 s of frames is a stream that works,
# three times what the frozen check needs to raise.
RECONNECT_MAX_S = 300.0
RESET_AFTER_S = 60.0
# A line where nothing moved (hls.Stamped.moved) and no car was counted for longer than this is a
# gap once something moves again (module docstring). ASSUMPTION, from one 20-min night at line B
# (2026-10-27 03:32-03:52 UTC) with its motion bursts thinned (bursts assumed to scale with
# traffic): at 300 s such gaps came 0 an hour at that night's traffic and at half of it, and 0.13
# an hour at a quarter; at 120 s, 0, 0.8 and 5.8. The trade-off: a picture frozen with noise for
# less than this reads as a quiet road, and so does a longer one with a car counted in it at least
# this often, since a car counted moves `moved_at` on (ADR-0016; a false count on a frozen picture,
# how often is not measured, ASSUMPTION).
QUIET_S = 300.0
SAVE_EVERY_S = 600       # how often the loop counters' learnt roads are saved
MIXED = "mixed"          # the one group of a pair whose lines share no lane type: every lane

log = logging.getLogger(__name__)


class TransitRunner:
    def __init__(self, cam_a: Camera, cam_b: Camera, methods=METHODS,
                 lo_min: float = LO_MIN, hi_min: float = HI_MIN, device: str | None = None):
        if 2 * hi_min > SIGNATURE_TTL_S / 60:
            raise ValueError(f"hi_min {hi_min} min: no anchor could be trusted within the "
                             f"{SIGNATURE_TTL_S / 3600:.0f} h signatures are kept")
        # where YOLOX runs: "auto", "gpu" or "cpu"; None reads $CHISME_YOLOX_DEVICE, else "auto"
        self.device = device
        # camera -> (its last frame before YOLOX's GPU path stopped, why, its device_record then)
        self.yolox_stopped: dict[str, tuple[datetime, str, dict]] = {}
        self.cams = (cam_a, cam_b)
        self.lines = {cam_a.id: _one_line(cam_a), cam_b.id: _one_line(cam_b)}
        self.line_a, self.line_b = self.lines[cam_a.id], self.lines[cam_b.id]
        self.methods = tuple(methods)
        self.lo_min, self.hi_min = lo_min, hi_min
        self.types = {self.lines[cam.id]: transit.lane_types(cam.count_zones)
                      for cam in self.cams}     # line -> lane -> lane type
        self.groups = _groups(self.types[self.line_a], self.types[self.line_b])
        if self.groups != (MIXED,):
            alone = [f"{cam.id} {lane} ({kind})" for cam in self.cams
                     for lane, kind in self.types[self.lines[cam.id]].items()
                     if kind not in self.groups]
            if alone:
                log.warning("no lane type at both lines, so never measured: %s", ", ".join(alone))
        # (method, group) -> why that method does not measure that group
        self.not_measured = unmeasured(cam_a, cam_b, self.methods)
        if self.not_measured:
            log.warning("a zone of the group is not counted by the method, so not measured: %s",
                        "; ".join(f"{m} {g} ({why})" for (m, g), why in self.not_measured.items()))
        self.counters = {cam.id: self._counters(cam) for cam in self.cams}
        self.crossings: dict[tuple[str, str], deque[Crossing]] = {
            (m, ln): deque() for m in self.methods for ln in (self.line_a, self.line_b)}
        self.gaps: dict[tuple[str, str], list[Gap]] = {key: [] for key in self.crossings}
        self.learning_since: dict[str, datetime | None] = {}   # camera -> when loop learning began
        self.first_at: dict[str, datetime] = {}
        self.last_at: dict[str, datetime] = {}
        # camera -> its last frame that moved or counted a car (or its first frame): kept across
        # reconnects, so a line is seen up to here and no further (seen_until)
        self.moved_at: dict[str, datetime] = {}
        # camera -> its last frame before its latest StreamFrozen: a still stretch since
        # `moved_at` that holds one is a freeze throughout (_quiet)
        self.frozen_at: dict[str, datetime] = {}
        self.seen_mono: dict[str, float] = {}   # camera -> time.monotonic() of its latest frame
        self.reconnects: dict[str, int] = {cam.id: 0 for cam in self.cams}   # reads retried
        # camera -> what its frozen check judged since the last records (_judged), and how far
        # its latest frame was past `moved_at` at the last measure: the records' Q-32 numbers
        self.judged: dict[str, dict] = {cam.id: _unjudged() for cam in self.cams}
        self.hold_lag: dict[str, float] = {}
        # (method, group) -> the last measurement, and the prior it was opened with
        self.previous: dict[tuple[str, str],
                            tuple[transit.Measurement, transit.Prior | None]] = {}
        self.errors: dict[str, str] = {}
        self.blocked: set[str] = set()      # cameras whose server refused us: never retried
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._saved_at = time.monotonic()

    def _counters(self, cam: Camera) -> list:
        """Each method's counter, over the zones that method counts (_counts). A zone stays in
        when its group is not measured by the method: line A's loop judges a car across lane-3
        and lane-4 by both (loop._beside), so leaving lane-4 out would change lane-3's count."""
        out = []
        loop_zones = tuple(z for z in cam.count_zones if _counts(z, "loop"))
        if "loop" in self.methods and loop_zones:      # a LoopCounter with no zones refuses
            from .loop import LoopCounter
            loop = LoopCounter(cam, loop_zones)
            refs = _load_refs(cam)
            if refs:
                still = loop.seed(refs)
                log.info("%s: loop roads seeded; still learning %s", cam.id, still or "none")
            out.append(loop)
        yolox_zones = tuple(z for z in cam.count_zones if _counts(z, "yolox"))
        if "yolox" in self.methods and yolox_zones:
            from .detect import DetectorCounter
            # one network per camera: one thread each. gpu.GpuRefused ("gpu" asked for and
            # refused) goes up to the command, which stops
            out.append(DetectorCounter(cam, zones=yolox_zones, device=self.device))
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
        frozen = 0          # reads in a row that ended in StreamFrozen, since one that worked
        while not self._stop.is_set():
            reader = None
            seen_gaps = 0
            resumed = True
            first_fed = last_fed = None     # the first and the latest frame this read gave
            why, stalled = None, False
            try:
                reader = live_reader(cam)
                for frame in reader.frames(None):
                    if self._stop.is_set():
                        return
                    last = self.last_at.get(cam.id)
                    if last is not None and frame.at <= last:
                        # A fresh reader starts at the playlist's oldest segment: these frames
                        # were counted before the reconnect. What its check judged still counts
                        # (the native repeat it carries was measured by this read's decoder).
                        with self._lock:
                            self._judged(cam.id, frame)
                        continue
                    if resumed and last is not None:
                        # The stream was not read from the last frame before to this one.
                        self._gap(cam, Gap(last, (frame.at - last).total_seconds(),
                                           "stream down or reconnecting"))
                    resumed = False
                    while seen_gaps < len(reader.gaps):
                        gap = reader.gaps[seen_gaps]
                        if gap.after is not None and frame.at <= gap.after:
                            break   # found ahead of the frames: it reaches the counters with them
                        self._gap(cam, gap)
                        seen_gaps += 1
                    self._frame(cam, line, frame)
                    first_fed, last_fed = first_fed or frame.at, frame.at
            except Blocked as exc:
                self.errors[cam.id] = f"BLOCKED {exc}"
                if cam.source == "hls":
                    # The City refused this client: stop, as ADR-0014 decision 5 says. Never
                    # retried, never disguised.
                    self.blocked.add(cam.id)
                    log.error("%s: blocked by the server (%s); no longer read", cam.id, exc)
                    return
                why = exc
            except StreamFrozen as exc:
                # The frozen frames were already counted as road with no cars: take back the
                # stretch the reader's rule judged as a gap, so the line's count is not read as
                # zero traffic.
                self.errors[cam.id] = f"StreamFrozen: {exc}"
                with self._lock:    # the frame it raised on never reaches _frame
                    self._judged(cam.id, exc)
                last = self.last_at.get(cam.id)
                if last is not None:
                    since = max(last - timedelta(seconds=exc.seconds), self.first_at[cam.id])
                    self._gap(cam, Gap(since, (last - since).total_seconds(),
                                       "picture frozen or black"))
                    self.frozen_at[cam.id] = last
                why, stalled = exc, True
            except StreamError as exc:
                self.errors[cam.id] = f"{type(exc).__name__}: {exc}"
                why = exc
            except Exception as exc:
                self.errors[cam.id] = f"{type(exc).__name__}: {exc}"
                log.exception("%s failed; reconnecting", cam.id)
            finally:
                if reader is not None:
                    reader.http.close()
            if first_fed is not None and (last_fed - first_fed).total_seconds() >= RESET_AFTER_S:
                frozen = 0
            if stalled:
                frozen += 1
            wait = min(RECONNECT_MAX_S, RECONNECT_S * 2 ** max(0, frozen - 1))
            if why is not None:
                log.warning("%s: %s; reconnecting in %.0f s", cam.id, why, wait)
            if self._stop.is_set():
                return
            with self._lock:
                self.reconnects[cam.id] = self.reconnects.get(cam.id, 0) + 1
            self._stop.wait(wait)

    def _gap(self, cam: Camera, gap: Gap) -> None:
        line = self.lines[cam.id]
        for counter in self.counters[cam.id]:
            counter.gap(gap)
        with self._lock:
            for m in self.methods:
                self.gaps[(m, line)].append(gap)

    def _frame(self, cam: Camera, line: str, frame) -> None:
        self.errors.pop(cam.id, None)
        # None is a reader with no frozen check (hls.Stamped.moved), which cannot say "still"
        moved = getattr(frame, "moved", None) is not False
        if moved:
            self._quiet(cam, frame.at)
        counted = False
        for counter in list(self.counters[cam.id]):
            try:
                found = [c for c in counter.add(frame) if c.line == line]
            except GpuFailed as exc:
                # Not the stream's fault: reconnecting would raise it again on every frame, and
                # leave both methods dark. Only YOLOX stops here. The stop is process-wide
                # (gpu.STATE): the other camera's YOLOX, if on the GPU, stops at its next frame.
                self._yolox_failed(cam, counter, frame.at, exc)
                continue
            own = counter.gaps()
            counted = counted or bool(found)
            with self._lock:
                self.crossings[(counter.method, line)].extend(found)
                self.gaps[(counter.method, line)].extend(own)
            if counter.method == "loop":
                self._learning(cam, line, counter, frame.at)
        if counted and not moved:
            # A car crossed in a frame whose picture barely changed: the line was live, so the
            # frame is judged as a moving one is, but only now that its cars are found. A gap
            # then holds them, and they are cut out with it.
            self._quiet(cam, frame.at)
        with self._lock:    # seen only once its crossings and gaps are kept
            self.first_at.setdefault(cam.id, frame.at)
            if moved or counted or cam.id not in self.moved_at:
                self.moved_at[cam.id] = frame.at
            self.last_at[cam.id] = frame.at
            self.seen_mono[cam.id] = time.monotonic()
            self._judged(cam.id, frame)

    def _judged(self, cid: str, source) -> None:
        """Fold what the frozen check judged (a frame's, hls.Stamped, or a StreamFrozen's
        `repeats`, `grey` and `native_repeat_s`; absent or None: not judged) into the camera's
        numbers since the last records: the most repeats, the darkest grey, the longest native
        repeat. Called with the lock held, from the camera's own thread."""
        kept = self.judged.setdefault(cid, _unjudged())
        for key, value, pick in (("repeats", getattr(source, "repeats", None), max),
                                 ("grey", getattr(source, "grey", None), min),
                                 ("native", getattr(source, "native_repeat_s", None), max)):
            if value is not None:
                kept[key] = value if kept[key] is None else pick(kept[key], value)

    def _yolox_failed(self, cam: Camera, counter, at: datetime, exc: GpuFailed) -> None:
        """YOLOX's GPU path stopped (gpu.GpuFailed) on this camera's frame at `at`. Its counter
        is dropped for the rest of the run, and its line is unseen by YOLOX from the frame
        before (`since`; an open gap, _gaps_for) - never counted on the CPU in its place. The
        loop counter goes on. Said loudly: in the log here, and in the status and every yolox
        record from now on."""
        with self._lock:
            if counter in self.counters[cam.id]:
                self.counters[cam.id].remove(counter)
            record = getattr(counter, "device_record", None)
            since = self.last_at.get(cam.id, at)
            self.yolox_stopped[cam.id] = (since, str(exc),
                                          dict(record()) if callable(record) else {})
        log.error("%s: YOLOX STOPPED for the rest of this run: %s. Its line is unseen by YOLOX "
                  "from %s UTC (a gap in every yolox record), never counted on the CPU instead; "
                  "the loop counter goes on.", cam.id, exc, f"{since:%H:%M:%S}")

    def _quiet(self, cam: Camera, at: datetime) -> None:
        """Something moved at `at`. If nothing had at the line for more than QUIET_S, that
        stretch could have been a picture frozen with noise as well as a quiet road: it becomes
        a gap, through to `at` (a frame's crossings carry its time, so this frame's are in it).
        So does a shorter one that held a StreamFrozen: a fresh read's check raises only on a
        full window, so the end of that freeze, read after a reopen, was never taken back."""
        since = self.moved_at.get(cam.id)
        frozen = self.frozen_at.get(cam.id)
        if since is not None and ((at - since).total_seconds() > QUIET_S
                                  or frozen is not None and frozen >= since):
            seconds = (at - since).total_seconds()
            self._gap(cam, Gap(since, seconds, f"nothing moved for {seconds:.0f} s"))

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
        """Both lines are observed up to the older of their latest frames, each held back to its
        last frame that moved or counted a car (`moved_at`; module docstring)."""
        with self._lock:
            if len(self.last_at) < 2:
                return None
            return min(min(at, self.moved_at.get(cid, at)) for cid, at in self.last_at.items())

    def measure(self) -> dict[tuple[str, str], tuple[transit.Measurement, transit.Now | None]]:
        """(method, group) -> its measurement and the count now, for each one measurable."""
        with self._lock:
            if not self.last_at:
                return {}
            # Per-vehicle data goes after SIGNATURE_TTL_S by the newest frame either camera has,
            # plus the wall time since it arrived: even while the other camera is down and
            # nothing can be measured, and while both are.
            mono = time.monotonic()
            cutoff = max(at + timedelta(seconds=mono - self.seen_mono.get(cid, mono))
                         for cid, at in self.last_at.items()) - timedelta(seconds=SIGNATURE_TTL_S)
            for m in self.methods:
                self._expire(m, cutoff)
            at = self.seen_until()
            # The hold's lag at each line now, with `at`: its latest frame past `moved_at`
            self.hold_lag = {cid: max(0.0, (last - self.moved_at.get(cid, last)).total_seconds())
                             for cid, last in self.last_at.items()}
            if at is None or len(self.first_at) < 2:
                return {}
            horizon = max(max(self.first_at.values()), cutoff)
            if horizon >= at:
                # One camera is more than the TTL behind: no measurement before it is kept.
                self.previous.clear()
                return {}
            out = {}
            for m in self.methods:
                ga = self._gaps_for(m, self.line_a, at)
                gb = self._gaps_for(m, self.line_b, at)
                for group in self.groups:
                    key = (m, group)
                    if key in self.not_measured:
                        continue
                    a = self._of_group(self.crossings[(m, self.line_a)], self.line_a, group)
                    b = self._of_group(self.crossings[(m, self.line_b)], self.line_b, group)
                    start, prior = self._start(key, horizon, ga + gb)
                    try:
                        measured = transit.measure(a, b, self.lo_min, self.hi_min, gaps_a=ga,
                                                   gaps_b=gb,
                                                   start=start if prior is None else None,
                                                   end=at, prior=prior)
                    except ValueError as exc:        # e.g. `start` inside a gap
                        log.warning("%s %s: not measurable: %s", m, group, exc)
                        self.previous.pop(key, None)  # start afresh next tick, not from a stale one
                        continue
                    self.previous[key] = (measured, prior)
                    current = transit.now_from(measured, at) if measured.epochs else None
                    out[key] = (measured, current)
        return out

    def _of_group(self, crossings, line: str, group: str) -> list[Crossing]:
        """The crossings at `line` in the lanes of lane-type group `group`."""
        if group == MIXED:
            return list(crossings)
        return transit.of_lane_type(crossings, self.types[line], group)

    def _start(self, key: tuple[str, str], horizon: datetime,
               gaps: list[Gap]) -> tuple[datetime, transit.Prior | None]:
        """Where the measurement of `key`, (method, group), starts, and the cars-between count
        carried to it. Each group carries its own count, from its own last measurement.

        The start never moves backwards (once an earlier measurement moved it past a gap, it
        stays there), and it is never left just before a gap: the count carried across a gap
        needs RATE_WINDOW_MIN of rate before it, so a start inside that stretch moves to the
        gap's end.

        A count is carried only from a measurement that saw the start. While the start stays
        where the previous measurement started, this one opens as that one did, never from the
        previous one's own output: a first look-alike guess fed back every tick could be neither
        confirmed nor disputed, and would stand until anchors outvoted it. When the start moves,
        the count at it is carried only if its uncertainty is known, so that anchors can check
        it; otherwise the measurement starts afresh."""
        prev, opened = self.previous.get(key, (None, None))
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
        if prev is None:
            return start, None
        if start == prev.start:
            return start, opened
        # Past prev.end prior_at gives None: nothing is carried from what did not see the start.
        prior = transit.prior_at(prev, start)
        return start, prior if prior is not None and prior.uncertainty is not None else None

    def _gaps_for(self, method: str, line: str, at: datetime) -> list[Gap]:
        gaps = list(self.gaps[(method, line)])
        if method == "loop":
            for cam in self.cams:
                since = self.learning_since.get(cam.id)
                # `at` can be held back before it (seen_until): nothing is open there yet
                if self.lines[cam.id] == line and since is not None and since < at:
                    gaps.append(Gap(since, (at - since).total_seconds(), "loop zones learning"))
        if method == "yolox":
            for cam in self.cams:
                stopped = self.yolox_stopped.get(cam.id)
                # open to the end of every measurement from now on: the counter is gone
                if self.lines[cam.id] == line and stopped is not None and stopped[0] < at:
                    gaps.append(Gap(stopped[0], (at - stopped[0]).total_seconds(),
                                    f"YOLOX stopped: {stopped[1]}"))
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
        """One record per (method, group) measured. Each also carries each line's Q-32 numbers
        (module docstring), the same in every record of one call: what the frozen checks judged
        since the last call that made records, which these take, and the hold's lag at the
        measure."""
        devices = self.devices()
        line_numbers = self._numbers() if measured else {}
        out = []
        for (method, group), (m, current) in measured.items():
            rec = transit.record(m, transit.windows(m), current, lane_type=group)
            rec.update({"method": method,    # m.method is "unknown" when neither line had a car
                        "written_at": datetime.now(UTC).isoformat(),
                        # to the minute: the latest frame's time can be a car's crossing time
                        "seen_until": m.end.replace(second=0, microsecond=0).isoformat(),
                        "line_a": f"{self.cams[0].id}:{self.line_a}",
                        "line_b": f"{self.cams[1].id}:{self.line_b}",
                        # how often each line's stream was opened again since the start: with
                        # gaps_a / gaps_b, what cut the epochs
                        "reconnects_a": self.reconnects.get(self.cams[0].id, 0),
                        "reconnects_b": self.reconnects.get(self.cams[1].id, 0),
                        "anchoring": transit.anchoring(m)})
            rec.update(line_numbers)
            if method == "yolox":
                # where its boxes came from at each camera: a loop count touches no YOLOX path
                rec["devices"] = {cid: dict(d) for cid, d in devices.items()}
            out.append(rec)
        return out

    def _numbers(self) -> dict:
        """Each line's Q-32 numbers for the records (`*_a`, `*_b`), taken: the frozen checks'
        start afresh for the next records. None where a line was not judged since the last."""
        out = {}
        with self._lock:
            for cam, end in zip(self.cams, ("a", "b"), strict=True):
                kept = self.judged.get(cam.id) or _unjudged()
                self.judged[cam.id] = _unjudged()
                lag = self.hold_lag.get(cam.id)
                out.update({
                    f"most_repeats_{end}": kept["repeats"],
                    f"native_repeat_s_{end}": kept["native"],
                    f"darkest_grey_{end}": None if kept["grey"] is None else round(kept["grey"], 1),
                    f"hold_lag_s_{end}": None if lag is None else round(lag, 1)})
        return out

    def devices(self) -> dict[str, dict]:
        """camera -> where its YOLOX boxes come from (detect.DetectorCounter.device_record:
        "device", "cpu" or "gpu:<adapter>", and on the GPU how many frames were also detected on
        the CPU, "cpu_checked", and how many of those differed, "cpu_differed"), read now; for a
        camera whose GPU path stopped, as it was then, with why ("stopped") and the minute of
        its last frame counted ("stopped_at")."""
        out = {}
        with self._lock:
            for cam in self.cams:
                for counter in self.counters.get(cam.id, ()):
                    record = getattr(counter, "device_record", None)
                    if counter.method == "yolox" and callable(record):
                        out[cam.id] = dict(record())
                if cam.id in self.yolox_stopped:
                    since, why, was = self.yolox_stopped[cam.id]
                    out[cam.id] = {**was, "stopped": why, "stopped_at": since.replace(
                        second=0, microsecond=0).isoformat()}
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
            with self._lock:
                last, moved = self.last_at.get(cam.id), self.moved_at.get(cam.id)
                held = None if last is None or moved is None else (last - moved).total_seconds()
                reconnects = self.reconnects.get(cam.id, 0)
            if held is not None and held >= 10:     # 1 in 100 over 8.4 s on a live night
                parts[-1] += f", nothing moved for {held:.0f} s"
            if reconnects:
                parts[-1] += f", reopened {reconnects}x"
            if cam.id in self.blocked:
                parts[-1] += " (stopped: blocked)"
        lines = [" | ".join(parts)]
        for m, group in ((m, g) for m in self.methods for g in self.groups):
            if (m, group) in self.not_measured:
                lines.append(f"  {m:5} {group:6}: not measured: {self.not_measured[(m, group)]}")
                continue
            with self._lock:
                a = [c.at for c in self._of_group(self.crossings[(m, self.line_a)],
                                                  self.line_a, group)]
                b = [c.at for c in self._of_group(self.crossings[(m, self.line_b)],
                                                  self.line_b, group)]
            if at is not None:
                since = at - timedelta(minutes=10)
                rate_a = sum(1 for t in a if t >= since) / 10
                rate_b = sum(1 for t in b if t >= since) / 10
            else:
                rate_a = rate_b = 0.0
            text = (f"  {m:5} {group:6}: A {len(a)} cars ({rate_a:.1f}/min)  "
                    f"B {len(b)} ({rate_b:.1f}/min)")
            if (m, group) in measured:
                meas, current = measured[(m, group)]
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
            if (m, group) in measured:
                lines.append("  " + transit.anchoring_text(
                    m, group, transit.anchoring(measured[(m, group)][0]),
                    stopped=m == "yolox" and bool(self.yolox_stopped)))
        return "\n".join(lines + self.device_lines())

    def device_lines(self) -> list[str]:
        """The status's YOLOX lines: where it runs at each camera, and, loudly, where its GPU
        path stopped."""
        devices = self.devices()
        out = []
        if devices:
            out.append("  yolox device: " + "; ".join(f"{cid} {device_text(d)}"
                                                      for cid, d in devices.items()))
        with self._lock:
            stopped = sorted(self.yolox_stopped.items())
        for cid, (since, why, _) in stopped:
            out.append(f"  YOLOX STOPPED at {cid}: {why}. Unseen by YOLOX there since "
                       f"{since:%H:%M:%S} UTC for the rest of this run (a gap, never counted on "
                       "the CPU instead); the loop goes on.")
        return out

    def save_refs(self) -> None:
        for cam in self.cams:
            for counter in self.counters[cam.id]:
                if counter.method == "loop":
                    _save_refs(cam, counter.references())
        self._saved_at = time.monotonic()

    def maybe_save_refs(self) -> None:
        if time.monotonic() - self._saved_at >= SAVE_EVERY_S:
            self.save_refs()


def device_text(record: dict) -> str:
    """One camera's YOLOX path (TransitRunner.devices) for the status: "cpu", or
    "gpu:<adapter> (checked on the CPU: 12 frames, 0 differed)", and STOPPED once stopped."""
    text = str(record.get("device", "unknown"))
    if "cpu_checked" in record:
        text += (f" (checked on the CPU: {record['cpu_checked']} frames, "
                 f"{record.get('cpu_differed', 0)} differed)")
    return text + (" STOPPED" if record.get("stopped") else "")


def refused_text(exc: Exception) -> str:
    """What `camera transit` and `camera estimate` print when "gpu" was asked for and refused
    (gpu.GpuRefused) as the counters were built: nothing has been read."""
    return (f"YOLOX on the GPU was asked for (--device gpu, or {DEVICE_ENV}=gpu) and refused: "
            f"{exc}. Nothing was started. --device auto runs YOLOX on the CPU when the GPU is "
            "refused; --device cpu runs it there always.")


def _unjudged() -> dict:
    """A camera's frozen-check numbers before any frame is judged (TransitRunner._judged)."""
    return {"repeats": None, "grey": None, "native": None}


def _one_line(cam: Camera) -> str:
    lines = sorted({z.line for z in cam.count_zones})
    if len(lines) != 1:
        raise ValueError(f"{cam.id}: needs exactly one counting line, has {lines or 'none'}")
    return lines[0]


def _groups(types_a: dict[str, str | None], types_b: dict[str, str | None]) -> tuple[str, ...]:
    """The lane-type groups measured: each lane type that zones at both lines carry, else
    (MIXED,), every lane pooled. Pooled, the k-th car at A is paired with the (offset + k)-th at B
    whatever its type, and SENTRI, Ready and general cars reorder between the lines. In
    simulation, anchored as here, a pooled window's median followed one type's trip, the type
    with most enclosed anchors: 41-47 min in some runs and 7-11 in others, unflagged, against a
    flow-weighted mean of 35-39. With half the cars SENTRI at 8 min and the rest at 40, every
    pooled window read 7.8-11.2 min, anchored and unflagged, where each group measured on its
    own read 7.9-8.1 and 39.9-40.1 (the scenario of LaneTypeGroups in
    tests/border/test_border_camera_runner.py: pooled through the runner as of 1221c9f, which
    measured no groups and had no "normal" type, so read as "general"; by group through this
    runner as of d2f3b29)."""
    shared = ({t for t in types_a.values()} & {t for t in types_b.values()}) - {None}
    return tuple(sorted(shared)) or (MIXED,)


def unmeasured(cam_a: Camera, cam_b: Camera,
               methods=METHODS) -> dict[tuple[str, str], str]:
    """(method, group) -> why that method does not measure that lane-type group: a zone of the
    group, at either line, that the method does not count (_counts). The runner's
    `not_measured`, which a reader of its records (a replay) can rebuild from the cameras."""
    types = [transit.lane_types(cam.count_zones) for cam in (cam_a, cam_b)]
    out = {}
    for m in methods:
        for g in _groups(*types):
            refused = [z for cam in (cam_a, cam_b) for z in cam.count_zones
                       if (g == MIXED or z.lane_type == g) and not _counts(z, m)]
            if refused:
                out[(m, g)] = "; ".join(f"{z.camera} {z.lane} is counted by "
                                        f"{' and '.join(z.methods)} only" for z in refused)
    return out


def _counts(zone: Zone, method: str) -> bool:
    """Whether `method` counts cars at `zone`. A zone's `methods` (crossing.Zone, cameras.json
    "methods") names the counters whose count there holds; one with none is counted by every
    method."""
    return zone.methods is None or method in zone.methods


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
        arrays[f"{line}|{lane}|geometry"] = _geometry(cam, line, lane)
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
        # Opened here, not by np.load: np.load leaves the file open when the zip is truncated.
        with open(path, "rb") as f, np.load(f) as saved:
            out = {}
            for k in {k.rsplit("|", 1)[0] for k in saved.files}:
                line, lane = k.split("|", 1)
                kept = saved[f"{k}|geometry"] if f"{k}|geometry" in saved.files else None
                if kept is None or not np.array_equal(kept, _geometry(cam, line, lane)):
                    log.warning("%s: the loop road of zone %s/%s was learnt on another zone or "
                                "camera geometry; learning it afresh", cam.id, line, lane)
                    continue
                out[(line, lane)] = (saved[f"{k}|grey"].copy(), saved[f"{k}|edges"].copy())
            return out
    except (OSError, ValueError, KeyError, EOFError, zipfile.BadZipFile) as exc:
        log.warning("%s: unreadable loop roads (%s); learning afresh", cam.id, exc)
        return {}


def _geometry(cam: Camera, line: str, lane: str) -> np.ndarray:
    """What a zone's road was learnt on: the zone's quad and the camera's frame size, ground
    rectangle and masks. LoopCounter.seed() checks only the patch's shape, which a zone redrawn
    elsewhere can share (pdn-north's lane-1 to lane-3 do), so a road is kept only with this
    unchanged. A changed mask is stale too: the pixels it covered were learnt as 0
    (loop.zone_features), and uncovered they read as a car."""
    quads = [z.quad for z in cam.count_zones if (z.line, z.lane) == (line, lane)]
    return np.float64([*np.ravel(quads), *cam.frame_size, *np.ravel(cam.ground), cam.width_m,
                       cam.length_m, *np.ravel(cam.mask)])


def cmd_transit(args, cameras) -> int:
    from .worker import AlreadyRunning, hold_lock

    for cid in (args.a, args.b):
        if cid not in cameras:
            raise SystemExit(f"unknown camera {cid}")
    try:
        device = requested_device(getattr(args, "device", None))
    except ValueError as exc:       # a $CHISME_YOLOX_DEVICE that is none of auto, gpu, cpu
        raise SystemExit(str(exc)) from None
    state_dir().mkdir(parents=True, exist_ok=True)
    try:
        held = hold_lock(state_dir() / "transit.lock")
    except AlreadyRunning as exc:
        print(exc)
        return 1
    methods = [m.strip() for m in args.methods.split(",") if m.strip()]
    try:
        runner = TransitRunner(cameras[args.a], cameras[args.b], methods, device=device)
    except GpuRefused as exc:
        held.close()
        print(refused_text(exc), flush=True)
        return 1
    except BaseException:
        held.close()
        raise
    runner.start()
    devices = "; ".join(f"{cid} {device_text(d)}" for cid, d in runner.devices().items())
    print(f"Crossing time {args.a} -> {args.b}, methods {', '.join(methods)}"
          + (f", YOLOX ({device}) on {devices}" if devices else "") + ". "
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
