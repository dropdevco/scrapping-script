"""Occupancy and motion along one lane strip, and the queue estimate a window of them gives.

Everything here is a fixed computation over pixels, so a given sequence of frames always
produces the same estimate. Thresholds are starting values, tuned against recorded footage
and graded against timed crossings; each says what it guards against.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import cv2
import numpy as np

from .calibration import PX_PER_M, Strip

BIN_M = 1.0              # occupancy is decided per metre of lane
HIGHPASS_M = 1.0         # structure scale: smoother than this is light, not a vehicle
DIFF_LEVEL = 24          # grey levels from the empty road before a pixel counts as "something"
EDGE_LEVEL = 45          # gradient a vehicle's outline adds over the empty road's own texture
OCCUPIED_FRACTION = 0.30 # share of a metre's pixels that must differ; below it is glare or noise
QUEUED_TIME = 0.5        # a metre occupied at least half the window is queue, not passing traffic
GAP_M = 9.0              # cars queue a few metres apart; a gap longer than this ends the queue
QUEUE_DENSITY = 0.6      # share of a queue's metres that must be queued: see _queue_run()
RUN_OCCUPANCY = 0.67     # mean share of the window a queue's metres are occupied
MIN_RUN_M = 10.0         # with the booth out of view, a queue is at least two cars long
BEYOND_VIEW_M = 4.0      # a queue reaching this close to the last visible metre continues past it
UPDATE_STEP = 1.5        # grey levels per frame the reference may drift toward an empty pixel
PROFILE_SIGMA_ROWS = 4   # half a metre of smoothing along the lane
SEGMENT_M = 12.0         # correlation window: long enough to hold a car and the gap behind it
SEGMENT_STEP_M = 6.0
MAX_SPEED_M_S = 6.0      # a queue never moves faster; caps the correlation search
MIN_PEAK = 0.55          # weaker correlation than this is not the same cars moving
MIN_STRUCTURE = 2.0      # a segment with less profile contrast has nothing to track
MIN_SPEED_M_S = 0.02     # slower than this over a whole window is a stalled queue
MIN_FRAMES = 20
SCALE_SLACK = 0.25       # widening of the range when an unverified scale does not cancel


@dataclass(frozen=True)
class Image:
    """One frame's grey levels and edge strength, in image pixels.

    Both are measured on the image and only then warped into a strip. Measured on the strip
    instead, an edge far from the camera is stretched over many upsampled rows and its
    gradient per row collapses, so a car the colour of the asphalt — found only by its
    outline — vanished at range, and shrank further under a calibration of the wrong scale.
    Computed once per frame and shared by every lane the camera watches."""
    grey: np.ndarray
    edges: np.ndarray


def _gradient(g: np.ndarray) -> np.ndarray:
    return (np.abs(cv2.Sobel(g, cv2.CV_32F, 1, 0, ksize=3))
            + np.abs(cv2.Sobel(g, cv2.CV_32F, 0, 1, ksize=3)))


def image_features(frame_bgr: np.ndarray) -> Image:
    g = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2GRAY) if frame_bgr.ndim == 3 else frame_bgr
    g = cv2.GaussianBlur(g, (3, 3), 0).astype(np.float32)
    return Image(g, _gradient(g))


def strip_features(strip: Strip, image: Image) -> tuple[np.ndarray, np.ndarray]:
    """A lane's grey *structure* and edge strength, in strip pixels.

    Structure is the grey level minus a HIGHPASS_M blur of itself, taken in the strip so the
    blur is the same size in metres near the camera and far from it. At night the empty road
    has no single brightness: the headlights of whatever is queued nearby light the
    cobblestones, so the same empty Paso del Norte (Sur) street read up to 24 levels darker or
    brighter than its reference from one 20-minute round to the next, and a plain difference
    called it a queue. Removing everything smoother than a couple of metres drops what light
    does and keeps what a vehicle adds. (A blur fixed in image pixels was tried first: it was
    narrower than a car close to the camera, filtered the car's body away, and lost the queue.)
    The blur is normalised by the strip's valid area so the black border around it does not
    leak in."""
    g = strip.warp(image.grey)
    weight = strip.valid.astype(np.float32)
    sigma = HIGHPASS_M * PX_PER_M
    smooth = cv2.GaussianBlur(g * weight, (0, 0), sigma) / np.maximum(
        cv2.GaussianBlur(weight, (0, 0), sigma), 1e-3)
    return (g - smooth + 128.0) * weight, strip.warp(image.edges)


class Reference:
    """The empty road, per strip pixel: its grey level and its own edge strength.

    Bootstrapped from the per-pixel median of frames taken when the lane is clear, then
    allowed to drift only where nothing is detected, at most UPDATE_STEP a frame. That
    follows dusk and dawn without ever absorbing a queue that stands still: occupied pixels
    are never updated.
    """

    def __init__(self, grey: np.ndarray, edges: np.ndarray):
        self.image = grey.astype(np.float32)
        self.edges = edges.astype(np.float32)

    @classmethod
    def from_images(cls, strip: Strip, images: list[Image]) -> Reference:
        return cls.from_strips([strip_features(strip, image) for image in images])

    @classmethod
    def from_strips(cls, warped: list[tuple[np.ndarray, np.ndarray]]) -> Reference:
        """Per-pixel median over moments spread across time. Measured on 2026-10-19: nine
        moments 20 minutes apart gave a clean empty road for Paso del Norte northbound even
        though six of the nine were queued, because no one car sits in a pixel for long. A
        median over one 45-second clip learnt a waiting queue as road."""
        return cls(np.median(np.stack([g for g, _ in warped]).astype(np.float32), axis=0),
                   np.median(np.stack([e for _, e in warped]).astype(np.float32), axis=0))

    def update(self, g: np.ndarray, e: np.ndarray, free: np.ndarray) -> None:
        self.image[free] += np.clip(g - self.image, -UPDATE_STEP, UPDATE_STEP)[free]
        self.edges[free] += np.clip(e - self.edges, -UPDATE_STEP, UPDATE_STEP)[free]


def foreground(g: np.ndarray, e: np.ndarray, ref: Reference, valid: np.ndarray) -> np.ndarray:
    differs = np.abs(g - ref.image) > DIFF_LEVEL
    outlined = (e - ref.edges) > EDGE_LEVEL
    return (differs | outlined) & valid


def _bins(n_rows: int) -> list[tuple[int, int]]:
    size = round(BIN_M * PX_PER_M)
    return [(r, min(r + size, n_rows)) for r in range(0, n_rows, size)]


def occupancy(fg: np.ndarray, valid: np.ndarray) -> np.ndarray:
    """Per metre: True when enough of the visible pixels hold something."""
    out = np.zeros(len(_bins(fg.shape[0])), bool)
    for i, (a, b) in enumerate(_bins(fg.shape[0])):
        seen = valid[a:b].sum()
        out[i] = seen > 0 and fg[a:b].sum() / seen >= OCCUPIED_FRACTION
    return out


def profile(g: np.ndarray, ref: Reference, valid: np.ndarray) -> np.ndarray:
    """The strip's signature along its length: each row's mean departure from the empty road.

    Subtracting the reference removes lane markings and patches, which would otherwise
    anchor every correlation at zero shift and make a moving queue look parked."""
    diff = np.where(valid, g - ref.image, 0.0)
    counts = valid.sum(axis=1)
    rows = np.divide(diff.sum(axis=1), counts, out=np.zeros(len(counts)), where=counts > 0)
    # Smoothed along the lane: the gradient step below reads noise as slope otherwise, and
    # that shrinks every far-range speed toward zero (errors in variables). Measured on
    # rendered single cars: 0.70 of true speed at 25 m from the booth unsmoothed, 1.01 with.
    return cv2.GaussianBlur(rows.reshape(-1, 1), (1, 0), PROFILE_SIGMA_ROWS).ravel()


def _shift(cur: np.ndarray, prev: np.ndarray, a: int, b: int, k: int) -> float | None:
    """Rows the content of prev[a:b] moved toward row 0 to become cur[a:b], or None."""
    seg = cur[a:b]
    if seg.std() < MIN_STRUCTURE:
        return None
    scores = {}
    for d in range(-k, k + 1):
        lo, hi = a + d, b + d
        if lo < 0 or hi > len(prev):
            continue
        other = prev[lo:hi]
        if other.std() < MIN_STRUCTURE:
            continue
        scores[d] = float(np.corrcoef(seg, other)[0, 1])
    if not scores:
        return None
    best = max(scores, key=lambda d: (scores[d], -abs(d)))   # ties go to the smaller shift
    if scores[best] < MIN_PEAK:
        return None
    return _refine(seg, prev, a, b, float(best), k)


def _refine(seg: np.ndarray, prev: np.ndarray, a: int, b: int, d: float, k: int) -> float:
    """Sub-row refinement of an integer shift (1-D Lucas-Kanade).

    Near the booth end a far camera sees a metre of lane in a pixel or two, so a car that
    moved 30 cm moved less than a strip row. Integer correlation snaps that to zero and a
    moving queue reads as parked; the gradient step recovers the fraction."""
    rows = np.arange(a, b, dtype=np.float64)
    index = np.arange(len(prev), dtype=np.float64)
    start = d
    for _ in range(12):
        sampled = np.interp(rows + d, index, prev)
        slope = np.gradient((sampled + seg) / 2)   # both frames' slope: one noisy side biases
        energy = float((slope * slope).sum())
        if energy < 1e-6:
            break
        step = float(((seg - sampled) * slope).sum()) / energy
        d = float(np.clip(d + step, max(-k, start - 2), min(k, start + 2)))
        if abs(step) < 0.005:
            break
    return d


def segment_speeds(cur: np.ndarray, prev: np.ndarray, rows_valid: np.ndarray,
                   dt: float) -> list[tuple[float, float]]:
    """(segment centre in metres from the booth end, speed toward the booth in m/s)."""
    size, step = round(SEGMENT_M * PX_PER_M), round(SEGMENT_STEP_M * PX_PER_M)
    k = max(1, round(MAX_SPEED_M_S * dt * PX_PER_M))
    out = []
    for a in range(0, len(cur) - size + 1, step):
        b = a + size
        if rows_valid[a:b].mean() < 0.7:
            continue
        rows = _shift(cur, prev, a, b, k)
        if rows is not None:
            out.append(((a + b) / 2 / PX_PER_M, rows / PX_PER_M / dt))
    return out


@dataclass
class LaneEstimate:
    camera: str
    lane: str
    port_number: str
    direction: str
    state: str                    # queue | no_queue | stalled | insufficient
    frames: int
    seconds: float
    queue_m: float = 0.0          # visible queue, from the strip's booth end
    beyond_view: bool = False     # the queue continues past the last metre the camera sees
    speed_m_s: float | None = None
    wait_min: float | None = None
    low_min: float | None = None
    high_min: float | None = None
    lower_bound: bool = False     # wait_min is a floor: part of the queue is out of view
    scale_verified: bool = False
    detail: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {k: (round(v, 2) if isinstance(v, float) else v)
                for k, v in plain(self.__dict__).items()}


def plain(value):
    """numpy scalars and arrays as plain Python, so a reading always serialises to JSON.
    (np.bool_ in `beyond_view` crashed the live worker on its first line of output.)"""
    if isinstance(value, dict):
        return {k: plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [plain(v) for v in value]
    if isinstance(value, np.ndarray):
        return value.tolist()
    if isinstance(value, np.generic):
        return value.item()
    return value


def _queue_run(share: np.ndarray, seen: np.ndarray,
               booth_in_view: bool) -> tuple[int, int, float] | None:
    """(first, last, density) of the queue in this view, in metres from its booth end.

    Runs of queued metres are split wherever a gap is longer than GAP_M. A run is a queue
    only if it is dense: every real queue labelled on 2026-10-19 was at least 70 % queued
    metres along its length, every false one islands under 50 % (a vendor's cart, people in
    the street). It must also be occupied most of the time on average: real queues averaged
    0.72-1.00 of the window, false runs 0.53-0.62 (a stretch that someone crossed now and
    then, held just over QUEUED_TIME). With the booth in view the queue must start at it. With the booth out of view
    it need not: at Lerdo at 07:16 local the cars at the far end had just moved off toward the
    booth, leaving 15 clear metres at the head of the view, and requiring the queue to touch
    that edge missed a full lane. There a run must instead be at least MIN_RUN_M long, so one
    parked object is not a queue.
    """
    queued = share >= QUEUED_TIME
    runs, start, last, gap = [], -1, -1, 0.0
    for i in range(len(queued)):
        if not seen[i]:
            continue                       # out of view: neither extends nor breaks a run
        if queued[i]:
            if start < 0:
                start = i
            last, gap = i, 0.0
        elif start >= 0:
            gap += BIN_M
            if gap > GAP_M:
                runs.append((start, last))
                start, last, gap = -1, -1, 0.0
    if start >= 0:
        runs.append((start, last))
    head = int(np.flatnonzero(seen)[0]) if seen.any() else 0
    for first, last in runs:
        span = seen[first:last + 1]
        if not span.any():
            continue
        density = float(queued[first:last + 1][span].mean())
        if density < QUEUE_DENSITY or float(share[first:last + 1][span].mean()) < RUN_OCCUPANCY:
            continue
        if booth_in_view and (first - head) * BIN_M > GAP_M:
            return None                    # the nearest queue does not reach the booth
        if not booth_in_view and (last - first + 1) * BIN_M < MIN_RUN_M:
            continue
        return first, last, density
    return None


class LaneWindow:
    """Accumulates frames of one lane and turns them into one estimate."""

    def __init__(self, strip: Strip, reference: Reference, dt: float):
        self.strip, self.ref, self.dt = strip, reference, dt
        self.occupied: list[np.ndarray] = []
        self.speeds: list[list[tuple[float, float]]] = []
        self._prev: np.ndarray | None = None

    def add(self, frame: np.ndarray | Image) -> None:
        image = frame if isinstance(frame, Image) else image_features(frame)
        g, e = strip_features(self.strip, image)
        fg = foreground(g, e, self.ref, self.strip.valid)
        self.occupied.append(occupancy(fg, self.strip.valid))
        cur = profile(g, self.ref, self.strip.valid)
        if self._prev is not None:
            self.speeds.append(segment_speeds(cur, self._prev, self.strip.rows_valid, self.dt))
        self._prev = cur
        # Never let the reference learn a vehicle: grow the detection before excluding it.
        busy = cv2.dilate(fg.astype(np.uint8), np.ones((9, 9), np.uint8)) > 0
        self.ref.update(g, e, self.strip.valid & ~busy)

    def result(self) -> LaneEstimate:
        cam, lane = self.strip.camera, self.strip.lane
        base = {"camera": cam.id, "lane": lane.lane, "port_number": cam.port_number,
                "direction": cam.direction, "frames": len(self.occupied),
                "seconds": len(self.occupied) * self.dt, "scale_verified": cam.scale_verified}
        if len(self.occupied) < MIN_FRAMES:
            return LaneEstimate(state="insufficient", **base)

        bins = _bins(len(self.strip.rows_valid))
        seen = np.array([self.strip.rows_valid[a:b].mean() >= 0.5 for a, b in bins])
        share = np.mean(self.occupied, axis=0)
        visible = np.flatnonzero(seen)
        detail = {"occupied_share": [round(float(s), 2) for s in share],
                  "visible_m": [float(visible[0] * BIN_M), float((visible[-1] + 1) * BIN_M)]
                  if len(visible) else None}
        booth_distance = lane.booth_distance_m
        run = _queue_run(share, seen, booth_in_view=booth_distance == 0)
        if run is None:
            # Nothing queued at this camera. With the booth in view that is a zero wait; with
            # the booth out of view a queue could still sit between here and it.
            wait = 0.0 if booth_distance == 0 else None
            return LaneEstimate(state="no_queue", wait_min=wait, low_min=wait, high_min=wait,
                                detail=detail, **base)
        first, last, density = run
        span = seen[first:last + 1]
        detail["queue_density"] = round(density, 2)
        detail["queue_occupancy"] = round(float(share[first:last + 1][span].mean()), 2)
        detail["queue_head_m"] = first * BIN_M

        queue_m = (last + 1) * BIN_M
        beyond = bool((visible[-1] - last) * BIN_M < BEYOND_VIEW_M)
        # Only segments wholly inside the queue: at its tail, cars joining and gaps opening
        # make the profile change shape rather than move, which correlates as false motion.
        lo, hi = first * BIN_M + SEGMENT_M / 2, queue_m - SEGMENT_M / 2
        per_pair = [np.mean([v for centre, v in pair if lo <= centre <= hi])
                    for pair in self.speeds if any(lo <= centre <= hi for centre, _ in pair)]
        detail["speed_samples"] = len(per_pair)
        if len(per_pair) < MIN_FRAMES // 2:
            return LaneEstimate(state="insufficient", queue_m=queue_m, beyond_view=beyond,
                                detail=detail, **base)

        speed = float(np.mean(per_pair))
        quarters = [float(np.mean(q)) for q in np.array_split(np.array(per_pair), 4) if len(q)]
        detail["quarter_speeds"] = [round(q, 3) for q in quarters]
        common = {"queue_m": queue_m, "beyond_view": beyond, "speed_m_s": speed,
                  "detail": detail, **base}
        if speed < MIN_SPEED_M_S:
            return LaneEstimate(state="stalled", lower_bound=True, **common)
        if booth_distance is None:
            return LaneEstimate(state="queue", **common)    # speed and length, but no wait

        distance = booth_distance + queue_m
        wait = distance / speed / 60
        fastest = max(max(quarters), MIN_SPEED_M_S)
        slowest = max(min(quarters), MIN_SPEED_M_S)
        low, high = distance / fastest / 60, distance / slowest / 60
        if booth_distance > 0 and not cam.scale_verified:
            low, high = low / (1 + SCALE_SLACK), high * (1 + SCALE_SLACK)
        if beyond:
            low, high = wait, None
        return LaneEstimate(state="queue", wait_min=wait, low_min=min(low, wait),
                            high_min=None if high is None else max(high, wait),
                            lower_bound=beyond, **common)


ROLLING_SECONDS = 900    # stop-and-go: a one-minute window can fall entirely inside a stop


def rolling(history: list[tuple[float, LaneEstimate]], now: float,
            booth_distance_m: float | None) -> dict:
    """Speed and wait over the last ROLLING_SECONDS of windows from one lane.

    At Paso del Norte at 04:36 local on 2026-10-19 the queue sat still for 15 s, advanced a
    full car length in the next 15, and stopped again; consecutive minute-long windows read
    anywhere from 0.00 to 0.23 m/s. Each window's speed is weighted by its length, stalled
    windows count as the zero they measured, and windows with no queue are left out (they
    say nothing about how fast a queue moves). The queue length is the latest window's."""
    recent = [(t, e) for t, e in history if now - t <= ROLLING_SECONDS
              and e.state in ("queue", "stalled") and e.speed_m_s is not None]
    if not recent:
        return {"speed_m_s": None, "wait_min": None, "windows": 0}
    seconds = sum(e.seconds for _, e in recent)
    speed = max(0.0, sum(e.speed_m_s * e.seconds for _, e in recent) / seconds)
    latest = max(recent, key=lambda item: item[0])[1]
    wait = None
    if booth_distance_m is not None and speed >= MIN_SPEED_M_S:
        wait = (booth_distance_m + latest.queue_m) / speed / 60
    return plain({"speed_m_s": float(speed), "wait_min": wait, "windows": len(recent),
                  "seconds": float(seconds), "lower_bound": latest.beyond_view})
