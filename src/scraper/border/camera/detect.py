"""The vision-model counter: YOLOX-S boxes, tracked from frame to frame and counted across a line.

ADR-0014 runs two counters side by side on the same stamped frames. This is the model one; the
deterministic loop on the lane strip is `loop.LoopCounter`. Neither is trusted until both have
been compared with hand counts.

The model is Megvii's YOLOX-S, `yolox_s.onnx` as published on the 0.1.1rc0 release page
(Apache-2.0). It is pinned by its sha256: any other file is refused, nothing is ever downloaded,
and it is never trained on our data. OpenCV's DNN module runs it on the CPU. On 2026-10-24 on
this machine (i7-13620H), one 640x640 pass took 305 ms on 1 thread, 108 ms on 4 and 70 ms on
16. It used 0.3 CPU-s of real work, and up to 0.86 CPU-s at 16 threads, where the extra is
threads waiting. The output was bit-identical across OpenCV's two DNN engines and across 1-16
threads.

By the owner's decision of 2026-10-27 the same model runs by default on the NVIDIA GPU, through
ONNX Runtime's DirectML provider, wherever that path is installed and passes its checks (gpu.py:
5.4 ms and 0.006 CPU-s a pass), and on the CPU otherwise, or when asked ($CHISME_YOLOX_DEVICE).
The two paths gave the same boxes on every frame measured, but their raw outputs are not
bit-identical, so a box on a threshold can differ: `Yolox.device` says which path made a count,
and records of the two are never graded together.

Which preprocessing these weights need was settled by measurement, not by the tag's name. The
source tagged 0.1.1rc0 feeds the model RGB, divided by 255, with the ImageNet mean taken off and
divided by the std. On 2 x 28 live bridgepdn1 frames (2026-10-24, evening) that gave no box
above 0.1. Raw BGR 0-255 gave 10-19 vehicles a frame; raw RGB gave fewer, with lower scores.
The weights now on the 0.1.1rc0 release page are
the current generation: YOLOX's README links them there at 40.5 mAP, and keeps the older
"legacy" weights in Megvii-BaseDetection/storage, release 0.0.1. Current YOLOX's ValTransform
applies the old normalisation only when `legacy=True`. So this module follows YOLOX's current
ONNX demo (main branch, read 2026-10-24) line for line:
- demo/ONNXRuntime/onnx_inference.py calls `preproc(img, (640, 640))`, with no mean or std;
- yolox/data/data_augment.py `preproc`: a uint8 640x640 canvas of 114, the image scaled by
  r = min(640 / h, 640 / w) with INTER_LINEAR into its top-left corner, channels left as BGR,
  values left at 0-255, HWC to CHW, float32;
- yolox/utils/demo_utils.py `demo_postprocess`: grids for strides 8, 16, 32 (no P6), x-major
  within a row, centre = (offset + cell) x stride, size = exp(raw) x stride;
- the demo's score is objectness x class probability. Boxes go from centre/size to corners and
  are divided by r. `multiclass_nms(..., nms_thr=0.45, score_thr=0.1)` is class-agnostic by
  default: each anchor takes its best of the 80 classes, keeps it if the score is above 0.1, and
  `nms` drops a box whose IoU with a better one is above 0.45. Areas count both edge pixels:
  (x2 - x1 + 1) x (y2 - y1 + 1).
Only car, motorcycle, bus and truck boxes are kept, after that NMS. One departure: equal scores
are ordered by a stable sort, lowest index first, where the demo's `argsort()[::-1]` leaves that
order to numpy's sort.

Small cars get more pixels: the model sees only the region around the camera's count zones,
not the whole frame. That region is cut into overlapping tiles when it is much wider than tall
and its cars would otherwise be small in the 640x640 input. At pdn-north a car 1.6-2.0 m wide is
52-66 px wide at 1280x720 where it comes into the picture and 45-57 px at line A, two thirds of
that at 854x480 (through its ground rectangle, whose scale is unverified: ASSUMPTION), so tiles
magnify them. Its drawn line A gives a region of
852 x 293 px at 1280x720 and 568 x 195 px at 854x480, each cut into two tiles, scaled 1.28x
and 1.92x respectively, so a lane is about 127 px wide in the model's input at either size
(cameras.json through `plan_tiles`, 2026-10-26). At bridgepdn1 the cars are 150-400 px wide
near the camera, already large at half scale, so one pass is enough. Each pass costs the
same, so tiles are the price. On 2026-10-24 at 4 threads, a frame took 102 ms wall
and 0.34 CPU-s with one pass, and 205-251 ms and 0.69-0.78 CPU-s with two. Two streams at
2 fps keep up with one pass each, or one pass plus two. A camera with no count zones never runs
the model: it has nothing to count.

Tracking is greedy, deterministic association (ByteTrack's high and low score passes, without
its Kalman filter). Each track predicts its next box with a motion model that is exact for a car
driving straight at constant speed through a pinhole camera (see `Track`), and a box is matched
only near that prediction and at nearly its width. A track with no motion yet may go forward
along the lane, in the direction the zones give. A track is counted once per line, when the
bottom centre of its box (where a car seen from behind meets the road) leaves a zone across the
far edge, having come in across the near edge.

How far to trust it, by an exact-box simulation (2026-10-24; 3 seeds x 5 min at 2 fps; 4.5 x 1.8
x 1.5 m cars through pinhole cameras matched to bridgepdn1 and pdn-north, and the rendered lane
of tests/border/camera_scene.py; boxes clipped to the model's region). With a mildly noisy
detector (1 box in 10 missed, 1.5 % jitter, scores 0.2-0.9) and the zone drawn by the rule
below, it missed at most 2 cars in any case from a crawl to 8 m/s (66-435 cars each) and 4-6 %
at 12 m/s, and counted 1 car twice in 6810. The first version missed 3-22 % at 8 m/s and
28-50 % at 12 m/s. With 1 box in 4 missed it misses 7-12 % at 8 m/s and 22-28 % at 12 m/s. So
the yolox count is not to be trusted above 8 m/s (29 km/h), nor at a zone that breaks the rule.
In a creeping queue it can count cars twice, and lose others. Rebuilt on 2026-10-26 with the
test suite's `drive` (0.5 m/s for 5 minutes, seeds 11-13, 66 cars that must be counted; zones
25-29 m out at pdn-north's pinhole, boxes 37-43 px at 854x480, and 30-34 m out at bridgepdn1's,
49-55 px). Among those 66, with 1 box in 4 missed, 2 % jitter and scores 0.15-0.9: 0 twice
and 2 lost at pdn-north, 1 twice and 2 lost at bridgepdn1. Where boxes jitter by 1.5 px
whatever their size (the suite's Road with jitter_px), with 1 box in 10 missed and scores
0.2-0.9: 9 twice and 18 lost at pdn-north (14 % short), 5 twice and 11 lost at bridgepdn1 (9 %
short). Cars the run allows but does not require added 0-3 counts on top. In that queue the
car ahead is about 0.4 of a box further on, and 1.5 px of jitter is about twice a frame's
creep (0.8 px). How much real YOLOX boxes jitter has not been measured. Hand counts have to
settle both before a transit is built on this count.

The rule for drawing zones: at the highest speed expected, a car must be seen whole, inside the
model's region, in at least 3 frames before it reaches a zone's near edge. When 1 frame in 3
gives no sure box, 95 % of cars then start a track in time. At 2 fps and 12 m/s that is 18 m of
road. At bridgepdn1 a zone 12-15 m out leaves about one frame, since the picture starts a few
metres from the pole: exact boxes were all counted there, but the mildly noisy detector lost
5 % of cars at 5 m/s, 18 % at 8 m/s and 32 % at 12 m/s. A zone 30-34 m out lost 1 in 435 at
8 m/s and 5 % at 12 m/s.

pdn-north's line A as drawn breaks the rule above about 6 m/s: it has about 9.3 m of road in
view below its near edge (nominal metres through the unverified ground rectangle). The same
detector, seed 11 for 3 minutes (`drive`'s defaults), a 1.5 m zone 9.1 m past where the frame's
bottom meets a car's rear (about 16.5-18.0 m from the pole), lost 1 of 68 cars at 4 m/s, 2 of 80
at 6, 8 of 87 at 8 and 19 of 95 at 12; with 18 m in view, 0, 0, 0 and 2 of 96. Over 3 seeds x
5 minutes the 9.1 m zone lost 30 of 435 at 8 m/s and 105 of 477 at 12 (2026-10-26). So the yolox
count at line A is short in free flow, and a transit reads that as drift, until line A is
redrawn (cameras.json's notes; free-flow speeds there are unmeasured: ASSUMPTION).

Privacy (ADR-0014 decision 4): frames are analysed in memory and never written. A crossing's
colour comes from the upper middle of the box, downsampled to 4 x 4 pixels before any statistic
is taken. A rear plate sits in the lower half of a car seen from behind, and no driver's face is
visible from behind. No plate or face is ever cropped or read. Tracks and signatures live in
memory only, for at most SIGNATURE_TTL_S, and are never logged or written.
"""
from __future__ import annotations

import hashlib
import math
import os
from collections import deque
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np

from .calibration import Camera
from .crossing import SIGNATURE_TTL_S, Crossing, Zone, chroma, look
from .hls import Gap, Stamped

# --- the model --------------------------------------------------------------------------------
MODEL_SHA256 = "c5c2d13e59ae883e6af3b45daea64af4833a4951c92d116ec270d9ddbe998063"   # ADR-0014
MODEL_FILE = "yolox_s.onnx"
MODEL_DIR_ENV = "CHISME_MODEL_DIR"              # overrides ~/.chisme/models
INPUT_SIZE = 640                                # the exported graph is fixed at 1x3x640x640
PAD_VALUE = 114                                 # preproc's canvas grey
STRIDES = (8, 16, 32)                           # demo_postprocess without P6: 6400+1600+400 anchors
VEHICLE_CLASSES = {2: "car", 3: "motorcycle", 5: "bus", 7: "truck"}   # COCO ids
CANDIDATE_SCORE = 0.1                           # the demo's score_thr; weaker boxes are dropped
NMS_IOU = 0.45                                  # the demo's nms_thr


def default_model_path() -> Path:
    """Where the model is looked for: $CHISME_MODEL_DIR, else ~/.chisme/models."""
    base = os.environ.get(MODEL_DIR_ENV)
    return (Path(base) if base else Path.home() / ".chisme" / "models") / MODEL_FILE


class ModelRefused(RuntimeError):
    """The model file is missing, or is not the pinned YOLOX-S."""


def read_model(path: Path | None = None) -> bytes:
    """The model's bytes, after checking them against MODEL_SHA256. The same bytes that were
    hashed are the ones loaded, so the file cannot change between the check and the load."""
    path = Path(path) if path is not None else default_model_path()
    if not path.is_file():
        raise ModelRefused(
            f"no model at {path}. Put Megvii's YOLOX 0.1.1rc0 {MODEL_FILE} there (sha256 "
            f"{MODEL_SHA256}), or point {MODEL_DIR_ENV} at its folder. Nothing is downloaded.")
    data = path.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    if digest != MODEL_SHA256:
        raise ModelRefused(
            f"refusing {path}: its sha256 is {digest}, but only YOLOX-S 0.1.1rc0 "
            f"({MODEL_SHA256}) is allowed (ADR-0014).")
    return data


def cpu_net(model: bytes):
    """The model's bytes, as read_model gave them, as an OpenCV DNN network: the CPU path.
    OpenCV runs it on the CPU by default, and its new engine has no other target."""
    return cv2.dnn.readNetFromONNX(np.frombuffer(model, np.uint8))


def load_net(path: Path | None = None):
    """The pinned model as an OpenCV DNN network, on the CPU. The GPU path is gpu.py's."""
    return cpu_net(read_model(path))


# --- preprocessing and decoding, as YOLOX's ONNX demo does them ------------------------------
def preprocess(image: np.ndarray, size: int = INPUT_SIZE) -> tuple[np.ndarray, float]:
    """A BGR uint8 image as the model's 1x3xSxS input, and the scale r it was resized by.

    `preproc` line for line: still BGR, still 0-255 (see the module docstring)."""
    padded = np.ones((size, size, 3), dtype=np.uint8) * PAD_VALUE
    r = min(size / image.shape[0], size / image.shape[1])
    h, w = int(image.shape[0] * r), int(image.shape[1] * r)
    resized = cv2.resize(image, (w, h), interpolation=cv2.INTER_LINEAR).astype(np.uint8)
    padded[:h, :w] = resized
    padded = padded.transpose(2, 0, 1)
    return np.ascontiguousarray(padded, dtype=np.float32)[None], r


def _grids(size: int) -> tuple[np.ndarray, np.ndarray]:
    grids, strides = [], []
    for stride in STRIDES:
        hsize = wsize = size // stride
        xv, yv = np.meshgrid(np.arange(wsize), np.arange(hsize))
        grids.append(np.stack((xv, yv), 2).reshape(1, -1, 2))
        strides.append(np.full((1, hsize * wsize, 1), stride))
    return np.concatenate(grids, 1), np.concatenate(strides, 1)


_GRIDS = _grids(INPUT_SIZE)


def decode(output: np.ndarray, size: int = INPUT_SIZE) -> tuple[np.ndarray, np.ndarray]:
    """The raw (1, 8400, 85) output as corner boxes (N, 4) in input pixels and scores (N, 80).

    `demo_postprocess`, then the demo's own lines: score = obj x cls, centre/size to corners."""
    grids, strides = _GRIDS if size == INPUT_SIZE else _grids(size)
    outputs = np.array(output, dtype=np.float32)   # a copy: the demo decodes in place
    outputs[..., :2] = (outputs[..., :2] + grids) * strides
    outputs[..., 2:4] = np.exp(outputs[..., 2:4]) * strides
    predictions = outputs[0]
    boxes = predictions[:, :4]
    scores = predictions[:, 4:5] * predictions[:, 5:]
    boxes_xyxy = np.ones_like(boxes)
    boxes_xyxy[:, 0] = boxes[:, 0] - boxes[:, 2] / 2.
    boxes_xyxy[:, 1] = boxes[:, 1] - boxes[:, 3] / 2.
    boxes_xyxy[:, 2] = boxes[:, 0] + boxes[:, 2] / 2.
    boxes_xyxy[:, 3] = boxes[:, 1] + boxes[:, 3] / 2.
    return boxes_xyxy, scores


def nms(boxes: np.ndarray, scores: np.ndarray, nms_thr: float = NMS_IOU) -> list[int]:
    """The demo's single-class `nms`: indices kept, best first. A box is dropped when its IoU
    with a kept box is above `nms_thr`; areas count both edge pixels (the +1)."""
    if len(boxes) == 0:
        return []
    x1, y1, x2, y2 = boxes[:, 0], boxes[:, 1], boxes[:, 2], boxes[:, 3]
    areas = (x2 - x1 + 1) * (y2 - y1 + 1)
    order = np.argsort(-scores, kind="stable")  # the demo: scores.argsort()[::-1]
    keep: list[int] = []
    while order.size > 0:
        i = int(order[0])
        keep.append(i)
        xx1 = np.maximum(x1[i], x1[order[1:]])
        yy1 = np.maximum(y1[i], y1[order[1:]])
        xx2 = np.minimum(x2[i], x2[order[1:]])
        yy2 = np.minimum(y2[i], y2[order[1:]])
        w = np.maximum(0.0, xx2 - xx1 + 1)
        h = np.maximum(0.0, yy2 - yy1 + 1)
        inter = w * h
        ovr = inter / (areas[i] + areas[order[1:]] - inter)
        inds = np.where(ovr <= nms_thr)[0]
        order = order[inds + 1]
    return keep


@dataclass(frozen=True)
class Detection:
    box: tuple[float, float, float, float]      # x1, y1, x2, y2 in frame pixels
    score: float                                # objectness x class probability
    cls: int                                    # COCO class id, one of VEHICLE_CLASSES


def vehicles(boxes: np.ndarray, scores: np.ndarray, score_thr: float = CANDIDATE_SCORE,
             nms_thr: float = NMS_IOU) -> list[Detection]:
    """Vehicle boxes, best first: the demo's class-agnostic `multiclass_nms` (best of the 80
    classes per anchor, above score_thr, one NMS for all), then only VEHICLE_CLASSES."""
    if len(boxes) == 0:
        return []
    cls_inds = scores.argmax(1)
    cls_scores = scores[np.arange(len(cls_inds)), cls_inds]
    mask = cls_scores > score_thr               # the demo's strict comparison
    boxes, cls_inds, cls_scores = boxes[mask], cls_inds[mask], cls_scores[mask]
    return [Detection(tuple(float(c) for c in boxes[i]), float(cls_scores[i]), int(cls_inds[i]))
            for i in nms(boxes, cls_scores, nms_thr) if int(cls_inds[i]) in VEHICLE_CLASSES]


# --- where the model looks --------------------------------------------------------------------
REGION_MARGIN_LANES = 1.5    # the zones' box grows by this many lane widths on every side
TILE_ASPECT = 2.0            # a region wider than this many times its height is tiled...
LARGE_INPUT_PX = 96          # ...unless a lane is already this wide in the model's input. COCO
                             # calls objects over 96 x 96 px "large", the size detectors find
                             # best; magnifying further costs a pass and gains little
CUT_SLACK_PX = 2.0           # a box this close to a tile's inner cut is clipped by it
JOIN_ROWS = 0.5              # two parts of one vehicle cut by both tiles share this much of their
                             # rows (overlap / union of their vertical extents)


@dataclass(frozen=True)
class Tile:
    x0: int
    y0: int
    x1: int
    y1: int
    cut_left: bool = False   # the left edge cuts through the region, so boxes touching it are
    cut_right: bool = False  # partial cars that the neighbouring tile sees whole


def scaled_quads(zones: Sequence[Zone], frame_size: tuple[int, int],
                 image_size: tuple[int, int]) -> list[np.ndarray]:
    """Each zone's quad in the pixels of the frames actually read."""
    sx, sy = image_size[0] / frame_size[0], image_size[1] / frame_size[1]
    return [np.array([(x * sx, y * sy) for x, y in z.quad], dtype=np.float64) for z in zones]


def plan_tiles(quads: Sequence[np.ndarray], image_size: tuple[int, int]) -> list[Tile]:
    """The parts of the frame the model is run on.

    The zones' bounding box plus REGION_MARGIN_LANES lane widths on each side: a car must be
    seen whole before it reaches a zone and after it leaves. ASSUMPTION: a lane's width at a
    zone's near edge is about a car's size there, which makes it the right unit for both the
    margin and the tile overlap. Without zones the whole frame is used."""
    w, h = image_size
    if not quads:
        return [Tile(0, 0, w, h)]
    points = np.concatenate(quads)
    lane = float(np.median([np.linalg.norm(q[1] - q[0]) for q in quads]))
    margin = REGION_MARGIN_LANES * lane
    x0 = max(0, math.floor(points[:, 0].min() - margin))
    y0 = max(0, math.floor(points[:, 1].min() - margin))
    x1 = min(w, math.ceil(points[:, 0].max() + margin))
    y1 = min(h, math.ceil(points[:, 1].max() + margin))
    rw, rh = x1 - x0, y1 - y0
    if rw <= 0 or rh <= 0:
        return [Tile(0, 0, w, h)]
    n = max(1, math.ceil(rw / (TILE_ASPECT * rh)))
    if n == 1 or lane * INPUT_SIZE / max(rw, rh) >= LARGE_INPUT_PX:
        return [Tile(x0, y0, x1, y1)]
    overlap = min(margin, rw / n)               # most cars fit whole in one tile; Yolox.detect
                                                # joins the two parts of a wider one
    tw = math.ceil((rw + (n - 1) * overlap) / n)
    tiles = []
    for i in range(n):
        left = x0 + round(i * (rw - tw) / (n - 1))
        tiles.append(Tile(left, y0, min(x1, left + tw), y1, cut_left=i > 0, cut_right=i < n - 1))
    return tiles


class Yolox:
    """The detector: a network with setInput/forward (OpenCV DNN, gpu.DmlNet, or a stand-in in
    tests). `device` names the path its boxes come from; gpu.GpuYolox sets "gpu:<adapter>"."""

    device = "cpu"

    def __init__(self, net):
        self.net = net

    @classmethod
    def load(cls, path: Path | None = None, device: str | None = None,
             name: str = "yolox") -> Yolox:
        """The pinned model on the path gpu.select_device chooses for `device` ("auto", "gpu"
        or "cpu"; None reads $CHISME_YOLOX_DEVICE, else "auto"). `name` labels the log."""
        from .gpu import load_yolox  # here, not above: gpu.py imports this module
        return load_yolox(path, device, name)

    def record(self) -> dict:
        """What a record says about where the boxes came from."""
        return {"device": self.device}

    def infer(self, image: np.ndarray) -> list[Detection]:
        """Vehicles in one image, in its own pixels."""
        blob, r = preprocess(image)
        self.net.setInput(blob)
        boxes, scores = decode(self.net.forward())
        return vehicles(boxes / r, scores)

    def detect(self, image: np.ndarray, tiles: Sequence[Tile]) -> list[Detection]:
        """Vehicles in the tiles of a frame, in frame pixels, best first.

        A box cut by a tile's inner edge is usually a car the neighbouring tile sees whole,
        and is dropped. A vehicle wider than the overlap is cut in both tiles, so its two
        parts are joined instead: the part ending at tile i's right cut and the part starting
        at tile i + 1's left cut, when each reaches across the overlap and their rows agree."""
        found: list[Detection] = []
        ends: list[tuple[int, Detection]] = []      # cut by their tile's right edge
        starts: list[tuple[int, Detection]] = []    # cut by their tile's left edge
        for i, t in enumerate(tiles):
            for d in self.infer(image[t.y0:t.y1, t.x0:t.x1]):
                bx0, by0, bx1, by1 = d.box
                left = t.cut_left and bx0 <= CUT_SLACK_PX
                right = t.cut_right and bx1 >= (t.x1 - t.x0) - 1 - CUT_SLACK_PX
                d = Detection((bx0 + t.x0, by0 + t.y0, bx1 + t.x0, by1 + t.y0), d.score, d.cls)
                if right and not left:
                    ends.append((i, d))
                elif left and not right:
                    starts.append((i, d))
                else:                               # whole, or wider than its tile
                    found.append(d)
        for i, a in ends:
            for j, b in starts:
                if j != i + 1:
                    continue
                if a.box[0] > tiles[j].x0 + CUT_SLACK_PX or b.box[2] < tiles[i].x1 - CUT_SLACK_PX:
                    continue                        # one part is whole in the other tile
                rows = min(a.box[3], b.box[3]) - max(a.box[1], b.box[1])
                span = max(a.box[3], b.box[3]) - min(a.box[1], b.box[1])
                if span <= 0 or rows / span < JOIN_ROWS:
                    continue
                best = a if a.score >= b.score else b
                found.append(Detection((a.box[0], min(a.box[1], b.box[1]), b.box[2],
                                        max(a.box[3], b.box[3])), best.score, best.cls))
        if len(tiles) == 1:
            return found
        # A car in the overlap of two tiles is seen whole by both: keep the better box.
        boxes = np.array([d.box for d in found], dtype=np.float64).reshape(-1, 4)
        scores = np.array([d.score for d in found], dtype=np.float64)
        return [found[i] for i in nms(boxes, scores)]


# --- tracking ---------------------------------------------------------------------------------
HIGH_SCORE = 0.4             # a box this sure may start a track (ByteTrack's first pass)
LOW_SCORE = CANDIDATE_SCORE  # weaker boxes only keep an existing track alive (its second pass)
RESIDUAL_GATE = 0.3          # box sizes (sqrt of the area) a box may sit from where its track's
                             # motion puts it: box noise, a partly hidden car, a lane change
ACCEL_GATE = 0.5             # x (time unseen)^2, box sizes the gate widens by: half of 2 m/s^2,
                             # brisk for a queue, which is under 1.1 car widths per s^2 along the
                             # road wherever the camera is lower than the car is far (ASSUMPTION:
                             # true at PDN; a box size is about a car's width there)
WIDTH_GATE = 0.2             # |ln(width / predicted width)| a box may differ by, about 20 %. In a
                             # dense queue seen from a low camera the car ahead sits only 0.24-0.33
                             # box sizes further on, inside RESIDUAL_GATE, but is 18-23 % narrower
WIDTH_ACCEL_GATE = 0.1       # per s^2: WIDTH_GATE widens over the time unseen, as ACCEL_GATE does
AREA_RATIO_MAX = 2.5         # a box this much bigger or smaller in area is another vehicle
FIRST_SPEED = 3.0            # box sizes per second a track with no motion yet may go forward:
                             # 12 m/s at 12 m from a 6 m pole is about 2.2
FIRST_ACROSS = 0.5           # box sizes it may go across the lane, or back, on that first step
FIRST_SHRINK_MAX = 4.0       # its area may shrink this much on that step: going away at 12 m/s
                             # near the camera, a car's area falls 2.1-3.2x per frame at 2 fps
MAX_STEP_S = 5.0             # frames further apart than this are a gap even if nobody said so
MAX_AGE_S = 3.0              # a track unseen this long is dropped. ASSUMPTION: a car hidden
                             # behind a passing truck reappears within about 3 s
ROOM_S = 0.75                # a car keeps at least this many seconds of its speed as distance to
                             # the car ahead, centre to centre. Below 6 m/s a car's own length is
                             # more; at 12 m/s it asks for a 4.5 m gap. A track walking one car a
                             # frame at 2 fps implies two cars' spacing a second, and fails it
SAME_LANE = 0.5              # box sizes across within which a box is in the same lane
CAR_CLASS = 2                # the only class whose width is known well enough to range it by
MOTION_SMOOTHING = 0.5       # weight of the old motion when a new step is measured
CONFIRMED_STEPS = 2          # steps before a track's motion is trusted. The first comes through
                             # the loose first gate and may be the car ahead; the second replaces it
SCORE_HISTORY = 20           # detection scores kept per track for its confidence


def box_size(box: Sequence[float]) -> float:
    """sqrt of the area: the unit the position gates are in, so they hold near and far alike."""
    return math.sqrt(max(0.0, box[2] - box[0]) * max(0.0, box[3] - box[1]))


def box_centre(box: Sequence[float]) -> tuple[float, float]:
    return ((box[0] + box[2]) / 2, (box[1] + box[3]) / 2)


def box_width(box: Sequence[float]) -> float:
    return box[2] - box[0]


@dataclass
class Track:
    """One vehicle's boxes, and its motion in a form that stays constant at constant speed.

    A pinhole camera sees a car that drives straight at a constant speed narrow (or widen) so
    that 1 / width changes linearly in time, and sees its box move by an amount proportional
    to the change in width, toward the vanishing point or away from it. So `rate` (the
    centre's velocity divided by the widths at both ends of a step) and `shrink`
    (d(1 / width)/dt) are constants of that motion, and `predicted` is exact for it, near the
    camera and far. On 3-D car boxes at 2 fps it was off by at most 0.07 box sizes at 12 m/s,
    8 m from a 6 m pole, and by under 0.02 beyond 20 m. A constant velocity in pixels is not:
    near the camera the picture of a car slows as the car goes away, so a velocity from one
    step overshoots the next by up to half, and a box predicted at its old size fails the size
    gate a frame later (review of 2026-10-24: 80 of 160 cars lost at 12 m/s with exact boxes).
    Width, not area, carries the scale: a car seen from behind keeps its width when the car
    behind hides its lower part, and the height of a flat or distant box is a few pixels.

    A step to or from a box that touches the edge of what the detector saw (`truncated`)
    teaches no motion: such a box grows as the car comes into view, not as it moves."""
    id: int
    box: tuple[float, float, float, float]
    born: datetime
    seen: datetime                              # capture time of its last box
    rate: tuple[float, float] = (0.0, 0.0)      # centre velocity / (width before x after), 1/(px s)
    shrink: float = 0.0                         # d(1 / width)/dt, 1/(px s)
    steps: int = 0                              # steps its motion is learnt from; 0 = none yet
    aspect: float = 1.0                         # height / width, smoothed over its boxes
    truncated: bool = False                     # its last box touches the edge of what was seen
    scores: deque = field(default_factory=lambda: deque(maxlen=SCORE_HISTORY))
    classes: dict = field(default_factory=dict) # class id -> boxes of that class
    armed: set = field(default_factory=set)     # lines it entered across a zone's near edge
    counted: set = field(default_factory=set)   # lines it has been counted at

    @property
    def point(self) -> tuple[float, float]:
        """Bottom centre: where a car seen from behind meets the road."""
        return ((self.box[0] + self.box[2]) / 2, self.box[3])

    @property
    def cls(self) -> int:
        return min(self.classes, key=lambda c: (-self.classes[c], c))

    def predicted(self, at: datetime) -> tuple[float, float, float, float]:
        """Where its box should be at `at`, by its motion so far (its last box if none)."""
        dt = max(0.0, (at - self.seen).total_seconds())
        w1 = box_width(self.box)
        if w1 <= 0 or self.steps == 0:
            return self.box
        u = 1 / w1 + self.shrink * dt
        limit = math.sqrt(AREA_RATIO_MAX)       # a width the size gates would refuse anyway
        w2 = min(max(1 / u if u > 0 else w1 * limit, w1 / limit), w1 * limit)
        cx, cy = box_centre(self.box)
        cx, cy = cx + self.rate[0] * w1 * w2 * dt, cy + self.rate[1] * w1 * w2 * dt
        return (cx - w2 / 2, cy - w2 * self.aspect / 2, cx + w2 / 2, cy + w2 * self.aspect / 2)

    def update(self, d: Detection, at: datetime, truncated: bool = False) -> None:
        dt = (at - self.seen).total_seconds()
        w0, w1 = box_width(self.box), box_width(d.box)
        if dt > 0 and w0 > 0 and w1 > 0 and not (truncated or self.truncated):
            (cx0, cy0), (cx1, cy1) = box_centre(self.box), box_centre(d.box)
            k = MOTION_SMOOTHING if self.steps >= CONFIRMED_STEPS else 0.0
            self.rate = (k * self.rate[0] + (1 - k) * (cx1 - cx0) / (dt * w0 * w1),
                         k * self.rate[1] + (1 - k) * (cy1 - cy0) / (dt * w0 * w1))
            self.shrink = k * self.shrink + (1 - k) * (1 / w1 - 1 / w0) / dt
            self.steps += 1
        if w1 > 0 and not truncated:
            self.aspect = (MOTION_SMOOTHING * self.aspect
                           + (1 - MOTION_SMOOTHING) * (d.box[3] - d.box[1]) / w1)
        self.box, self.seen, self.truncated = d.box, at, truncated
        self.scores.append(d.score)
        self.classes[d.cls] = self.classes.get(d.cls, 0) + 1


Heading = Callable[[tuple[float, float]], "tuple[float, float] | None"]


class Tracker:
    """Greedy association frame to frame. The same boxes in the same order give the same
    tracks: every tie is broken by cost, then track id, then the box's place in the list.

    A track with motion is matched only near where that motion puts it (RESIDUAL_GATE), at
    nearly the width it predicts (WIDTH_GATE), both widening with the time unseen. A track
    with no motion yet may go forward along the lane, as `heading` gives the direction of
    travel at an image point, up to FIRST_SPEED, and shrink as a car going away does; without
    a heading that first gate is a circle. A track whose motion rests on one step may still
    be matched as if it had none, and its second step replaces that motion.

    In a slow queue the car ahead is where a fast car would be a frame later. A track that
    took its box would learn a speed of one car a frame and walk on, counting cars already
    counted (review of 2026-10-24: up to 15 extra counts per 45 cars). Three rules make it rare:
    the size gate (the car ahead is narrower), the second step that replaces a first one, and
    `_too_fast`: no step may imply a speed that leaves no room behind the next car ahead."""

    def __init__(self, heading: Heading | None = None,
                 bounds: tuple[float, float, float, float] | None = None) -> None:
        self.tracks: list[Track] = []
        self._next_id = 1
        self.heading = heading
        self.bounds = bounds                    # x0, y0, x1, y1 of what the detector saw

    def clear(self) -> None:
        self.tracks = []

    def truncated(self, box: Sequence[float]) -> bool:
        """Whether a box touches the edge of what the detector saw, so the vehicle may go on
        beyond it and the box is only the part in view."""
        if self.bounds is None:
            return False
        x0, y0, x1, y1 = self.bounds
        return (box[0] <= x0 + CUT_SLACK_PX or box[1] <= y0 + CUT_SLACK_PX
                or box[2] >= x1 - 1 - CUT_SLACK_PX or box[3] >= y1 - 1 - CUT_SLACK_PX)

    def _cost(self, t: Track, d: Detection, at: datetime) -> float | None:
        """How far `d` is from where `t` should be, in box sizes; None outside the gates.

        A track whose motion rests on one step may also be matched as if it had none: that
        step came through the loose first gate and may have been the car ahead."""
        if t.steps >= CONFIRMED_STEPS:
            return self._moving_cost(t, d, at)
        first = self._first_cost(t, d, at)
        if t.steps == 0:
            return first
        moving = self._moving_cost(t, d, at)
        return first if moving is None else moving if first is None else min(first, moving)

    def _moving_cost(self, t: Track, d: Detection, at: datetime) -> float | None:
        """Near where its motion puts it, at nearly the width it predicts."""
        dt = max(0.0, (at - t.seen).total_seconds())
        p = t.predicted(at)
        p_size, size = box_size(p), box_size(d.box)
        if p_size <= 0 or size <= 0:
            return None
        ratio = (size / p_size) ** 2
        if not 1 / AREA_RATIO_MAX <= ratio <= AREA_RATIO_MAX:
            return None
        if abs(math.log(box_width(d.box) / box_width(p))) > WIDTH_GATE + WIDTH_ACCEL_GATE * dt * dt:
            return None
        (px, py), (dx, dy) = box_centre(p), box_centre(d.box)
        miss = math.hypot(dx - px, dy - py) / p_size
        if miss > RESIDUAL_GATE + ACCEL_GATE * dt * dt:
            return None
        return miss + abs(math.log(ratio)) / 2

    def _first_cost(self, t: Track, d: Detection, at: datetime) -> float | None:
        """Forward along the lane from its last box, as far as FIRST_SPEED goes."""
        dt = max(0.0, (at - t.seen).total_seconds())
        b_size, size = box_size(t.box), box_size(d.box)
        if b_size <= 0 or size <= 0:
            return None
        ratio = (size / b_size) ** 2
        if not 1 / FIRST_SHRINK_MAX <= ratio <= AREA_RATIO_MAX:
            return None
        (bx, by), (dx, dy) = box_centre(t.box), box_centre(d.box)
        rx, ry = (dx - bx) / b_size, (dy - by) / b_size
        miss = math.hypot(rx, ry)
        reach = FIRST_ACROSS + FIRST_SPEED * dt
        h = self.heading(t.point) if self.heading is not None else None
        if h is None:
            if miss > reach:
                return None
        else:
            along, across = rx * h[0] + ry * h[1], abs(rx * h[1] - ry * h[0])
            if across > FIRST_ACROSS or not -FIRST_ACROSS <= along <= reach:
                return None
        return miss + abs(math.log(ratio)) / 2

    def _too_fast(self, t: Track, d: Detection, at: datetime,
                  others: list[Detection]) -> bool:
        """Whether moving `t` to `d` means a speed that leaves no room behind the car ahead
        of `d` (ROOM_S). That is the step of a track that took the box of the car ahead and
        would walk on, one car a frame. Distance from the camera goes as 1 / width, so speed
        and room are both measured in it, which perspective does not distort: in pixels,
        6 m behind a car 12 m out spans as much as 22 m ahead of it. Only cars ahead are
        ranged: a bus 2.55 m wide looks 30 % nearer than it is. ASSUMPTION: cars are 1.7-2.0 m
        wide, so a car's range is good to about 10 %."""
        h = self.heading(t.point) if self.heading is not None else None
        dt = (at - t.seen).total_seconds()
        w0, w1 = box_width(t.box), box_width(d.box)
        if h is None or dt <= 0 or w0 <= 0 or w1 <= 0:
            return False
        speed = (1 / w1 - 1 / w0) / dt
        if speed <= 0:
            return False
        size = box_size(d.box)
        dx, dy = box_centre(d.box)
        for o in others:
            if o is d or o.cls != CAR_CLASS or box_width(o.box) <= 0 or self.truncated(o.box):
                continue                        # a bus or truck is wider than a car; a box at
                                                # the edge is narrower than its car
            ox, oy = box_centre(o.box)
            along = (ox - dx) * h[0] + (oy - dy) * h[1]
            across = abs((ox - dx) * h[1] - (oy - dy) * h[0]) / size
            room = 1 / box_width(o.box) - 1 / w1
            if along > 0 and across < SAME_LANE and 0 < room < speed * ROOM_S:
                return True                     # (a car ahead that looks nearer ranges nothing)
        return False

    def _pairs(self, tracks: list[Track], dets: list[tuple[int, Detection]],
               at: datetime, others: list[Detection] | None = None) -> list[tuple[Track, int]]:
        candidates = []
        for t in tracks:
            for di, d in dets:
                cost = self._cost(t, d, at)
                if cost is not None and others and self._too_fast(t, d, at, others):
                    cost = None
                if cost is not None:
                    candidates.append((cost, t.id, di, t))
        candidates.sort(key=lambda c: c[:3])
        used_t: set[int] = set()
        used_d: set[int] = set()
        pairs = []
        for _, tid, di, t in candidates:
            if tid in used_t or di in used_d:
                continue
            used_t.add(tid)
            used_d.add(di)
            pairs.append((t, di))
        return pairs

    def update(self, dets: Iterable[Detection],
               at: datetime) -> list[tuple[Track, tuple[float, float] | None]]:
        """Match one frame's boxes; return each track seen in it with its previous bottom
        centre (None for a new track).

        Sure boxes go first to tracks with motion, then to tracks without; weak boxes then
        only keep unmatched tracks alive; sure boxes left over start tracks."""
        self.tracks = [t for t in self.tracks if (at - t.seen).total_seconds() <= MAX_AGE_S
                       and (at - t.born).total_seconds() <= SIGNATURE_TTL_S]
        dets = list(dets)
        high = [(i, d) for i, d in enumerate(dets) if d.score >= HIGH_SCORE]
        low = [(i, d) for i, d in enumerate(dets) if LOW_SCORE <= d.score < HIGH_SCORE]
        seen: list[tuple[Track, tuple[float, float] | None]] = []
        sure = [d for _, d in high]
        first = self._pairs([t for t in self.tracks if t.steps], high, at, sure)
        used = {di for _, di in first}
        first += self._pairs([t for t in self.tracks if not t.steps],
                             [(i, d) for i, d in high if i not in used], at, sure)
        matched = {t.id for t, _ in first}
        second = self._pairs([t for t in self.tracks if t.id not in matched], low, at, sure)
        for t, di in sorted(first + second, key=lambda p: p[0].id):
            before = t.point
            t.update(dets[di], at, self.truncated(dets[di].box))
            seen.append((t, before))
        used = {di for _, di in first}
        for di, d in high:
            if di in used:
                continue
            w = box_width(d.box)
            t = Track(self._next_id, d.box, at, at, truncated=self.truncated(d.box),
                      aspect=(d.box[3] - d.box[1]) / w if w > 0 else 1.0)
            self._next_id += 1
            t.scores.append(d.score)
            t.classes[d.cls] = 1
            self.tracks.append(t)
            seen.append((t, None))
        return seen


# --- counting ---------------------------------------------------------------------------------
BODY_ROWS = (0.20, 0.50)     # rows of the box the colour comes from: roof, rear window and the
                             # panel below it. A rear plate sits lower, in the bottom half
BODY_COLS = (0.25, 0.75)     # the middle half across, clear of the background beside the car
SIGNATURE_SAMPLES = (4, 4)   # the body region is reduced to this many pixels before any statistic
HEADING_STEP = 0.01          # zone depths: the step along the lane a heading is measured over


class ZoneGeometry:
    """A zone in frame pixels, with the map to its own unit square: t across (0 left, 1 right),
    s along (0 at the near edge, 1 at the far edge)."""

    def __init__(self, zone: Zone, quad: np.ndarray):
        self.zone, self.quad = zone, quad
        unit = np.float32([(0, 0), (1, 0), (1, 1), (0, 1)])
        self.matrix = cv2.getPerspectiveTransform(quad.astype(np.float32), unit).astype(np.float64)
        self.inverse = cv2.getPerspectiveTransform(unit, quad.astype(np.float32)).astype(np.float64)
        centre = self.matrix @ [*quad.mean(axis=0), 1.0]
        self._front = np.sign(centre[2])        # points on the road share the quad's sign
        self.far_width = float(np.linalg.norm(quad[2] - quad[3]))

    def unit(self, point: tuple[float, float]) -> tuple[float, float] | None:
        v = self.matrix @ [point[0], point[1], 1.0]
        if np.sign(v[2]) != self._front:
            return None                         # beyond the horizon: no place on the road
        return (float(v[0] / v[2]), float(v[1] / v[2]))

    def heading(self, point: tuple[float, float]) -> tuple[float, float] | None:
        """The image direction a car at `point` travels in: toward the far edge, along the
        lane's sides as the perspective bends them."""
        u = self.unit(point)
        if u is None:
            return None
        v = self.inverse @ [u[0], u[1] + HEADING_STEP, 1.0]
        if np.sign(v[2]) != np.sign((self.inverse @ [u[0], u[1], 1.0])[2]):
            return None
        dx, dy = v[0] / v[2] - point[0], v[1] / v[2] - point[1]
        norm = math.hypot(dx, dy)
        return (float(dx / norm), float(dy / norm)) if norm > 0 else None


def _crossing(a: tuple[float, float], b: tuple[float, float], level: float) -> float | None:
    """Where the step a -> b crosses s = level moving forward, as t; None if it does not or
    crosses outside the zone's sides."""
    (t0, s0), (t1, s1) = a, b
    if not s0 < level <= s1:
        return None
    t = t0 + (level - s0) / (s1 - s0) * (t1 - t0)
    return t if 0.0 <= t <= 1.0 else None


def signature(image: np.ndarray, box: Sequence[float],
              zone_width: float) -> tuple[float, ...] | None:
    """The shared form (crossing.look): the body's colour on the colour wheel, its brightness,
    and the box width over the zone width. The class is not part of it: it travels as
    Crossing.vehicle.

    The body region is reduced to SIGNATURE_SAMPLES pixels before any statistic, so no detail
    of the car survives."""
    x0, y0, x1, y1 = (float(v) for v in box)
    w, h = x1 - x0, y1 - y0
    c0 = max(0, round(x0 + BODY_COLS[0] * w))
    c1 = min(image.shape[1], round(x0 + BODY_COLS[1] * w))
    r0 = max(0, round(y0 + BODY_ROWS[0] * h))
    r1 = min(image.shape[0], round(y0 + BODY_ROWS[1] * h))
    if c1 <= c0 or r1 <= r0 or zone_width <= 0:
        return None
    small = cv2.resize(image[r0:r1, c0:c1], SIGNATURE_SAMPLES, interpolation=cv2.INTER_AREA)
    hsv = cv2.cvtColor(small, cv2.COLOR_BGR2HSV).reshape(-1, 3).astype(np.float64)
    x, y = chroma(hsv[:, 0] * 2, hsv[:, 1] / 255)       # OpenCV hue is half the degrees
    return look(x, y, float(np.median(hsv[:, 2])) / 255, w / zone_width)


class DetectorCounter:
    """crossing.Counter by YOLOX-S boxes: detect in the zones' tiles, track, count at the lines."""

    method = "yolox"

    def __init__(self, camera: Camera, detector: Yolox | None = None,
                 zones: Sequence[Zone] | None = None, device: str | None = None):
        self.camera = camera
        self.zones = tuple(zones if zones is not None else camera.count_zones)
        w, h = camera.frame_size
        for z in self.zones:
            # A far edge no box can reach reads as a line with no cars, never as one unseen.
            # Zones are scaled with the frames read, so inside at this size is inside at all.
            if not all(0 <= x <= w and 0 <= y <= h for x, y in z.quad):
                raise ValueError(f"{camera.id} zone {z.line}/{z.lane}: its near and far edges "
                                 f"are not inside the {w}x{h} frame")
        # `device` ("auto" | "gpu" | "cpu", None for $CHISME_YOLOX_DEVICE) is used only when
        # the detector is loaded here
        self.detector = (detector if detector is not None
                         else Yolox.load(device=device, name=camera.id))
        self.tracker = Tracker(self._heading)
        self.tiles: list[Tile] = []
        self.geometry: list[ZoneGeometry] = []
        self.lines: list[str] = sorted({z.line for z in self.zones})
        self._size: tuple[int, int] | None = None
        self._last_at: datetime | None = None
        self._gaps: list[Gap] = []

    @property
    def device(self) -> str:
        """The path this counter's boxes come from: "cpu", or "gpu:<adapter>" (gpu.py)."""
        return self.device_record()["device"]

    def device_record(self) -> dict:
        """What its records say about that path: {"device": ...}, and on the GPU how many
        frames were also checked on the CPU and how many of those differed (gpu.GpuYolox)."""
        record = getattr(self.detector, "record", None)
        return dict(record()) if callable(record) else {"device": Yolox.device}

    def _setup(self, size: tuple[int, int]) -> None:
        quads = scaled_quads(self.zones, self.camera.frame_size, size)
        self.geometry = [ZoneGeometry(z, q) for z, q in zip(self.zones, quads, strict=True)]
        self.tiles = plan_tiles(quads, size)
        self.tracker.bounds = (min(t.x0 for t in self.tiles), min(t.y0 for t in self.tiles),
                               max(t.x1 for t in self.tiles), max(t.y1 for t in self.tiles))
        self._size = size
        self.tracker.clear()                    # boxes from another size are not comparable

    def _heading(self, point: tuple[float, float]) -> tuple[float, float] | None:
        """Direction of travel at `point`, from the zone whose lane it is in or nearest to."""
        best = None
        for i, zg in enumerate(self.geometry):
            u = zg.unit(point)
            if u is not None:
                outside = max(0.0, -u[0], u[0] - 1.0)
                if best is None or (outside, i) < best[:2]:
                    best = (outside, i, zg)
        return best[2].heading(point) if best is not None else None

    def add(self, frame: Stamped) -> list[Crossing]:
        if not self.lines:
            return []                           # nothing to count at: never run the model
        if self._last_at is not None:
            step = (frame.at - self._last_at).total_seconds()
            if step <= 0 or step > MAX_STEP_S:
                # Frames this far apart (or out of order) were not all seen: say so, as the loop
                # counter does, so the line is not read as seen with no cars.
                self._gaps.append(Gap(self._last_at, max(step, 0.0),
                                      f"frames {step:+.1f} s apart"))
                self.tracker.clear()
        self._last_at = frame.at
        size = (frame.image.shape[1], frame.image.shape[0])
        if size != self._size:
            self._setup(size)
        dets = self.detector.detect(frame.image, self.tiles)
        crossings = []
        for track, before in self.tracker.update(dets, frame.at):
            if before is None:
                continue
            for zg in self._count(track, before, track.point):
                crossings.append(Crossing(
                    self.camera.id, zg.zone.line, zg.zone.lane, frame.at, self.method,
                    signature(frame.image, track.box, zg.far_width),
                    round(float(np.median(track.scores)), 3), VEHICLE_CLASSES.get(track.cls)))
        return crossings

    def _count(self, track: Track, a: tuple[float, float],
               b: tuple[float, float]) -> list[ZoneGeometry]:
        """The zones (at most one per line) whose far edge this step took the track across."""
        out = []
        for line in self.lines:
            if line in track.counted:
                continue
            entered = line in track.armed
            exits = []
            for i, zg in enumerate(self.geometry):
                if zg.zone.line != line:
                    continue
                ua, ub = zg.unit(a), zg.unit(b)
                if ua is None or ub is None:
                    continue
                if not entered and _crossing(ua, ub, 0.0) is not None:
                    entered = True
                t = _crossing(ua, ub, 1.0)
                if t is not None:
                    exits.append((abs(t - 0.5), i, zg))   # the zone it left most squarely
            if entered:
                track.armed.add(line)
            if entered and exits:
                track.counted.add(line)
                out.append(min(exits, key=lambda e: e[:2])[2])
        return out

    def gap(self, gap: Gap) -> None:
        """Frames were missed: no track survives across them."""
        self.tracker.clear()

    def gaps(self) -> list[Gap]:
        """Gaps this counter found itself (crossing.Counter), drained on each call."""
        out, self._gaps = self._gaps, []
        return out


__all__ = ["CANDIDATE_SCORE", "MODEL_SHA256", "Detection", "DetectorCounter", "ModelRefused",
           "Tile", "Track", "Tracker", "Yolox", "cpu_net", "decode", "default_model_path",
           "load_net", "nms", "plan_tiles", "preprocess", "read_model", "signature", "vehicles"]
