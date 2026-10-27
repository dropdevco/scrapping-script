"""`camera estimate`: the queue reader and both crossing counters in one process, and each
method's camera wait for Paso del Norte northbound, side by side (combine.py, ADR-0015).

    python -m scraper.border camera estimate [--port 8090] [--every 60] [--queue-every 120]
                                             [--methods loop,yolox]
    python -m scraper.border camera estimate --replay transit-<date>.jsonl
                                             [--queue queue-<date>.jsonl]

One process holds both locks: `transit.lock`, and through its Worker `worker.lock`. So
`camera view`, `camera run` and `camera transit` refuse to start while it runs, and it refuses
while any of them runs. Inside it:
- runner.TransitRunner on pdn-north (line A) and city-pdn-north (line B), as `camera transit`
  runs it: both counting methods on every frame, each over the zones it counts (cameras.json
  "methods"), and a lane-type group a method does not count is not measured by it;
- worker.Worker on pdn-north and pdn-south-street, unchanged: the queue reader. pdn-north is
  pulled twice, once by each (the owner's OK, 2026-10-26). CBP's published waits are fetched
  for the page only, and never reach a camera number;
- viewer.serve on 127.0.0.1, the existing page, with its "camera estimate" section fed from
  here (`Live`). A camera marked display: false (the City's) is never read by the Worker, so
  no picture of it exists to serve.

Every `--every` seconds a tick measures the counts (`runner.measure`), appends the runner's
records to transit-<date>.jsonl as `camera transit` does, combines them with the queue
reader's recent lane signals into one camera record (`combine.combine`), appends that to
camera-<date>.jsonl, and prints a status. Every lane signal the Worker emits is appended to
queue-<date>.jsonl. Files hold derived numbers only, and every time in them is cut to the
minute; no frame and no per-vehicle datum is written.

A record's `at` is its transit records' `seen_until`, and the queue readings it uses are those
whose observed_at, cut to the minute, is not after `at`. `--replay` applies the same rule to
the logged files, so it grades a formula change on every past run. It can differ from what a
live tick wrote where a reading reached the log after that tick, after a restart (the new
process starts with no readings in memory), or across a UTC midnight (one --queue file).
Each transit record `camera estimate` writes names its tick (`run`, the process's start cut
to the minute, and `tick`, a count), so a replay splits the log into the live ticks exactly.

The owner is shown each method's wait on this PC (ADR-0015 decision 2, accepted 2026-10-26),
through combine.display, the one rule for what is shown; nothing votes (decisions 3 and 4,
proposed). When the City refuses us, line B stops for good (ADR-0014 decision 5): no camera
wait is made from then on, and one alert says so.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections import deque
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

import cv2
import numpy as np

from . import combine, transit
from .calibration import PX_PER_M, Camera, Strip
from .measure import BIN_M, ROLLING_SECONDS, _bins
from .reader import WINDOW_SECONDS, state_dir

PAIRS_FILE = Path(__file__).with_name("pairs.json")
PAIR = "pdn-north"           # the pair this command measures, keyed by its camera A
THREADS = 4                  # OpenCV threads: detect.py's YOLOX timings were taken at 4
KEEP_S = ROLLING_SECONDS + 15 * 60   # queue signals kept in memory: the 15 min combine rolls
                             # over, plus 15 min for the counting lines to run behind the reader
LAG_ALERT_S = 120            # ASSUMPTION: a line further behind live than this is worth an alert

log = logging.getLogger(__name__)


# --- the pair ---------------------------------------------------------------------------------

def load_pair(cameras: Mapping[str, Camera], path: Path = PAIRS_FILE, key: str = PAIR) -> dict:
    """pairs.json's entry, plus what combine needs from cameras.json: camera A's lane types
    (`a_lanes`; its strips and count zones share their lane names) and line A's place on each
    strip (`a_m`, `r_m`, from `line_a_place`)."""
    with open(path, encoding="utf-8-sig") as f:
        pair = dict(json.load(f)[key])
    cam_a = cameras[pair["a"]]
    pair["a_lanes"] = transit.lane_types(cam_a.count_zones)
    pair["a_m"], pair["r_m"] = line_a_place(cam_a)
    return pair


def line_a_place(cam: Camera) -> tuple[dict[str, float], dict[str, float]]:
    """Per strip of `cam` with a count zone of the same lane name: metres from the strip's booth
    end to the zone's far edge (`a_m`, where a car is counted across the line), and from there
    to the last metre the strip sees (`r_m`, the run-up a line reaching past the view still
    has to cover). The zone's far-edge corners go through the strip's ground homography, the
    one the queue reader measures with, and the view's end is found as the reader finds it
    (whole metres at least half seen), so both are in the queue reader's own metres. A zone
    outside what its strip sees gives that lane no place, and combine then says so."""
    a_m, r_m = {}, {}
    for lane in cam.lanes:
        zones = [z for z in cam.count_zones if z.lane == lane.lane]
        if not zones:
            continue
        strip = Strip(cam, lane, cam.frame_size)
        far = np.float32([[zones[0].quad[2], zones[0].quad[3]]])
        rows = cv2.perspectiveTransform(far, strip.matrix)[0][:, 1]
        place = float(rows.mean()) / PX_PER_M
        seen = [strip.rows_valid[a:b].mean() >= 0.5 for a, b in _bins(len(strip.rows_valid))]
        visible = np.flatnonzero(seen)
        if not len(visible):
            continue
        end = float(visible[-1] + 1) * BIN_M
        if not 0 <= place < end:
            log.warning("%s %s: line A lies outside what the strip sees", cam.id, lane.lane)
            continue
        a_m[lane.lane], r_m[lane.lane] = round(place, 1), round(end - place, 1)
    return a_m, r_m


def queue_cameras(cameras: Mapping[str, Camera], pair: Mapping) -> list[Camera]:
    """The cameras the queue reader reads for this pair: camera A and the street. A camera
    marked display: false is never among them, so the page can never get a picture of it."""
    chosen = [cameras[cid] for cid in (pair.get("a"), pair.get("street")) if cid in cameras]
    return [c for c in chosen if c.display and c.lanes]


# --- the queue reader's log -------------------------------------------------------------------

class QueueLog:
    """Every lane signal the Worker emits (the dict `camera run` prints), appended to
    queue-<UTC date>.jsonl with its `observed_at` cut to the minute, and the last KEEP_S of
    them kept in memory for combine. Error lines are written and not kept. The Worker emits
    from several threads at once, hence the lock."""

    def __init__(self, keep_s: float = KEEP_S):
        self.keep_s = keep_s
        self._lock = threading.Lock()
        self._kept: deque[dict] = deque()
        self._newest: float | None = None
        self._failed = False

    def add(self, sig: Mapping) -> None:
        line = dict(sig)
        if line.get("observed_at"):
            line["observed_at"] = _minute(line["observed_at"])
        with self._lock:
            try:
                _append(state_dir() / f"queue-{datetime.now(UTC):%Y-%m-%d}.jsonl", [line])
            except OSError as exc:            # a full disk must not stop the readings
                if not self._failed:
                    log.error("queue signals not written (%s); kept in memory", exc)
                self._failed = True
            else:
                self._failed = False
            if not _is_reading(line):
                return
            t = _parse(line["observed_at"]).timestamp()
            self._newest = t if self._newest is None else max(self._newest, t)
            self._kept.append(line)
            while self._kept and (self._newest - _parse(self._kept[0]["observed_at"]).timestamp()
                                  > self.keep_s):
                self._kept.popleft()

    def recent(self, at: datetime) -> list[dict]:
        with self._lock:
            return within(list(self._kept), at)

    def latest(self) -> dict[tuple[str, str], dict]:
        """(camera, lane) -> the evidence of its newest reading, for the status line."""
        with self._lock:
            out: dict[tuple[str, str], tuple[str, dict]] = {}
            for sig in self._kept:
                ev = sig["evidence"]
                key = (ev.get("camera"), ev.get("lane"))
                if key not in out or out[key][0] <= sig["observed_at"]:
                    out[key] = (sig["observed_at"], ev)
            return {k: v[1] for k, v in sorted(out.items())}


def within(signals: Iterable[Mapping], at: datetime) -> list[dict]:
    """The lane readings observed in the ROLLING_SECONDS up to `at`, not after it, to the
    minute (observed_at is cut to the minute when logged): the live tick may not have later
    ones yet, and a replay should use only what the tick could have."""
    t_at = at.timestamp()
    return [dict(s) for s in signals if _is_reading(s)
            and 0 <= t_at - _parse(s["observed_at"]).timestamp() <= ROLLING_SECONDS]


def _is_reading(sig: Mapping) -> bool:
    return bool(sig.get("observed_at")) and isinstance(sig.get("evidence"), Mapping)


# --- one tick ---------------------------------------------------------------------------------

def tick(runner, queue_log: QueueLog, pair: Mapping,
         name: Mapping | None = None) -> tuple[dict, list[dict], dict | None]:
    """Measure, write the runner's records as `camera transit` does, and combine them into the
    camera record, appended to camera-<UTC date>.jsonl. Returns (measured, records, camera
    record or None). `name` ({"run", "tick"}) is written into each transit record, so a
    replay knows which records one tick wrote (`ticks`).

    Once line B's camera has refused us it is never read again (ADR-0014 decision 5), and its
    count would only be re-measured over the same frozen stretch: then nothing is written and
    no camera wait made. `measure` still runs, since it is what drops per-vehicle data after
    SIGNATURE_TTL_S."""
    measured = runner.measure()
    if pair["b"] in runner.blocked:
        return measured, [], None
    records = runner.records(measured)
    for rec in records:
        rec.update(name or {})
    runner.write(records)
    if not records:
        return measured, records, None
    at = _parse(records[0]["seen_until"])
    camera = combine.combine(records, queue_log.recent(at), pair, at,
                             not_measured=runner.not_measured)
    _append(state_dir() / f"camera-{datetime.now(UTC):%Y-%m-%d}.jsonl", [camera])
    return measured, records, camera


class Live:
    """What the page's camera-estimate section shows: the latest camera record, in memory. The
    page reads it through viewer.estimate_view, which applies combine.display."""

    def __init__(self, pair: Mapping):
        self._lock = threading.Lock()
        self.pair = {k: pair.get(k) for k in ("a", "b", "port_number", "direction",
                                              "show_minutes", "camera_votes")}
        self.record: dict | None = None
        self.stopped: str | None = None
        self.updated_at: str | None = None

    def update(self, record: dict | None, stopped: str | None = None,
               now: datetime | None = None) -> None:
        """The tick's record, or None when it measured nothing or failed, with `stopped` saying
        why there is none: an older record is never left on the page as if it were this
        minute's. A record of a line far behind live is flagged by the page (`updated_at`)."""
        with self._lock:
            self.record, self.stopped = record, stopped
            self.updated_at = _minute((now or datetime.now(UTC)).isoformat())

    def snapshot(self) -> dict:
        with self._lock:
            return json.loads(json.dumps({"pair": self.pair, "record": self.record,
                                          "stopped": self.stopped,
                                          "updated_at": self.updated_at}))


class Alerts:
    """One alert when a line's camera refuses us (never cleared: it is never read again), and
    one when a line falls more than LAG_ALERT_S behind live, and again when it catches up."""

    def __init__(self, notify: Callable[[str], None]):
        self.notify = notify
        self.blocked: set[str] = set()
        self.behind: set[str] = set()

    def check(self, runner, now: datetime) -> list[str]:
        out = []
        for cam in runner.cams:
            line = runner.lines[cam.id]
            if cam.id in runner.blocked:
                if cam.id not in self.blocked:
                    self.blocked.add(cam.id)
                    out.append(f"Border camera estimate: {cam.id} refused us "
                               f"({runner.errors.get(cam.id, 'blocked')}). Line {line} has "
                               "stopped for good and is never retried (ADR-0014 decision 5): "
                               "no camera wait from now on.")
                continue
            last = runner.last_at.get(cam.id)
            if last is None:
                continue
            lag = (now - last).total_seconds()
            if lag > LAG_ALERT_S and cam.id not in self.behind:
                self.behind.add(cam.id)
                out.append(f"Border camera estimate: line {line} ({cam.id}) is {lag:.0f} s "
                           "behind live.")
            elif lag <= LAG_ALERT_S and cam.id in self.behind:
                self.behind.discard(cam.id)
                out.append(f"Border camera estimate: line {line} ({cam.id}) has caught up.")
        for text in out:
            self.notify(text)
        return out


def status_text(runner, records: Sequence[Mapping], camera: Mapping | None,
                queue_log: QueueLog, pair: Mapping, cpu: str = "",
                alerts: Sequence[str] = ()) -> str:
    """The terminal status for one tick, in ASCII (a cp1252 console cannot print "≥").

    The runner's own status is printed without its measurements, so it never prints a
    transit's minutes: only the streams, the counts and rates per method and group, and loop
    learning. The camera wait comes only through combine.status_text, under the display rule.
    `alerts` (this tick's Alerts.check) are printed last: without Telegram set up, the terminal
    is the only place they show."""
    lines = [f"[{datetime.now(UTC):%H:%M:%S} UTC]", runner.status({})]
    for rec in records:
        epoch = (rec.get("epochs") or [{}])[-1]
        sd = epoch.get("offset_sd")
        lines.append(f"  count {rec.get('method')!s:5} {rec.get('lane_type')!s:6}: "
                     f"{epoch.get('source', 'none')}, sd {'-' if sd is None else sd}")
    a_m = pair.get("a_m") or {}
    for (cid, lane), ev in queue_log.latest().items():
        text = (f"  queue {cid} {lane}: {ev.get('state')} {float(ev.get('queue_m') or 0):.0f} m"
                f"{'+' if ev.get('beyond_view') else ''}")
        if cid == pair.get("a") and lane in a_m:
            reaches = ev.get("beyond_view") or float(ev.get("queue_m") or 0) >= a_m[lane]
            text += ", reaches A" if reaches else ", short of A"
        lines.append(text)
    if cpu:
        lines.append(f"  {cpu}")
    if pair["b"] in runner.blocked:
        lines.append(f"  camera wait: none - line B stopped: {pair['b']} refused us "
                     "(ADR-0014 decision 5)")
    elif camera is None:
        lines.append("  camera wait: nothing measured yet")
    else:
        lines.append(combine.status_text(camera, bool(pair.get("show_minutes"))))
    lines += [f"  alert: {text}" for text in alerts]
    return "\n".join(lines).encode("ascii", "replace").decode("ascii")


class Cpu:
    """This process's CPU since the last call, from time.process_time: ffmpeg's child processes
    are not in it."""

    def __init__(self):
        self.at, self.cpu = time.monotonic(), time.process_time()

    def text(self) -> str:
        at, cpu = time.monotonic(), time.process_time()
        share = (cpu - self.cpu) / max(at - self.at, 1e-6)
        self.at, self.cpu = at, cpu
        return f"process CPU {share:.1f} CPU-s/s of {os.cpu_count()} (ffmpeg not included)"


# --- the process ------------------------------------------------------------------------------

class Estimate:
    """The locks, the runner, the Worker and the page's state, for one `camera estimate`.
    Nothing is read until `start`: building one opens no stream."""

    def __init__(self, cameras: Mapping[str, Camera], pair: Mapping, methods: Sequence[str],
                 queue_every: float, notify: Callable[[str], None] | None = None,
                 alert: bool = True):
        from .runner import TransitRunner
        from .worker import Worker, hold_lock

        state_dir().mkdir(parents=True, exist_ok=True)
        self.pair = pair
        self.held = hold_lock(state_dir() / "transit.lock")    # AlreadyRunning: refused
        self.worker = None
        try:
            self.queue_log = QueueLog()
            self.worker = Worker(queue_cameras(cameras, pair), seconds=WINDOW_SECONDS,
                                 every=queue_every, emit=self.queue_log.add, alert=alert,
                                 compare_cbp=True)
            self.runner = TransitRunner(cameras[pair["a"]], cameras[pair["b"]], methods)
        except BaseException:
            self._release()
            raise
        self.live = Live(pair)
        self.alerts = Alerts(notify or self.worker._notify)   # the worker's Telegram path
        self.stop_event = threading.Event()
        # Each tick's name in the transit log: this run's start, cut to the minute, and a count.
        self.run = _minute(datetime.now(UTC).isoformat())
        self.ticks = 0

    def start(self) -> None:
        self.runner.start()
        threading.Thread(target=self.worker.forever, args=(self.stop_event,), daemon=True,
                         name="queue-reader").start()

    def step(self, cpu: str = "", now: datetime | None = None) -> str:
        """One tick: measure, write, combine, show, alert. Returns the status printed.

        A tick that raises (a full disk) leaves the page saying there is no wait this minute,
        not showing the last one; cmd_estimate logs the error and ticks again."""
        now = now or datetime.now(UTC)
        self.ticks += 1
        try:
            _, records, camera = tick(self.runner, self.queue_log, self.pair,
                                      {"run": self.run, "tick": self.ticks})
        except Exception as exc:
            self.live.update(None, f"No camera wait this minute: the tick failed "
                                   f"({type(exc).__name__}); it is tried again next tick.", now)
            raise
        stopped = None
        if self.pair["b"] in self.runner.blocked:
            stopped = (f"Line B stopped: {self.pair['b']} refused us "
                       f"({self.runner.errors.get(self.pair['b'], 'blocked')}). It is never "
                       "retried (ADR-0014 decision 5), so there is no camera wait.")
        self.live.update(camera, stopped, now)
        alerts = self.alerts.check(self.runner, now)
        self.runner.maybe_save_refs()
        return status_text(self.runner, records, camera, self.queue_log, self.pair, cpu, alerts)

    def close(self) -> None:
        self.stop_event.set()
        try:
            self.runner.stop()
        except Exception:
            log.exception("saving the loop roads at exit failed")
        self._release()

    def _release(self) -> None:
        if self.worker is not None:
            self.worker._held.close()
        self.held.close()


def cmd_estimate(args, cameras: Mapping[str, Camera]) -> int:
    from .viewer import serve
    from .worker import AlreadyRunning

    if getattr(args, "replay", None):
        return cmd_replay(args, cameras)
    if getattr(args, "queue", None):
        raise SystemExit("--queue goes with --replay")
    pair = load_pair(cameras)
    methods = [m.strip() for m in args.methods.split(",") if m.strip()]
    try:
        est = Estimate(cameras, pair, methods, args.queue_every)
    except AlreadyRunning as exc:
        print(exc)
        return 1
    cv2.setNumThreads(THREADS)          # no code set it before; detect.py was timed at 4
    server = None
    try:
        est.start()
        server = serve(args.port)
        server.worker, server.estimate = est.worker, est.live
        threading.Thread(target=server.serve_forever, daemon=True, name="viewer").start()
        print(f"Camera estimate, {pair['a']} -> {pair['b']}, methods {', '.join(methods)}. "
              f"Page: http://127.0.0.1:{args.port}  Records: {state_dir()}  (Ctrl+C to stop)",
              flush=True)
        cpu = Cpu()
        while True:
            time.sleep(args.every)
            try:
                print(est.step(cpu.text()), flush=True)
            except Exception:  # a full disk or a locked file must never stop the counting
                log.exception("estimate tick failed; retrying next tick")
    except KeyboardInterrupt:
        pass
    finally:
        if server is not None:
            server.shutdown()
            server.server_close()
        est.close()
    return 0


# --- replay -----------------------------------------------------------------------------------

def replay(records: Iterable[Mapping], signals: Iterable[Mapping], pair: Mapping,
           not_measured: Mapping[tuple[str, str], str] | None = None) -> list[dict]:
    """The camera records made again from the logged transit records (split into the live
    ticks by `ticks`) and queue signals, under the live rule: see the module docstring for
    where they can differ from the records the live ticks wrote. `not_measured` is the live
    runner's (runner.unmeasured), rebuilt from the cameras and the live run's methods."""
    readings = [s for s in signals if _is_reading(s)]
    out = []
    for group in ticks(records):
        at = _parse(group[0]["seen_until"])
        out.append(combine.combine(group, within(readings, at), pair, at,
                                   not_measured=not_measured))
    return out


def ticks(records: Iterable[Mapping]) -> Iterator[list[Mapping]]:
    """The logged transit records, one list per live tick, in order.

    A record `camera estimate` wrote names its tick (`run`, `tick`), and a tick is the records
    naming it. One without a name (`camera transit`'s, or one logged before names were) is
    grouped as before: the records in a row that share a seen_until and name each method and
    group once. That can merge two ticks of a frozen line when the first lacks a group."""
    one: list[Mapping] = []
    keys: set = set()
    name = None
    for rec in records:
        if rec.get("kind") != "transit" or not rec.get("seen_until"):
            continue
        key = (rec.get("method"), rec.get("lane_type"))
        this = (rec.get("run"), rec["tick"]) if rec.get("tick") is not None else None
        if one and (this != name or (this is None and (
                rec["seen_until"] != one[0]["seen_until"] or key in keys))):
            yield one
            one, keys = [], set()
        one.append(rec)
        keys.add(key)
        name = this
    if one:
        yield one


def cmd_replay(args, cameras: Mapping[str, Camera]) -> int:
    from .runner import unmeasured

    source = Path(args.replay)
    records = read_jsonl(source)
    signals = read_jsonl(Path(args.queue)) if getattr(args, "queue", None) else []
    pair = load_pair(cameras)
    # The live run's --methods: a method all of whose groups were skipped leaves no record to
    # infer it from, and the replay would then differ from the live camera record.
    methods = [m.strip() for m in args.methods.split(",") if m.strip()]
    out = replay(records, signals, pair,
                 unmeasured(cameras[pair["a"]], cameras[pair["b"]], methods))
    state_dir().mkdir(parents=True, exist_ok=True)
    path = state_dir() / f"camera-replay-{source.stem}.jsonl"
    path.unlink(missing_ok=True)
    _append(path, out)
    read = sum(1 for r in records if r.get("kind") == "transit")
    print(f"{len(out)} camera records from {read} transit records -> {path}")
    print("self-check, the logged Now against the later by-entry windows:")
    print(json.dumps(combine.self_check(records), indent=2))
    return 0


def read_jsonl(path: Path) -> list[dict]:
    """A JSONL log, UTF-8 with or without a BOM. A line that is not a JSON object (the last
    line of a process killed mid-write) is skipped, and the count said."""
    out, bad = [], 0
    with open(path, encoding="utf-8-sig") as f:
        for line in f:
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except ValueError:
                bad += 1
                continue
            if isinstance(value, dict):
                out.append(value)
            else:
                bad += 1
    if bad:
        log.warning("%s: %d unreadable lines skipped", path, bad)
    return out


# --- helpers ----------------------------------------------------------------------------------

def _append(path: Path, rows: Iterable[Mapping]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.writelines(json.dumps(row, ensure_ascii=False, default=str) + "\n" for row in rows)


def _parse(text: str) -> datetime:
    t = datetime.fromisoformat(text)
    return t if t.tzinfo is not None else t.replace(tzinfo=UTC)


def _minute(text: str) -> str:
    t = _parse(text)
    return (t - timedelta(seconds=t.second, microseconds=t.microsecond)).isoformat()
