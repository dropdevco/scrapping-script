"""Transit maths against a simulated first-in-first-out stretch whose every car is known.

Cars arrive at line A as a seeded Poisson stream (6 a minute) and reach line B after a transit
that drifts slowly between 20 and 90 minutes, never falling faster than time passes, so the order
holds. Each car has a colour: about 3 in 4 white, silver, grey or black, 15 % dark red or dark
blue, and the rest one of six vivid colours (ADR-0014's assumption). Each camera reads that
colour with its own noise, and the City camera with a white-balance shift as well, so cars of one
colour are easily confused. Counters can miss a car or count it twice, at a rate per line, and a
line can stop reading for a while (a Gap).

Times are quantised to the 2 fps frames. The simulation is seeded, so every run sees the same
cars. The drift numbers are printed (pytest -s) as well as checked.
"""
from __future__ import annotations

import functools
import itertools
import json
import math
import random
import unittest
from collections import defaultdict
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import ClassVar

import pytest

pytest.importorskip("cv2")   # crossing imports hls, which imports the stream reader

from scraper.border.camera.crossing import SIGNATURE_TTL_S, Crossing, Zone, look
from scraper.border.camera.hls import Gap
from scraper.border.camera.transit import (
    CHECK_MAX_MIN,
    GAP_PAD_S,
    MATCH_DISTANCE,
    Anchor,
    Prior,
    _Candidate,
    _largest_chain,
    anchors,
    colour_distance,
    count_at,
    cumulative,
    drift,
    lane_types,
    measure,
    now,
    now_from,
    of_lane_type,
    offset_at,
    offset_from_anchors,
    ordered,
    prior_at,
    record,
    transits,
    windows,
)

T0 = datetime(2026, 10, 24, 14, 0, tzinfo=UTC)
FRAME_S = 0.5                 # both counters read 2 frames a second
RATE = 6.0                    # cars a minute
WARMUP_S = 120 * 60           # longer than the longest transit, so the bridge is full at T0
LO_MIN, HI_MIN = 15, 100      # the transit bounds the anchors search within
TICK = timedelta(microseconds=1)
SURE = ("given", "anchors", "continued")

NEUTRAL = ((0.92, 0.5, 0.5), (0.68, 0.5, 0.5), (0.42, 0.5, 0.5), (0.1, 0.5, 0.5))
COMMON = ((0.3, 0.75, 0.6), (0.25, 0.5, 0.2))            # dark red, dark blue
VIVID = ((0.8, 0.45, 0.95), (0.5, 0.15, 0.7), (0.7, 0.8, 0.9),
         (0.5, 0.95, 0.65), (0.7, 0.4, 0.1), (0.35, 0.8, 0.15))
COMMON_SHARE = 0.15
CAR_SPREAD = 0.03             # shade differences between cars of one colour
CAMERA_NOISE = 0.025          # per reading, per component
SHIFT_A = (0.0, 0.0, 0.0)
SHIFT_B = (0.02, -0.01, 0.015)  # the City camera sees every colour a little differently


def transit_s(t_s: float) -> float:
    """20-90 min, slowly varying. Its slope stays under 1, so no car overtakes another."""
    return 60 * (55 + 35 * math.sin(2 * math.pi * t_s / (400 * 60)))


STEADY_S = 55 * 60            # a constant transit: as many cars leave as arrive


def frame(t_s: float) -> float:
    return round(t_s / FRAME_S) * FRAME_S


@dataclass(frozen=True)
class Sim:
    a: tuple[Crossing, ...]
    b: tuple[Crossing, ...]
    owner: dict                      # Crossing -> car number (the truth; tests only)
    enter: tuple[float, ...]         # per car, seconds after T0
    exit: tuple[float, ...]
    offset: int                      # cars between the lines at T0
    gaps_b: tuple[Gap, ...]
    minutes: float

    def between(self, at: datetime) -> int:
        """Cars that crossed A before `at` and B at or after it."""
        t = (at - T0).total_seconds()
        return sum(1 for e, x in zip(self.enter, self.exit) if e < t <= x)

    def true_minutes(self, crossing: Crossing) -> float:
        i = self.owner[crossing]
        return (self.exit[i] - self.enter[i]) / 60


@functools.cache
def simulate(seed: int = 1, minutes: float = 360.0, vivid: float = 0.10,
             miss_a: float = 0.0, double_a: float = 0.0,
             miss_b: float = 0.0, double_b: float = 0.0,
             gap_b: tuple[float, float] | None = None,
             colourless_after_min: float | None = None, steady: bool = False) -> Sim:
    cars, looks = random.Random(f"{seed}/cars"), random.Random(f"{seed}/looks")
    horizon = minutes * 60
    enter, exit_, colour = [], [], []
    t = -WARMUP_S
    while True:
        t += cars.expovariate(RATE / 60)
        if t >= horizon:
            break
        enter.append(frame(t))
        exit_.append(frame(t + (STEADY_S if steady else transit_s(t))))
        u = looks.random()
        palette = VIVID if u < vivid else COMMON if u < vivid + COMMON_SHARE else NEUTRAL
        centre = palette[looks.randrange(len(palette))]
        colour.append(tuple(c + looks.gauss(0, CAR_SPREAD) for c in centre))
    unseen_b = [] if gap_b is None else [(gap_b[0] * 60, gap_b[0] * 60 + gap_b[1])]
    owner: dict = {}

    def observe(times, line, camera, miss, double, shift, unseen):
        errors, noise = random.Random(f"{seed}/{line}/errors"), random.Random(f"{seed}/{line}")
        out = []
        for i, t in enumerate(times):           # FIFO: car order is crossing order at both lines
            u_miss, u_double = errors.random(), errors.random()
            if not 0 <= t < horizon or any(s <= t < e for s, e in unseen) or u_miss < miss:
                continue
            for extra in ((0.0, 1.0) if u_double < double else (0.0,)):
                sig = tuple(round(c + s + noise.gauss(0, CAMERA_NOISE), 4)
                            for c, s in zip(colour[i], shift))
                if colourless_after_min is not None and t >= colourless_after_min * 60:
                    sig = None                  # night: no colour to read
                c = Crossing(camera, line, "1" if i % 2 else "2",
                             T0 + timedelta(seconds=t + extra), "loop", sig)
                out.append(c)
                owner[c] = i
        return tuple(out)

    a = observe(enter, "A", "pdn-north", miss_a, double_a, SHIFT_A, [])
    b = observe(exit_, "B", "bridgepdn1", miss_b, double_b, SHIFT_B, unseen_b)
    gaps = () if gap_b is None else (Gap(T0 + timedelta(minutes=gap_b[0]), gap_b[1], "test"),)
    offset = sum(1 for e, x in zip(enter, exit_) if e < 0 <= x)
    return Sim(a, b, owner, tuple(enter), tuple(exit_), offset, gaps, minutes)


@functools.cache
def wheel(seed: int, transit_min: float, turn_b: float = 0.0, saturation_b: float = 1.0,
          value_b: float = 0.0, minutes: float = 180.0,
          ) -> tuple[tuple[Crossing, ...], tuple[Crossing, ...], int]:
    """A stretch read in crossing.look()'s form (width share included), with hues anywhere on the
    colour wheel, so a camera that renders colour its own way moves one car's reading onto
    another's. 1 car in 10 is distinctive (saturation 0.5-0.9), the rest neutral. Every car takes
    transit_min, give or take 20 s, first in first out. Line B's camera turns hue by turn_b
    degrees, scales saturation by saturation_b and adds value_b. Returns A's and B's crossings
    in [T0, T0 + minutes) and the cars between the lines at T0."""
    cars, noise = random.Random(f"{seed}/wheel"), random.Random(f"{seed}/wheel/noise")
    enter, exit_, looks = [], [], []
    t = -WARMUP_S
    while (t := t + cars.expovariate(RATE / 60)) < minutes * 60:
        distinctive = cars.random() < 0.10
        hue = cars.uniform(0, 360)
        sat = cars.uniform(0.5, 0.9) if distinctive else abs(cars.gauss(0, 0.04))
        val = cars.uniform(0.3, 0.9) if distinctive else cars.choice((0.9, 0.65, 0.4, 0.1))
        enter.append(frame(t))
        exit_.append(t + transit_min * 60 + cars.gauss(0, 20))
        looks.append((hue, sat, val, cars.uniform(0.53, 0.67)))
    exit_ = [frame(x) for x in sorted(exit_)]            # first in, first out

    def read(hue, sat, val, width, turn=0.0, k=1.0, dv=0.0):
        h = math.radians(hue + turn)
        return look(k * sat * math.cos(h) + noise.gauss(0, 0.02),
                    k * sat * math.sin(h) + noise.gauss(0, 0.02),
                    val + dv + noise.gauss(0, 0.02), width + noise.gauss(0, 0.02))

    a = tuple(Crossing("pdn-north", "A", "1", T0 + timedelta(seconds=e), "loop", read(*c))
              for e, c in zip(enter, looks) if 0 <= e < minutes * 60)
    b = tuple(Crossing("bridgepdn1", "B", "1", T0 + timedelta(seconds=x), "loop",
                       read(*c, turn_b, saturation_b, value_b))
              for x, c in zip(exit_, looks) if 0 <= x < minutes * 60)
    return a, b, sum(1 for e, x in zip(enter, exit_) if e < 0 <= x)


def quiet_stretch(vivid_cars: dict[int, tuple[float, float, float]], cars: int = 120,
                  transit_min: float = 30.0) -> tuple[list[Crossing], list[Crossing]]:
    """A car every 30 s, all neutral but `vivid_cars` (car number -> colour), each taking
    `transit_min`, with the bridge empty at T0. Readings are exact."""
    a, b = [], []
    for i in range(cars):
        sig = vivid_cars.get(i, NEUTRAL[i % len(NEUTRAL)])
        at = T0 + timedelta(seconds=30 * i)
        a.append(Crossing("pdn-north", "A", "1", at, "loop", sig))
        b.append(Crossing("bridgepdn1", "B", "1", at + timedelta(minutes=transit_min), "loop",
                          sig))
    return a, b


def blind_b(sim: Sim, gaps: list[tuple[float, float]]) -> tuple[list[Crossing], list[Gap]]:
    """Line B misses (minute, seconds) after each frame at `minute`: its crossings in the
    stretch are dropped, and the reader reports the Gap."""
    unseen = [(T0 + timedelta(minutes=m), T0 + timedelta(minutes=m, seconds=s)) for m, s in gaps]
    b = [c for c in sim.b if not any(g0 < c.at <= g1 for g0, g1 in unseen)]
    return b, [Gap(g0, (g1 - g0).total_seconds(), "test") for g0, g1 in unseen]


def anchor_cars(sim: Sim, found: list[Anchor], start: datetime) -> list[tuple[int, int]]:
    oa, ob = ordered(sim.a, start), ordered(sim.b, start)
    return [(sim.owner[oa[x.a_index - 1]], sim.owner[ob[x.b_index - 1]]) for x in found]


class CountsTest(unittest.TestCase):
    def test_cumulative_counts_and_lane_types(self):
        zones = (Zone("pdn-north", "A", "1", ((0, 0),) * 4, "general"),
                 Zone("pdn-north", "A", "2", ((0, 0),) * 4, "ready"),
                 Zone("pdn-north", "A", "3", ((0, 0),) * 4, None))
        types = lane_types(zones)
        self.assertEqual(types, {"1": "general", "2": "ready", "3": None})
        cs = [Crossing("pdn-north", "A", lane, T0 + timedelta(seconds=s), "loop")
              for lane, s in (("1", 30), ("2", 10), ("3", 20), ("9", 40), ("1", 5))]
        self.assertEqual([c.at.second for c in of_lane_type(cs, types, "general")], [30, 5])
        self.assertEqual([c.lane for c in of_lane_type(cs, types, None)], ["3"])  # not "9"
        times = cumulative(cs)
        self.assertEqual([t.second for t in times], [5, 10, 20, 30, 40])
        self.assertEqual(count_at(times, T0 + timedelta(seconds=20)), 3)
        self.assertEqual([t.second for t in cumulative(cs, T0 + timedelta(seconds=20))],
                         [20, 30, 40])
        with self.assertRaises(ValueError):
            lane_types(zones + (Zone("pdn-north", "A", "1", ((0, 0),) * 4, "sentri"),))


class ExactRecoveryTest(unittest.TestCase):
    """(1) Perfect counts and the right offset give every transit exactly."""

    def test_every_transit_is_recovered_exactly(self):
        sim = simulate()
        ts = transits(sim.a, sim.b, sim.offset, start=T0)
        # Which of two cars in one frame came first is not knowable, so `ordered` may list them
        # either way round; their transits are the same set. Check them car by car in the order
        # the cars really came.
        oa = sorted(ordered(sim.a, T0), key=lambda c: sim.owner[c])
        complete = sum(1 for e, x in zip(sim.enter, sim.exit) if 0 <= e and x < sim.minutes * 60)
        self.assertEqual(len(ts), complete)
        for c, t in zip(oa, ts):
            self.assertEqual(t.entered_at, c.at)
            self.assertAlmostEqual(t.minutes, sim.true_minutes(c), places=9)
        self.assertTrue(20 <= min(t.minutes for t in ts) and max(t.minutes for t in ts) <= 90)

    def test_given_offset_through_measure_matches(self):
        sim = simulate()
        m = measure(sim.a, sim.b, LO_MIN, HI_MIN, start=T0, offset=sim.offset)
        self.assertEqual([e.source for e in m.epochs], ["given"])
        # The same transits. A given count is sure for CHECK_MAX_MIN, as one anchors set is: a
        # counter that drifts takes a fixed offset further out with every car (DriftLawTest).
        checked = T0 + timedelta(minutes=CHECK_MAX_MIN)
        self.assertEqual(m.transits, tuple(replace(t, flagged=t.entered_at > checked)
                                           for t in transits(sim.a, sim.b, sim.offset, start=T0)))

    def test_now_counts_the_cars_between(self):
        sim = simulate()
        for minute in (30, 125, 300):
            at = T0 + timedelta(minutes=minute)
            n = now(sim.a, sim.b, sim.offset, at, start=T0)
            self.assertEqual(n.between, sim.between(at + timedelta(microseconds=1)))
            exits = sum(1 for x in sim.exit if minute * 60 - 600 < x <= minute * 60)
            self.assertAlmostEqual(n.exit_rate, exits / 10)
            self.assertAlmostEqual(n.minutes, n.between / n.exit_rate)
        quiet = now([], [], 4, T0 + timedelta(minutes=30))
        self.assertEqual((quiet.between, quiet.exit_rate, quiet.minutes), (4, 0.0, None))
        early = now(sim.a, sim.b, sim.offset, T0 + timedelta(minutes=1), start=T0)
        self.assertIsNone(early.exit_rate)          # one minute seen is too little for a rate


class DriftLawTest(unittest.TestCase):
    """(2) With a fixed offset, the transit error grows as bias_A x elapsed at A minus
    bias_B x elapsed at B: (bias_A - bias_B) x elapsed once that is long against the transit.

    In cars, the error is bias_A x (cars counted at A) - bias_B x (cars counted at B); it becomes
    minutes through the exit rate. So the law in minutes needs as many cars leaving as arriving,
    and is checked on a steady stretch. Single misses and doubles make each run's count error a
    random walk (about 14 cars, 2.3 min, after five hours here), so the law, an expectation, is
    checked on SEEDS runs pooled."""

    SEEDS = 10

    CASES = (   # (miss_a, double_a, miss_b, double_b)
        (0.01, 0.05, 0.01, 0.01),      # A counts 4 % too many, B is right on average
        (0.03, 0.01, 0.01, 0.04),      # A 2 % too few, B 3 % too many
        (0.01, 0.04, 0.01, 0.04),      # both 3 % too many: the drift cancels
    )

    def test_error_follows_the_law(self):
        for case in self.CASES:
            with self.subTest(case=case):
                miss_a, double_a, miss_b, double_b = case
                bias_a, bias_b = double_a - miss_a, double_b - miss_b
                xs, errors, predicted = [], [], []
                for seed in range(1, self.SEEDS + 1):
                    sim = simulate(seed=seed, miss_a=miss_a, double_a=double_a,
                                   miss_b=miss_b, double_b=double_b, steady=True)
                    ts = transits(sim.a, sim.b, sim.offset, start=T0)
                    for c, t in zip(ordered(sim.a, T0), ts):
                        i = sim.owner[c]
                        xs.append(sim.enter[i] / 60)
                        errors.append(t.minutes - sim.true_minutes(c))
                        predicted.append(bias_a * sim.enter[i] / 60 - bias_b * sim.exit[i] / 60)
                slope = _ols_slope(xs, errors)
                law = bias_a - bias_b
                late = [e for x, e in zip(xs, errors) if 270 <= x < 300]
                late_pred = [p for x, p in zip(xs, predicted) if 270 <= x < 300]
                print(f"\ndrift bias_A={bias_a:+.3f} bias_B={bias_b:+.3f}: error grows "
                      f"{slope:+.4f} min per min elapsed, the law says {law:+.4f}; at 270-300 "
                      f"min the median error is {_median(late):+.2f} min, the law with the "
                      f"transit term says {_median(late_pred):+.2f} "
                      f"({self.SEEDS} runs, {len(xs)} transits)")
                self.assertLess(abs(slope - law), 0.2 * abs(law) + 0.008)
                self.assertLess(abs(_median(late) - _median(late_pred)), 2.5)

    def test_anchors_measure_the_bias_and_keep_transits_honest(self):
        miss_a, double_a, miss_b, double_b = self.CASES[1]
        sim = simulate(miss_a=miss_a, double_a=double_a, miss_b=miss_b, double_b=double_b)
        fixed = transits(sim.a, sim.b, sim.offset, start=T0)
        m = measure(sim.a, sim.b, LO_MIN, HI_MIN, start=T0)
        truth = {c.at: sim.true_minutes(c) for c in sim.a}
        fixed_err = sorted(abs(t.minutes - truth[t.entered_at]) for t in fixed)
        anchored_err = sorted(abs(t.minutes - truth[t.entered_at]) for t in m.transits)
        d = m.epochs[0].drift
        law = (double_a - miss_a) - (double_b - miss_b)
        realised = _realised_bias(sim, "A") - _realised_bias(sim, "B")
        print(f"\nanchors: bias_A - bias_B estimated {d.bias_difference:+.4f}; set "
              f"{law:+.4f}, realised in this run {realised:+.4f} ({d.n} anchors, "
              f"{d.cars_per_hour:+.2f} cars/h); median |error| {_median(anchored_err):.2f} min "
              f"re-anchored, {_median(fixed_err):.2f} fixed; p90 {_q(anchored_err, 0.9):.2f} "
              f"against {_q(fixed_err, 0.9):.2f}")
        self.assertLess(abs(d.bias_difference - realised), 0.01)
        self.assertLess(_median(anchored_err), 1.0)
        self.assertLess(_median(anchored_err), _median(fixed_err) / 3)


class AnchorTest(unittest.TestCase):
    """(3) With 1 car in 10 distinctive, anchors recover the offset to within a car or two."""

    def test_anchors_recover_the_offset(self):
        sim = simulate()
        found = anchors(sim.a, sim.b, LO_MIN, HI_MIN, start=T0)
        est = offset_from_anchors(found)
        pairs = anchor_cars(sim, found, T0)
        right = sum(1 for x, y in pairs if x == y)
        print(f"\nanchors: {len(found)} found for {len(sim.a)} cars, {right / len(found):.0%} "
              f"the same car; offset {est.offset} against {sim.offset}, spread {est.spread}, "
              f"{est.outliers} outliers, p10-p90 {est.low:.0f}-{est.high:.0f}")
        self.assertLessEqual(abs(est.offset - sim.offset), 2)
        self.assertGreater(len(found), 100)
        self.assertGreater(right / len(found), 0.8)
        for x, y in itertools.pairwise(found):    # first-in-first-out: no two pairs cross
            self.assertLess(x.a_index, y.a_index)
            self.assertLess(x.b_index, y.b_index)
        self.assertTrue(all(LO_MIN <= x.minutes <= HI_MIN for x in found))
        self.assertTrue(all(0 <= x.confidence <= 1 for x in found))

    def test_measure_from_anchors_alone(self):
        sim = simulate()
        m = measure(sim.a, sim.b, LO_MIN, HI_MIN, start=T0)
        exact = transits(sim.a, sim.b, sim.offset, start=T0)
        self.assertEqual(m.epochs[0].source, "anchors")
        truth = {c.at: sim.true_minutes(c) for c in sim.a}
        err = sorted(abs(t.minutes - truth[t.entered_at]) for t in m.transits)
        self.assertGreater(len(m.transits), 0.95 * len(exact))
        self.assertLess(_q(err, 0.9), 0.5)

    def test_no_colour_no_anchors(self):
        sim = simulate(colourless_after_min=0.0)
        self.assertEqual(anchors(sim.a, sim.b, LO_MIN, HI_MIN, start=T0), [])
        m = measure(sim.a, sim.b, LO_MIN, HI_MIN, start=T0)
        self.assertEqual(m.epochs[0].source, "unknown")
        self.assertEqual(m.transits, ())
        self.assertTrue(now_from(m, T0 + timedelta(minutes=60)).flagged)

    def test_too_few_anchors_are_used_but_flagged(self):
        a, b = quiet_stretch({40: VIVID[0], 80: VIVID[1]})
        found = anchors(a, b, LO_MIN, HI_MIN)
        self.assertEqual([(x.a_index, x.b_index) for x in found], [(41, 41), (81, 81)])
        m = measure(a, b, LO_MIN, HI_MIN, start=T0)
        self.assertEqual([(e.source, e.offset) for e in m.epochs], [("few-anchors", 0)])
        self.assertEqual(len(m.transits), 120)
        self.assertTrue(all(t.flagged and t.minutes == 30.0 for t in m.transits))
        given = measure(a, b, LO_MIN, HI_MIN, start=T0, offset=0)
        self.assertFalse(any(t.flagged for t in given.transits))

    def test_width_share_is_not_compared_across_the_lines(self):
        # look()'s width share is a share of each zone's assumed or pixel width, and those differ
        # between the cameras: here a car fills 0.66 of its zone at A and all of it at B. On all
        # four parts the readings were 0.34 apart, and no car matched itself.
        a, b = quiet_stretch({30: VIVID[0], 60: VIVID[1], 90: VIVID[2]})
        a = [replace(c, signature=(*c.signature, 0.66)) for c in a]
        b = [replace(c, signature=(*c.signature, 1.0)) for c in b]
        found = anchors(a, b, LO_MIN, HI_MIN)
        self.assertEqual([(x.a_index, x.b_index) for x in found], [(31, 31), (61, 61), (91, 91)])
        self.assertLessEqual(colour_distance(a[30].signature, b[30].signature), MATCH_DISTANCE)

    def test_a_truck_is_not_anchored_to_a_car(self):
        # crossing.py keeps the class apart so that a truck is never matched to a car on colour
        # alone. A yellow truck at A, and a yellow car where its partner would be at B:
        a, b = quiet_stretch({40: VIVID[0]})
        a = [replace(c, vehicle="truck" if i == 40 else "car") for i, c in enumerate(a)]
        b = [replace(c, vehicle="car") for c in b]
        self.assertEqual(anchors(a, b, LO_MIN, HI_MIN), [])
        b[40] = replace(b[40], vehicle="truck")                 # the truck itself: anchored
        self.assertEqual([(x.a_index, x.b_index) for x in anchors(a, b, LO_MIN, HI_MIN)],
                         [(41, 41)])
        # Rarity is judged among what a car could be matched to: four yellow cars near the
        # truck do not make the yellow truck common (4 of 60 neighbours would be over 1 in 20).
        for i in (36, 38, 42, 44):
            a[i] = replace(a[i], signature=VIVID[0])
            b[i] = replace(b[i], signature=VIVID[0])
        self.assertIn((41, 41),
                      [(x.a_index, x.b_index) for x in anchors(a, b, LO_MIN, HI_MIN)])

    def test_largest_chain_and_its_tie_breaks(self):
        def cand(ia, ib):
            return _Candidate(ia, ib, 0.05, 0.01, 1.0)
        # (1,1)-(2,2)-(3,3) is the longest; (1,3) and (2,1) each cross it.
        picked = _largest_chain([(cand(1, 1), 0.5), (cand(1, 3), 0.9), (cand(2, 1), 0.9),
                                 (cand(2, 2), 0.5), (cand(3, 3), 0.5)])
        self.assertEqual([(c.ia, c.ib) for c, _ in picked], [(1, 1), (2, 2), (3, 3)])
        # Two chains of two: the more confident one wins ...
        picked = _largest_chain([(cand(1, 1), 0.2), (cand(1, 2), 0.6), (cand(2, 3), 0.5)])
        self.assertEqual([(c.ia, c.ib) for c, _ in picked], [(1, 2), (2, 3)])
        # ... and at equal confidence, the earlier one.
        picked = _largest_chain([(cand(1, 1), 0.5), (cand(1, 2), 0.5), (cand(2, 3), 0.5)])
        self.assertEqual([(c.ia, c.ib) for c, _ in picked], [(1, 1), (2, 3)])

    def test_offset_spread_shows_disagreeing_anchors(self):
        def anchor(ia, ib):
            return Anchor(ia, ib, T0 + timedelta(minutes=ia), T0 + timedelta(minutes=ia + 40), 1)
        est = offset_from_anchors([anchor(i, i + 50) for i in range(1, 10)]
                                  + [anchor(20, 90), anchor(30, 60)])
        self.assertEqual((est.offset, est.spread, est.n, est.outliers), (50, 0.0, 11, 2))
        self.assertIsNone(offset_from_anchors([]))
        rising = [anchor(10 * i, 10 * i + 50 + i) for i in range(12)]   # one car per 10 min
        d = drift(rising, exit_rate=2.0, entry_rate=1.0)
        self.assertAlmostEqual(d.cars_per_hour, 6.0)
        self.assertAlmostEqual(d.minutes_per_hour, -3.0)      # 6 cars at 2 a minute
        self.assertAlmostEqual(d.bias_difference, -0.1)       # 6 of the 60 cars an hour at A


class GapTest(unittest.TestCase):
    """(4) A gap cuts the run: the windows it touches are flagged, no transit spans it, and the
    offset after it comes from new anchors, or is carried and flagged until they come."""

    GAP = (200.0, 180.0)        # line B stops reading at 200 min, for 3 min

    def setUp(self):
        self.sim = simulate(gap_b=self.GAP)
        self.g0 = T0 + timedelta(minutes=self.GAP[0])
        self.cut = self.g0 + TICK      # the frame at g0 was seen: the epoch keeps it
        self.g1 = self.g0 + timedelta(seconds=self.GAP[1] + GAP_PAD_S)

    def test_gap_windows_are_flagged_and_reanchored(self):
        sim = self.sim
        m = measure(sim.a, sim.b, LO_MIN, HI_MIN, gaps_b=sim.gaps_b, start=T0)
        self.assertEqual([(e.start, e.end) for e in m.epochs], [(T0, self.cut), (self.g1, m.end)])
        self.assertEqual([e.source for e in m.epochs], ["anchors", "anchors"])
        self.assertLessEqual(abs(m.epochs[0].offset - sim.offset), 2)
        self.assertLessEqual(abs(m.epochs[1].offset - sim.between(self.g1)), 2)
        self._no_transit_spans(m)
        self.assertFalse(any(t.flagged for t in m.transits))
        wins = windows(m)
        self.assertEqual(len(wins), 24)
        hit = [w for w in wins if w.gap]
        self.assertEqual([w.start for w in hit], [T0 + timedelta(minutes=195)])
        self.assertTrue(all(w.flagged for w in hit))
        self.assertFalse(any(w.flagged for w in wins if not w.gap))

    def test_without_new_anchors_the_offset_is_carried_and_flagged(self):
        sim = simulate(gap_b=self.GAP, colourless_after_min=self.GAP[0])
        m = measure(sim.a, sim.b, LO_MIN, HI_MIN, gaps_b=sim.gaps_b, start=T0)
        self.assertEqual([e.source for e in m.epochs], ["anchors", "carried"])
        carried, truth = m.epochs[1].offset, sim.between(self.g1)
        print(f"\ncarried across a 3 min gap: offset {carried} against {truth}")
        self.assertLessEqual(abs(carried - truth), 10)
        self._no_transit_spans(m)
        after = [t for t in m.transits if t.entered_at >= self.g1]
        self.assertGreater(len(after), 100)
        self.assertTrue(all(t.flagged for t in after))
        self.assertFalse(any(t.flagged for t in m.transits if t.entered_at < self.g0))
        wins = windows(m)
        self.assertTrue(all(w.flagged for w in wins if w.end > self.g1))
        self.assertTrue(now_from(m, T0 + timedelta(minutes=300)).flagged)
        self.assertIsNone(now_from(m, self.g0 + timedelta(seconds=30)).between)  # in the gap

    def test_gaps_at_both_lines_cut_once(self):
        sim = self.sim
        gaps_a = (Gap(self.g0 + timedelta(minutes=2), 200.0, "test"),   # overlaps B's
                  Gap(None, 30.0, "before the first frame"))             # cuts nothing
        m = measure(sim.a, sim.b, LO_MIN, HI_MIN, gaps_a=gaps_a, gaps_b=sim.gaps_b, start=T0)
        resume = self.g0 + timedelta(minutes=2, seconds=200 + GAP_PAD_S)
        self.assertEqual([(e.start, e.end) for e in m.epochs], [(T0, self.cut), (resume, m.end)])
        self.assertEqual([w.start for w in windows(m) if w.gap], [T0 + timedelta(minutes=195)])
        for t in m.transits:
            self.assertTrue(t.exited_at < self.g0 or t.entered_at >= resume, t)

    def test_between_in_windows_matches_the_truth(self):
        sim = self.sim
        m = measure(sim.a, sim.b, LO_MIN, HI_MIN, gaps_b=sim.gaps_b, start=T0)
        wins = windows(m)
        for w in wins[:-1]:
            if w.end <= self.g0 or w.end >= self.g1:
                self.assertLessEqual(abs(w.between - sim.between(w.end)), 2, w.start)
        by_exit = sum(w.by_exit.n for w in wins)
        self.assertEqual(by_exit, len(m.transits))
        self.assertEqual(sum(w.by_entry.n for w in wins), len(m.transits))
        self.assertEqual(sum(w.entered for w in wins), len(sim.a))
        self.assertEqual(sum(w.exited for w in wins), len(sim.b))

    def _no_transit_spans(self, m):
        for t in m.transits:     # both ends before the gap, or both after it
            self.assertTrue(t.exited_at < self.g0 or t.entered_at >= self.g1, t)


class RecordTest(unittest.TestCase):
    """(5) The JSONL record holds derived numbers only."""

    ALLOWED: ClassVar[dict[str, set[str]]] = {
        "": {"kind", "method", "lane_type", "start", "end", "transits", "transits_flagged",
             "epochs", "gaps_a", "gaps_b", "windows", "now"},
        "gaps_a": {"start", "seconds", "reasons"},
        "gaps_b": {"start", "seconds", "reasons"},
        "epochs": {"start", "end", "source", "offset", "offset_sd", "carried_offset",
                   "anchors", "anchors_kept", "anchors_enclosed", "anchor_offset",
                   "anchor_spread", "anchor_outliers", "anchor_p10", "anchor_p90",
                   "drift_cars_per_hour", "drift_minutes_per_hour", "bias_difference"},
        "windows": {"start", "by_exit", "by_entry", "by_exit_complete", "by_entry_complete",
                    "entered", "exited", "exit_rate", "between", "gap", "flagged", "anchors",
                    "anchor_residual"},
        "by_exit": {"n", "flagged", "median", "p10", "p90"},
        "by_entry": {"n", "flagged", "median", "p10", "p90"},
        "now": {"at", "between", "exit_rate", "minutes", "flagged"},
    }

    def test_record_has_no_per_vehicle_fields(self):
        sim = simulate(gap_b=GapTest.GAP)
        m = measure(sim.a, sim.b, LO_MIN, HI_MIN, gaps_b=sim.gaps_b, start=T0)
        wins = windows(m)
        # The runner reads now at its newest frame, which can be a crossing's own time.
        last = max(c.at for c in sim.a + sim.b if c.at.second)
        rec = record(m, wins, now_from(m, last), lane_type="general")
        text = json.dumps(rec)                           # serialisable as it is
        self._walk(rec, "")
        self.assertEqual({k for w in rec["windows"] for k in w}, self.ALLOWED["windows"])
        self.assertEqual(len(rec["windows"]), len(wins))
        self.assertEqual(len(rec["epochs"]), len(m.epochs))
        self.assertNotIn("signature", text)
        # The record's own times are whole minutes; a crossing's off the minute must not show.
        stamps = sorted({c.at.astimezone(UTC).isoformat(timespec="seconds")
                         for c in sim.a + sim.b if c.at.second}
                        | {c.at.isoformat() for c in sim.a + sim.b if c.at.microsecond})
        self.assertEqual([s for s in stamps if s in text], [])

    def test_same_crossings_same_record(self):
        # Input order does not matter, only times, even for cars counted in one frame. The
        # biased run has per-car offsets that move when two cars of one frame swap places.
        for kw in ({"gap_b": GapTest.GAP},
                   {"miss_a": 0.03, "double_a": 0.01, "miss_b": 0.01, "double_b": 0.04}):
            with self.subTest(**kw):
                sim = simulate(**kw)
                frames = defaultdict(list)
                for c in sim.a:
                    frames[c.at].append(c)
                self.assertGreater(sum(1 for cs in frames.values() if len(cs) > 1), 10)
                one = record(m := measure(sim.a, sim.b, LO_MIN, HI_MIN, gaps_b=sim.gaps_b,
                                          start=T0), windows(m))
                shuffled_a, shuffled_b = list(sim.a), list(sim.b)
                random.Random(7).shuffle(shuffled_a)
                random.Random(8).shuffle(shuffled_b)
                reversed_a = [c for t in sorted(frames) for c in reversed(frames[t])]
                for a, b in ((shuffled_a, shuffled_b), (reversed_a, sim.b)):
                    two = record(m2 := measure(a, b, LO_MIN, HI_MIN, gaps_b=sim.gaps_b,
                                               start=T0), windows(m2))
                    self.assertEqual(one, two)

    def test_one_counter_at_a_time(self):
        sim = simulate()
        yolox = [Crossing(c.camera, c.line, c.lane, c.at, "yolox") for c in sim.b[:5]]
        with self.assertRaises(ValueError):
            measure(sim.a, yolox, LO_MIN, HI_MIN, start=T0)

    def test_each_lines_gaps_are_written_as_they_cut_with_why(self):
        # So a run's records name the line and the cause of every cut. Each stretch is written
        # as the epochs were cut at it (padded, merged, clipped to the measured span), its start
        # to the minute, each reason once, and the order the gaps come in does not matter.
        sim = simulate(gap_b=GapTest.GAP)
        frozen = T0 + timedelta(minutes=100, seconds=37.25)
        end = T0 + timedelta(minutes=300)
        gaps_a = [Gap(frozen + timedelta(seconds=10), 40.0, "stream down or reconnecting"),
                  Gap(frozen, 20.0, "picture frozen or black"),            # merges with the first
                  Gap(frozen + timedelta(seconds=30), 10.0, "stream down or reconnecting"),
                  Gap(end - timedelta(seconds=10), 60.0, "segment expired"),   # runs past the end
                  Gap(end - timedelta(seconds=10), 5.0, "frames +5.0 s apart"),  # same start
                  Gap(T0 - timedelta(minutes=2), 30.0, "before the start"),    # cuts nothing
                  Gap(None, 30.0, "before the first frame")]                   # cuts nothing
        records = []
        for given in (gaps_a, gaps_a[::-1]):
            m = measure(sim.a, sim.b, LO_MIN, HI_MIN, gaps_a=given, gaps_b=sim.gaps_b, start=T0,
                        end=end)
            records.append(record(m, windows(m)))
        rec = records[0]
        self.assertEqual(rec["gaps_a"], [
            {"start": "2026-10-24T15:40:00+00:00", "seconds": 55.0,
             "reasons": ["picture frozen or black", "stream down or reconnecting"]},
            {"start": "2026-10-24T18:59:00+00:00", "seconds": 10.0,
             "reasons": ["frames +5.0 s apart", "segment expired"]}])
        self.assertEqual(rec["gaps_b"], [{"start": "2026-10-24T17:20:00+00:00",
                                          "seconds": GapTest.GAP[1] + GAP_PAD_S,
                                          "reasons": ["test"]}])
        self.assertEqual(records[1], rec)
        self.assertNotIn("15:40:37", json.dumps(rec))
        self._walk(rec, "")

    def _walk(self, node, key):
        if isinstance(node, dict):
            self.assertLessEqual(set(node), self.ALLOWED[key], key)
            for k, v in node.items():
                if isinstance(v, (dict, list)):
                    self._walk(v, k)
                else:
                    self.assertIsInstance(v, (str, int, float, bool, type(None)), k)
        elif key == "reasons":                  # a gap's reasons: words, no time or vehicle
            for item in node:
                self.assertIsInstance(item, str, key)
        else:
            for item in node:
                self.assertIsInstance(item, dict, key)
                self._walk(item, key)


class ShortEpochTest(unittest.TestCase):
    """(6) In an epoch not much longer than one transit every anchor pairs a car with a
    look-alike: the B cars of its first transit belong to A cars from before it, the A cars of
    its last transit leave after it. Such an epoch must come out right or flagged, never sure and
    wrong. Before the fix, 40-80 min epochs took offsets about 200 cars out, unflagged."""

    def test_short_epoch_between_two_gaps(self):
        for seed in (1, 2, 3):
            sim = simulate(seed=seed, steady=True)
            truth = {c.at: sim.true_minutes(c) for c in sim.a}
            for length in (40, 60, 80):
                with self.subTest(seed=seed, length=length):
                    b, gaps = blind_b(sim, [(120, 5.0), (120 + length, 5.0)])
                    m = measure(sim.a, b, LO_MIN, HI_MIN, gaps_b=gaps, start=T0)
                    mid = m.epochs[1]
                    self.assertLessEqual(abs(mid.offset - sim.between(mid.start)), 3)
                    inside = [t for t in m.transits if mid.start <= t.entered_at < mid.end]
                    if length < STEADY_S / 60:          # nobody who entered it left in it
                        self.assertEqual(inside, [])
                    else:
                        err = sorted(abs(t.minutes - truth[t.entered_at]) for t in inside)
                        self.assertLess(_median(err), 0.5)

    def test_a_run_shorter_than_its_bounds_is_flagged(self):
        sim = simulate(steady=True)
        for minutes in (60, 70):
            with self.subTest(minutes=minutes):
                end = T0 + timedelta(minutes=minutes)
                a = [c for c in sim.a if c.at < end]
                b = [c for c in sim.b if c.at < end]
                m = measure(a, b, LO_MIN, HI_MIN, start=T0, end=end)
                self.assertEqual([e.source for e in m.epochs], ["few-anchors"])
                self.assertTrue(m.transits and all(t.flagged for t in m.transits))
                self.assertTrue(all(w.flagged for w in windows(m)))
                self.assertTrue(now_from(m, end - TICK).flagged)

    def test_now_after_a_late_gap(self):
        sim = simulate(steady=True)
        b, gaps = blind_b(sim, [(290, 5.0)])
        m = measure(sim.a, b, LO_MIN, HI_MIN, gaps_b=gaps, start=T0)
        last = m.epochs[-1]
        self.assertLessEqual(abs(last.offset - sim.between(last.start)), 3)
        at = T0 + timedelta(minutes=355)
        n = now_from(m, at)
        print(f"\nnow 65 min after a late gap: {n.between} between against "
              f"{sim.between(at + TICK)}, {n.minutes:.1f} min against 55 ({last.source})")
        self.assertLessEqual(abs(n.between - sim.between(at + TICK)), 3)
        # between / the last 10 minutes' exit rate: the rate alone is good to about 13 %
        # (Poisson, 60 cars). Before the fix this read 18 min against 55.
        self.assertLess(abs(n.minutes - STEADY_S / 60), 10)


class CarryTest(unittest.TestCase):
    """(7) Carrying the count across gaps."""

    def test_two_gaps_a_minute_apart_at_night(self):
        # The epoch between them is under MIN_RATE_SPAN_MIN long. The rate before the second
        # gap is counted across the first, so the count is not lost for the rest of the night.
        sim = simulate(colourless_after_min=150.0)
        b, gaps = blind_b(sim, [(200, 5.0), (201, 5.0)])
        m = measure(sim.a, b, LO_MIN, HI_MIN, gaps_b=gaps, start=T0)
        self.assertEqual([e.source for e in m.epochs], ["anchors", "carried", "carried"])
        for e in m.epochs:
            self.assertLessEqual(abs(e.offset - sim.between(e.start)), 2, e.start)
        last = m.epochs[-1].start
        self.assertGreater(sum(1 for t in m.transits if t.entered_at >= last), 100)
        at = T0 + timedelta(minutes=300)
        self.assertLessEqual(abs(now_from(m, at).between - sim.between(at + TICK)), 2)

    def test_a_car_in_the_last_frame_before_a_gap_keeps_its_transit(self):
        # A car every 30 s, 30 min between the lines, empty at T0. B misses 60 s after its frame
        # at 45:00, in which car 30 crossed: that frame was seen, so car 30 is counted before
        # the gap, and the count carried to 46:05 is the true 60.
        a, b = quiet_stretch({10: VIVID[0], 20: VIVID[1], 25: VIVID[2]})
        b = [c for i, c in enumerate(b) if i not in (31, 32)]       # unseen in the gap
        gap = Gap(T0 + timedelta(minutes=45), 60.0, "test")
        m = measure(a, b, LO_MIN, HI_MIN, gaps_b=[gap], start=T0, offset=0)
        self.assertEqual([(e.source, e.offset) for e in m.epochs], [("given", 0), ("carried", 60)])
        before = [t for t in m.transits if t.entered_at <= gap.after]
        after = [t for t in m.transits if t.entered_at > gap.after]
        self.assertEqual(len(before), 31)
        self.assertEqual(before[-1].exited_at, gap.after)
        self.assertEqual(len(after), 27)
        self.assertTrue(all(t.minutes == 30.0 for t in m.transits))

    def test_a_misreported_gap_is_disputed(self):
        # B was blind for 10 minutes but its reader said 5 s, so the carried count is about 60
        # cars short of what the data hold. Enclosed anchors after the gap disagree with it:
        # they set the offset, flagged.
        sim = simulate()
        b = [c for c in sim.b
             if not T0 + timedelta(minutes=60) < c.at <= T0 + timedelta(minutes=70)]
        gaps = [Gap(T0 + timedelta(minutes=60), 5.0, "test")]
        m = measure(sim.a, b, LO_MIN, HI_MIN, gaps_b=gaps, start=T0, offset=sim.offset)
        e = m.epochs[1]
        self.assertEqual(e.source, "disputed")
        self.assertGreater(abs(e.incoming - e.offset), 40)
        truth = {c.at: sim.true_minutes(c) for c in sim.a}
        after = [t for t in m.transits if t.entered_at >= e.start]
        self.assertTrue(after and all(t.flagged for t in after))
        self.assertLess(_median(sorted(abs(t.minutes - truth[t.entered_at]) for t in after)), 0.5)

    def test_a_given_count_needs_its_start_seen(self):
        sim = simulate()
        with self.assertRaises(ValueError):
            measure(sim.a, sim.b, LO_MIN, HI_MIN, start=T0, offset=sim.offset,
                    gaps_b=[Gap(T0 - timedelta(seconds=1), 60.0, "test")])


class LiveTest(unittest.TestCase):
    """(8) ADR-0014 decision 4 keeps a signature for at most 3 h. Measured in horizons no longer
    than that, each one started from the count the last one gives at its start (`prior_at`),
    the run's windows come out as the batch's with every signature kept. A window's by-exit
    numbers are complete in a horizon once every car that left in it entered after its start.
    Its flag may still be up there while anchors have yet to confirm an offset carried across a
    gap: never the other way round."""

    HORIZON_MIN, STEP_MIN = 180, 30

    def test_horizons_within_the_signature_ttl(self):
        sim = simulate(gap_b=GapTest.GAP)
        batch = {w.start: w for w in windows(measure(sim.a, sim.b, LO_MIN, HI_MIN,
                                                     gaps_b=sim.gaps_b, start=T0))}
        final: dict[datetime, object] = {}
        prior, sources = None, []
        for end_min in range(self.HORIZON_MIN, int(sim.minutes) + 1, self.STEP_MIN):
            end = T0 + timedelta(minutes=end_min)
            start = T0 if prior is None else prior.at
            a = [c for c in sim.a if start <= c.at < end]
            b = [c for c in sim.b if start <= c.at < end]
            self.assertTrue(all(end - c.at <= timedelta(seconds=SIGNATURE_TTL_S)
                                for c in a + b if c.signature is not None))
            m = measure(a, b, LO_MIN, HI_MIN, gaps_b=sim.gaps_b, end=end,
                        start=start if prior is None else None, prior=prior)
            sources.append([e.source for e in m.epochs])
            for w in windows(m):
                if w.by_exit_complete:
                    final.setdefault(w.start, w)
            prior = prior_at(m, end + timedelta(minutes=self.STEP_MIN - self.HORIZON_MIN))
            self.assertIsInstance(prior, Prior)
        waiting = [s.time().isoformat("minutes") for s, w in final.items()
                   if w.flagged and not batch[s].flagged]
        print(f"\nlive horizons: sources {sources}; {len(final)} windows complete, flagged "
              f"while confirmation was pending: {waiting}")
        self.assertGreaterEqual(len(final), 15)
        for start, w in final.items():
            mine, theirs = w.by_exit, batch[start].by_exit
            self.assertEqual((mine.n, mine.median, mine.p10, mine.p90),
                             (theirs.n, theirs.median, theirs.p10, theirs.p90), start)
            self.assertTrue(w.flagged or not batch[start].flagged, start)
        self.assertLessEqual(len(waiting), 2)

    def test_a_prior_is_the_count_at_its_own_time(self):
        sim = simulate(gap_b=GapTest.GAP)
        p = Prior(T0, sim.offset, sure=True, uncertainty=0.0)
        with self.assertRaises(ValueError):
            measure(sim.a, sim.b, LO_MIN, HI_MIN, prior=p, start=T0 + timedelta(minutes=1))
        with self.assertRaises(ValueError):
            measure(sim.a, sim.b, LO_MIN, HI_MIN, prior=p, offset=sim.offset)
        m = measure(sim.a, sim.b, LO_MIN, HI_MIN, gaps_b=sim.gaps_b, prior=p)
        inside = prior_at(m, T0 + timedelta(minutes=201))         # in the gap: the next epoch
        self.assertEqual(inside.at, m.epochs[1].start)
        self.assertEqual(inside.between, offset_at(m.epochs[1], inside.at))

    def test_nothing_is_counted_past_the_measured_end(self):
        # Line B stopped at `end` and line A went on. Past `end` the cars between are unknown:
        # they were A's crossings since then with none leaving, sure and unflagged, and the next
        # horizon took that as "continued" (240 cars between against 30 in a review run).
        sim = simulate(steady=True)
        end = T0 + timedelta(minutes=120)
        b = [c for c in sim.b if c.at < end]
        m = measure(sim.a, b, LO_MIN, HI_MIN, start=T0, end=end, offset=sim.offset)
        self.assertEqual(prior_at(m, end).between, sim.between(end))
        self.assertIsNone(prior_at(m, end + timedelta(minutes=1)))
        later = now_from(m, end + timedelta(minutes=60))
        self.assertTrue(later.flagged)
        self.assertIsNone(later.between)

    def test_a_count_no_anchor_has_checked_for_long_is_flagged(self):
        # Colour goes at 150 min, so no anchor checks the count after that, and line A misses
        # 3 % of its cars, so the count drifts about 10 cars an hour. Chained horizon to horizon,
        # a sure count stayed "continued" (sure, unflagged) all night, 104 cars out by morning.
        # It may stay sure CHECK_MAX_MIN after the last anchor's crossing at B, no longer.
        sim = simulate(steady=True, colourless_after_min=150.0, miss_a=0.03)
        prior, seen = None, []
        for end_min in range(self.HORIZON_MIN, int(sim.minutes) + 1, self.STEP_MIN):
            end = T0 + timedelta(minutes=end_min)
            start = T0 if prior is None else prior.at
            a = [c for c in sim.a if start <= c.at < end]
            b = [c for c in sim.b if start <= c.at < end]
            m = measure(a, b, LO_MIN, HI_MIN, end=end, start=start if prior is None else None,
                        prior=prior)
            current = now_from(m, end)
            seen.append((end_min, [e.source for e in m.epochs], current.flagged,
                         current.between - sim.between(end)))
            prior = prior_at(m, end + timedelta(minutes=self.STEP_MIN - self.HORIZON_MIN))
        print(f"\nnight horizons (end, sources, flagged, error in cars between): {seen}")
        self.assertEqual(seen[0][1:3], (["anchors"], False))
        # The last coloured car crossed A at 95 min and B at 150. Anchors still inside a horizon
        # (it starts HORIZON_MIN before its end) may confirm the count; once none is, it may stay
        # sure CHECK_MAX_MIN after the last of them crossed B, and no longer.
        last_a, last_b = 150 - STEADY_S / 60, 150
        late = [s for s in seen
                if s[0] - self.HORIZON_MIN > last_a and s[0] > last_b + CHECK_MAX_MIN]
        self.assertTrue(late)
        for end_min, sources, flagged, _ in late:
            self.assertTrue(flagged, end_min)
            self.assertFalse(any(s in SURE for s in sources), (end_min, sources))
        # While they are still inside, they keep the epoch "anchors", but not its count sure:
        # the horizon ending at 240 read 16 cars too many, unflagged, before that was fixed.
        for end_min, _, flagged, _ in seen:
            if end_min > last_b + CHECK_MAX_MIN:
                self.assertTrue(flagged, end_min)

    def test_anchors_still_measured_do_not_keep_a_count_sure(self):
        # The same night in one measurement. Its last anchors crossed B before 150 min, and
        # they kept every car's transit and every count after them sure, to the end of the run.
        # A car that crossed A, and a count read, more than CHECK_MAX_MIN after the last of them
        # crossed B is flagged. The epoch itself stays "anchors".
        sim = simulate(steady=True, colourless_after_min=150.0, miss_a=0.03)
        m = measure(sim.a, sim.b, LO_MIN, HI_MIN, start=T0)
        self.assertEqual([e.source for e in m.epochs], ["anchors"])
        limit = m.epochs[0].checked_at + timedelta(minutes=CHECK_MAX_MIN)
        truth = {c.at: sim.true_minutes(c) for c in sim.a}
        late = [t for t in m.transits if t.entered_at > limit]
        print(f"\nnight in one measurement: {len(late)} transits past {limit:%H:%M}, median "
              f"error {_median(sorted(t.minutes - truth[t.entered_at] for t in late)):+.2f} min")
        self.assertGreater(len(late), 100)
        self.assertTrue(all(t.flagged for t in late))
        self.assertFalse(any(t.flagged for t in m.transits if t.entered_at <= limit))
        self.assertFalse(now_from(m, limit).flagged)
        self.assertTrue(now_from(m, limit + TICK).flagged)
        wins = windows(m)
        self.assertEqual([w.flagged for w in wins], [w.end > limit for w in wins])


class SignatureScaleTest(unittest.TestCase):
    """(9) Signatures must be in crossing.look()'s 0-1 form. Any other (hue in degrees, 0-255
    channels) is refused, not turned into look-alike anchors, and so is a part that is not a
    number."""

    def test_raw_signatures_are_refused(self):
        a, b = quiet_stretch({})
        raw = (217.0, 40.0, 180.0, 1.8, 2.5)              # loop.py's form, 2026-10-24
        a = [replace(c, signature=raw) for c in a]
        b = [replace(c, signature=raw) for c in b]
        with self.assertRaises(ValueError) as caught:
            measure(a, b, LO_MIN, HI_MIN, start=T0)
        self.assertNotIn("217", str(caught.exception))      # per-vehicle data stays out
        with self.assertRaises(ValueError):
            anchors(a, b, LO_MIN, HI_MIN)

    def test_a_part_that_is_not_a_number_is_named_so(self):
        a, b = quiet_stretch({})
        a[7] = replace(a[7], signature=(math.nan, 0.0, 0.5, 0.6))
        with self.assertRaisesRegex(ValueError, "not finite"):
            measure(a, b, LO_MIN, HI_MIN, start=T0)


class LookAlikeTest(unittest.TestCase):
    """(10) Where a car's true partner cannot match it, look-alikes chain up in its place and
    scatter by tens of cars: a trip longer than hi_min, or a camera at B that renders colour its
    own way (15 degrees of hue alone moves a saturation-0.7 car 0.18, past MATCH_DISTANCE). Such
    an epoch must come out flagged, never sure and wrong. Before the fix, most seeds gave
    "anchors" 33-588 cars out, unflagged. Signatures are in crossing.look()'s form here."""

    SEEDS = (*range(1, 9), 14)              # 14: at 110 min 3 of 7 enclosed look-alikes agree
                                            # with their own line, and only "at least half"
                                            # keeps it flagged (the only such seed in 1-60)
    LO, HI = 0.5, 90.0                      # the runner's bounds

    def outcomes(self, **kw) -> list[tuple[int, str, int | None]]:
        out = []
        for seed in self.SEEDS:
            a, b, truth = wheel(seed, **kw)
            e = measure(a, b, self.LO, self.HI, start=T0,
                        end=T0 + timedelta(minutes=180)).epochs[0]
            out.append((seed, e.source, None if e.offset is None else e.offset - truth))
        return out

    def test_a_trip_longer_than_hi_min(self):
        out = self.outcomes(transit_min=110.0)
        self.assertEqual([o for o in out if o[1] in SURE and abs(o[2]) > 3], [], out)

    def test_colours_rendered_differently_at_b(self):
        out = self.outcomes(transit_min=47.0, turn_b=15.0, saturation_b=0.8, value_b=0.08)
        self.assertEqual([o for o in out if o[1] in SURE and abs(o[2]) > 3], [], out)

    def test_true_anchors_still_set_the_offset(self):
        out = self.outcomes(transit_min=47.0)
        self.assertEqual([o for o in out if o[1] != "anchors" or abs(o[2]) > 2], [], out)


class WindowTest(unittest.TestCase):
    """(11) What a window says about the measurement it came from."""

    def test_complete_windows_are_marked(self):
        # A live caller rewrites every window of its horizon each time. A window's by-exit numbers
        # lack the cars that entered before the horizon's start, its by-entry numbers the cars
        # still between the lines at its end, and nothing in a record said so.
        sim = simulate(steady=True)
        whole = measure(sim.a, sim.b, LO_MIN, HI_MIN, start=T0, offset=sim.offset)
        batch = {w.start: w for w in windows(whole)}
        start, end = T0 + timedelta(minutes=7), T0 + timedelta(minutes=322, seconds=30)
        m = measure([c for c in sim.a if start <= c.at < end],
                    [c for c in sim.b if start <= c.at < end], LO_MIN, HI_MIN,
                    start=start, end=end, offset=sim.between(start))
        wins = windows(m)
        for w in (wins[0], wins[-1]):          # straddles the start; holds the end
            self.assertFalse(w.by_exit_complete or w.by_entry_complete, w.start)
        exits = [w for w in wins if w.by_exit_complete]
        entries = [w for w in wins if w.by_entry_complete]
        self.assertTrue(exits and entries)

        def numbers(s):         # not the flags: each count was given at its own start
            return s.n, s.median, s.p10, s.p90
        for w in exits:
            self.assertEqual(numbers(w.by_exit), numbers(batch[w.start].by_exit), w.start)
        for w in entries:
            self.assertEqual(numbers(w.by_entry), numbers(batch[w.start].by_entry), w.start)
        short = [w for w in wins if not w.by_exit_complete
                 and w.by_exit.n < batch[w.start].by_exit.n]
        self.assertTrue(short)                 # the marks are not idle: those windows lack cars
        rec = record(m, wins)
        self.assertEqual([w["by_exit_complete"] for w in rec["windows"]],
                         [w.by_exit_complete for w in wins])

    def test_windows_count_only_the_measured_span(self):
        # A live caller passes every crossing it holds: B's from before a start moved past a gap,
        # A's from after B's last frame. They are outside the measurement and count in no window.
        sim = simulate(steady=True)
        start, end = T0 + timedelta(minutes=9), T0 + timedelta(minutes=58, seconds=30)
        m = measure(sim.a, sim.b, LO_MIN, HI_MIN, start=start, end=end,
                    offset=sim.between(start))
        wins = windows(m)

        def n(cs, t0, t1):
            return sum(1 for c in cs if t0 <= c.at < t1)
        self.assertEqual(wins[0].exited, n(sim.b, start, T0 + timedelta(minutes=15)))
        self.assertAlmostEqual(wins[0].exit_rate, wins[0].exited / 6)
        self.assertEqual(wins[-1].entered, n(sim.a, T0 + timedelta(minutes=45), end))


class EdgeLookAlikeTest(unittest.TestCase):
    """(12) Anchors their epoch does not enclose can only confirm the count carried into it, and
    near its edges they pair cars with look-alikes (ShortEpochTest). Once the count is confirmed,
    those must not set the cars' offsets. Found at the end of the camera audit: line A blind for
    30 s, then the cars of the next 45 s never counted (each stood in a zone when counting
    resumed), measured as the runner measures, from a sure count 180 min before the end. The
    trend line was fitted to every anchor, look-alikes too, and they tilted it. Before the fix,
    47 of 688 such measurements (seeds 1-8, A blind at 200 or 209 min, ends every minute from
    280 to 322) gave a sure number that was wrong. In the runner, read every minute for two hours
    after the gap, 20 window medians and 90 counts of the cars between were sure and wrong, with
    or without the cars missed (simulations, 2026-10-26)."""

    LO, HI = 0.5, 90.0                      # the runner's bounds
    CASES = (   # (seed, A blind from minute, measured to minute, A's cars missed after, s)
        (1, 209, 320, 45), (2, 200, 288, 45), (3, 200, 291, 45), (4, 200, 320, 45),
        (5, 200, 295, 45), (6, 200, 281, 45), (7, 209, 294, 45), (8, 209, 300, 45),
        (7, 209, 288, 0),   # nothing missed: one look-alike that agrees set the line's slope
    )

    def measured(self, seed, cut_min, end_min, missed_s):
        sim = simulate(seed=seed, steady=True)
        g0 = T0 + timedelta(minutes=cut_min)
        a = [c for c in sim.a if not g0 < c.at <= g0 + timedelta(seconds=30 + missed_s)]
        end = T0 + timedelta(minutes=end_min)
        start = end - timedelta(minutes=180)
        # The true count, sure as an anchored one is (ANCHORED_SD).
        m = measure(a, sim.b, self.LO, self.HI, gaps_a=[Gap(g0, 30.0, "test")], end=end,
                    prior=Prior(start, sim.between(start), sure=True, uncertainty=2.0))
        return sim, m, now_from(m, end)

    def assertAllRight(self, sim, m, current):
        wrong = self.sure_and_wrong(sim, m, current)
        self.assertEqual(wrong[:3], [], f"{len(wrong)} sure and wrong")

    def sure_and_wrong(self, sim, m, current) -> list:
        """What a reader takes as sure and is not: an unflagged transit or window median more
        than a minute from the steady trip, an unflagged count of the cars between more than 3
        out."""
        trip = STEADY_S / 60
        out = [("transit", t.entered_at, t.minutes) for t in m.transits
               if not t.flagged and abs(t.minutes - trip) > 1]
        truth = sim.between(current.at + TICK)
        if not current.flagged and abs(current.between - truth) > 3:
            out.append(("now", current.between, truth))
        for w in windows(m):
            for s in (w.by_exit, w.by_entry):
                if not w.flagged and s.median is not None and abs(s.median - trip) > 1:
                    out.append(("window", w.start, s.median))
        return out

    def test_the_audits_case(self):
        # Seed 2 at 288 min. The epoch before the gap was confirmed, but its line, tilted by
        # look-alikes, put its end 241 cars short. That count was carried across, and after the
        # gap the look-alikes happened to agree with it: window medians of 9.8-15.6 min against
        # 55, and 47 cars between against 328, all unflagged.
        sim, m, current = self.measured(2, 200, 288, 45)
        self.assertAllRight(sim, m, current)
        before, after = m.epochs
        self.assertEqual(before.source, "anchors")
        # Counted right on a steady stretch, the offset at its end is the one at its start.
        self.assertEqual(offset_at(before, before.end), sim.between(before.start))
        self.assertLessEqual(abs(after.incoming - sim.between(after.start)), 3)
        self.assertNotIn(after.source, SURE)       # its anchors are look-alikes: none agrees

    def test_sure_numbers_are_right_and_still_given(self):
        seen = []
        for case in self.CASES:
            with self.subTest(case=case):
                sim, m, current = self.measured(*case)
                self.assertAllRight(sim, m, current)
                sure = sum(1 for t in m.transits if not t.flagged)
                seen.append((case, [e.source for e in m.epochs], sure, not current.flagged))
                # Not by flagging everything: each case keeps a sure epoch and its transits.
                self.assertTrue(any(e.source in SURE for e in m.epochs), m.epochs)
                self.assertGreater(sure, 100)
        print(f"\nedge look-alikes (case, sources, sure transits, now sure): {seen}")


class DriftConfirmTest(unittest.TestCase):
    """(13) Anchors that confirm a carried count must follow the counters' drift through the
    epoch. The counters drift when A and B miss or double count at different rates, which
    cameras.json expects (the loop misses about 30 % at line A in free flow, and at line B counts a
    col-1 car again in col-2; col-e, where a col-4 car does the same, is counted by yolox only).
    Held at the count carried in, the band kept 8 of 45 true anchors at about
    20 cars an hour of drift (A misses 3 % and doubles 1 %, B misses 1 % and doubles 4 %), their
    line came out flat, and sure transits were up to 6 min out (review, 2026-10-26). Measured as
    EdgeLookAlikeTest measures, A blind for 30 s, from a sure count 180 min before the end. Not
    "no unflagged transit more than 2 min out": a sure count stays sure CHECK_MAX_MIN after the
    anchors last set it, and over that a drifting count moves a few cars (up to 3.1 min here)."""

    CASES = ((2, 200, 252), (3, 200, 258), (3, 209, 267), (4, 209, 261))   # (seed, cut, end)

    def test_a_confirmed_count_follows_the_counters_drift(self):
        for seed, cut, end_min in self.CASES:
            with self.subTest(seed=seed, cut=cut, end=end_min):
                sim = simulate(seed=seed, steady=True, miss_a=0.03, double_a=0.01,
                               miss_b=0.01, double_b=0.04)
                g0 = T0 + timedelta(minutes=cut)
                a = [c for c in sim.a if not g0 < c.at <= g0 + timedelta(seconds=30)]
                end = T0 + timedelta(minutes=end_min)
                start = end - timedelta(minutes=180)
                # The count between in B's own counts (its doubles included), as anchors give it.
                between = sum(1 for c in sim.b if c.at >= start
                              and sim.enter[sim.owner[c]] < (start - T0).total_seconds())
                m = measure(a, sim.b, 0.5, 90.0, gaps_a=[Gap(g0, 30.0, "test")], end=end,
                            prior=Prior(start, between, sure=True, uncertainty=2.0))
                self.assertEqual(m.epochs[0].source, "anchors")
                truth = {c.at: sim.true_minutes(c) for c in a}
                err = sorted(abs(t.minutes - truth[t.entered_at])
                             for t in m.transits if not t.flagged)
                self.assertGreater(len(err), 100)
                self.assertLess(err[int(0.9 * (len(err) - 1))], 2.5)
                worst = max(abs(w.by_entry.median - STEADY_S / 60) for w in windows(m)
                            if not w.flagged and w.by_entry.median is not None)
                self.assertLess(worst, 2.0)


def _realised_bias(sim: Sim, line: str) -> float:
    """Net extra counts per true crossing in this run: doubles minus misses."""
    seen = sim.a if line == "A" else sim.b
    times = sim.enter if line == "A" else sim.exit
    true = sum(1 for t in times if 0 <= t < sim.minutes * 60)
    return (len(seen) - true) / true


def _ols_slope(xs, ys):
    mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
    return (sum((x - mx) * (y - my) for x, y in zip(xs, ys))
            / sum((x - mx) ** 2 for x in xs))


def _q(vals, q):
    vals = sorted(vals)
    return vals[min(len(vals) - 1, int(q * len(vals)))]


def _median(vals):
    return _q(vals, 0.5)


if __name__ == "__main__":
    unittest.main()
