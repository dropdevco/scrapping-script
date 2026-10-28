"""The always-on loop behind `camera run` and `camera view`, and the status it keeps.

One CameraReader per camera lives for the whole run, so each camera's in-memory bank of road
samples keeps growing and its references keep refreshing (an earlier `run` built a fresh reader
every cycle, and the refresh never took effect). Cameras are read in parallel: each reading
is a minute of live stream, so five cameras read one after another could never keep a
two-minute cadence.

A camera with lanes that have no learnt road learns them here instead of failing: its next
LEARN_CYCLES readings only collect samples, then those lanes are learnt from the emptiest of
them and saved. Lanes already learnt keep their references. That is quicker and less certain
than `camera reference --rounds 6`; the status says so.

One camera failing, for any reason, never stops the others or the worker: a stream fault is
"down", anything unexpected is "error", and both are shown on the page and retried next cycle.

With `compare_cbp` (the viewer), CBP's own published waits are fetched alongside each cycle
and kept in the status purely for display: they are never an input to the camera estimate.

Status is written to CAMERA_STATE_DIR/status.json after every change, for the viewer.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

import cv2

from ..cbp import PORTS
from .calibration import Camera
from .estimate import PAIRS_FILE
from .measure import (
    MIN_SPEED_M_S,
    LaneEstimate,
    image_features,
    plain,
    rolling,
    wait_bounds,
)
from .reader import CameraReader, as_signal, debug_image, live_frames, state_dir
from .stream import StreamError, find

LEARN_CYCLES = 3          # readings of samples before a camera with no learnt road starts estimating
OPEN_SECONDS = 15         # finding and opening a stream, on top of the video it reads
HISTORY_SECONDS = 3600
CBP_TIMEOUT = 10          # display-only and retried next cycle: one short attempt, never a stall

log = logging.getLogger(__name__)


def _iso(t: datetime) -> str:
    return t.astimezone(UTC).isoformat()


def bridge_name(camera: Camera) -> str:
    return PORTS.get(camera.port_number, {}).get("name", camera.port_number)


def camera_wait_cameras(path: Path = PAIRS_FILE) -> frozenset[str]:
    """The cameras whose road on to the booth a pair's camera wait times (ADR-0015): each
    pair's camera A and the street before it (pairs.json `a` and `street`)."""
    with open(path, encoding="utf-8-sig") as f:
        pairs = json.load(f)
    return frozenset(pair[role] for key, pair in pairs.items() if not key.startswith("_")
                     for role in ("a", "street") if pair.get(role))


def _approx_reasons(lane, scale_verified: bool) -> list[str]:
    """Why a lane's minutes are approximate. Only a booth out of view can make them so: with
    the booth in view the distance is the visible line and the scale cancels. Out of view
    they always are, however well the distance is known: the road to the booth is timed at
    the speed of the line in view."""
    if not lane.booth_distance_m:
        return []
    reasons = []
    if not lane.booth_distance_verified:
        reasons.append("booth distance is a map estimate")
    if not scale_verified:
        reasons.append("camera scale unverified")
    # ASSUMPTION: uniform speed all the way to the booth. ADR-0014 names "the queue out of
    # view behaves differently" as a suspected cause of PDN north reading 99-128 against 47.
    reasons.append("road out of view assumed to move like the line in view")
    return reasons


def _display(kind: str | None, minutes, low, high, approx: bool) -> tuple[str, str]:
    """The number as the page prints it, and its range text."""
    if kind == "clear":
        return "no line", ""
    if minutes is None:
        return "—", ""
    if kind == "zero":
        return "0", ""
    if kind == "floor":
        return f"≥ {round(low if low is not None else minutes)}", "at least: the line runs out of view"
    value = f"{'≈ ' if approx else ''}{round(minutes)}"
    if low is not None and high is not None and round(low) != round(high):
        return value, f"range {round(low)}–{round(high)} min"
    return value, ""


def lane_summary(est: LaneEstimate, lane, roll: dict, observed: datetime, *,
                 scale_verified: bool = True, camera_wait: bool = False) -> dict:
    """One lane, as the viewer shows it: a number of minutes whenever one can be stood behind,
    labelled for what it is, and a reason when there is none. `camera_wait`: this camera's
    road on to the booth is timed by a pair's camera wait (camera_wait_cameras).

    - queued: the 15-minute rolling wait and its range ("≈" whenever the booth is out of view:
      the road to it is timed at the visible speed, and its distance or the camera's scale may
      be unverified too). A line running out of view has no high end; it is a floor, stated at
      its range's low end ("≥"), only when the booth is in view, and otherwise an estimate that
      says the line past the view is not counted;
    - a minute with a line but too little movement to time it: this minute's line, timed at
      the last 15 minutes' speed. Never an older line's wait: a short line at the booth is
      always too short to time, and showing the wait of a long line from ten minutes ago
      would be wrong exactly when the line has cleared;
    - a line on a camera of the camera wait, booth out of view: the line's length and speed,
      and no minutes, because the wait is the camera wait's. The (D + L) / v above is the form
      ADR-0015 rejects as a wait, and the camera wait times this road. Cameras with no camera
      wait (Lerdo, BOTA) keep it, approximate, as nothing else times their road;
    - no line, booth in view: 0;
    - no line, booth out of view: "no line", and no minutes, saying whether the camera wait
      times the road on to the booth. There is nothing in view to time, and timing that road
      at a queue speed is (D + L) / v with L = 0. That ceiling ("under ~N", at the slowest
      lane's recent speed) read 18-41 min on empty lanes on 2026-10-27, and was dropped;
    - no line and no booth distance: "no line", and the reason why;
    - nothing to measure: no number, and the reason why."""
    queued = est.state in ("queue", "stalled")
    line = queued or (est.state == "insufficient" and est.queue_m > 0)
    distance = lane.booth_distance_m
    uncertainty = lane.booth_distance_uncertainty_m
    reasons = _approx_reasons(lane, scale_verified)
    speed = roll.get("speed_m_s")
    timed = speed is not None and speed >= MIN_SPEED_M_S
    n = roll.get("windows", 0)
    still = f"not moving over the last {n} reading{'' if n == 1 else 's'}"
    unseen = None if not distance else (
        f"the {round(distance)} m to the booth are out of view"
        + (": the camera wait times them" if camera_wait else "; nothing here times them"))
    minutes = low = high = None
    kind, why, note = None, None, None
    if line and camera_wait and distance:
        kind, note = "line", unseen
    elif queued:
        if distance is None:
            why = "booth distance not measured"
        elif not timed:
            why = f"line {still}"
        else:
            minutes, low, high = roll.get("wait_min"), roll.get("low_min"), roll.get("high_min")
            floor = distance == 0 and (roll.get("lower_bound") or est.beyond_view)
            kind = "floor" if floor else "estimate"
    elif est.state == "insufficient":
        if est.queue_m > 0 and distance is not None and timed:
            minutes = (distance + est.queue_m) / speed / 60
            low, high = wait_bounds(distance, uncertainty, est.queue_m, speed, speed, scale_verified)
            low, high = min(low, minutes), max(high, minutes)
            kind = "floor" if est.beyond_view and distance == 0 else "estimate"
            note = "this minute's line, timed at the last 15 minutes' speed"
        elif est.queue_m > 0 and distance is None:
            why = "booth distance not measured"
        elif est.queue_m > 0:
            # Speed is timed only on 12 m segments wholly inside the line, so a line shorter
            # than 12-18 m is never timed, however it moves.
            why = "a line, but too short or too still to time, and no recent speed on this lane"
        else:
            why = "too few frames this minute to read the lane"
    elif est.state == "no_queue":
        if distance == 0:
            minutes, low, high, kind = 0.0, 0.0, 0.0, "zero"
        elif distance is None:
            kind, why = "clear", "no line in view; booth distance not measured"
        else:
            kind, note = "clear", unseen
    else:
        why = "not enough movement to measure"
    if est.beyond_view:
        high = None                     # the line runs on past the view: no high end
        if kind == "estimate":
            # The minutes stop at the edge of the view. A floor says so in its "at least"; an
            # estimate has to name it, or "≈ 47" reads as the whole line's wait.
            reasons.append("the line past the view not counted")
    approx = bool(reasons) and kind in ("estimate", "floor")
    length = f"line {round(est.queue_m)} m{'+' if est.beyond_view else ''}"
    if kind == "line":
        value, range_text = length, ""
    else:
        value, range_text = _display(kind, minutes, low, high, approx)
    bits = []
    if range_text:
        bits.append(range_text)
    if line:
        if kind != "line":
            bits.append(length)
        if queued and timed:
            bits.append(f"moving {speed:.2f} m/s")
        elif queued and kind == "line":
            bits.append(still)
        if est.state == "stalled":
            bits.append("not moving this minute")
    elif est.state == "no_queue" and why is None:
        bits.append("no line in view")
    if note:
        bits.append(note)
    if approx:
        bits.append("approximate: " + " and ".join(reasons))
    if why:
        bits.append(why)
    def r(v):
        return None if v is None else round(v, 1)
    return plain({
        "lane": est.lane, "cbp_lane": lane.cbp_lane, "state": est.state, "kind": kind,
        "minutes": r(minutes), "low_min": r(low), "high_min": r(high),
        "approx": approx, "approx_reasons": reasons if approx else [],
        "lower_bound": kind == "floor",
        "why_no_minutes": why, "note": note,
        "display_value": value, "display_detail": " · ".join(bits),
        "queue_m": est.queue_m, "beyond_view": est.beyond_view,
        "speed_m_s": speed if queued else None,
        "window_speed_m_s": est.speed_m_s, "windows": roll.get("windows", 0),
        "updated_at": _iso(observed),
    })


def signal_from_roll(sig: dict, est: LaneEstimate, roll: dict) -> dict:
    """Publish the 15-minute wait with ITS range, not the single minute's: a central value
    paired with another estimate's low and high can fall outside its own range."""
    if est.state in ("queue", "stalled") and roll.get("wait_min") is not None:
        sig["minutes"] = round(roll["wait_min"])
        sig["low"] = None if roll.get("low_min") is None else round(roll["low_min"])
        sig["high"] = None if roll.get("high_min") is None else round(roll["high_min"])
    elif est.state in ("queue", "stalled"):
        sig["minutes"] = sig["low"] = sig["high"] = None
    return sig


class AlreadyRunning(RuntimeError):
    pass


def hold_lock(path):
    """Hold an exclusive lock on `path` for as long as the returned file stays open.

    Two workers on one CAMERA_STATE_DIR overwrite each other's status and learn and save the
    same cameras' references at once; on 2026-10-19 a test viewer and a real one did exactly
    that and the page showed both sets of cameras, duplicated. The OS releases the lock if the
    process dies, so a crash never leaves a stale one behind."""
    handle = open(path, "a+")  # noqa: SIM115 - held open for the life of the worker on purpose
    try:
        handle.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        raise AlreadyRunning(f"another camera worker is already running with {path.parent}; "
                             "stop it first, or give this one its own CAMERA_STATE_DIR") from None
    return handle


class Worker:
    def __init__(self, cameras: list[Camera], seconds: float, every: float,
                 emit: Callable[[dict], None] | None = None, alert: bool = True,
                 compare_cbp: bool = False):
        state_dir().mkdir(parents=True, exist_ok=True)
        self._held = hold_lock(state_dir() / "worker.lock")
        self.cameras, self.seconds, self.every = cameras, seconds, every
        self.emit, self.alert, self.compare_cbp = emit, alert, compare_cbp
        self.readers = {c.id: CameraReader(c) for c in cameras}
        self.camera_wait = camera_wait_cameras()
        self.history: dict[tuple[str, str], list] = {}
        self.learnt: dict[str, int] = {}
        # The latest "what it measured" picture per camera, JPEG bytes, in memory only: the
        # worker and viewer never write a frame to disk (QUESTIONS.md Q-7), this one included.
        self.overlays: dict[str, bytes] = {}
        self._lock = threading.Lock()
        self._write_failed = self._emit_failed = False
        self.status = {"started_at": _iso(datetime.now(UTC)), "every_s": every,
                       "reading_s": seconds, "cameras": {c.id: self._initial(c) for c in cameras}}
        self.write()

    def _initial(self, cam: Camera) -> dict:
        return {"id": cam.id, "bridge": bridge_name(cam), "view": cam.title,
                "port_number": cam.port_number, "direction": cam.direction,
                "direction_verified": cam.direction_verified, "phase": "waiting",
                "message": "starting", "lanes": [], "learning": None}

    def write(self) -> None:
        """Write status.json for anything outside this process. The viewer in this process reads
        self.status directly, so a failed write (a full disk) is logged, once, and readings go on."""
        with self._lock:
            try:
                state_dir().mkdir(parents=True, exist_ok=True)
                path = state_dir() / "status.json"
                tmp = path.with_suffix(".tmp")
                tmp.write_text(json.dumps(plain(self.status), ensure_ascii=False), encoding="utf-8")
                tmp.replace(path)
            except OSError as exc:
                if not self._write_failed:
                    log.error("camera status not written (%s); readings continue", exc)
                self._write_failed = True
            else:
                self._write_failed = False

    def _emit(self, payload: dict) -> None:
        """Hand one signal to `emit`. Output that has gone away (a closed pipe) is logged once
        and never stops the cameras."""
        if not self.emit:
            return
        try:
            self.emit(payload)
        except Exception as exc:  # noqa: BLE001 - whatever the consumer raises, readings go on
            if not self._emit_failed:
                log.error("camera signal not delivered (%s: %s)", type(exc).__name__, exc)
            self._emit_failed = True
        else:
            self._emit_failed = False

    def _set(self, cam: Camera, **fields) -> None:
        with self._lock:
            self.status["cameras"][cam.id].update(fields)
        self.write()

    def _learn(self, cam: Camera, reader: CameraReader, started: datetime,
               missing: list[str]) -> None:
        done = self.learnt.get(cam.id, 0)
        ready = started + timedelta(seconds=(LEARN_CYCLES - done - 1) * self.every
                                    + self.seconds + OPEN_SECONDS)
        what = "the empty road" if len(missing) == len(cam.lanes) else f"lanes {', '.join(missing)}"
        self._set(cam, phase="learning",
                  message=f"learning {what} ({done + 1} of {LEARN_CYCLES})",
                  eta_at=_iso(started + timedelta(seconds=self.seconds + OPEN_SECONDS)),
                  learning={"done": done, "needed": LEARN_CYCLES, "ready_at": _iso(ready)})
        for i, stamped in enumerate(live_frames(cam, self.seconds)):
            if i % 8 == 0:
                reader.bank.append(reader._sample(image_features(stamped.image)))
        self.learnt[cam.id] = done + 1
        if self.learnt[cam.id] >= LEARN_CYCLES:
            # Only the missing lanes: a few minutes of samples must not replace a good lane's
            # road (a car standing at the booth throughout would be learnt into it).
            reader._rebuild(missing)
            reader.save(missing)
            self.learnt[cam.id] = 0
            self._set(cam, phase="ok", message="road learnt; first estimate next cycle",
                      learning={"done": LEARN_CYCLES, "needed": LEARN_CYCLES,
                                "ready_at": _iso(datetime.now(UTC))})
        else:
            self._set(cam, phase="learning",
                      message=f"learning {what} ({done + 1} of {LEARN_CYCLES} done)",
                      learning={"done": done + 1, "needed": LEARN_CYCLES, "ready_at": _iso(ready)})

    def _read(self, cam: Camera, reader: CameraReader, started: datetime) -> None:
        self._set(cam, phase="reading", message="calculating",
                  eta_at=_iso(started + timedelta(seconds=self.seconds + OPEN_SECONDS)))
        estimates = reader.read(live_frames(cam, self.seconds), save=True)
        # The road's time, from the stream's own clock (ADR-0014), not when the read finished.
        observed = reader.last_at or datetime.now(UTC)
        if reader.last_frame is not None and cam.display:   # never a picture of a hidden feed
            ok, jpeg = cv2.imencode(".jpg", debug_image(reader, estimates),
                                    [cv2.IMWRITE_JPEG_QUALITY, 80])
            if ok:
                self.overlays[cam.id] = jpeg.tobytes()
        self._summarise(cam, estimates, observed)

    def _summarise(self, cam: Camera, estimates: list[LaneEstimate], observed: datetime) -> None:
        lanes = {lane.lane: lane for lane in cam.lanes}
        rolls = {}
        now = time.monotonic()
        for est in estimates:
            lane = lanes[est.lane]
            past = self.history.setdefault((cam.id, est.lane), [])
            past.append((now, est))
            past[:] = [(t, e) for t, e in past if now - t <= HISTORY_SECONDS]
            rolls[est.lane] = rolling(past, now, lane.booth_distance_m,
                                      lane.booth_distance_uncertainty_m, cam.scale_verified)
        summaries = []
        for est in estimates:
            lane, roll = lanes[est.lane], rolls[est.lane]
            summaries.append(lane_summary(est, lane, roll, observed,
                                          scale_verified=cam.scale_verified,
                                          camera_wait=cam.id in self.camera_wait))
            if self.emit:
                sig = as_signal(est, lane, observed, cam.direction_verified)
                sig["evidence"]["rolling"] = roll
                self._emit(plain(signal_from_roll(sig, est, roll)))
        self._set(cam, phase="ok", message="", lanes=summaries, updated_at=_iso(observed))

    def _one(self, cam: Camera, started: datetime) -> None:
        reader = self.readers[cam.id]
        was_failing = self.status["cameras"][cam.id]["phase"] in ("down", "error")
        try:
            if cam.source == "youtube":
                self._set(cam, video_id=find(cam.channel, cam.title).video_id)
            missing = reader.prepare()
            if missing:
                self._learn(cam, reader, started, missing)
            else:
                self._read(cam, reader, started)
            if was_failing:
                self._notify(f"Border camera {cam.id} is back.")
        except StreamError as exc:
            self._fail(cam, "down", exc, was_failing)
        except Exception as exc:  # one camera must never stop the others
            log.exception("camera %s failed", cam.id)
            self._fail(cam, "error", exc, was_failing)
        finally:
            self._set(cam, next_at=_iso(started + timedelta(seconds=self.every)))

    def _fail(self, cam: Camera, phase: str, exc: Exception, was_failing: bool) -> None:
        message = f"{type(exc).__name__}: {exc}"
        self._set(cam, phase=phase, message=message)
        self._emit({"camera": cam.id, "error": message})
        if not was_failing:
            self._notify(f"Border camera {cam.id} stopped: {message}")

    def _notify(self, text: str) -> None:
        if not self.alert:
            return
        from ...core.http import HttpClient
        from ...social.notify import notify_alert

        async def send():
            async with HttpClient() as http:
                await notify_alert(http, text)
        try:
            asyncio.run(send())
        except Exception as exc:  # noqa: BLE001 - an alert that fails must never stop readings
            log.error("camera alert failed: %s", exc)

    def _cbp(self) -> None:
        """CBP's published waits for the ports these cameras watch, shown beside the camera
        estimate as a sanity check. Never an input to it: the camera number has to stand on
        its own, or comparing the two would prove nothing. Runs alongside the cameras, with
        one short attempt: it is display-only and is fetched again next cycle anyway."""
        from ...core.http import HttpClient
        from .. import cbp

        async def get():
            async with HttpClient(timeout=CBP_TIMEOUT) as http:
                payload = await http.get_json(cbp.FEED_URL, retries=0)
            if not isinstance(payload, list):
                raise TypeError(f"CBP feed returned {type(payload).__name__}, expected a list")
            return cbp.build(payload)
        try:
            snapshot = asyncio.run(get())
        except Exception as exc:  # noqa: BLE001 - CBP being down must not stop the cameras
            with self._lock:
                self.status["cbp_error"] = f"{type(exc).__name__}: {exc}"
            return
        wanted = {c.port_number for c in self.cameras}
        ports = {}
        for port in snapshot.ports:
            if port.port_number not in wanted:
                continue
            ports[port.port_number] = {
                lane_id: {"state": lane.state, "minutes": lane.delay_minutes,
                          "lanes_open": lane.lanes_open, "label": lane.cbp_update_label}
                for lane_id, lane in port.lanes.items() if lane_id.startswith("car")}
        with self._lock:
            self.status["cbp"] = ports
            self.status["cbp_at"] = _iso(datetime.now(UTC))
            self.status.pop("cbp_error", None)

    def cycle(self) -> None:
        started = datetime.now(UTC)
        jobs = [lambda cam=cam: self._one(cam, started) for cam in self.cameras]
        if self.compare_cbp:
            jobs.append(self._cbp)
        with ThreadPoolExecutor(max_workers=len(jobs)) as pool:
            list(pool.map(lambda job: job(), jobs))
        with self._lock:
            self.status["cycle_at"] = _iso(started)
        self.write()
        heartbeat = state_dir() / "heartbeat.json"
        heartbeat.write_text(json.dumps({"at": _iso(datetime.now(UTC)), "cameras": {
            cid: c["phase"] for cid, c in self.status["cameras"].items()}}))

    def forever(self, stop: threading.Event | None = None) -> None:
        while not (stop and stop.is_set()):
            began = time.monotonic()
            try:
                self.cycle()
            except Exception:  # e.g. a full disk writing status: keep going
                log.exception("camera worker cycle failed; retrying next cycle")
            wait = max(0.0, self.every - (time.monotonic() - began))
            if stop:
                stop.wait(wait)
            else:
                time.sleep(wait)
