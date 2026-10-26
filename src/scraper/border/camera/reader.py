"""Read queues off the bridge cameras.

    python -m scraper.border camera list                       # calibrated cameras, stream status
    python -m scraper.border camera snapshot --camera pdn-north --out pdn.jpg
                                                               # one frame with the lanes drawn on
    python -m scraper.border camera reference --camera pdn-north --rounds 6 --gap 600
                                                               # learn the road from six moments
    python -m scraper.border camera read --camera pdn-north [--seconds 60] [--json]
    python -m scraper.border camera run --cameras pdn-north,lerdo-south-booths [--every 300]
                                                               # the always-on worker
    python -m scraper.border camera view [--cameras ...] [--every 120] [--port 8090]
                                                               # the worker plus a live page

`read` and `reference` take --source FILE to work from a recorded clip instead of the live
stream, which is how a calibration is tuned and how a reading is reproduced.

Runs on a machine we own (QUESTIONS.md, Q-3), not GitHub Actions: it has to watch video
continuously. State (each lane's empty-road reference, the worker's heartbeat) lives in
CAMERA_STATE_DIR, by default ~/.chisme/camera.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
import zipfile
from collections import deque
from datetime import UTC, datetime
from pathlib import Path

import cv2
import numpy as np

from .calibration import PX_PER_M, Camera, Strip, live_region, load_cameras
from .hls import HlsReader, Stamped
from .measure import (
    BIN_M,
    Image,
    LaneEstimate,
    LaneWindow,
    Reference,
    foreground,
    image_features,
    plain,
    strip_features,
)
from .stream import StreamError, find, frames, resolve

log = logging.getLogger(__name__)

FPS = 2.0                 # two frames a second: a queued car moves well under a metre between them
YOUTUBE_SIZE = (854, 480) # FPFCH streams are read at 480p (stream.FORMAT); the City at frame_size
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


class StaleReference(ValueError):
    """A saved reference learnt for a different calibration of the same lane."""


def _geometry(strip: Strip) -> np.ndarray:
    """The calibration a lane's road is learnt for, saved next to it. The strip's pixel size
    alone does not tell: a lane moved along the road, or a ground rectangle redrawn, keeps its
    size and puts other road under every pixel. A changed mask is stale too: the pixels it
    covered were learnt black (strip_features), and uncovered they read as a car."""
    cam, lane = strip.camera, strip.lane
    return np.array([*cam.frame_size, *np.ravel(cam.ground), cam.width_m, cam.length_m,
                     *lane.across, *lane.along, lane.booth == "far", *np.ravel(cam.mask)],
                    np.float64)


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
        self.last_at: datetime | None = None     # capture time of last_frame, when stamped
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

    def _rebuild(self, lanes: list[str] | None = None) -> None:
        """Re-learn every lane's road from the bank, or only `lanes`."""
        size = self.bank[-1][0]
        samples = [s_lanes for s, s_lanes in self.bank if s == size]
        for lane, strip in self.strips(size).items():
            if lanes is not None and lane not in lanes:
                continue
            ref = emptiest_reference([(g.astype(np.float32), e.astype(np.float32))
                                      for g, e in (s[lane] for s in samples)],
                                     strip.valid, self.references.get(lane))
            if ref is not None:
                self.references[lane] = ref

    def learn(self, images: list[Image]) -> None:
        """Learn the scene and every lane's empty road from images taken at separate moments."""
        self.bank.extend(self._sample(image) for image in images)
        self._rebuild()

    def save(self, lanes: list[str] | None = None) -> None:
        """Save every lane's road, or only `lanes`. Each file is written whole and then moved
        into place, so a worker killed mid-save never leaves a half-written reference."""
        state_dir().mkdir(parents=True, exist_ok=True)
        stamp = np.array(datetime.now(UTC).isoformat())
        for lane, ref in self.references.items():
            if lanes is not None and lane not in lanes:
                continue
            path = _state_path(self.camera, f"{lane}.reference")
            tmp = path.with_suffix(".tmp")
            with open(tmp, "wb") as f:
                np.savez_compressed(f, grey=ref.image, edges=ref.edges,
                                    size=np.array(self._strips[lane].size),
                                    geometry=_geometry(self._strips[lane]), saved_at=stamp)
            os.replace(tmp, path)

    def _load_lane(self, lane: str, strip: Strip) -> Reference | None:
        """One lane's saved road, or None if there is none. The archive is closed before
        anything else happens: on Windows a file still open cannot be deleted, and the relearn
        path deletes stale references (it once crashed the worker on every restart)."""
        path = _state_path(self.camera, f"{lane}.reference")
        if not path.exists():
            return None
        with np.load(path) as saved:
            size = tuple(int(v) for v in saved["size"])
            grey, edges = saved["grey"].copy(), saved["edges"].copy()
            # One saved before its geometry was recorded cannot be checked: it is relearnt once.
            geometry = saved["geometry"].copy() if "geometry" in saved.files else None
        if (size != strip.size or geometry is None
                or not np.array_equal(geometry, _geometry(strip))):
            raise StaleReference(f"{self.camera.id}/{lane} was learnt for a different "
                                 "calibration; relearn it")
        return Reference(grey, edges)

    def prepare(self) -> list[str]:
        """Load every lane reference that exists and still fits its calibration, delete the
        stale ones, and return the lanes that still need learning. A strip's size does not
        depend on the frame size, so this needs no frame. Lanes already learnt are kept: a
        lane added to cameras.json must not cost the others their references."""
        missing = []
        for lane, strip in self.strips(self.camera.frame_size).items():
            if lane in self.references:
                continue
            try:
                ref = self._load_lane(lane, strip)
            except (StaleReference, ValueError, KeyError, EOFError, OSError,
                    zipfile.BadZipFile) as exc:
                # Stale or unreadable (a corrupt file would otherwise fail every cycle, for
                # ever): drop it and learn the lane again.
                log.warning("dropping %s/%s reference: %s", self.camera.id, lane, exc)
                _state_path(self.camera, f"{lane}.reference").unlink(missing_ok=True)
                ref = None
            if ref is None:
                missing.append(lane)
            else:
                self.references[lane] = ref
        return missing

    def load(self, size: tuple[int, int]) -> None:
        for lane, strip in self.strips(size).items():
            ref = self._load_lane(lane, strip)
            if ref is None:
                raise FileNotFoundError(
                    f"no learnt road for {self.camera.id}/{lane}: run `camera reference --camera "
                    f"{self.camera.id} --rounds 6` (or --source with several clips)")
            self.references[lane] = ref

    def read(self, source_frames, save: bool = True) -> list[LaneEstimate]:
        windows: dict[str, LaneWindow] = {}
        # Frames for the bank, spread over a read of unknown length: keep every `stride`-th and
        # halve the list (doubling the stride) once it passes 2 * SAMPLES_PER_WINDOW. A read then
        # holds a few images (~3.3 MB each at 480p), never one per frame: that was ~400 MB per
        # camera-minute, with every camera read at once.
        kept: list[Image] = []
        stride = 1
        for n, item in enumerate(source_frames):
            # Live frames come stamped with their capture time (hls.Stamped); recorded clips are
            # bare frames taken to be 1/FPS apart.
            frame, at = (item.image, item.at) if isinstance(item, Stamped) else (item, None)
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
                window.add(raw, None if at is None else at.timestamp())
            if n % stride == 0:
                kept.append(raw)
                if len(kept) > 2 * SAMPLES_PER_WINDOW:
                    kept, stride = kept[::2], stride * 2
            self.last_frame, self.last_at = frame, at
        self.last_windows = windows
        if kept:
            picks = kept if len(kept) <= SAMPLES_PER_WINDOW else [
                kept[j * len(kept) // SAMPLES_PER_WINDOW] for j in range(SAMPLES_PER_WINDOW)]
            self.bank.extend(self._sample(im) for im in picks)
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


def as_signal(est: LaneEstimate, lane, observed_at: datetime,
              direction_verified: bool = False) -> dict:
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
        "evidence": {**est.to_dict(), "lane_verified": lane.lane_verified,
                     "direction_verified": direction_verified,
                     "booth_distance_m": lane.booth_distance_m,
                     "booth_distance_uncertainty_m": lane.booth_distance_uncertainty_m,
                     "booth_distance_verified": lane.booth_distance_verified},
    }


def _source(camera: Camera, source: str | None) -> str:
    return source or resolve(camera.channel, camera.title)


def live_reader(camera: Camera) -> HlsReader:
    """The camera's live stream (ADR-0014): every frame stamped with its capture time from the
    stream's own clock, so a reading is timed by the road, not by the network."""
    from ...core.config import settings  # only live reads need the settings

    if camera.source == "hls":
        url_fn, size = (lambda: camera.url), camera.frame_size
    else:
        url_fn, size = (lambda: resolve(camera.channel, camera.title)), YOUTUBE_SIZE
    return HlsReader(url_fn, camera.stream_clock, size, FPS, settings.user_agent,
                     live_region(camera))


def live_frames(camera: Camera, seconds: float | None):
    return live_reader(camera).frames(seconds)


def source_frames(camera: Camera, source: str | None, seconds: float):
    """A recorded clip (`source`) as bare frames, or the live stream as stamped ones."""
    if source:
        return frames(source, FPS, seconds, live_region(camera))
    return live_frames(camera, seconds)


def _camera(cameras: dict[str, Camera], cid: str) -> Camera:
    if cid not in cameras:
        raise SystemExit(f"unknown camera {cid}; known: {', '.join(sorted(cameras))}")
    return cameras[cid]


def cmd_list(args, cameras) -> int:
    for cam in cameras.values():
        if cam.source == "hls":
            status = f"HLS {cam.url} (never displayed)" if not cam.display else f"HLS {cam.url}"
            lanes = ", ".join(lane.lane for lane in cam.lanes) or f"{len(cam.count_zones)} count zones"
            print(f"{cam.id:<16} {cam.port_number} {cam.direction:<5} [{lanes}] - {status}")
            continue
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
    if args.source:
        frame = next(frames(args.source, FPS, 1, live_region(cam)))
    else:
        frame = next(live_frames(cam, 1)).image
    reader = CameraReader(cam)
    shown = frame.copy()
    sx, sy = frame.shape[1] / cam.frame_size[0], frame.shape[0] / cam.frame_size[1]
    for x, y, w, h in cam.mask:
        cv2.rectangle(shown, (round(x * sx), round(y * sy)),
                      (round((x + w) * sx), round((y + h) * sy)), (0, 0, 200), 2)
    if cam.lanes:
        ground = np.int32([[x * sx, y * sy] for x, y in cam.ground])
        cv2.polylines(shown, [ground], True, (0, 255, 255), 2)
    for zone in cam.count_zones:                 # counting bands: magenta, far edge thicker
        quad = np.int32([[x * sx, y * sy] for x, y in zone.quad])
        cv2.polylines(shown, [quad], True, (255, 0, 255), 1)
        cv2.line(shown, tuple(int(v) for v in quad[2]), tuple(int(v) for v in quad[3]),
                 (255, 0, 255), 2)
        cv2.putText(shown, f"{zone.line}:{zone.lane}", tuple(int(v) for v in quad[3]),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.45, (255, 0, 255), 1)
    for name, strip in reader.strips((frame.shape[1], frame.shape[0])).items():
        outline = np.int32(strip.outline())
        cv2.polylines(shown, [outline], True, (0, 220, 0), 2)
        cv2.putText(shown, f"{name} booth", tuple(int(v) for v in outline[0]),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 220, 0), 1)
    cv2.imwrite(args.out, shown)
    print(f"wrote {args.out}: yellow = ground rectangle, green = lane strips, magenta = count zones, "
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
            got = [f.image for f in live_frames(cam, args.seconds)]
            images.extend(image_features(f) for f in got[::8])
            print(f"round {n + 1}/{args.rounds}: {len(got)} frames", flush=True)
    reader = CameraReader(cam)
    reader.learn(images)
    reader.save()
    print(f"learnt the road for {cam.id} from {len(images)} samples into {state_dir()}")
    return 0


def _estimate_with(cam: Camera, source: str | None, seconds: float,
                   debug: str | None = None) -> tuple[list[LaneEstimate], CameraReader]:
    reader = CameraReader(cam)
    estimates = reader.read(source_frames(cam, source, seconds), save=source is None)
    if debug:
        cv2.imwrite(debug, debug_image(reader, estimates))
    return estimates, reader


def _read_once(cam: Camera, source: str | None, seconds: float,
               debug: str | None = None) -> list[dict]:
    started = datetime.now(UTC)
    estimates, reader = _estimate_with(cam, source, seconds, debug)
    observed = reader.last_at or started        # the road's time when the stream says it
    lanes = {lane.lane: lane for lane in cam.lanes}
    return [as_signal(e, lanes[e.lane], observed, cam.direction_verified) for e in estimates]


def cmd_read(args, cameras) -> int:
    cam = _camera(cameras, args.camera)
    try:
        signals = _read_once(cam, args.source, args.seconds, args.debug)
    except StreamError as exc:
        print(json.dumps({"camera": cam.id, "error": type(exc).__name__, "detail": str(exc)}))
        return 1
    for sig in signals:
        if args.json:
            print(json.dumps(plain(sig), ensure_ascii=False))
            continue
        ev = sig["evidence"]
        wait = "unknown" if sig["minutes"] is None else f"{sig['minutes']} min"
        if ev["lower_bound"] and sig["low"] is not None:
            wait = f"at least {sig['low']} min"        # a floor is its range's low end
        speed = "-" if ev["speed_m_s"] is None else f"{ev['speed_m_s']:.2f} m/s"
        print(f"{cam.id}/{ev['lane']}: {ev['state']}, queue {ev['queue_m']:.0f} m"
              f"{'+' if ev['beyond_view'] else ''}, speed {speed}, wait {wait}"
              f" (range {sig['low']}-{sig['high']})")
    return 0


def cmd_run(args, cameras) -> int:
    """The always-on worker: every camera read in parallel every `--every` seconds, forever,
    one JSON signal per lane on stdout. See worker.py."""
    from .worker import AlreadyRunning, Worker

    chosen = [_camera(cameras, c.strip()) for c in args.cameras.split(",") if c.strip()]
    try:
        worker = Worker(chosen, seconds=args.seconds, every=args.every,
                        emit=lambda sig: print(json.dumps(sig, ensure_ascii=False), flush=True))
    except AlreadyRunning as exc:
        print(exc)
        return 1
    worker.forever()
    return 0


def cmd_view(args, cameras) -> int:
    from .viewer import cmd_view as view

    if args.cameras:
        for c in args.cameras.split(","):
            if c.strip():
                _camera(cameras, c.strip())
    return view(args, cameras)


def cmd_transit(args, cameras) -> int:
    from .runner import cmd_transit as transit

    return transit(args, cameras)


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
    p = sub.add_parser("view", help="the worker plus a live page at http://127.0.0.1:PORT")
    p.add_argument("--cameras", help="comma-separated; default every calibrated camera")
    p.add_argument("--seconds", type=float, default=WINDOW_SECONDS)
    p.add_argument("--every", type=float, default=120)
    p.add_argument("--port", type=int, default=8090)
    p.add_argument("--json", action="store_true", help="also print each signal")
    p = sub.add_parser("transit", help="crossing time between two cameras' counting lines")
    p.add_argument("--a", default="pdn-north", help="camera with counting line A (entry)")
    p.add_argument("--b", default="city-pdn-north", help="camera with counting line B (exit)")
    p.add_argument("--methods", default="loop,yolox")
    p.add_argument("--every", type=float, default=60, help="seconds between measurements")
    p.add_argument("--minutes", type=float, help="stop after this long (default: run on)")
    args = parser.parse_args(argv)
    cameras = load_cameras()
    handler = {"list": cmd_list, "snapshot": cmd_snapshot, "reference": cmd_reference,
               "read": cmd_read, "run": cmd_run, "view": cmd_view,
               "transit": cmd_transit}[args.cmd]
    return handler(args, cameras)


if __name__ == "__main__":
    sys.exit(main())
