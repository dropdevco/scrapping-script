"""The always-on loop behind `camera run` and `camera view`, and the status it keeps.

One CameraReader per camera lives for the whole run, so each camera's in-memory bank of road
samples keeps growing and its references keep refreshing (an earlier `run` built a fresh reader
every cycle, and the refresh never took effect). Cameras are read in parallel: each reading
is a minute of live stream, so five cameras read one after another could never keep a
two-minute cadence.

A camera with no learnt road learns it here instead of failing: its first LEARN_CYCLES
readings only collect samples, then the road is learnt from the emptiest of them and saved.
That is quicker and less certain than `camera reference --rounds 6`; the status says so.

Status is written to CAMERA_STATE_DIR/status.json after every change, for the viewer.
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

from ..cbp import PORTS
from .calibration import Camera, live_region
from .measure import LaneEstimate, image_features, plain, rolling
from .reader import FPS, CameraReader, _state_path, as_signal, state_dir
from .stream import StreamError, frames, resolve

LEARN_CYCLES = 3          # readings of samples before a camera with no learnt road starts estimating
OPEN_SECONDS = 15         # finding and opening a stream, on top of the video it reads
HISTORY_SECONDS = 3600

log = logging.getLogger(__name__)


def _iso(t: datetime) -> str:
    return t.astimezone(UTC).isoformat()


def bridge_name(camera: Camera) -> str:
    return PORTS.get(camera.port_number, {}).get("name", camera.port_number)


def lane_summary(est: LaneEstimate, lane, roll: dict, observed: datetime) -> dict:
    """One lane, as the viewer shows it.

    `minutes` is the rolling wait when the lane is queued and its booth distance is known,
    0 when nothing is queued and the booth is in view, otherwise None. `why_no_minutes`
    says which of those reasons applies, so the page never shows a blank without one."""
    queued = est.state in ("queue", "stalled")
    minutes, why = None, None
    if queued:
        minutes = roll.get("wait_min")
        if minutes is None:
            why = ("booth distance not measured" if lane.booth_distance_m is None
                   else "queue not moving")
    elif est.state == "no_queue":
        minutes = 0.0 if lane.booth_distance_m == 0 else None
        if minutes is None:
            why = "no queue in view; booth out of view"
    else:
        why = "not enough movement to measure"
    return plain({
        "lane": est.lane, "cbp_lane": lane.cbp_lane, "state": est.state,
        "minutes": None if minutes is None else round(minutes, 1),
        "lower_bound": bool(queued and (roll.get("lower_bound") or est.beyond_view)),
        "why_no_minutes": why,
        "queue_m": est.queue_m, "beyond_view": est.beyond_view,
        "speed_m_s": roll.get("speed_m_s") if queued else None,
        "window_speed_m_s": est.speed_m_s, "windows": roll.get("windows", 0),
        "updated_at": _iso(observed),
    })


class Worker:
    def __init__(self, cameras: list[Camera], seconds: float, every: float,
                 emit: Callable[[dict], None] | None = None, alert: bool = True):
        self.cameras, self.seconds, self.every = cameras, seconds, every
        self.emit, self.alert = emit, alert
        self.readers = {c.id: CameraReader(c) for c in cameras}
        self.history: dict[tuple[str, str], list] = {}
        self.learnt: dict[str, int] = {}
        self._lock = threading.Lock()
        self.status = {"started_at": _iso(datetime.now(UTC)), "every_s": every,
                       "reading_s": seconds, "cameras": {c.id: self._initial(c) for c in cameras}}
        self.write()

    def _initial(self, cam: Camera) -> dict:
        return {"id": cam.id, "bridge": bridge_name(cam), "view": cam.title,
                "port_number": cam.port_number, "direction": cam.direction,
                "direction_verified": cam.direction_verified, "phase": "waiting",
                "message": "starting", "lanes": [], "learning": None}

    def write(self) -> None:
        with self._lock:
            state_dir().mkdir(parents=True, exist_ok=True)
            path = state_dir() / "status.json"
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(plain(self.status), ensure_ascii=False), encoding="utf-8")
            tmp.replace(path)

    def _set(self, cam: Camera, **fields) -> None:
        with self._lock:
            self.status["cameras"][cam.id].update(fields)
        self.write()

    def _has_road(self, cam: Camera) -> bool:
        return all(_state_path(cam, f"{lane.lane}.reference").exists() for lane in cam.lanes)

    def _learn(self, cam: Camera, reader: CameraReader, started: datetime) -> None:
        done = self.learnt.get(cam.id, 0)
        ready = started + timedelta(seconds=(LEARN_CYCLES - done - 1) * self.every
                                    + self.seconds + OPEN_SECONDS)
        self._set(cam, phase="learning",
                  message=f"learning the empty road ({done + 1} of {LEARN_CYCLES})",
                  eta_at=_iso(started + timedelta(seconds=self.seconds + OPEN_SECONDS)),
                  learning={"done": done, "needed": LEARN_CYCLES, "ready_at": _iso(ready)})
        url = resolve(cam.channel, cam.title)
        for i, frame in enumerate(frames(url, FPS, self.seconds, live_region(cam))):
            if i % 8 == 0:
                reader.bank.append(reader._sample(image_features(frame)))
        self.learnt[cam.id] = done + 1
        if self.learnt[cam.id] >= LEARN_CYCLES:
            reader._rebuild()
            reader.save()
            self._set(cam, phase="ok", message="road learnt; first estimate next cycle",
                      learning={"done": LEARN_CYCLES, "needed": LEARN_CYCLES,
                                "ready_at": _iso(datetime.now(UTC))})
        else:
            self._set(cam, phase="learning",
                      message=f"learning the empty road ({done + 1} of {LEARN_CYCLES} done)",
                      learning={"done": done + 1, "needed": LEARN_CYCLES, "ready_at": _iso(ready)})

    def _read(self, cam: Camera, reader: CameraReader, started: datetime) -> None:
        self._set(cam, phase="reading", message="calculating",
                  eta_at=_iso(started + timedelta(seconds=self.seconds + OPEN_SECONDS)))
        url = resolve(cam.channel, cam.title)
        estimates = reader.read(frames(url, FPS, self.seconds, live_region(cam)), save=True)
        observed = datetime.now(UTC)
        lanes = {lane.lane: lane for lane in cam.lanes}
        summaries = []
        for est in estimates:
            lane = lanes[est.lane]
            past = self.history.setdefault((cam.id, est.lane), [])
            past.append((time.monotonic(), est))
            past[:] = [(t, e) for t, e in past if time.monotonic() - t <= HISTORY_SECONDS]
            roll = rolling(past, time.monotonic(), lane.booth_distance_m)
            summaries.append(lane_summary(est, lane, roll, observed))
            if self.emit:
                sig = as_signal(est, lane, observed, cam.direction_verified)
                sig["evidence"]["rolling"] = roll
                if est.state in ("queue", "stalled"):
                    sig["minutes"] = None if roll["wait_min"] is None else round(roll["wait_min"])
                    sig["low"] = sig["minutes"] if roll.get("lower_bound") else sig["low"]
                    sig["high"] = None if roll.get("lower_bound") else sig["high"]
                self.emit(plain(sig))
        self._set(cam, phase="ok", message="", lanes=summaries, updated_at=_iso(observed))

    def _one(self, cam: Camera, started: datetime) -> None:
        reader = self.readers[cam.id]
        was_down = self.status["cameras"][cam.id]["phase"] == "down"
        try:
            if not reader.references and not self._has_road(cam):
                self._learn(cam, reader, started)
            else:
                try:
                    self._read(cam, reader, started)
                except ValueError:          # learnt for an older calibration: learn it again
                    for lane in cam.lanes:
                        _state_path(cam, f"{lane.lane}.reference").unlink(missing_ok=True)
                    reader.references.clear()
                    self._learn(cam, reader, started)
            if was_down:
                self._notify(f"Border camera {cam.id} is back.")
        except StreamError as exc:
            self._set(cam, phase="down", message=f"{type(exc).__name__}: {exc}")
            if self.emit:
                self.emit({"camera": cam.id, "error": f"{type(exc).__name__}: {exc}"})
            if not was_down:
                self._notify(f"Border camera {cam.id} stopped: {type(exc).__name__}: {exc}")
        finally:
            self._set(cam, next_at=_iso(started + timedelta(seconds=self.every)))

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

    def cycle(self) -> None:
        started = datetime.now(UTC)
        with ThreadPoolExecutor(max_workers=len(self.cameras)) as pool:
            list(pool.map(lambda cam: self._one(cam, started), self.cameras))
        with self._lock:
            self.status["cycle_at"] = _iso(started)
        self.write()
        heartbeat = state_dir() / "heartbeat.json"
        heartbeat.write_text(json.dumps({"at": _iso(datetime.now(UTC)), "cameras": {
            cid: c["phase"] for cid, c in self.status["cameras"].items()}}))

    def forever(self, stop: threading.Event | None = None) -> None:
        while not (stop and stop.is_set()):
            began = time.monotonic()
            self.cycle()
            wait = max(0.0, self.every - (time.monotonic() - began))
            if stop:
                stop.wait(wait)
            else:
                time.sleep(wait)
