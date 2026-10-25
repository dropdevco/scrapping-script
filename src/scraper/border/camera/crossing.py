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

`signature` describes how a car looks (colour, size) so that a few distinctive cars can be
matched between the two lines as anchors. It lives in memory only, for at most
SIGNATURE_TTL_S, and is never written anywhere. No plate and no face is ever read (ADR-0014).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

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
    signature: tuple[float, ...] | None = None # in memory only, never written
    confidence: float = 1.0


class Counter(Protocol):
    method: str

    def add(self, frame: Stamped) -> list[Crossing]:
        """Process one stamped frame; return the cars that crossed in it."""

    def gap(self, gap: Gap) -> None:
        """Frames were missed: drop any per-car state that spans the gap."""


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
