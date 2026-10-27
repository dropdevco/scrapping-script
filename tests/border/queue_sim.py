"""A deterministic first-in-first-out bottleneck, for the combined camera estimate's tests.

Cars reach line A at the rate `arrivals`, unless a queue holds them back, and leave through the
booths one at a time at the rate `discharge`. The road between holds a fixed number of cars when
it is jammed: `s_b` between line B and the booths, `s_ab` between A and B. A car passes a point
as soon as its free drive gets it there and the car that many places ahead of it (the cars that
fit between that point and the booths) has left the booths. That is Newell's simplified
kinematic wave with an infinite backward wave speed: a queue that reaches past a point holds
every car at it until there is room ahead. `meter`, if given, caps the rate at which cars can
cross A, as a toll or a signal before A would.

A car's `arrive` is when it would have crossed A with nothing in the way: when it joined the
back of the line. So its total wait, from joining the line to reaching a booth, is
`booth - arrive`, and while the line reaches back over A that includes its time queued before
A. The free drive from the back of the line to A is left out, as a queue that is jammed between
them has none.

Everything is deterministic. Arrivals are evenly spaced at the current rate, each moved by a
seeded jitter of up to a third of the spacing so that no two cars cross together. A counter's
capture `p` keeps evenly spread cars (every other one at 0.5), not random ones: a random miss
pattern moves each car's place at B by about sqrt(n p (1 - p)), and at p 0.5 that is several
cars within half an hour, more than anchors are allowed to disagree by. Times are cut to the
2 fps frames. About 1 car in 10 is distinctive (a vivid colour anywhere on the wheel), as in
test_border_camera_transit's wheel(); each line reads every colour with a little noise.

The truth per car (`Car`) is for the tests only; the crossings carry only what a counter gives.
"""
from __future__ import annotations

import functools
import math
import random
from bisect import bisect_left, bisect_right
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from scraper.border.camera.crossing import Crossing, look

T0 = datetime(2026, 10, 26, 14, 0, tzinfo=UTC)
FRAME_S = 0.5               # both counters read 2 frames a second
HEADWAY_S = 1.0             # two cars never cross one line closer together than this
WARMUP_MIN = 150.0          # the road fills before T0, so the line is in place when counting starts
AFTER_MIN = 180.0           # cars keep coming after the end: a car joining at the end has a truth
JITTER = 1 / 3              # an arrival moves by up to this share of the spacing, seeded

Steps = tuple[tuple[float, float], ...]   # (from minute after T0, cars per minute); the first
                                          # step also holds before T0


def rate(steps: Steps, minute: float) -> float:
    """The rate in force at `minute` after T0."""
    starts = [s for s, _ in steps]
    return steps[max(0, bisect_right(starts, minute) - 1)][1]


def frame(t_s: float) -> float:
    return round(t_s / FRAME_S) * FRAME_S


@dataclass(frozen=True)
class Car:
    """One car's truth, in seconds after T0. Tests only."""
    arrive: float               # when it would have crossed A unhindered: when it joined the line
    a: float
    b: float
    booth: float

    @property
    def a_to_b(self) -> float:
        return (self.b - self.a) / 60

    @property
    def b_to_booth(self) -> float:
        return (self.booth - self.b) / 60

    @property
    def before_a(self) -> float:
        return (self.a - self.arrive) / 60

    @property
    def total(self) -> float:
        return (self.booth - self.arrive) / 60


@dataclass(frozen=True)
class Run:
    a: tuple[Crossing, ...]     # what line A's counter gave, in [T0, T0 + minutes)
    b: tuple[Crossing, ...]
    cars: tuple[Car, ...]       # every car, warm-up included, in order
    minutes: float

    @staticmethod
    def at(minute: float) -> datetime:
        return T0 + timedelta(minutes=minute)

    def _s(self, at: datetime) -> float:
        return (at - T0).total_seconds()

    def crossed_a(self, at: datetime) -> Car:
        """The last car to cross A at or before `at`."""
        k = bisect_right([c.a for c in self.cars], self._s(at)) - 1
        return self.cars[k]

    def joining(self, at: datetime) -> Car:
        """The first car to join the line at or after `at`."""
        k = bisect_left([c.arrive for c in self.cars], self._s(at))
        return self.cars[k]

    def crossing_b(self, at: datetime) -> Car:
        """The first car to cross B at or after `at`."""
        k = bisect_left([c.b for c in self.cars], self._s(at))
        return self.cars[k]

    def between(self, at: datetime) -> int:
        """Cars that crossed A before `at` and B at or after it."""
        t = self._s(at)
        return sum(1 for c in self.cars if c.a < t <= c.b)

    def before_a(self, at: datetime) -> int:
        """Cars in the line before A at `at`: joined it, not yet across A."""
        t = self._s(at)
        return sum(1 for c in self.cars if c.arrive <= t < c.a)


def kept(i: int, p: float) -> bool:
    """Whether a counter with capture `p` counts car `i`: evenly spread, every 1/p-th car."""
    return math.floor((i + 1) * p) > math.floor(i * p)


@functools.cache
def run(*, minutes: float = 200.0, arrivals: Steps = ((0.0, 8.0),),
        discharge: Steps = ((0.0, 8.0),), meter: Steps | None = None,
        free_ab_min: float = 4.0, free_bb_min: float = 2.0, s_b: int = 60, s_ab: int = 200,
        p_a: float = 1.0, p_b: float = 1.0, lanes_a: int = 5, lanes_b: int = 5,
        distinctive: float = 0.10, seed: int = 1) -> Run:
    """One run, from WARMUP_MIN before T0 to `minutes` after it (see the module docstring). Cars
    keep arriving for AFTER_MIN more, for their truth only: no crossing is given past the end."""
    rng, noise = random.Random(f"{seed}/queue"), random.Random(f"{seed}/queue/noise")
    s_a = s_b + s_ab
    arrive, looks = [], []
    t = -WARMUP_MIN * 60
    while t < (minutes + AFTER_MIN) * 60:
        spacing = 60 / rate(arrivals, t / 60)
        arrive.append(t + rng.uniform(-JITTER, JITTER) * spacing)
        t += spacing
        vivid = rng.random() < distinctive
        looks.append((rng.uniform(0, 360),
                      rng.uniform(0.5, 0.9) if vivid else abs(rng.gauss(0, 0.04)),
                      rng.uniform(0.3, 0.9) if vivid else rng.choice((0.9, 0.65, 0.4, 0.1)),
                      rng.uniform(0.53, 0.67)))
    cars: list[Car] = []
    for i, r in enumerate(arrive):
        prev = cars[-1] if cars else None
        a = r
        if prev is not None:
            a = max(a, prev.a + HEADWAY_S)
            if meter is not None:
                a = max(a, prev.a + 60 / rate(meter, prev.a / 60))
        if i >= s_a:
            a = max(a, cars[i - s_a].booth)
        b = a + free_ab_min * 60
        if prev is not None:
            b = max(b, prev.b + HEADWAY_S)
        if i >= s_b:
            b = max(b, cars[i - s_b].booth)
        booth = b + free_bb_min * 60
        if prev is not None:
            booth = max(booth, prev.booth + 60 / rate(discharge, prev.booth / 60))
        cars.append(Car(r, frame(a), frame(b), booth))

    def read(hue, sat, val, width):
        h = math.radians(hue)
        return look(sat * math.cos(h) + noise.gauss(0, 0.02),
                    sat * math.sin(h) + noise.gauss(0, 0.02),
                    val + noise.gauss(0, 0.02), width + noise.gauss(0, 0.02))

    end = minutes * 60
    a_out = tuple(Crossing("pdn-north", "A", f"lane-{i % lanes_a + 1}", T0 + timedelta(seconds=c.a),
                           "loop", read(*looks[i]))
                  for i, c in enumerate(cars) if 0 <= c.a < end and kept(i, p_a))
    b_out = tuple(Crossing("city-pdn-north", "B", f"col-{i % lanes_b + 1}",
                           T0 + timedelta(seconds=c.b), "loop", read(*looks[i]))
                  for i, c in enumerate(cars) if 0 <= c.b < end and kept(i, p_b))
    return Run(a_out, b_out, tuple(cars), minutes)
