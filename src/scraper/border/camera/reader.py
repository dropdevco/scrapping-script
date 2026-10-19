"""Read queues off the bridge cameras.

    python -m scraper.border camera list                       # calibrated cameras, stream status
    python -m scraper.border camera snapshot --camera pdn-north --out pdn.jpg
                                                               # one frame with the lanes drawn on
    python -m scraper.border camera reference --camera pdn-north [--seconds 60]
                                                               # learn the empty road; run when clear
    python -m scraper.border camera read --camera pdn-north [--seconds 60] [--json]
    python -m scraper.border camera run --cameras pdn-north,lerdo-fila [--every 300]
                                                               # the always-on worker

`read` and `reference` take --source FILE to work from a recorded clip instead of the live
stream, which is how a calibration is tuned and how a reading is reproduced.

Runs on a machine we own (QUESTIONS.md, Q-3), not GitHub Actions: it has to watch video
continuously. State (each lane's empty-road reference, the worker's heartbeat) lives in
CAMERA_STATE_DIR, by default ~/.chisme/camera.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from collections import deque
from datetime import UTC, datetime
from pathlib import Path

import cv2
import numpy as np

from .calibration import PX_PER_M, Camera, Strip, live_region, load_cameras
from .measure import (
    BIN_M,
    Image,
    LaneEstimate,
    LaneWindow,
    Reference,
    foreground,
    image_features,
    rolling,
    strip_features,
)
from .stream import StreamError, find, frames, resolve

FPS = 2.0                 # two frames a second: a queued car moves well under a metre between them
WINDOW_SECONDS = 60
REFERENCE_SECONDS = 20


def state_dir() -> Path:
    return Path(os.environ.get("CAMERA_STATE_DIR") or Path.home() / ".chisme" / "camera")


EMPTY_MAX = 0.12          # share of a lane that may differ for a sample to count as "empty"
EMPTY_MIN_SAMPLES = 4     # fewer empty samples than this and the old reference is kept


def emptiest_reference(samples: list[tuple[np.ndarray, np.ndarray]], valid: np.ndarray,
                       current: Reference | None) -> Reference | None:
    """The road, learnt only from the moments the lane was (nearly) empty.

    A plain median over moments learns cars once the lane is queued more than half the time,
    and Paso del Norte northbound was queued from 03:16 local on: a reference learnt from
    three queued rounds out of five showed car shapes, and the empty street read as a queue.
    So: start from the median (or the reference already held), score every sample by how much
    of the lane differs from it, keep the near-empty ones and take their median, three times.
    When no sample is near-empty — a lane queued all hour — the reference already held is
    kept rather than replaced by a picture of the queue. Returns None to mean "keep it"."""
    if not samples:
        return None
    ref = current or Reference.from_strips(samples)
    for _ in range(3):
        scores = [float(foreground(g, e, ref, valid)[valid].mean()) for g, e in samples]
        empty = [s for s, score in zip(samples, scores) if score <= EMPTY_MAX]
        if len(empty) < EMPTY_MIN_SAMPLES:
            if current is not None:
                return None
            ranked = sorted(zip(scores, range(len(samples))))
            empty = [samples[i] for _, i in ranked[:EMPTY_MIN_SAMPLES]]
        ref = Reference.from_strips(empty)
    return ref


def _state_path(camera: Camera, name: str) -> Path:
    return state_dir() / f"{camera.id}.{name}.npz"


BANK_SIZE = 36            # in-memory lane samples for self-refreshing references: ~1 h at 3 a window
BANK_MIN = 18             # rebuild once the bank spans ~30 minutes of separate moments
SAMPLES_PER_WINDOW = 3


class CameraReader:
    """One camera: its lane strips and their empty-road references.

    References are learnt from moments spread across time, never from one clip (see
    Reference.from_strips), and the worker keeps refreshing them from a small in-memory bank
    of lane samples. Only the medians are written to CAMERA_STATE_DIR; no frame is."""

    def __init__(self, camera: Camera):
        self.camera = camera
        self._strips: dict[str, Strip] = {}
        self.references: dict[str, Reference] = {}
        self.bank: deque = deque(maxlen=BANK_SIZE)
        self.last_frame: np.ndarray | None = None
        self.last_windows: dict[str, LaneWindow] = {}

    def strips(self, size: tuple[int, int]) -> dict[str, Strip]:
        if not self._strips or next(iter(self._strips.values())).frame_size != size:
            self._strips = {lane.lane: Strip(self.camera, lane, size) for lane in self.camera.lanes}
        return self._strips

    def _sample(self, image: Image) -> tuple:
        size = (image.grey.shape[1], image.grey.shape[0])
        lanes = {lane: tuple(x.astype(np.float16) for x in strip_features(strip, image))
                 for lane, strip in self.strips(size).items()}
        return size, lanes

    def _rebuild(self) -> None:
        size = self.bank[-1][0]
        samples = [lanes for s, lanes in self.bank if s == size]
        for lane, strip in self.strips(size).items():
            ref = emptiest_reference([(g.astype(np.float32), e.astype(np.float32))
                                      for g, e in (s[lane] for s in samples)],
                                     strip.valid, self.references.get(lane))
            if ref is not None:
                self.references[lane] = ref

    def learn(self, images: list[Image]) -> None:
        """Learn the scene and every lane's empty road from images taken at separate moments."""
        self.bank.extend(self._sample(image) for image in images)
        self._rebuild()

    def save(self) -> None:
        state_dir().mkdir(parents=True, exist_ok=True)
        stamp = np.array(datetime.now(UTC).isoformat())
        for lane, ref in self.references.items():
            np.savez_compressed(_state_path(self.camera, f"{lane}.reference"), grey=ref.image,
                                edges=ref.edges, size=np.array(self._strips[lane].size),
                                saved_at=stamp)

    def load(self, size: tuple[int, int]) -> None:
        for lane, strip in self.strips(size).items():
            path = _state_path(self.camera, f"{lane}.reference")
            if not path.exists():
                raise FileNotFoundError(
                    f"no learnt road for {self.camera.id}/{lane}: run `camera reference --camera "
                    f"{self.camera.id} --rounds 6` (or --source with several clips)")
            ref = np.load(path)
            if tuple(ref["size"]) != strip.size:
                raise ValueError(f"{self.camera.id}/{lane} was learnt for a different "
                                 "calibration; relearn it")
            self.references[lane] = Reference(ref["grey"], ref["edges"])

    def read(self, source_frames, save: bool = True) -> list[LaneEstimate]:
        windows: dict[str, LaneWindow] = {}
        raw_images: list[Image] = []
        for frame in source_frames:
            raw = image_features(frame)            # once per frame, shared by every lane
            if not windows:
                size = (frame.shape[1], frame.shape[0])
                if not self.references:
                    self.load(size)
                windows = {lane: LaneWindow(strip, Reference(self.references[lane].image.copy(),
                                                             self.references[lane].edges.copy()),
                                            1 / FPS)
                           for lane, strip in self.strips(size).items()}
            for window in windows.values():
                window.add(raw)
            raw_images.append(raw)
            self.last_frame = frame
        self.last_windows = windows
        if raw_images:
            step = max(1, len(raw_images) // SAMPLES_PER_WINDOW)
            self.bank.extend(self._sample(im) for im in raw_images[::step][:SAMPLES_PER_WINDOW])
            if len(self.bank) >= BANK_MIN:
                self._rebuild()
                if save:
                    self.save()
        return [w.result() for w in windows.values()]


def debug_image(reader: CameraReader, estimates: list[LaneEstimate]) -> np.ndarray:
    """The last frame with every metre of each lane shaded by how much of the window it was
    occupied (clear = no tint, red = always), the queue's end marked, and the numbers."""
    shown = reader.last_frame.copy()
    tint = shown.copy()
    for est in estimates:
        window = reader.last_windows[est.lane]
        strip = window.strip
        inverse = np.linalg.inv(strip.matrix)
        width = strip.size[0]
        share = est.detail.get("occupied_share", [])
        step = round(BIN_M * PX_PER_M)
        for i, value in enumerate(share):
            a, b = i * step, min((i + 1) * step, strip.size[1])
            if not strip.rows_valid[a:b].any():
                continue
            quad = cv2.perspectiveTransform(np.float32([[(0, a), (width, a), (width, b), (0, b)]]),
                                            inverse)[0].astype(np.int32)
            colour = (0, int(255 * (1 - value)), int(255 * value))
            cv2.fillPoly(tint, [quad], colour)
        if est.queue_m:
            r = min(round(est.queue_m * PX_PER_M), strip.size[1] - 1)
            end = cv2.perspectiveTransform(np.float32([[(0, r), (width, r)]]), inverse)[0]
            cv2.line(shown, tuple(int(v) for v in end[0]), tuple(int(v) for v in end[1]),
                     (255, 255, 0), 3)
    cv2.addWeighted(tint, 0.35, shown, 0.65, 0, shown)
    y = shown.shape[0] - 12 * (len(estimates) + 1)
    for est in estimates:
        speed = "-" if est.speed_m_s is None else f"{est.speed_m_s:.2f} m/s"
        text = f"{est.lane}: {est.state} queue {est.queue_m:.0f} m speed {speed}"
        cv2.putText(shown, text, (10, y), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (0, 0, 0), 3)
        cv2.putText(shown, text, (10, y), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (255, 255, 255), 1)
        y += 14
    return shown


def as_signal(est: LaneEstimate, lane, observed_at: datetime) -> dict:
    """The estimate in the shape of SPEC.md §3's Signal, kind `measured`. A strip whose CBP
    lane type nobody has confirmed carries `lane: null`, so fusion cannot file it under the
    wrong lane; the strip's own name travels in `evidence`."""
    minutes = None if est.wait_min is None else round(est.wait_min)
    return {
        "port_number": est.port_number, "lane": lane.cbp_lane, "direction": est.direction,
        "kind": "measured", "source": f"cam:{est.camera}",
        "state": "open" if est.state in ("queue", "no_queue", "stalled") else "no_data",
        "minutes": minutes,
        "low": None if est.low_min is None else round(est.low_min),
        "high": None if est.high_min is None else round(est.high_min),
        "observed_at": observed_at.isoformat(), "independent": True,
        "evidence": est.to_dict(),
    }


def _source(camera: Camera, source: str | None) -> str:
    return source or resolve(camera.channel, camera.title)


def _camera(cameras: dict[str, Camera], cid: str) -> Camera:
    if cid not in cameras:
        raise SystemExit(f"unknown camera {cid}; known: {', '.join(sorted(cameras))}")
    return cameras[cid]


def cmd_list(args, cameras) -> int:
    for cam in cameras.values():
        try:
            listed = find(cam.channel, cam.title)
            status = f"live ({listed.video_id})" if listed.live else "listed, not live"
        except StreamError as exc:
            status = f"DOWN: {exc}"
        lanes = ", ".join(lane.lane for lane in cam.lanes)
        scale = "scale verified" if cam.scale_verified else "scale unverified"
        print(f"{cam.id:<16} {cam.port_number} {cam.direction:<5} [{lanes}] {scale} - {status}")
    return 0


def cmd_snapshot(args, cameras) -> int:
    cam = _camera(cameras, args.camera)
    frame = next(frames(_source(cam, args.source), FPS, 1, live_region(cam)))
    reader = CameraReader(cam)
    shown = frame.copy()
    sx, sy = frame.shape[1] / cam.frame_size[0], frame.shape[0] / cam.frame_size[1]
    for x, y, w, h in cam.mask:
        cv2.rectangle(shown, (round(x * sx), round(y * sy)),
                      (round((x + w) * sx), round((y + h) * sy)), (0, 0, 200), 2)
    ground = np.int32([[x * sx, y * sy] for x, y in cam.ground])
    cv2.polylines(shown, [ground], True, (0, 255, 255), 2)
    for name, strip in reader.strips((frame.shape[1], frame.shape[0])).items():
        outline = np.int32(strip.outline())
        cv2.polylines(shown, [outline], True, (0, 220, 0), 2)
        cv2.putText(shown, f"{name} booth", tuple(int(v) for v in outline[0]),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 220, 0), 1)
    cv2.imwrite(args.out, shown)
    print(f"wrote {args.out}: yellow = ground rectangle, green = lane strips "
          f"(label at the booth end), red = masked overlays")
    return 0


def cmd_reference(args, cameras) -> int:
    """Learn the road from moments spread across time: several recorded clips (--source, may
    repeat), or --rounds live samples --gap seconds apart. One moment is refused: a median
    over a single moment learns whatever stood in the lane then."""
    cam = _camera(cameras, args.camera)
    sources = args.source or []
    if len(sources) < 2 and args.rounds < 2:
        raise SystemExit("learn from at least two moments: --source CLIP twice or more, "
                         "or --rounds N (N >= 2) on the live stream")
    images: list[Image] = []
    if sources:
        for src in sources:
            for i, frame in enumerate(frames(src, FPS, args.seconds, live_region(cam))):
                if i % 8 == 0:
                    images.append(image_features(frame))
    else:
        for n in range(args.rounds):
            if n:
                time.sleep(args.gap)
            got = list(frames(_source(cam, None), FPS, args.seconds, live_region(cam)))
            images.extend(image_features(f) for f in got[::8])
            print(f"round {n + 1}/{args.rounds}: {len(got)} frames", flush=True)
    reader = CameraReader(cam)
    reader.learn(images)
    reader.save()
    print(f"learnt the road for {cam.id} from {len(images)} samples into {state_dir()}")
    return 0


def _estimate(cam: Camera, source: str | None, seconds: float,
              debug: str | None = None) -> list[LaneEstimate]:
    reader = CameraReader(cam)
    estimates = reader.read(frames(_source(cam, source), FPS, seconds, live_region(cam)),
                            save=source is None)
    if debug:
        cv2.imwrite(debug, debug_image(reader, estimates))
    return estimates


def _read_once(cam: Camera, source: str | None, seconds: float,
               debug: str | None = None) -> list[dict]:
    started = datetime.now(UTC)
    lanes = {lane.lane: lane for lane in cam.lanes}
    return [as_signal(e, lanes[e.lane], started) for e in _estimate(cam, source, seconds, debug)]


def cmd_read(args, cameras) -> int:
    cam = _camera(cameras, args.camera)
    try:
        signals = _read_once(cam, args.source, args.seconds, args.debug)
    except StreamError as exc:
        print(json.dumps({"camera": cam.id, "error": type(exc).__name__, "detail": str(exc)}))
        return 1
    for sig in signals:
        if args.json:
            print(json.dumps(sig, ensure_ascii=False))
            continue
        ev = sig["evidence"]
        wait = "unknown" if sig["minutes"] is None else f"{sig['minutes']} min"
        if ev["lower_bound"] and sig["minutes"] is not None:
            wait = f"at least {sig['minutes']} min"
        speed = "-" if ev["speed_m_s"] is None else f"{ev['speed_m_s']:.2f} m/s"
        print(f"{cam.id}/{ev['lane']}: {ev['state']}, queue {ev['queue_m']:.0f} m"
              f"{'+' if ev['beyond_view'] else ''}, speed {speed}, wait {wait}"
              f" (range {sig['low']}-{sig['high']})")
    return 0


async def _alert(text: str) -> None:
    from ...core.http import HttpClient
    from ...social.notify import notify_alert
    async with HttpClient() as http:
        await notify_alert(http, text)


def cmd_run(args, cameras) -> int:
    """The always-on worker: one window per camera, every `--every` seconds, forever.

    A camera that fails is reported once when it goes down and once when it comes back,
    over Telegram, and keeps being retried; it never takes the others with it."""
    chosen = [_camera(cameras, c.strip()) for c in args.cameras.split(",") if c.strip()]
    heartbeat = state_dir() / "heartbeat.json"
    heartbeat.parent.mkdir(parents=True, exist_ok=True)
    down: dict[str, str] = {}
    history: dict[tuple[str, str], list] = {}
    while True:
        started = time.monotonic()
        status = {}
        for cam in chosen:
            try:
                observed = datetime.now(UTC)
                lanes = {lane.lane: lane for lane in cam.lanes}
                for est in _estimate(cam, None, args.seconds):
                    lane = lanes[est.lane]
                    past = history.setdefault((cam.id, est.lane), [])
                    past.append((time.monotonic(), est))
                    past[:] = [(t, e) for t, e in past if time.monotonic() - t <= 3600]
                    sig = as_signal(est, lane, observed)
                    # The signal's number is the rolling one: a single window in stop-and-go
                    # traffic is too noisy to publish. The window's own reading stays in evidence.
                    roll = rolling(past, time.monotonic(), lane.booth_distance_m)
                    sig["evidence"]["rolling"] = roll
                    if est.state in ("queue", "stalled"):
                        sig["minutes"] = None if roll["wait_min"] is None else round(roll["wait_min"])
                        sig["low"] = sig["minutes"] if roll.get("lower_bound") else sig["low"]
                        sig["high"] = None if roll.get("lower_bound") else sig["high"]
                    print(json.dumps(sig, ensure_ascii=False), flush=True)
                status[cam.id] = "ok"
                if cam.id in down:
                    asyncio.run(_alert(f"Border camera {cam.id} is back."))
                    del down[cam.id]
            except (StreamError, FileNotFoundError, ValueError) as exc:
                status[cam.id] = f"{type(exc).__name__}: {exc}"
                print(json.dumps({"camera": cam.id, "error": status[cam.id]}), flush=True)
                if cam.id not in down:
                    asyncio.run(_alert(f"Border camera {cam.id} stopped: {status[cam.id]}"))
                down[cam.id] = status[cam.id]
        heartbeat.write_text(json.dumps({"at": datetime.now(UTC).isoformat(), "cameras": status}))
        time.sleep(max(0.0, args.every - (time.monotonic() - started)))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m scraper.border camera", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    p = sub.add_parser("snapshot")
    p.add_argument("--camera", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--source")
    p = sub.add_parser("reference")
    p.add_argument("--camera", required=True)
    p.add_argument("--seconds", type=float, default=REFERENCE_SECONDS)
    p.add_argument("--source", action="append", help="a recorded clip; repeat for each moment")
    p.add_argument("--rounds", type=int, default=0, help="live samples to take")
    p.add_argument("--gap", type=float, default=600, help="seconds between live samples")
    p = sub.add_parser("read")
    p.add_argument("--camera", required=True)
    p.add_argument("--seconds", type=float, default=WINDOW_SECONDS)
    p.add_argument("--source")
    p.add_argument("--json", action="store_true")
    p.add_argument("--debug", help="write the last frame with occupancy and queue drawn on")
    p = sub.add_parser("run")
    p.add_argument("--cameras", required=True)
    p.add_argument("--seconds", type=float, default=WINDOW_SECONDS)
    p.add_argument("--every", type=float, default=300)
    args = parser.parse_args(argv)
    cameras = load_cameras()
    handler = {"list": cmd_list, "snapshot": cmd_snapshot, "reference": cmd_reference,
               "read": cmd_read, "run": cmd_run}[args.cmd]
    return handler(args, cameras)


if __name__ == "__main__":
    sys.exit(main())
