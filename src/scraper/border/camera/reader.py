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
from datetime import UTC, datetime
from pathlib import Path

import cv2
import numpy as np

from .calibration import Camera, Strip, live_region, load_cameras
from .measure import LaneEstimate, LaneWindow, Reference, image_features
from .stream import StreamError, find, frames, resolve

FPS = 2.0                 # two frames a second: a queued car moves well under a metre between them
WINDOW_SECONDS = 60
REFERENCE_SECONDS = 60


def state_dir() -> Path:
    return Path(os.environ.get("CAMERA_STATE_DIR") or Path.home() / ".chisme" / "camera")


def _ref_path(camera: Camera, lane: str) -> Path:
    return state_dir() / f"{camera.id}.{lane}.reference.npz"


class CameraReader:
    def __init__(self, camera: Camera):
        self.camera = camera
        self._strips: dict[str, Strip] = {}

    def strips(self, frame: np.ndarray) -> dict[str, Strip]:
        size = (frame.shape[1], frame.shape[0])
        if not self._strips or next(iter(self._strips.values())).frame_size != size:
            self._strips = {lane.lane: Strip(self.camera, lane, size) for lane in self.camera.lanes}
        return self._strips

    def learn_reference(self, source_frames: list[np.ndarray]) -> dict[str, Reference]:
        """Per-pixel median of frames of a clear road. Run it when the lanes are empty or
        flowing freely: a median over a standing queue learns the queue as road."""
        images = [image_features(f) for f in source_frames]
        refs = {}
        for lane, strip in self.strips(source_frames[0]).items():
            refs[lane] = Reference.from_images(strip, images)
            path = _ref_path(self.camera, lane)
            path.parent.mkdir(parents=True, exist_ok=True)
            np.savez_compressed(path, grey=refs[lane].image, edges=refs[lane].edges,
                                size=np.array(strip.size),
                                saved_at=np.array(datetime.now(UTC).isoformat()))
        return refs

    def stored_reference(self, lane: str, strip: Strip) -> Reference:
        path = _ref_path(self.camera, lane)
        if not path.exists():
            raise FileNotFoundError(f"no empty-road reference for {self.camera.id}/{lane}: "
                                    f"run `camera reference --camera {self.camera.id}` while it is clear")
        data = np.load(path)
        if tuple(data["size"]) != strip.size:
            raise ValueError(f"{path.name} was learnt for a different calibration; relearn it")
        return Reference(data["grey"], data["edges"])

    def read(self, source_frames, references: dict[str, Reference] | None = None,
             save: bool = True) -> list[LaneEstimate]:
        windows: dict[str, LaneWindow] = {}
        for frame in source_frames:
            if not windows:
                for lane, strip in self.strips(frame).items():
                    ref = (references or {}).get(lane) or self.stored_reference(lane, strip)
                    windows[lane] = LaneWindow(strip, ref, 1 / FPS)
            image = image_features(frame)          # once per frame, shared by every lane
            for window in windows.values():
                window.add(image)
        if save and not references:
            # The references drifted toward the light of this window; keep that, so the next
            # window starts from today's road rather than the night it was learnt in.
            for lane, window in windows.items():
                np.savez_compressed(_ref_path(self.camera, lane), grey=window.ref.image,
                                    edges=window.ref.edges, size=np.array(window.strip.size),
                                    saved_at=np.array(datetime.now(UTC).isoformat()))
        return [w.result() for w in windows.values()]


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
        print(f"{cam.id:<16} {cam.port_number} {cam.direction:<5} [{lanes}] {scale} — {status}")
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
    for name, strip in reader.strips(frame).items():
        outline = np.int32(strip.outline())
        cv2.polylines(shown, [outline], True, (0, 220, 0), 2)
        cv2.putText(shown, f"{name} booth", tuple(int(v) for v in outline[0]),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 220, 0), 1)
    cv2.imwrite(args.out, shown)
    print(f"wrote {args.out}: yellow = ground rectangle, green = lane strips "
          f"(label at the booth end), red = masked overlays")
    return 0


def cmd_reference(args, cameras) -> int:
    cam = _camera(cameras, args.camera)
    got = list(frames(_source(cam, args.source), FPS, args.seconds, live_region(cam)))
    CameraReader(cam).learn_reference(got)
    print(f"learnt the empty road for {cam.id} from {len(got)} frames into {state_dir()}")
    return 0


def _read_once(cam: Camera, source: str | None, seconds: float) -> list[dict]:
    started = datetime.now(UTC)
    estimates = CameraReader(cam).read(frames(_source(cam, source), FPS, seconds, live_region(cam)))
    lanes = {lane.lane: lane for lane in cam.lanes}
    return [as_signal(e, lanes[e.lane], started) for e in estimates]


def cmd_read(args, cameras) -> int:
    cam = _camera(cameras, args.camera)
    try:
        signals = _read_once(cam, args.source, args.seconds)
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
    while True:
        started = time.monotonic()
        status = {}
        for cam in chosen:
            try:
                for sig in _read_once(cam, None, args.seconds):
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
    p.add_argument("--source")
    p = sub.add_parser("read")
    p.add_argument("--camera", required=True)
    p.add_argument("--seconds", type=float, default=WINDOW_SECONDS)
    p.add_argument("--source")
    p.add_argument("--json", action="store_true")
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
