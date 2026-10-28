"""The camera wait (combine.py) against a simulated first-in-first-out bottleneck whose every
car is known (queue_sim). Each case runs the real transit.measure and transit.record, as the
runner does, and then combine.

The queue reader's lane signals are scripted, in the shape the worker emits, to match the
simulated line before A: one queued car per SPACING_M of lane, moving one car-length per car
that crosses. The views are taken to hold whatever line the case needs; a real street view is
about 48 m long, and a line past it is a floor (TailTest). So these cases check how combine adds
the segments, not the queue reader.
"""
from __future__ import annotations

import ast
import copy
import json
import math
import re
import unittest
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest

pytest.importorskip("cv2")   # measure (the queue reader's maths) imports OpenCV

import queue_sim as qs

from scraper.border.camera import combine, transit
from scraper.border.camera.hls import Gap
from scraper.border.camera.runner import HI_MIN, LO_MIN

PAIRS = Path(combine.__file__).with_name("pairs.json")
A_LANES = {"lane-1": "normal", "lane-2": "normal", "lane-3": "normal", "lane-4": "sentri",
           "lane-5": "sentri"}                    # pdn-north's count zones (cameras.json, Q-22)
A_M, R_M = 66.0, 9.3      # line A from the strips' booth end, and the run-up below it: the
                          # design's nominal values (estimate.load_pair computes them at load)
SPACING_M = 7.0           # ASSUMPTION for the scripted readings: one queued car per 7 m of lane
RATE = 8.0                # cars a minute through the bottleneck in every case below

# The line's back between A and B (the 16:27 frames): 120 cars more than the road carries
# freely build up before T0, and hold.
PAST_A = {"arrivals": ((-150.0, 10.0), (-90.0, 8.0))}
# The line reaches back over A: 180 cars too many fill the 260 places to the booths and back up.
STRADDLE = {"arrivals": ((-150.0, 12.0), (-60.0, 8.0))}
# Something before A lets 8 a minute through while the booths could take 12: the line waits
# before A, and A to the booths is free.
METERED = {"arrivals": ((-150.0, 10.0), (-30.0, 8.0)), "discharge": ((0.0, 12.0),),
           "meter": ((0.0, 8.0),), "free_ab_min": 2.0}


def pair(**changes) -> dict:
    """pairs.json's PDN entry, plus what estimate.load_pair adds from cameras.json."""
    with open(PAIRS, encoding="utf-8") as f:
        out = dict(json.load(f)["pdn-north"])
    out.update(a_lanes=dict(A_LANES), a_m=dict.fromkeys(A_LANES, A_M),
               r_m=dict.fromkeys(A_LANES, R_M))
    out.update(changes)
    return out


def measured(run: qs.Run, end_min: float | None = None, *, start_min: float = 0.0,
             group: str = "sentri", method: str = "loop", b=None, gaps_b=()):
    """One measurement and its record, as TransitRunner.measure and .records make them."""
    end = run.at(run.minutes if end_min is None else end_min)
    m = transit.measure(run.a, run.b if b is None else b, LO_MIN, HI_MIN, gaps_b=gaps_b,
                        start=run.at(start_min), end=end)
    now = transit.now_from(m, end) if m.epochs else None
    rec = transit.record(m, transit.windows(m), now, lane_type=group)
    rec.update({"method": method, "seen_until": end.replace(second=0).isoformat(),
                "line_a": "pdn-north:A", "line_b": "city-pdn-north:B"})
    return m, rec


def timed_k(run: qs.Run, m: transit.Measurement, minutes=(140, 160, 180), real: bool = False,
            group: str = "sentri") -> dict:
    """A pairs.json K_eff entry, timed as the owner would: a car's minutes from B to the booths
    times the method's counted rate at B that minute (or, `real`, the true rate)."""
    ks = []
    for minute in minutes:
        at = run.at(minute)
        ks.append(run.crossing_b(at).b_to_booth
                  * (RATE if real else transit.now_from(m, at).exit_rate))
    return {"lane_type": group, "light": "day", "low": min(ks), "high": max(ks), "n": len(ks),
            "date": "2026-10-26"}


def signals(camera: str, lane: str, end, *, state: str = "queue", queue_m: float = 0.0,
            speed: float | None = None, beyond: bool = False, head: float = 0.0,
            minutes: int = 15, every: int = 2) -> list[dict]:
    """The worker's lane signals (reader.as_signal) for one lane over the last `minutes`."""
    out = []
    for k in range(0, minutes, every):
        out.append({
            "port_number": "240202", "lane": None, "direction": "north", "kind": "measured",
            "source": f"cam:{camera}", "state": "open", "minutes": None, "low": None,
            "high": None, "observed_at": (end - timedelta(minutes=k)).isoformat(),
            "independent": True,
            "evidence": {"camera": camera, "lane": lane, "port_number": "240202",
                         "direction": "north", "state": state, "frames": 120, "seconds": 60.0,
                         "queue_m": queue_m, "beyond_view": beyond, "speed_m_s": speed,
                         "detail": {"queue_head_m": head,
                                    "quarter_speeds": None if speed is None else [speed] * 4}}})
    return out


def sentri_signals(end, **reading) -> list[dict]:
    """signals() for pdn-north's two SENTRI strips, read alike: lane-4 and lane-5 are the one
    SENTRI lane, split by barrels near the camera (owner, 2026-10-27), and a group's tail is
    timed by the median of its strips."""
    return [s for lane in A_LANES if A_LANES[lane] == "sentri"
            for s in signals("pdn-north", lane, end, **reading)]


def single_file_speed(rate: float = RATE) -> float:
    """m/s of a line in one lane that moves up one car for every car through: rate x spacing."""
    return rate * SPACING_M / 60


def no_line_at_a(end) -> list[dict]:
    """Every pdn-north strip with a line that does not reach A, and an empty street."""
    out = []
    for lane in A_LANES:
        out += signals("pdn-north", lane, end, queue_m=40.0, speed=single_file_speed())
    for lane in ("left-column", "right-column"):
        out += signals("pdn-south-street", lane, end, state="no_queue")
    return out


def wait(record: dict, group: str = "sentri", method: str = "loop") -> dict:
    return record["groups"][group]["methods"][method]


def segment(w: dict, name: str) -> dict:
    return next(s for s in w["segments"] if s["name"] == name)


def walk(value, path=()):
    """Every (path, key or None, leaf) in a JSON-like value."""
    if isinstance(value, dict):
        for k, v in value.items():
            yield path, k, v
            yield from walk(v, (*path, k))
    elif isinstance(value, list):
        for i, v in enumerate(value):
            yield from walk(v, (*path, i))


class WholeWaitTest(unittest.TestCase):
    """Design §5 tests 1-4: the total against each car's truth, per where the line lies."""

    def test_line_wholly_past_a_with_k_eff_is_within_5_percent(self):
        """1. The line's back between A and B. With K_eff timed in the simulation and B inside
        the line, the total is the true wait; without K_eff it is a floor with no minutes."""
        run = qs.run(**PAST_A)
        m, rec = measured(run)
        end = run.at(run.minutes)
        truth = run.joining(end)
        self.assertEqual(rec["epochs"][-1]["source"], "anchors")
        timed = pair(k_eff={"loop": [timed_k(run, m)], "yolox": []}, primary_booths=10)
        w = wait(combine.combine([rec], no_line_at_a(end), timed, end,
                                 q_b={("loop", "sentri"): True}))
        self.assertFalse(w["floor"])
        self.assertEqual(segment(w, "tail_to_a")["minutes"], 0.0)
        self.assertAlmostEqual(w["minutes"], truth.total, delta=0.05 * truth.total)
        self.assertAlmostEqual(segment(w, "a_to_b")["minutes"], truth.a_to_b,
                               delta=0.05 * truth.a_to_b)
        self.assertAlmostEqual(segment(w, "b_to_booth")["minutes"], truth.b_to_booth,
                               delta=0.05 * truth.b_to_booth)

        untimed = wait(combine.combine([rec], no_line_at_a(end), pair(), end))
        self.assertTrue(untimed["floor"])
        self.assertIsNone(untimed["minutes"])
        self.assertIsNone(untimed["high"])
        self.assertFalse(segment(untimed, "b_to_booth")["known"])
        self.assertLessEqual(untimed["low"], truth.total)

    def test_line_straddling_a_is_within_5_percent(self):
        """2. The line reaches back over A, past pdn-north's near edge and on into the street.
        The part of it past A is in N / mu, and only the part before A is in the tail."""
        run = qs.run(**STRADDLE)
        m, rec = measured(run)
        end = run.at(run.minutes)
        truth = run.joining(end)
        before = run.before_a(end)
        self.assertGreater(truth.before_a, 10)
        v = single_file_speed()
        gap = pair()["gap_street_a_m"]["value"]
        queue = (sentri_signals(end, queue_m=75.0, speed=v, beyond=True)
                 + signals("pdn-south-street", "left-column", end,
                           queue_m=before * SPACING_M - R_M - gap, speed=v)
                 + signals("pdn-south-street", "right-column", end, state="no_queue"))
        timed = pair(k_eff={"loop": [timed_k(run, m)], "yolox": []}, primary_booths=10)
        w = wait(combine.combine([rec], queue, timed, end, q_b={("loop", "sentri"): True}))
        self.assertFalse(w["floor"])
        self.assertAlmostEqual(segment(w, "tail_to_a")["minutes"], truth.before_a,
                               delta=0.05 * truth.before_a)
        self.assertAlmostEqual(segment(w, "a_to_b")["minutes"], truth.a_to_b,
                               delta=0.05 * truth.a_to_b)
        self.assertAlmostEqual(w["minutes"], truth.total, delta=0.05 * truth.total)
        self.assertIn("pdn-south-street", segment(w, "tail_to_a")["sensor"])

    def test_line_before_a_gives_a_floor_near_the_wait_less_b_to_booth(self):
        """3. Metered before A, so A to the booths is free and B is not inside the line: a
        floor. Its low end is within 5 % of the true wait less B to the booths, because the
        line before A is most of the wait (the A->B low sits 2 sigma under its minutes)."""
        run = qs.run(**METERED)
        _, rec = measured(run)
        end = run.at(run.minutes)
        truth = run.joining(end)
        self.assertGreater(truth.before_a, 20)
        queue = [s for lane in A_LANES
                 for s in signals("pdn-north", lane, end, state="no_queue")]
        queue += signals("pdn-south-street", "left-column", end,
                         queue_m=run.before_a(end) * SPACING_M, speed=single_file_speed())
        queue += signals("pdn-south-street", "right-column", end, state="no_queue")
        w = wait(combine.combine([rec], queue, pair(), end))
        self.assertTrue(w["floor"])
        self.assertIsNone(w["minutes"])
        target = truth.total - truth.b_to_booth
        self.assertLessEqual(w["low"], target)
        self.assertGreaterEqual(w["low"], 0.95 * target)
        self.assertIn("metered before A", " ".join(w["info"]))

    def test_half_the_cars_counted_at_b(self):
        """4. Line B counts every other car. The anchored A->B holds within its range (both
        counts and the rate are in counted cars), a K_eff timed against the counted rate gives
        the true B to the booths, and a K of real cars over the counted rate reads 1/p_B = 2x
        high: the unit rule."""
        run = qs.run(**PAST_A, p_b=0.5)
        m, rec = measured(run)
        end = run.at(run.minutes)
        truth = run.joining(end)
        self.assertEqual(rec["epochs"][-1]["source"], "anchors")
        ab = combine.a_to_b(rec)
        self.assertTrue(ab.known)
        self.assertLessEqual(ab.low, truth.a_to_b)
        self.assertGreaterEqual(ab.high, truth.a_to_b)
        counted = pair(k_eff={"loop": [timed_k(run, m)], "yolox": []}, primary_booths=10)
        bb = combine.b_to_booth(rec, counted, end, q_b=True)
        self.assertAlmostEqual(bb.minutes, truth.b_to_booth, delta=0.05 * truth.b_to_booth)
        real = pair(k_eff={"loop": [timed_k(run, m, real=True)], "yolox": []}, primary_booths=10)
        wrong = combine.b_to_booth(rec, real, end, q_b=True)
        self.assertAlmostEqual(wrong.minutes / truth.b_to_booth, 2.0, delta=0.1)


class CountTest(unittest.TestCase):
    """Design §5 tests 5-7: what the count between the lines gives, and its range."""

    def test_a_to_b_follows_the_cars_the_road_holds_not_its_metres(self):
        """5. Cars are conserved through the fan-out, metres are not. In the simulation the
        same stretch from A to B holds 40 cars per column (the simulation's number, not a
        measurement), so with the line over A, 2, 5 and 6 columns hold 80, 200 and 240 cars and
        take 10, 25 and 30 min. The count between the lines gives each run its own truth, with
        no metres or columns given. (Relabelling B's columns alone changes no crossing time.)"""
        truths = []
        for columns in (2, 5, 6):
            run = qs.run(**STRADDLE, s_ab=40 * columns, lanes_b=columns)
            _, rec = measured(run)
            truth = run.joining(run.at(run.minutes)).a_to_b
            truths.append(truth)
            self.assertAlmostEqual(combine.a_to_b(rec).minutes, truth, delta=0.05 * truth)
        self.assertGreater(max(truths) - min(truths), 15.0)    # the runs differ, so it can fail

    def test_an_unanchored_count_gives_no_minutes_and_no_range(self):
        """6a. 100 min of crossings encloses no anchor (2 x hi_min is 180): the count is
        "few-anchors", its sd unknown, so no minutes and no range, and the owner sees "—"."""
        _, rec = measured(qs.run(**PAST_A), start_min=100.0)
        self.assertEqual(rec["epochs"][-1]["source"], "few-anchors")
        self.assertIsNone(rec["epochs"][-1]["offset_sd"])
        ab = combine.a_to_b(rec)
        self.assertEqual((ab.minutes, ab.low, ab.high, ab.known), (None, None, None, False))
        out = combine.combine([rec], [], pair(), qs.Run.at(200.0))
        shown = combine.display(wait(out))
        self.assertEqual(shown["value"], "—")
        self.assertIn("count not anchored", shown["detail"])

    def test_a_carried_count_gives_minutes_with_its_flag_and_is_not_shown(self):
        """6b. Line B misses 30 s eight minutes before the end. The count carried across the
        gap is used, with its sd, so A->B has minutes; it is flagged, and not shown."""
        run = qs.run(**PAST_A)
        g0 = run.at(192.0)
        b = [c for c in run.b if not g0 < c.at <= g0 + timedelta(seconds=30)]
        _, rec = measured(run, b=b, gaps_b=(Gap(g0, 30.0, "test"),))
        self.assertEqual(rec["epochs"][-1]["source"], "carried")
        self.assertIsNotNone(rec["epochs"][-1]["offset_sd"])
        self.assertTrue(rec["now"]["flagged"])
        ab = combine.a_to_b(rec)
        self.assertTrue(ab.known)
        w = wait(combine.combine([rec], no_line_at_a(run.at(200.0)), pair(), run.at(200.0)))
        self.assertTrue(any(f.startswith("count flagged") for f in w["transient"]))
        self.assertTrue(any(f.startswith("exit rate over less than") for f in w["transient"]))
        self.assertFalse(w["count"]["sure"])
        self.assertEqual(combine.display(w)["value"], "—")

    def test_few_exits_give_no_high_and_a_stalled_line_no_number(self):
        """6c-d. n_B = mu x 10 min <= 4 bounds no rate, so no high: a floor at a low that keeps
        the rate's uncertainty. Under MIN_EXIT_RATE the line is stalled: no number at all."""
        _, rec = measured(qs.run(**PAST_A))
        slow = copy.deepcopy(rec)
        slow["now"]["exit_rate"] = 0.4
        ab = combine.a_to_b(slow)
        self.assertIsNone(ab.high)
        self.assertIsNone(ab.minutes)
        self.assertIsNotNone(ab.low)
        self.assertLessEqual(ab.low, 0.5 * slow["now"]["between"] / 0.4)   # 1 + 2/sqrt(4)
        self.assertIn("too few exits", ab.why)
        stalled = copy.deepcopy(rec)
        stalled["now"]["exit_rate"] = 0.03
        ab = combine.a_to_b(stalled)
        self.assertEqual((ab.minutes, ab.low, ab.high), (None, None, None))
        self.assertEqual(ab.why, "line B stalled")
        w = combine.method_wait(stalled, combine.tail_to_a([], pair(), "sentri", qs.Run.at(200)),
                                pair(), qs.Run.at(200))
        self.assertIn("stalled at line B", w.transient)

    def test_low_never_exceeds_minutes_nor_minutes_high(self):
        """7. Across the simulated cases, every known segment and total."""
        checked = 0
        for kw in (PAST_A, STRADDLE, METERED, {**PAST_A, "p_b": 0.5}, {**PAST_A, "p_a": 0.9}):
            run = qs.run(**kw)
            m, rec = measured(run)
            end = run.at(run.minutes)
            timed = pair(k_eff={"loop": [timed_k(run, m)], "yolox": []}, primary_booths=10)
            out = combine.combine([rec], no_line_at_a(end), timed, end,
                                  q_b={("loop", "sentri"): True})
            w = wait(out)
            for s in [*w["segments"], w]:
                if s["minutes"] is not None:
                    self.assertLessEqual(s["low"], s["minutes"], (kw, s))
                    self.assertLessEqual(s["minutes"], s["high"], (kw, s))
                    checked += 1
        self.assertGreaterEqual(checked, 15)


class RecordTest(unittest.TestCase):
    """Design §5 tests 8-11: what the camera record holds and what combine is."""

    @classmethod
    def setUpClass(cls):
        cls.run_ = qs.run(**PAST_A)
        cls.end = cls.run_.at(cls.run_.minutes)
        cls.m, cls.loop = measured(cls.run_)
        _, cls.yolox = measured(qs.run(**PAST_A, p_a=0.98), method="yolox")
        cls.pair = pair(k_eff={"loop": [timed_k(cls.run_, cls.m)],
                               "yolox": [timed_k(cls.run_, cls.m)]}, primary_booths=10)
        cls.both = {("loop", "sentri"): True, ("yolox", "sentri"): True}

    def test_agree_is_set_and_nothing_mixes_the_methods(self):
        """8. Close methods agree, far ones do not, and a floor says neither. No mean, envelope
        or combined minutes exist anywhere in the record."""
        out = combine.combine([self.loop, self.yolox], no_line_at_a(self.end), self.pair,
                              self.end, q_b=self.both)
        group = out["groups"]["sentri"]
        self.assertEqual(list(group["methods"]), ["loop", "yolox"])
        self.assertTrue(group["agree"])
        far = copy.deepcopy(self.yolox)
        far["now"]["between"] *= 3
        out_far = combine.combine([self.loop, far], no_line_at_a(self.end), self.pair,
                                  self.end, q_b=self.both)
        self.assertFalse(out_far["groups"]["sentri"]["agree"])
        out_floor = combine.combine([self.loop, self.yolox], no_line_at_a(self.end), pair(),
                                    self.end)
        self.assertIsNone(out_floor["groups"]["sentri"]["agree"])
        for record in (out, out_far, out_floor):
            keys = {k for _, k, _ in walk(record) if isinstance(k, str)}
            self.assertFalse(keys & {"mean", "average", "combined", "envelope", "blend"})
            for g in record["groups"].values():
                self.assertFalse({"minutes", "low", "high"} & set(g))

    def test_a_method_not_measured_keeps_its_place_in_methods_order(self):
        """cameras.json counts col-e by yolox only, so SENTRI has a yolox wait and a loop that is
        not measured. The loop is still listed first, in crossing.METHODS order, not after the
        measured method, so SENTRI's cards read in the same order as the normal group's."""
        out = combine.combine([self.yolox], no_line_at_a(self.end), self.pair, self.end,
                              not_measured={("loop", "sentri"): "x"})
        self.assertEqual(list(out["groups"]["sentri"]["methods"]), ["loop", "yolox"])

    def test_not_measured_wins_over_a_record_of_the_same_method_and_group(self):
        """A replay that rebuilt not_measured from a later cameras.json can hold a record the
        live run measured for a (method, group) now refused. The method is listed as not
        measured, and its hidden wait feeds neither `agree` nor the vote's reasons."""
        why = "city-pdn-north col-e is counted by yolox only"
        both = combine.combine([self.loop, self.yolox], no_line_at_a(self.end), self.pair,
                               self.end, q_b=self.both)
        self.assertTrue(both["groups"]["sentri"]["agree"])        # what the hidden wait gave
        self.assertTrue([r for r in both["groups"]["sentri"]["would_vote_reasons"]
                         if "for loop" in r])
        out = combine.combine([self.loop, self.yolox], no_line_at_a(self.end), self.pair,
                              self.end, q_b=self.both, not_measured={("loop", "sentri"): why})
        group = out["groups"]["sentri"]
        self.assertEqual(group["methods"]["loop"],
                         {"method": "loop", "lane_type": "sentri", "not_measured": why})
        self.assertIsNone(group["agree"])
        self.assertIn(f"loop not measured: {why}", group["would_vote_reasons"])
        self.assertFalse([r for r in group["would_vote_reasons"] if "for loop" in r],
                         group["would_vote_reasons"])
        alone = combine.combine([self.yolox], no_line_at_a(self.end), self.pair, self.end,
                                q_b=self.both, not_measured={("loop", "sentri"): why})
        self.assertEqual(out["groups"], alone["groups"])

    def test_lane_ids_and_the_vote(self):
        """9. "mixed" and "normal" have no CBP lane id; "sentri" files as car_sentri. None
        would vote: the vote is undecided (ADR-0015 decision 3), and a group with no lane id
        never votes, even once everything else is in place."""
        recs = [dict(self.loop, lane_type=group) for group in ("mixed", "normal", "sentri")]
        out = combine.combine(recs, no_line_at_a(self.end), self.pair, self.end)
        self.assertEqual({g: out["groups"][g]["lane"] for g in out["groups"]},
                         {"mixed": None, "normal": None, "sentri": "car_sentri"})
        for g in out["groups"].values():
            self.assertIs(g["would_vote"], False)
            self.assertIn("camera votes not decided (ADR-0015 decision 3 proposed)",
                          g["would_vote_reasons"])
        hands = [{"method": "loop", "lane_type": t, "line": line, "capture": 1.0}
                 for t in ("normal", "sentri") for line in ("A", "B")]
        ready = pair(camera_votes=True, line_a_verified=True, line_b_verified=True,
                     hand_counts=hands)
        decided = combine.combine([dict(self.loop, lane_type="normal")], [], ready, self.end)
        self.assertIn("lane type normal has no CBP lane id",
                      decided["groups"]["normal"]["would_vote_reasons"])
        self.assertIs(decided["groups"]["normal"]["would_vote"], False)

    def test_no_per_vehicle_data_and_times_cut_to_the_minute(self):
        """10. Derived numbers only: no key names a vehicle, and every time is to the minute."""
        out = combine.combine([self.loop, self.yolox], no_line_at_a(self.end), self.pair,
                              self.end + timedelta(seconds=37), q_b=self.both)
        banned = {"signature", "signatures", "anchors", "index", "a_index", "b_index",
                  "entered_at", "exited_at", "crossings", "a_times", "b_times", "plate",
                  "vehicle", "transits"}
        times = 0
        for _, key, leaf in walk(out):
            self.assertNotIn(key, banned)
            if isinstance(leaf, str) and re.fullmatch(r"\d{4}-\d\d-\d\dT[\d:]+.*", leaf):
                self.assertRegex(leaf, r"T\d\d:\d\d:00")
                times += 1
        self.assertEqual(times, 1)
        self.assertEqual(out["at"], "2026-10-26T17:20:00+00:00")

    def test_combine_is_pure(self):
        """11. No import of cbp, fusion or poll, and no file, network, clock or console."""
        tree = ast.parse(Path(combine.__file__).read_text(encoding="utf-8"))
        modules = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules |= {a.name for a in node.names}
            elif isinstance(node, ast.ImportFrom):
                modules.add(("." * node.level) + (node.module or ""))
                modules |= {a.name for a in node.names}
        parts = {p for name in modules for p in name.strip(".").split(".")}
        self.assertFalse(parts & {"cbp", "fusion", "poll", "os", "io", "pathlib", "socket",
                                  "urllib", "requests", "httpx", "sqlite3", "time", "json",
                                  "subprocess", "logging", "threading"}, modules)
        calls = {n.func.id if isinstance(n.func, ast.Name) else n.func.attr
                 for n in ast.walk(tree) if isinstance(n, ast.Call)
                 and isinstance(n.func, (ast.Name, ast.Attribute))}
        self.assertFalse(calls & {"open", "print", "input", "now", "today", "utcnow",
                                  "read_text", "write_text"}, calls)


class SelfCheckTest(unittest.TestCase):
    def test_self_check_finds_a_seeded_live_edge_error(self):
        """12. Nows logged live every 4 min, against the by-entry windows a later horizon
        completes. Honest, they match it; with 40 cars added to each live count (5 min at 8
        cars a minute), self_check reports that error."""
        run = qs.run(**PAST_A, minutes=300.0)
        live = [measured(run, minute)[1] for minute in range(181, 211, 4)]
        later = measured(run)[1]
        honest = combine.self_check([*live, later])["loop/sentri"]
        self.assertGreaterEqual(honest["windows"], 2)
        self.assertLess(abs(honest["error_median"]), 0.5)
        self.assertEqual(honest["covered"], 1.0)
        self.assertFalse(honest["calibrated"])
        seeded = copy.deepcopy(live)
        for rec in seeded:
            rec["now"]["between"] += 40
            rec["now"]["minutes"] = rec["now"]["between"] / rec["now"]["exit_rate"]
        wrong = combine.self_check([*seeded, later])["loop/sentri"]
        self.assertEqual(wrong["windows"], honest["windows"])
        self.assertAlmostEqual(wrong["error_median"], 40 / RATE, delta=0.5)


class DisplayTest(unittest.TestCase):
    """The one rule for what the owner sees (owner decision 2026-10-26, ADR-0015 decision 2)."""

    @classmethod
    def setUpClass(cls):
        run = qs.run(**PAST_A)
        cls.end = run.at(run.minutes)
        m, cls.rec = measured(run)
        cls.timed = pair(k_eff={"loop": [timed_k(run, m)], "yolox": []}, primary_booths=10)

    def shown(self, out, show_minutes=True):
        return combine.display(wait(out), show_minutes)

    def test_minutes_only_for_a_sure_count_with_every_segment_known(self):
        out = combine.combine([self.rec], no_line_at_a(self.end), self.timed, self.end,
                              q_b={("loop", "sentri"): True})
        shown = self.shown(out)
        self.assertTrue(shown["minutes_shown"])
        self.assertEqual(shown["value"], str(round(wait(out)["minutes"])))
        self.assertTrue(shown["detail"].startswith("range "))

    def test_a_floor_is_shown_as_at_least_with_its_reasons(self):
        out = combine.combine([self.rec], no_line_at_a(self.end), pair(), self.end)
        shown = self.shown(out)
        self.assertFalse(shown["minutes_shown"])
        self.assertEqual(shown["value"], f"≥ {math.floor(wait(out)['low'])}")
        self.assertIn("B to the booths not timed", shown["detail"])

    def test_a_count_anchors_have_not_checked_lately_shows_a_dash_not_minutes(self):
        """No car has a colour from minute 120 on, as at night: the epoch keeps its anchored
        source and sd, but anchors last checked it over CHECK_MAX_MIN before the end, so transit
        flags Now. Every segment is known, and still the owner sees "—", not minutes."""
        run = qs.run(**PAST_A)
        dark = run.at(120.0)

        def unseen(cs):
            return tuple(replace(c, signature=None) if c.at >= dark else c for c in cs)
        m, rec = measured(replace(run, a=unseen(run.a), b=unseen(run.b)))
        self.assertIn(rec["epochs"][-1]["source"], transit._SURE)
        self.assertIsNotNone(rec["epochs"][-1]["offset_sd"])
        self.assertTrue(rec["now"]["flagged"])
        timed = pair(k_eff={"loop": [timed_k(run, m)], "yolox": []}, primary_booths=10)
        out = combine.combine([rec], no_line_at_a(self.end), timed, self.end,
                              q_b={("loop", "sentri"): True})
        self.assertTrue(all(s["known"] for s in wait(out)["segments"]))
        self.assertFalse(wait(out)["count"]["sure"])
        self.assertEqual(self.shown(out),
                         {"value": "—", "minutes_shown": False,
                          "detail": "count not checked by anchors in the last 60 min"})

    def test_a_line_down_at_the_live_edge_says_there_is_no_count(self):
        """Line B gives nothing for the last 5 min. Transit has no count now, though the epoch
        before the gap was anchored: the owner is told there is no count, not that anchors have
        not checked it lately."""
        run = qs.run(**PAST_A)
        gap = Gap(self.end - timedelta(minutes=5), 300.0, "test: line B down")
        _, rec = measured(run, b=[c for c in run.b if c.at < gap.after], gaps_b=(gap,))
        self.assertIsNone(rec["now"]["between"])
        self.assertIn(rec["epochs"][-1]["source"], transit._SURE)
        self.assertIsNotNone(rec["epochs"][-1]["offset_sd"])
        out = combine.combine([rec], no_line_at_a(self.end), pair(), self.end)
        self.assertEqual(self.shown(out), {"value": "—", "minutes_shown": False,
                                           "detail": "no count of the cars between the lines"})
        self.assertIn("count flagged: no count of the cars between the lines",
                      wait(out)["transient"])

    def test_show_minutes_false_shows_nothing_but_that_it_is_recorded(self):
        out = combine.combine([self.rec], no_line_at_a(self.end), self.timed, self.end,
                              q_b={("loop", "sentri"): True})
        shown = self.shown(out, show_minutes=False)
        self.assertEqual((shown["value"], shown["minutes_shown"]), ("", False))
        self.assertEqual(combine.status_text(out, show_minutes=False),
                         f"  camera wait: {combine.NOT_SHOWN}")

    def test_status_has_one_line_per_group_with_the_methods_side_by_side(self):
        yolox = dict(self.rec, method="yolox")
        normal = [dict(r, lane_type="normal") for r in (self.rec, yolox)]
        out = combine.combine([self.rec, yolox, *normal], no_line_at_a(self.end), pair(),
                              self.end)
        lines = combine.status_text(out).splitlines()
        self.assertEqual(len(lines), 2)
        self.assertTrue(lines[0].startswith("  camera wait normal (no CBP lane): loop: >= "))
        self.assertTrue(lines[1].startswith("  camera wait sentri (car_sentri): loop: >= "))
        self.assertIn("  |  yolox: >= ", lines[1])
        "\n".join(lines).encode("cp1252")          # a Windows console can print it


class TailTest(unittest.TestCase):
    """The line before A: where it is not known, the total is a floor."""

    def setUp(self):
        self.end = qs.Run.at(200.0)
        self.v = single_file_speed()

    def tail(self, queue, group="sentri"):
        return combine.tail_to_a(queue, pair(), group, self.end)

    def test_a_line_inside_a_to_b_adds_nothing(self):
        seg = self.tail(no_line_at_a(self.end))
        self.assertEqual((seg.minutes, seg.low, seg.high, seg.known), (0.0, 0.0, 0.0, True))

    def test_a_strip_not_read_lately_is_not_known(self):
        old = sentri_signals(self.end - timedelta(minutes=10), queue_m=40.0, speed=self.v)
        seg = self.tail(old)
        self.assertFalse(seg.known)
        self.assertEqual(seg.low, 0.0)
        self.assertIn("lane-5 not read", seg.why)

    def test_a_normal_lane_reaching_a_does_not_reach_sentri(self):
        queue = no_line_at_a(self.end)
        queue = [s for s in queue if s["evidence"]["lane"] != "lane-2"]
        queue += signals("pdn-north", "lane-2", self.end, queue_m=A_M + 7.0, speed=self.v)
        self.assertEqual(self.tail(queue).minutes, 0.0)
        normal = self.tail(queue, "normal")
        self.assertTrue(normal.known)
        self.assertEqual(normal.minutes, 0.0)         # the median of lanes 1-3
        self.assertAlmostEqual(normal.high, 7.0 / self.v / 60)

    def test_a_line_past_the_street_view_is_a_floor(self):
        queue = (sentri_signals(self.end, queue_m=75.0, speed=self.v, beyond=True)
                 + signals("pdn-south-street", "left-column", self.end, queue_m=48.0,
                           speed=self.v, beyond=True))
        seg = self.tail(queue)
        self.assertFalse(seg.known)
        self.assertAlmostEqual(seg.low, (R_M + 48.0) / self.v / 60, places=6)
        self.assertIn("past the street's view", seg.why)

    def test_a_line_past_the_near_edge_ending_between_the_views_is_a_floor(self):
        queue = (sentri_signals(self.end, queue_m=75.0, speed=self.v, beyond=True)
                 + signals("pdn-south-street", "left-column", self.end, state="no_queue"))
        seg = self.tail(queue)
        self.assertFalse(seg.known)
        self.assertIn("ends between the views", seg.why)

    def test_error_lines_are_skipped(self):
        queue = [{"camera": "pdn-north", "error": "StreamLost: test"}, *no_line_at_a(self.end)]
        self.assertTrue(self.tail(queue).known)

    def test_a_reading_after_at_is_not_used(self):
        """The tail at `at` rests on readings up to `at` only, the window estimate.within hands
        over, so a replay gives what the live tick gave. A queue reader ahead of the counting
        line changes nothing."""
        ahead = sentri_signals(self.end + timedelta(minutes=4), queue_m=A_M + 7.0,
                               speed=self.v, minutes=4)
        seg = self.tail(no_line_at_a(self.end) + ahead)
        self.assertEqual((seg.minutes, seg.known), (0.0, True))
        seg = self.tail(ahead)
        self.assertFalse(seg.known)
        self.assertIn("lane-5 not read", seg.why)

    def lanes(self, normal: dict, sentri: dict, street: dict) -> list[dict]:
        """Lanes 1-3 read as `normal`, lanes 4-5 as `sentri`, and both street columns as
        `street`."""
        out = []
        for lane in ("lane-1", "lane-2", "lane-3"):
            out += signals("pdn-north", lane, self.end, **normal)
        out += sentri_signals(self.end, **sentri)
        for column in ("left-column", "right-column"):
            out += signals("pdn-south-street", column, self.end, **street)
        return out

    def test_one_types_line_on_the_street_is_not_anothers_metered_line(self):
        """Lanes 1-3 run past pdn-north's near edge onto the street, and lanes 4-5 do not reach
        A. The street's line is the normal lanes' own, not a line metered before A, so it is not
        SENTRI's tail; whether SENTRI cars queue in it is the owner's to say, so SENTRI's tail
        is not known, with a floor of 0. The mirror case is the same for normal."""
        past = {"queue_m": 75.0, "speed": self.v, "beyond": True}
        short = {"queue_m": 20.0, "speed": self.v}
        street = {"queue_m": 40.0, "speed": self.v}
        queue = self.lanes(past, short, street)
        sentri = self.tail(queue)
        self.assertEqual((sentri.minutes, sentri.low, sentri.known), (None, 0.0, False))
        self.assertNotIn("metered", " ".join(sentri.info))
        self.assertIn("whether sentri cars queue in it is not known", sentri.why)
        normal = self.tail(queue, "normal")
        self.assertTrue(normal.known)
        self.assertGreater(normal.minutes, 0.0)
        mirror = self.tail(self.lanes(short, past, street), "normal")
        self.assertEqual((mirror.minutes, mirror.low, mirror.known), (None, 0.0, False))

    def test_a_street_line_with_no_lane_past_the_near_edge_is_metered_for_every_type(self):
        """No strip of pdn-north runs past its near edge, so a line on the street has free road
        to A: metered before A, and timed for each type (the street's columns are not typed)."""
        short = {"queue_m": 20.0, "speed": self.v}
        queue = self.lanes(short, short, {"queue_m": 60.0, "speed": self.v, "head": 30.0})
        for group in ("sentri", "normal"):
            seg = self.tail(queue, group)
            self.assertTrue(seg.known, group)
            self.assertAlmostEqual(seg.minutes, 30.0 / self.v / 60, msg=group)
            self.assertIn("metered before A", " ".join(seg.info))


class PairFactsTest(unittest.TestCase):
    """What pairs.json's K_eff and hand counts change, and what they do not."""

    @classmethod
    def setUpClass(cls):
        cls.run_ = qs.run(**PAST_A)
        cls.end = cls.run_.at(cls.run_.minutes)          # 17:20 UTC
        cls.m, cls.rec = measured(cls.run_)

    def b_to_booth(self, entry, **changes):
        facts = pair(k_eff={"loop": [entry], "yolox": []}, primary_booths=10, **changes)
        return combine.b_to_booth(self.rec, facts, self.end, q_b=True)

    def test_k_eff_holds_only_for_its_lane_type_and_hours(self):
        entry = timed_k(self.run_, self.m)
        self.assertTrue(self.b_to_booth(entry).known)
        self.assertTrue(self.b_to_booth(dict(entry, hours_utc=[16, 18])).known)
        self.assertTrue(self.b_to_booth(dict(entry, hours_utc=[22, 18])).known)   # past 0:00
        for other in (dict(entry, lane_type="normal"), dict(entry, hours_utc=[18, 22]),
                      dict(entry, hours_utc=[0, 17])):
            seg = self.b_to_booth(other)
            self.assertFalse(seg.known, other)
            self.assertIn("not timed", seg.why)

    def test_no_booth_count_leaves_the_canopy_unbounded(self):
        facts = pair(k_eff={"loop": [timed_k(self.run_, self.m)], "yolox": []})
        seg = combine.b_to_booth(self.rec, facts, self.end, q_b=True)
        self.assertFalse(seg.known)
        self.assertIsNone(seg.high)
        self.assertIn("primary booth count", seg.why)

    def test_hand_counts_and_timings_lift_or_raise_their_flags(self):
        def validation(**changes):
            out = combine.combine([self.rec], no_line_at_a(self.end), pair(**changes), self.end,
                                  q_b={("loop", "sentri"): True})
            return wait(out)["validation"]

        hand = "no hand count within 5% at A and B for loop at this hour"
        good = [{"method": "loop", "lane_type": "sentri", "line": line, "capture": 0.97}
                for line in ("A", "B")]
        self.assertIn(hand, validation())
        self.assertNotIn(hand, validation(hand_counts=good))
        self.assertIn(hand, validation(hand_counts=good[:1]))
        self.assertIn(hand, validation(hand_counts=[dict(h, capture=0.9) for h in good]))
        self.assertIn(hand, validation(hand_counts=[dict(h, method="yolox") for h in good]))
        two = dict(timed_k(self.run_, self.m, minutes=(150, 170)))
        self.assertIn("K_eff from fewer than 3 timings",
                      validation(k_eff={"loop": [two], "yolox": []}, primary_booths=10))
        self.assertNotIn("K_eff from fewer than 3 timings",
                         validation(k_eff={"loop": [timed_k(self.run_, self.m)], "yolox": []},
                                    primary_booths=10))


class PairsFileTest(unittest.TestCase):
    def test_pdn_norths_pair_as_the_owner_decided(self):
        """pairs.json holds pair-level facts only; minutes are shown (owner, 2026-10-26) and
        the camera does not vote (ADR-0015 decision 3 proposed); nothing is timed yet."""
        with open(PAIRS, encoding="utf-8") as f:
            raw = json.load(f)["pdn-north"]
        self.assertEqual((raw["a"], raw["b"], raw["street"]),
                         ("pdn-north", "city-pdn-north", "pdn-south-street"))
        self.assertIs(raw["show_minutes"], True)
        self.assertIs(raw["camera_votes"], False)
        self.assertEqual(raw["k_eff"], {"loop": [], "yolox": []})
        self.assertEqual(raw["hand_counts"], [])
        self.assertIsNone(raw["primary_booths"])
        self.assertFalse(raw["line_a_verified"] or raw["line_b_verified"])
        self.assertFalse({"a_m", "r_m", "a_lanes"} & set(raw))   # computed at load


if __name__ == "__main__":
    unittest.main()
