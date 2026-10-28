"""YOLOX-S on the GPU: ONNX Runtime's DirectML build on the NVIDIA adapter, checked against the CPU.

The owner's decision of 2026-10-27: run the model on the RTX 4070, where it runs better, to free
the CPU, by default ("auto") as long as that is safe. The CPU path (detect.cpu_net, OpenCV) stays,
and is used whenever this one is not safe or is not asked for ($CHISME_YOLOX_DEVICE, `--device`).

Measured on this machine on 2026-10-27/28 (onnxruntime-directml 1.24.4, DirectML 1.15.4, NVIDIA
driver 617.42). One 640x640 pass took 5.4 ms and 0.006 CPU-s on the GPU, against 113.9 ms and
0.41 CPU-s on the CPU at 4 threads. Over 721 + 1,201 live pdn-north frames, by day and by night,
both paths gave the same boxes, classes and order to the pixel, and the same 31 + 15 crossings.
The raw outputs are never bit-identical: they differ by up to 3.35e-4. Frames nudged by a few
grey levels put a score or an overlap on opposite sides of a threshold (0.1, 0.4, 0.45) on the
two paths; on real frames that is estimated at 0.1-0.7 decisions per 1,000 frames, most of which
change no box. So the same frames give the same counts per path, not across paths: `device` says
which path made a count, and records of the two must never be graded together.

What makes the GPU path safe, each point from that measurement and its review:
1. FP32 only. This version has no FP16 option, and the graph is the pinned model, read through
   detect.read_model (sha256 checked). Only onnxruntime ORT_VERSION is accepted (the pyproject
   `gpu` extra pins it). onnxruntime is imported here only, and only when the GPU is tried, so
   the CPU path never needs it.
2. The adapter is chosen at every start by vendor and name, from ONNX Runtime's own list of
   DirectML devices (`find_adapter`): an NVIDIA GPU, never the Intel chip or a software adapter,
   and never a fixed index. The RTX 4070 was DXGI adapter 1 in one run and 0 in the next, and
   the Intel chip gives outputs 0.5 away from the CPU's in 8 times the time.
3. Nothing falls back to the CPU inside ONNX Runtime: not one node
   (session.disable_cpu_ep_fallback), and not the whole session (InferenceSession's own fallback,
   which otherwise rebuilds a session on the CPU after an EP error and goes on). Every node is
   checked to be on DirectML.
4. One lock around every GPU run in the process (`GpuState.lock`). The runner runs one network
   per camera, each in its own thread; two sessions running at once failed with 80004005 in one
   trial of three and crashed the whole process in the other two. With the lock, 600 of 600.
5. A self-check at every start on drawn scenes (`check_scenes`), run on both paths: the same
   detections (`differences`) and raw outputs within RAW_TOLERANCE, or the GPU is refused and the
   reason said.
6. While counting, every CHECK_EVERY-th frame is detected on the CPU as well and compared. A
   difference is counted (`CrossCheck`) and logged, and the GPU's boxes are still the ones used:
   the paths are never switched mid-run. DIFFER_LIMIT differences within DIFFER_WINDOW checks stop
   the GPU path (GpuFailed).
7. Records say the path: `device` is "cpu" or "gpu:<adapter name>" (`GpuYolox.record`).
8. A GPU that fails mid-run (device removed, a power state, a run error, an output that is not
   finite) stops the GPU path for the rest of the process: GpuFailed, which the runner turns into
   a gap. It never goes on on the CPU in its place.
"""
from __future__ import annotations

import ctypes
import itertools
import logging
import os
import threading
from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

import cv2
import numpy as np

from . import detect
from .detect import HIGH_SCORE, Detection, Tile

log = logging.getLogger(__name__)

DEVICE_ENV = "CHISME_YOLOX_DEVICE"      # auto | gpu | cpu, for launchers; an argument overrides it
AUTO, GPU, CPU = "auto", "gpu", "cpu"
DEVICES = (AUTO, GPU, CPU)
ORT_VERSION = "1.24.4"                  # onnxruntime-directml, the version the GPU was measured on
PROVIDER = "DmlExecutionProvider"
NVIDIA = 0x10DE                         # PCI vendor id
INPUT_SHAPE = (1, 3, detect.INPUT_SIZE, detect.INPUT_SIZE)
OUTPUT_SHAPE = (1, sum((detect.INPUT_SIZE // s) ** 2 for s in detect.STRIDES), 85)

RAW_TOLERANCE = 1e-3     # max |GPU - CPU| over a raw output. Measured up to 3.35e-4 on live and
                         # darkened frames; the Intel chip gave 0.5
BOX_TOLERANCE_PX = 0.1   # a box corner may differ by this much, in frame pixels. Measured up to
                         # 3.9e-4; a box that changed because an overlap fell on the other side
                         # of NMS_IOU moved 0.73 px
SCORE_TOLERANCE = 1e-3   # measured up to 2.8e-6; that same box's score changed by 0.0084
CHECK_EVERY = 30         # one frame in this many is also detected on the CPU, the first included.
                         # ASSUMPTION: at 2 fps, a check every 15 s. With two tiles a check costs
                         # about 0.8 CPU-s, 0.027 CPU-s a frame on average, beside 0.021 for the
                         # GPU path, against 0.82 for the CPU path
DIFFER_LIMIT = 3         # this many differing checks...
DIFFER_WINDOW = 20       # ...within this many stop the GPU path. ASSUMPTION: a difference on real
                         # frames is a rare threshold flip (above), so three in five minutes at
                         # 2 fps means the GPU computes something else; a broken one differs on
                         # every check and is stopped within three (45 s)
CHECK_SCENES = 6         # drawn scenes in the start-up self-check
SCENE_SIZE = (854, 480)  # pdn-north's frames


class GpuRefused(RuntimeError):
    """The GPU path may not be used at this start: not installed, no NVIDIA adapter, a node not
    on DirectML, or a failed self-check. "auto" then runs on the CPU and says why; "gpu" stops."""


class GpuFailed(RuntimeError):
    """The GPU path stopped mid-run: the device was lost, a run failed, or its detections
    differed from the CPU's too often. Every later GPU run in the process raises it again. The
    runner records the camera's yolox line as unseen from that frame; it never counts it on the
    CPU in its place."""


class GpuState:
    """What every GPU network in the process shares: the one lock around every GPU run, and
    whether the GPU path has been stopped (and why)."""

    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.stopped: str | None = None
        self.announced: set[tuple[str, str]] = set()   # adapters already logged

    def stop(self, reason: str) -> GpuFailed:
        """Stop the GPU path for the rest of the process; the error to raise. The first reason
        is kept."""
        with self.lock:
            if self.stopped is None:
                self.stopped = reason
                log.error("YOLOX on the GPU is stopped for the rest of this run: %s", reason)
            return GpuFailed(self.stopped)


STATE = GpuState()


def requested_device(requested: str | None = None) -> str:
    """`requested`, else $CHISME_YOLOX_DEVICE, else "auto". Anything else is refused."""
    value = (requested if requested is not None else os.environ.get(DEVICE_ENV) or AUTO)
    value = value.strip().lower()
    if value not in DEVICES:
        raise ValueError(f"YOLOX device {value!r}: use one of {', '.join(DEVICES)} "
                         f"(--device or {DEVICE_ENV})")
    return value


# --- ONNX Runtime and the adapter -------------------------------------------------------------
def import_onnxruntime():
    """onnxruntime, imported now, when it is ORT_VERSION and has the DirectML provider."""
    try:
        import onnxruntime
    except (ImportError, OSError) as exc:
        raise GpuRefused(f"onnxruntime-directml {ORT_VERSION} is not installed "
                         f"(pip install -e \".[gpu]\"): {exc}") from exc
    if onnxruntime.__version__ != ORT_VERSION:
        raise GpuRefused(f"onnxruntime {onnxruntime.__version__} is installed; only "
                         f"{ORT_VERSION}, the version the GPU path was measured on, is accepted")
    if PROVIDER not in onnxruntime.get_available_providers():
        raise GpuRefused(f"this onnxruntime has no {PROVIDER} (onnxruntime-directml has)")
    return onnxruntime


@dataclass(frozen=True)
class Adapter:
    name: str           # the adapter's description, e.g. "NVIDIA GeForce RTX 4070 Laptop GPU"
    device_id: str      # DirectML's device_id (the DXGI adapter number) at this start only
    vendor_id: int
    pci_device: int     # the PCI device id
    luid: str           # changes between boots: for the log only


def find_adapter(ort) -> Adapter:
    """The NVIDIA GPU among ONNX Runtime's DirectML devices. Its device_id is read from ORT's
    own options for that device at every start, never assumed: it moves between runs. Intel's
    and Microsoft's (software) adapters are never taken. With more than one NVIDIA GPU, DXGI's
    own highest-performance order decides (ASSUMPTION: this machine has one)."""
    found, listed = [], []
    for ep in ort.get_ep_devices():
        if ep.ep_name != PROVIDER:
            continue
        hw, meta = ep.device, dict(ep.device.metadata)
        name = meta.get("Description", "")
        listed.append(f"{name or '?'} (vendor {hw.vendor_id:#06x})")
        device_id = dict(ep.ep_options).get("device_id")
        if (hw.type != ort.OrtHardwareDeviceType.GPU or hw.vendor_id != NVIDIA
                or not name.startswith("NVIDIA") or device_id is None):
            continue
        rank = int(meta.get("DxgiHighPerformanceIndex", len(found)))
        found.append((rank, Adapter(name, str(device_id), hw.vendor_id, hw.device_id,
                                    str(meta.get("LUID", "?")))))
    if not found:
        raise GpuRefused(f"no NVIDIA GPU among the DirectML devices: {', '.join(listed) or 'none'}")
    return min(found, key=lambda f: (f[0], f[1].device_id))[1]


def _driver_version(name: str) -> str:
    """The display driver's version as Windows records it for the adapter called `name`."""
    try:
        import winreg
        key = r"SYSTEM\CurrentControlSet\Control\Class\{4d36e968-e325-11ce-bfc1-08002be10318}"
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key) as adapters:
            for i in itertools.count():
                try:
                    sub = winreg.EnumKey(adapters, i)
                except OSError:
                    break
                try:
                    with winreg.OpenKey(adapters, sub) as k:
                        if winreg.QueryValueEx(k, "DriverDesc")[0] == name:
                            return str(winreg.QueryValueEx(k, "DriverVersion")[0])
                except OSError:
                    continue
    except (ImportError, OSError):
        pass
    return "unknown"


def _file_version(path: Path) -> str:
    """A Windows DLL's file version (VS_FIXEDFILEINFO), e.g. "1.15.4.0"."""
    try:
        api = ctypes.WinDLL("version")
        size = api.GetFileVersionInfoSizeW(str(path), None)
        buf = ctypes.create_string_buffer(size)
        if not size or not api.GetFileVersionInfoW(str(path), 0, size, buf):
            return "unknown"
        info, n = ctypes.c_void_p(), ctypes.c_uint()
        if not api.VerQueryValueW(buf, "\\", ctypes.byref(info), ctypes.byref(n)) or n.value < 16:
            return "unknown"
        words = ctypes.cast(info, ctypes.POINTER(ctypes.c_uint32 * 4)).contents
        ms, ls = words[2], words[3]             # dwFileVersionMS, dwFileVersionLS
        return f"{ms >> 16}.{ms & 0xFFFF}.{ls >> 16}.{ls & 0xFFFF}"
    except (AttributeError, OSError, ValueError):
        return "unknown"


def _announce(ort, adapter: Adapter) -> None:
    """Log the adapter and the versions once per process: what a GPU count was made with."""
    key = (adapter.name, adapter.device_id)
    with STATE.lock:
        if key in STATE.announced:
            return
        STATE.announced.add(key)
    dml = Path(ort.__file__).parent / "capi" / "DirectML.dll"
    log.info("YOLOX GPU: %s (DirectML device_id %s, PCI %04x:%04x, LUID %s), driver %s, "
             "onnxruntime %s, DirectML %s", adapter.name, adapter.device_id, adapter.vendor_id,
             adapter.pci_device, adapter.luid, _driver_version(adapter.name), ort.__version__,
             _file_version(dml))


# --- the network ------------------------------------------------------------------------------
class DmlNet:
    """setInput(blob) / forward() over one DirectML session: what detect.Yolox drives, and the
    array cv2.dnn's forward() gives for this model (float32, (1, 8400, 85), C-contiguous).

    The session options are the ones DirectML requires (no memory pattern, sequential) with one
    thread, as measured. ORT 1.24.4 has no FP16 option: the graph runs in float32."""

    def __init__(self, model: bytes, adapter: Adapter, ort):
        so = ort.SessionOptions()
        so.enable_mem_pattern = False                           # required by DirectML
        so.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL    # required by DirectML
        so.intra_op_num_threads = 1
        so.inter_op_num_threads = 1
        so.log_severity_level = 3
        so.add_session_config_entry("session.disable_cpu_ep_fallback", "1")
        so.add_session_config_entry("session.record_ep_graph_assignment_info", "1")
        with STATE.lock:
            try:
                # enable_fallback=0: ORT's Python class would otherwise rebuild the session on
                # the CPU after an EP error, at creation and at any later run, and go on
                self.session = ort.InferenceSession(
                    model, sess_options=so, enable_fallback=0,
                    providers=[(PROVIDER, {"device_id": adapter.device_id})])
            except Exception as exc:    # ORT's own error types differ by failure
                raise GpuRefused(f"no DirectML session on {adapter.name} (device_id "
                                 f"{adapter.device_id}): {type(exc).__name__}: {exc}") from exc
            providers = list(self.session.get_providers())
            placed: dict[str, int] = {}
            for sub in self.session.get_provider_graph_assignment_info():
                placed[sub.ep_name] = placed.get(sub.ep_name, 0) + len(sub.get_nodes())
        if not providers or providers[0] != PROVIDER:
            raise GpuRefused(f"the session runs on {providers}, not {PROVIDER}")
        if set(placed) != {PROVIDER} or not placed[PROVIDER]:
            raise GpuRefused(f"not every node is on {PROVIDER}: nodes by provider {placed}")
        self.adapter = adapter
        self.input_name = self.session.get_inputs()[0].name
        self.output_name = self.session.get_outputs()[0].name
        self._blob: np.ndarray | None = None

    def setInput(self, blob: np.ndarray) -> None:  # cv2.dnn's name
        blob = np.ascontiguousarray(blob, dtype=np.float32)
        if blob.shape != INPUT_SHAPE:
            raise ValueError(f"blob shape {blob.shape}; the graph is fixed at {INPUT_SHAPE}")
        self._blob = blob

    def forward(self) -> np.ndarray:
        if self._blob is None:
            raise RuntimeError("forward() before setInput()")
        with STATE.lock:
            if STATE.stopped is not None:
                raise GpuFailed(STATE.stopped)
            try:
                out = self.session.run([self.output_name], {self.input_name: self._blob})[0]
            except Exception as exc:    # device removed, out of memory, a driver reset...
                raise STATE.stop(f"a DirectML run on {self.adapter.name} failed: "
                                 f"{type(exc).__name__}: {exc}") from exc
        out = np.ascontiguousarray(out, dtype=np.float32)
        # ASSUMPTION: a working device always gives finite outputs (every measured pass did)
        if out.shape != OUTPUT_SHAPE or not np.isfinite(out).all():
            raise STATE.stop(f"{self.adapter.name} gave an output of shape {out.shape} that is "
                             f"not {OUTPUT_SHAPE} or not finite")
        return out


# --- comparing the two paths ------------------------------------------------------------------
def differences(gpu: Sequence[Detection], cpu: Sequence[Detection]) -> str | None:
    """How one frame's detections on the two paths differ; None when they agree: the same count,
    and in the same order the same class, every box corner within BOX_TOLERANCE_PX, and every
    score within SCORE_TOLERANCE and on the same side of HIGH_SCORE (which starts a track).
    Stricter than "the same pixel" and with no rounding edge for a tiny difference to cross."""
    if len(gpu) != len(cpu):
        return f"{len(gpu)} boxes on the GPU, {len(cpu)} on the CPU"
    for i, (g, c) in enumerate(zip(gpu, cpu, strict=True)):
        if g.cls != c.cls:
            return f"box {i}: class {g.cls} on the GPU, {c.cls} on the CPU"
        moved = max(abs(p - q) for p, q in zip(g.box, c.box, strict=True))
        if moved > BOX_TOLERANCE_PX:
            return f"box {i} is {moved:.3g} px apart"
        if (abs(g.score - c.score) > SCORE_TOLERANCE
                or (g.score >= HIGH_SCORE) != (c.score >= HIGH_SCORE)):
            return f"box {i}: score {g.score:.7f} on the GPU, {c.score:.7f} on the CPU"
    return None


class _Recorder:
    """A network that keeps a copy of each output, so a frame's raw outputs can be compared."""

    def __init__(self, net):
        self.net = net
        self.outputs: list[np.ndarray] = []

    def setInput(self, blob: np.ndarray) -> None:  # cv2.dnn's name
        self.net.setInput(blob)

    def forward(self) -> np.ndarray:
        out = self.net.forward()
        self.outputs.append(np.array(out, dtype=np.float32, copy=True))
        return out


def compare(net, cpu_net, image: np.ndarray,
            tiles: Sequence[Tile]) -> tuple[list[Detection], float, str | None]:
    """One frame on both paths: the GPU's detections, the largest raw difference between the
    two paths' outputs, and how their detections differ (None when they agree)."""
    gpu, cpu = _Recorder(net), _Recorder(cpu_net)
    found = detect.Yolox(gpu).detect(image, tiles)
    ref = detect.Yolox(cpu).detect(image, tiles)
    raw = max((float(np.abs(a.astype(np.float64) - b).max())
               for a, b in zip(gpu.outputs, cpu.outputs, strict=True)), default=0.0)
    return found, raw, differences(found, ref)


def _rear(img: np.ndarray, cx: int, bottom: int, w: int, colour) -> None:
    """A car seen from behind: tyres, body, rear window, lights and a plate-sized light patch."""
    h = int(w * 0.8)
    x0, x1, y0 = cx - w // 2, cx + w // 2, bottom - h
    cv2.ellipse(img, (cx, bottom), (w // 2 + 4, max(3, w // 12)), 0, 0, 360, (30, 30, 30), -1)
    tw = max(4, w // 7)
    cv2.rectangle(img, (x0 + 2, bottom - h // 4), (x0 + 2 + tw, bottom), (15, 15, 15), -1)
    cv2.rectangle(img, (x1 - 2 - tw, bottom - h // 4), (x1 - 2, bottom), (15, 15, 15), -1)
    body = np.array([(x0, bottom - h // 6), (x1, bottom - h // 6), (x1, y0 + h // 2),
                     (x1 - w // 6, y0), (x0 + w // 6, y0), (x0, y0 + h // 2)], np.int32)
    cv2.fillPoly(img, [body], colour)
    window = np.array([(x0 + w // 8, y0 + h // 2 - 2), (x1 - w // 8, y0 + h // 2 - 2),
                       (x1 - w // 5, y0 + h // 10), (x0 + w // 5, y0 + h // 10)], np.int32)
    cv2.fillPoly(img, [window], (60, 50, 40))
    lh = max(3, h // 10)
    for lx in (x0 + 3, x1 - 3 - w // 6):
        cv2.rectangle(img, (lx, y0 + h // 2 + 3), (lx + w // 6, y0 + h // 2 + 3 + lh),
                      (20, 20, 200), -1)
    cv2.rectangle(img, (cx - w // 8, bottom - h // 3), (cx + w // 8, bottom - h // 3 + lh + 2),
                  (200, 200, 200), -1)


def _side(img: np.ndarray, x0: int, bottom: int, w: int, colour) -> None:
    """A car seen from the side: body, cabin, window and two wheels."""
    h, r = int(w * 0.32), max(4, int(w * 0.1))
    cv2.rectangle(img, (x0, bottom - r - h), (x0 + w, bottom - r), colour, -1)
    top = bottom - r - h
    cabin = np.array([(x0 + w // 5, top), (x0 + w * 4 // 5, top),
                      (x0 + w * 7 // 10, top - int(h * 0.9)),
                      (x0 + w * 3 // 10, top - int(h * 0.9))], np.int32)
    cv2.fillPoly(img, [cabin], colour)
    window = np.array([(x0 + w // 4, top - 2), (x0 + w * 3 // 4, top - 2),
                       (x0 + w * 27 // 40, top - int(h * 0.8)),
                       (x0 + w * 13 // 40, top - int(h * 0.8))], np.int32)
    cv2.fillPoly(img, [window], (70, 60, 50))
    for wx in (x0 + w // 5, x0 + w * 4 // 5):
        cv2.circle(img, (wx, bottom - r), r, (20, 20, 20), -1)
        cv2.circle(img, (wx, bottom - r), r // 2, (150, 150, 150), -1)


def check_scenes(n: int = CHECK_SCENES, size: tuple[int, int] = SCENE_SIZE) -> list[np.ndarray]:
    """Drawn road scenes the pinned model finds vehicles in: four cars from behind and two from
    the side each, seeded, so a scene is the same pixels on every run. No camera frame is used.
    On 2026-10-28 the CPU path found 1-4 vehicles above 0.1 in each of the first eight."""
    w, h = size
    palette = [(40, 40, 160), (200, 200, 205), (30, 30, 30), (150, 90, 20), (60, 140, 60),
               (215, 215, 220)]
    scenes = []
    for seed in range(n):
        g = np.random.default_rng(seed)
        img = np.zeros((h, w, 3), np.uint8)
        img[:] = (110, 112, 108)                                    # road
        img[: h // 3] = (200, 170, 140)                             # sky
        for x in range(0, w, 160):
            cv2.line(img, (x + 40, h // 3 + 10), (x + 40, h), (220, 220, 220), 3)
        for _ in range(4):
            cw = int(g.integers(90, 200))
            _rear(img, int(g.integers(cw // 2 + 5, w - cw // 2 - 5)),
                  int(g.integers(h // 2 + 40, h - 10)), cw, palette[int(g.integers(0, 6))])
        for _ in range(2):
            cw = int(g.integers(140, 260))
            _side(img, int(g.integers(5, w - cw - 5)), int(g.integers(h // 3 + 60, h // 2 + 40)),
                  cw, palette[int(g.integers(0, 6))])
        img = cv2.GaussianBlur(img, (0, 0), 1.0)
        scenes.append(np.clip(img + g.normal(0, 4, img.shape), 0, 255).astype(np.uint8))
    return scenes


def check_tilings(size: tuple[int, int]) -> list[list[Tile]]:
    """The whole frame as one tile, and as two overlapping ones cut in the middle, so the
    self-check covers both scales and the joining of cut boxes."""
    w, h = size
    return [[Tile(0, 0, w, h)],
            [Tile(0, 0, w * 11 // 20, h, cut_right=True),
             Tile(w * 9 // 20, 0, w, h, cut_left=True)]]


def self_check(net, cpu_net) -> tuple[float, int]:
    """Every check scene, at every check tiling, on both paths: the largest raw difference and
    the number of vehicles found alike. GpuRefused at the first difference, or when no vehicle
    was found at all (a check that sees nothing proves nothing)."""
    worst, alike = 0.0, 0
    for k, image in enumerate(check_scenes()):
        for tiles in check_tilings((image.shape[1], image.shape[0])):
            try:
                found, raw, why = compare(net, cpu_net, image, tiles)
            except GpuFailed as exc:
                raise GpuRefused(f"a self-check run failed: {exc}") from exc
            worst = max(worst, raw)
            if raw > RAW_TOLERANCE:
                raise GpuRefused(f"self-check scene {k}, {len(tiles)} tile(s): raw outputs differ "
                                 f"from the CPU's by up to {raw:.3g} (at most {RAW_TOLERANCE:g})")
            if why is not None:
                raise GpuRefused(f"self-check scene {k}, {len(tiles)} tile(s): {why}")
            alike += len(found)
    if alike == 0:
        raise GpuRefused("the self-check's scenes gave no vehicle on either path")
    return worst, alike


# --- the detector -----------------------------------------------------------------------------
@dataclass
class CrossCheck:
    """The running check of the GPU against the CPU, as records carry it."""
    every: int = CHECK_EVERY
    frames: int = 0           # frames detected on the GPU
    checked: int = 0          # of them, also detected on the CPU
    differed: int = 0         # checks whose detections or raw outputs differed
    max_raw: float = 0.0      # the largest raw difference a check saw
    recent: deque = field(default_factory=lambda: deque(maxlen=DIFFER_WINDOW))


class GpuYolox(detect.Yolox):
    """detect.Yolox on a DmlNet, with every CHECK_EVERY-th frame detected on the CPU as well.

    The GPU's boxes are the ones returned, on a checked frame too: a count is never part GPU,
    part CPU. A difference is counted and logged; DIFFER_LIMIT within DIFFER_WINDOW checks stop
    the GPU path (GpuFailed)."""

    def __init__(self, net: DmlNet, cpu_net, name: str = "yolox", every: int = CHECK_EVERY):
        super().__init__(net)
        self.cpu_net = cpu_net
        self.name = name
        self.device = f"{GPU}:{net.adapter.name}"
        self.check = CrossCheck(every)

    def detect(self, image: np.ndarray, tiles: Sequence[Tile]) -> list[Detection]:
        n = self.check.frames
        self.check.frames += 1
        if n % self.check.every:
            return super().detect(image, tiles)
        found, raw, why = compare(self.net, self.cpu_net, image, tiles)
        c = self.check
        c.checked += 1
        c.max_raw = max(c.max_raw, raw)
        if raw > RAW_TOLERANCE and why is None:
            why = f"raw outputs differ by up to {raw:.3g} (at most {RAW_TOLERANCE:g})"
        c.recent.append(why is not None)
        if why is not None:
            c.differed += 1
            log.warning("%s: YOLOX GPU and CPU differ on frame %d (check %d, %d differed so "
                        "far): %s. The GPU's boxes are kept.", self.name, n, c.checked,
                        c.differed, why)
            if sum(c.recent) >= DIFFER_LIMIT:
                raise STATE.stop(f"{self.name}: {sum(c.recent)} of the last {len(c.recent)} "
                                 f"checks against the CPU differed; the latest: {why}")
        return found

    def record(self) -> dict:
        return {"device": self.device, "cpu_checked": self.check.checked,
                "cpu_differed": self.check.differed}


# --- choosing the path ------------------------------------------------------------------------
@dataclass(frozen=True)
class Selection:
    device: str               # "cpu" or "gpu:<adapter name>": what records carry
    reason: str               # why, in a sentence
    gpu: DmlNet | None        # the GPU network, when the GPU was chosen
    cpu: object               # the CPU (OpenCV) network: the path itself, or the GPU's check


def select_device(requested: str | None = None, path: Path | None = None,
                  name: str = "yolox") -> Selection:
    """Where YOLOX runs. `requested` (else $CHISME_YOLOX_DEVICE, else "auto"):
    - "cpu": the CPU; onnxruntime is never imported;
    - "gpu": the GPU when everything above holds, else GpuRefused;
    - "auto": the same, but the CPU, with a warning saying why, where "gpu" would be refused.
    The model's sha256 is checked first, whatever the path."""
    want = requested_device(requested)
    model = detect.read_model(path)
    cpu = detect.cpu_net(model)
    if want == CPU:
        return Selection(CPU, "the CPU was asked for", None, cpu)
    try:
        if STATE.stopped is not None:
            raise GpuRefused(f"the GPU path was stopped earlier in this run: {STATE.stopped}")
        ort = import_onnxruntime()
        adapter = find_adapter(ort)
        net = DmlNet(model, adapter, ort)
        _announce(ort, adapter)
        raw, alike = self_check(net, cpu)
    except GpuRefused as exc:
        if want == GPU:
            raise
        log.warning("%s: YOLOX runs on the CPU, not the GPU: %s", name, exc)
        return Selection(CPU, f"auto, and the GPU was refused: {exc}", None, cpu)
    reason = (f"self-check passed: {alike} vehicles alike on {CHECK_SCENES} drawn scenes, "
              f"raw outputs within {raw:.2g} of the CPU's")
    log.info("%s: YOLOX runs on %s (%s)", name, f"{GPU}:{adapter.name}", reason)
    return Selection(f"{GPU}:{adapter.name}", reason, net, cpu)


def load_yolox(path: Path | None = None, device: str | None = None,
               name: str = "yolox") -> detect.Yolox:
    """detect.Yolox on the path select_device chooses: GpuYolox on the GPU, else plain Yolox on
    the CPU network. `name` (the camera) only labels the log."""
    chosen = select_device(device, path, name)
    if chosen.gpu is None:
        return detect.Yolox(chosen.cpu)
    return GpuYolox(chosen.gpu, chosen.cpu, name)


__all__ = ["CHECK_EVERY", "DEVICES", "DEVICE_ENV", "DIFFER_LIMIT", "ORT_VERSION", "Adapter",
           "CrossCheck", "DmlNet", "GpuFailed", "GpuRefused", "GpuState", "GpuYolox", "Selection",
           "check_scenes", "compare", "differences", "find_adapter", "import_onnxruntime",
           "load_yolox", "requested_device", "select_device", "self_check"]
