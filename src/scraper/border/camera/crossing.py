"""Counting cars across a line: the shapes both counters share (ADR-0014).

A count zone is a short band across one lane, given as four image points at the camera's
`frame_size`, in this order: near-left, near-right, far-right, far-left. "Near" is where cars come
from and "far" is where they go, so a car crosses the zone from its near edge to its far edge. At
PDN both cameras look along the traffic, so far is higher in the picture. The zones of one camera
that share a `line` name ("A", "B") together make that camera's counting line.

Two counters implement `Counter` and run on the same stamped frames:
- `loop.LoopCounter`, deterministic: a virtual loop detector on the zone;
- `detect.DetectorCounter`, which tracks YOLOX-S boxes.

Each returns a `Crossing` when a car leaves the zone across its far edge. A crossing carries the
capture time of the frame where that was seen (hls.Stamped.at), never an arrival time.

`signature` describes how a car looks, so that a few distinctive cars can be matched between the
two lines as anchors. Both counters use one form, built by `look()`:

    (saturation x cos hue, saturation x sin hue, value, width share)

Every part is on a 0-1 scale; the first two run -1 to 1. The first two place the body's colour
on a colour wheel: hue is the angle, and how coloured the car is gives the distance from the
centre. Grey, white and black cars, whose hue is noise, sit together near (0, 0), and a red car
reads the same at 359 degrees as at 1. transit compares the first three parts only
(transit.colour_distance): the width share is a share of each zone's width, and the two cameras
measure that differently. The vehicle class, where a counter knows it, is kept apart in
`vehicle`, and transit never matches a truck to a car on colour alone (transit._same_class).

Signatures live in memory only, for at most SIGNATURE_TTL_S, and are never written anywhere. No
plate and no face is ever read (ADR-0014).
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

import numpy as np

from .hls import Gap, Stamped

SIGNATURE_TTL_S = 3 * 3600      # the longest believable wait; per-vehicle data is dropped after it


@dataclass(frozen=True)
class Zone:
    camera: str
    line: str                                  # "A", "B": zones with one name form one line
    lane: str
    quad: tuple[tuple[float, float], ...]      # near-left, near-right, far-right, far-left
    lane_type: str | None = None               # general | ready | sentri; None until confirmed


@dataclass(frozen=True)
class Crossing:
    camera: str
    line: str
    lane: str
    at: datetime                               # capture time, UTC
    method: str                                # "loop" | "yolox"
    signature: tuple[float, ...] | None = None # in memory only, never written; see look()
    confidence: float = 1.0
    vehicle: str | None = None                 # car | truck | bus | motorcycle, when known


def look(chroma_x: float, chroma_y: float, value: float,
         width_share: float) -> tuple[float, ...] | None:
    """The shared signature form (module docstring), clamped to its ranges and rounded. None,
    no signature, when a part is not a finite number: min and max pass a NaN through, and transit
    refuses every crossing of a method while one component lies off the scale."""
    x, y, v, w = parts = (float(chroma_x), float(chroma_y), float(value), float(width_share))
    if not all(math.isfinite(p) for p in parts):
        return None

    def clamp(p: float, lo: float, hi: float) -> float:
        return round(min(max(p, lo), hi), 3)
    return clamp(x, -1.0, 1.0), clamp(y, -1.0, 1.0), clamp(v, 0.0, 1.0), clamp(w, 0.0, 1.0)


def chroma(hue_deg, saturation) -> tuple[float, float]:
    """Mean colour-wheel position of pixels: hue in degrees, saturation 0-1 (arrays or floats)."""
    h = np.radians(np.asarray(hue_deg, dtype=np.float64))
    s = np.asarray(saturation, dtype=np.float64)
    return float(np.mean(s * np.cos(h))), float(np.mean(s * np.sin(h)))


class Counter(Protocol):
    method: str

    def add(self, frame: Stamped) -> list[Crossing]:
        """Process one stamped frame; return the cars that crossed in it."""

    def gap(self, gap: Gap) -> None:
        """Frames were missed: drop any per-car state that spans the gap."""

    def gaps(self) -> list[Gap]:
        """Gaps the counter found in the frames themselves (a stamp jump), drained on each call,
        so the transit maths cuts there too. A counter may return []. A zone still learning its
        road is not one of them: LoopCounter.states() shows it, and the caller must cut there as
        well (runner._learning), or the learning time reads as time with no cars."""


def zones_from_raw(camera_id: str, raw: list[dict]) -> tuple[Zone, ...]:
    zones = []
    for z in raw:
        quad = tuple(tuple(float(v) for v in p) for p in z["quad"])
        if len(quad) != 4:
            raise ValueError(f"{camera_id} zone {z.get('lane')}: a zone needs exactly four points")
        if z.get("lane_type") not in (None, "general", "ready", "sentri"):
            raise ValueError(f"{camera_id} zone {z.get('lane')}: unknown lane_type")
        zones.append(Zone(camera_id, str(z["line"]), str(z["lane"]), quad, z.get("lane_type")))
    return tuple(zones)
