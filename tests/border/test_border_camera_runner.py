"""The two-camera crossing-time runner: what becomes a gap, what is kept, and what is written."""
from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
import unittest.mock
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import numpy as np
import pytest

pytest.importorskip("cv2")

from test_border_camera_transit import T0 as SIM_T0
from test_border_camera_transit import simulate

from scraper.border.camera import runner as runner_mod
from scraper.border.camera import transit
from scraper.border.camera.calibration import camera_from_dict
from scraper.border.camera.crossing import SIGNATURE_TTL_S, Crossing
from scraper.border.camera.hls import Blocked, Gap, Stamped
from scraper.border.camera.runner import TransitRunner
from scraper.border.camera.stream import FROZEN_SECONDS, StreamFrozen, StreamLost

T0 = datetime(2026, 10, 24, 18, 0, tzinfo=UTC)
QUAD = [[100, 300], [300, 300], [300, 200], [100, 200]]


def cam(cid: str, line: str, lane_type: str | None = None):
    return camera_from_dict(cid, {
        "title": cid, "port_number": "240202", "direction": "north", "frame_size": [640, 360],
        "lanes": [], "count_zones": [{"line": line, "lane": "l1", "quad": QUAD,
                                      "lane_type": lane_type}]})


class FakeCounter:
    """Counts a car on the frames it is told to, and says 'learning' until it is told not to."""

    def __init__(self, camera, line, method="loop"):
        self.camera, self.line, self.method = camera, line, method
        self.learning = False
        self.cars_on: set[datetime] = set()
        self.gapped: list[Gap] = []

    def add(self, frame):
        if frame.at in self.cars_on:
            return [Crossing(self.camera.id, self.line, "l1", frame.at, self.method,
                             (0.0, 0.0, 0.5, 0.6))]
        return []

    def gap(self, gap):
        self.gapped.append(gap)

    def gaps(self):
        return []

    def states(self):
        return {(self.line, "l1"): "learning" if self.learning else "counting"}

    def references(self):
        return {(self.line, "l1"): (np.ones((4, 3), np.float32), np.zeros((4, 3), np.float32))}


def frame(at):
    return Stamped(np.zeros((360, 640, 3), np.uint8), at, 0)


class RunnerSetUp(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        patcher = unittest.mock.patch.dict(os.environ, {"CAMERA_STATE_DIR": self.dir.name})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.a, self.b = cam("cam-a", "A"), cam("cam-b", "B")
        with unittest.mock.patch.object(TransitRunner, "_counters", lambda self, c: []):
            self.run_ = TransitRunner(self.a, self.b, methods=("loop",))
        self.ca, self.cb = FakeCounter(self.a, "A"), FakeCounter(self.b, "B")
        self.run_.counters = {"cam-a": [self.ca], "cam-b": [self.cb]}

    def feed(self, camera, at):
        self.run_._frame(camera, self.run_.lines[camera.id], frame(at))

    def feed_both(self, seconds, step=30):
        for s in range(0, seconds, step):
            self.feed(self.a, T0 + timedelta(seconds=s))
            self.feed(self.b, T0 + timedelta(seconds=s))


class Runner(RunnerSetUp):
    def test_a_learning_line_is_a_gap_not_zero_cars(self):
        self.ca.learning = True
        self.feed(self.a, T0)
        self.feed(self.a, T0 + timedelta(minutes=3))
        open_gaps = self.run_._gaps_for("loop", "A", T0 + timedelta(minutes=3))
        self.assertEqual([round(g.seconds) for g in open_gaps], [180])
        self.ca.learning = False
        self.feed(self.a, T0 + timedelta(minutes=4))
        self.assertEqual([round(g.seconds) for g in self.run_.gaps[("loop", "A")]], [240])

    def test_a_stream_gap_reaches_the_counters_and_the_line(self):
        gap = Gap(T0, 12.0, "segment expired")
        self.run_._gap(self.a, gap)
        self.assertEqual(self.ca.gapped, [gap])
        self.assertEqual(self.run_.gaps[("loop", "A")], [gap])
        self.assertEqual(self.run_.gaps[("loop", "B")], [])

    def test_crossings_and_their_signatures_are_dropped_after_the_ttl(self):
        self.ca.cars_on = {T0}
        self.feed(self.a, T0)
        late = T0 + timedelta(seconds=SIGNATURE_TTL_S + 60)
        self.feed(self.a, late)
        self.feed(self.b, late)
        self.run_.measure()
        self.assertEqual(len(self.run_.crossings[("loop", "A")]), 0)

    def test_a_record_carries_counts_and_statistics_but_nothing_per_vehicle(self):
        self.ca.cars_on = {T0 + timedelta(seconds=s) for s in range(0, 1200, 30)}
        self.cb.cars_on = {T0 + timedelta(seconds=s) for s in range(600, 1800, 30)}
        for s in range(0, 1800, 30):
            self.feed(self.a, T0 + timedelta(seconds=s))
            self.feed(self.b, T0 + timedelta(seconds=s))
        records = self.run_.records(self.run_.measure())
        self.assertEqual(len(records), 1)
        text = json.dumps(records[0], default=str)
        for word in ("signature", "chroma", "vehicle", "lane\": \"l1"):
            self.assertNotIn(word, text)
        self.run_.write(records)
        written = [p for p in os.listdir(self.dir.name) if p.startswith("transit-")]
        self.assertEqual(len(written), 1)

    def test_learnt_loop_roads_survive_a_restart(self):
        runner_mod._save_refs(self.a, self.ca.references())
        loaded = runner_mod._load_refs(self.a)
        self.assertEqual(list(loaded), [("A", "l1")])
        np.testing.assert_array_equal(loaded[("A", "l1")][0], np.ones((4, 3), np.float32))

    def test_an_unreadable_saved_road_means_learning_afresh(self):
        runner_mod._refs_path(self.a).write_bytes(b"not an archive")
        self.assertEqual(runner_mod._load_refs(self.a), {})

    def test_a_camera_needs_exactly_one_counting_line(self):
        two = camera_from_dict("two", {
            "title": "t", "port_number": "1", "direction": "north", "frame_size": [640, 360],
            "lanes": [], "count_zones": [{"line": "A", "lane": "x", "quad": QUAD},
                                         {"line": "B", "lane": "y", "quad": QUAD}]})
        with self.assertRaises(ValueError):
            runner_mod._one_line(two)

    def test_a_truncated_saved_road_means_learning_afresh(self):
        runner_mod._save_refs(self.a, self.ca.references())
        path = runner_mod._refs_path(self.a)
        whole = path.read_bytes()
        path.write_bytes(whole[:len(whole) // 2])
        self.assertEqual(runner_mod._load_refs(self.a), {})

    def test_a_frame_is_seen_only_once_its_crossings_are_kept(self):
        # A tick between the two would read the frame as seen with none of its cars.
        seen = []
        self.ca.add = lambda f: seen.append(self.run_.last_at.get("cam-a")) or []
        self.feed(self.a, T0)
        self.feed(self.a, T0 + timedelta(seconds=1))
        self.assertEqual(seen, [None, T0])

    def test_each_record_names_its_method_even_with_no_cars(self):
        with unittest.mock.patch.object(TransitRunner, "_counters", lambda self, c: []):
            self.run_ = TransitRunner(self.a, self.b, methods=("loop", "yolox"))
        self.run_.counters = {
            "cam-a": [FakeCounter(self.a, "A"), FakeCounter(self.a, "A", "yolox")],
            "cam-b": [FakeCounter(self.b, "B"), FakeCounter(self.b, "B", "yolox")]}
        self.feed_both(3600)
        records = self.run_.records(self.run_.measure())
        self.assertEqual([r["method"] for r in records], ["loop", "yolox"])

    def test_a_record_pooling_lanes_not_confirmed_as_one_type_says_mixed(self):
        self.ca.cars_on = {T0 + timedelta(seconds=s) for s in range(0, 600, 60)}
        self.feed_both(600)
        records = self.run_.records(self.run_.measure())
        self.assertEqual([r["lane_type"] for r in records], ["mixed"])
        with unittest.mock.patch.object(TransitRunner, "_counters", lambda self, c: []):
            general = TransitRunner(cam("g-a", "A", "general"), cam("g-b", "B", "general"))
            split = TransitRunner(cam("g-a", "A", "general"), cam("s-b", "B", "sentri"))
        self.assertEqual(general._lane_type(), "general")
        self.assertEqual(split._lane_type(), "mixed")

    def test_a_record_does_not_carry_the_time_of_the_last_crossing(self):
        last = T0 + timedelta(minutes=29, seconds=30, microseconds=250000)
        self.ca.cars_on = {T0 + timedelta(seconds=s) for s in range(0, 1200, 30)}
        self.cb.cars_on = {last}
        self.feed_both(1770)
        self.feed(self.a, last)
        self.feed(self.b, last)
        records = self.run_.records(self.run_.measure())
        self.assertEqual(records[0]["seen_until"], (T0 + timedelta(minutes=29)).isoformat())
        self.assertNotIn(last.isoformat(), json.dumps(records[0], default=str))

    def test_nothing_measured_is_kept_once_a_camera_is_the_ttl_behind(self):
        self.ca.cars_on = {T0 + timedelta(seconds=s) for s in range(0, 1800, 30)}
        self.cb.cars_on = {T0 + timedelta(seconds=s) for s in range(600, 1800, 30)}
        self.feed_both(1800)
        self.run_.measure()
        self.assertIn("loop", self.run_.previous)
        self.feed(self.a, T0 + timedelta(seconds=1800 + SIGNATURE_TTL_S + 60))   # B is down
        self.assertEqual(self.run_.measure(), {})
        self.assertEqual(self.run_.previous, {})

    def test_per_vehicle_data_expires_by_wall_time_while_no_frame_arrives(self):
        self.ca.cars_on = {T0 + timedelta(seconds=s) for s in range(0, 1800, 30)}
        self.cb.cars_on = {T0 + timedelta(seconds=s) for s in range(600, 1800, 30)}
        self.feed_both(1800)
        self.run_.measure()
        later = time.monotonic() + SIGNATURE_TTL_S + 60     # both streams down since
        with unittest.mock.patch.object(runner_mod.time, "monotonic", lambda: later):
            self.assertEqual(self.run_.measure(), {})
        self.assertEqual(sum(len(kept) for kept in self.run_.crossings.values()), 0)
        self.assertEqual(self.run_.previous, {})


class ScriptedReader:
    """Stands in for hls.HlsReader: yields the given frame times, then raises `end`."""

    def __init__(self, times, end, gaps=()):
        self.times, self.end, self.gaps = times, end, list(gaps)
        self.http = unittest.mock.Mock()

    def frames(self, seconds=None):
        for at in self.times:
            yield frame(at)
        raise self.end


class Feeding(RunnerSetUp):
    """What `_feed` does around reconnects, freezes and blocks."""

    def feed_script(self, camera, readers):
        made = iter(readers)

        def next_reader(cam):
            try:
                return next(made)
            except StopIteration:
                self.run_._stop.set()
                raise StreamLost("script over") from None

        with unittest.mock.patch.object(runner_mod, "live_reader", next_reader), \
                unittest.mock.patch.object(runner_mod, "RECONNECT_S", 0.0):
            self.run_._feed(camera)

    def test_frames_replayed_after_a_reconnect_are_not_counted_twice(self):
        seen = []
        self.ca.add = lambda f: seen.append(f.at) or []
        first = [T0 + timedelta(seconds=s) for s in range(10)]
        again = [T0 + timedelta(seconds=s) for s in range(5, 15)]    # overlaps the first read
        self.feed_script(self.a, [ScriptedReader(first, StreamLost("dropped")),
                                  ScriptedReader(again, StreamLost("dropped"))])
        self.assertEqual(seen, sorted(set(seen)))
        self.assertEqual(len(seen), 15)
        resumed = [g for g in self.run_.gaps[("loop", "A")] if "reconnecting" in g.reason]
        self.assertTrue(all(g.seconds > 0 for g in resumed))

    def test_a_frozen_stretch_is_a_gap_not_an_empty_road(self):
        times = [T0 + timedelta(seconds=s) for s in range(60)]
        self.feed_script(self.a, [ScriptedReader(times, StreamFrozen("has not changed"))])
        frozen = [g for g in self.run_.gaps[("loop", "A")] if "frozen" in g.reason]
        self.assertEqual(len(frozen), 1)
        self.assertEqual(frozen[0].seconds, FROZEN_SECONDS)

    def test_a_city_camera_that_blocks_us_is_not_read_again(self):
        city = camera_from_dict("city", {
            "title": "City", "port_number": "240202", "direction": "north",
            "frame_size": [640, 360], "lanes": [], "display": False,
            "source": {"kind": "hls", "url": "https://example.org/x.m3u8",
                       "clock": "last-modified"},
            "count_zones": [{"line": "B", "lane": "l1", "quad": QUAD}]})
        self.run_.cams = (self.a, city)
        self.run_.lines["city"] = "B"
        self.run_.counters["city"] = [self.cb]
        calls = []

        def blocked(cam):
            calls.append(cam.id)
            return ScriptedReader([T0], Blocked("HTTP 403"))

        with unittest.mock.patch.object(runner_mod, "live_reader", blocked), \
                unittest.mock.patch.object(runner_mod, "RECONNECT_S", 0.0):
            self.run_._feed(city)                 # returns instead of retrying
        self.assertEqual(calls, ["city"])
        self.assertIn("city", self.run_.blocked)

    def test_a_reader_gap_found_ahead_reaches_the_counters_at_its_place(self):
        # The reader finds a missing segment before the frames reach it: forgetting the cars in
        # flight then would lose them in a stretch that counts as seen.
        events = []
        self.ca.add = lambda f: events.append(("frame", f.at)) or []
        self.ca.gap = lambda g: events.append(("gap", g.after))
        times = [T0 + timedelta(seconds=s) for s in [*range(9), *range(15, 20)]]
        hole = Gap(T0 + timedelta(seconds=8), 6.0, "segment expired")
        self.feed_script(self.a, [ScriptedReader(times, StreamLost("dropped"), gaps=[hole])])
        i = events.index(("gap", hole.after))
        self.assertEqual(events[i - 1], ("frame", T0 + timedelta(seconds=8)))
        self.assertEqual(events[i + 1], ("frame", T0 + timedelta(seconds=15)))
        self.assertEqual(self.run_.gaps[("loop", "A")], [hole])

    def test_signatures_expire_even_while_the_other_camera_is_down(self):
        self.ca.cars_on = {T0}
        self.feed(self.a, T0)
        self.feed(self.a, T0 + timedelta(seconds=SIGNATURE_TTL_S + 60))   # B never connects
        self.run_.measure()
        self.assertEqual(len(self.run_.crossings[("loop", "A")]), 0)

    def test_the_start_moves_past_a_gap_it_would_sit_just_before(self):
        gap = Gap(T0 + timedelta(minutes=5), 60.0, "segment expired")
        start, prior = self.run_._start("loop", T0, [gap])
        self.assertEqual(start, T0 + timedelta(minutes=6))
        self.assertIsNone(prior)


class Replay:
    """A counter that reports one simulated line's crossings as the frames reach them."""
    method = "loop"

    def __init__(self, crossings):
        self.todo = sorted(crossings, key=lambda c: c.at)
        self.i = 0

    def add(self, frame):
        found = []
        while self.i < len(self.todo) and self.todo[self.i].at <= frame.at:
            found.append(self.todo[self.i])
            self.i += 1
        return found

    def skip(self, at):              # the stream is down: these cars are never seen
        self.add(SimpleNamespace(at=at))

    def gap(self, gap):
        pass

    def gaps(self):
        return []

    def states(self):
        return {}


class Ticks(unittest.TestCase):
    """measure() tick after tick on a simulated steady queue (55 min from A to B,
    test_border_camera_transit.simulate), fed frame by frame as `_feed` feeds it."""
    STEP_S = 5

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        patcher = unittest.mock.patch.dict(os.environ, {"CAMERA_STATE_DIR": self.dir.name})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.a, self.b = cam("cam-a", "A"), cam("cam-b", "B")
        with unittest.mock.patch.object(TransitRunner, "_counters", lambda self, c: []):
            self.run_ = TransitRunner(self.a, self.b, methods=("loop",))

    def play(self, minutes, ticks, b_down=()):
        """Frames at both cameras every STEP_S for `minutes`, but none at B over each (from, to)
        minutes in `b_down`; the first frame after reports the reconnect gap. measure() at each
        minute in `ticks`."""
        sim = simulate(seed=1, minutes=minutes, steady=True)
        ra, rb = Replay(sim.a), Replay(sim.b)
        self.run_.counters = {"cam-a": [ra], "cam-b": [rb]}
        due = {round(t * 60): t for t in ticks}
        out = {}
        for s in range(0, int(minutes * 60) + 1, self.STEP_S):
            at = SIM_T0 + timedelta(seconds=s)
            self.run_._frame(self.a, "A", SimpleNamespace(at=at))
            if any(d0 * 60 < s < d1 * 60 for d0, d1 in b_down):
                rb.skip(at)
            else:
                last = self.run_.last_at.get("cam-b")
                if last is not None and (at - last).total_seconds() > self.STEP_S:
                    self.run_._gap(self.b, Gap(last, (at - last).total_seconds(),
                                               "stream down or reconnecting"))
                self.run_._frame(self.b, "B", SimpleNamespace(at=at))
            if s in due:
                out[due[s]] = self.run_.measure()
        return sim, out

    def test_a_reconnect_soon_after_the_start_does_not_stop_the_records(self):
        # B is down 30 s at minute 9, within RATE_WINDOW_MIN of the start, so the start moves
        # past it; the measurement before knew nothing of that gap.
        _, out = self.play(16, ticks=range(1, 17), b_down=[(9, 9.5)])
        self.assertEqual([t for t, measured in out.items() if "loop" not in measured], [])

    def test_after_a_long_outage_at_one_line_the_count_starts_afresh(self):
        # B is down from minute 200 to 375.5. By then the 3 h horizon has slid up to the outage,
        # so the start moves past it, beyond anything the last measurement saw: no count can be
        # carried there from it.
        _, out = self.play(380, ticks=[195, 300, 370, 380], b_down=[(200, 375.5)])
        self.assertEqual(out[195]["loop"][0].epochs[0].source, "anchors")
        self.assertIn("loop", out[380])
        self.assertNotIn(out[380]["loop"][0].epochs[0].source, transit._SURE)

    def test_an_unchanged_start_is_measured_afresh_not_from_its_own_last_guess(self):
        # In the first 3 h the start stays put: each tick must refit from all the anchors so far,
        # as one measurement over the same crossings does.
        sim, out = self.play(130, ticks=range(20, 131, 10))
        for t, measured in out.items():
            end = SIM_T0 + timedelta(minutes=t)
            alone = transit.measure([c for c in sim.a if c.at <= end],
                                    [c for c in sim.b if c.at <= end],
                                    runner_mod.LO_MIN, runner_mod.HI_MIN, start=SIM_T0, end=end)
            got = measured["loop"][0].epochs[0]
            self.assertEqual((got.source, got.offset),
                             (alone.epochs[0].source, alone.epochs[0].offset), f"minute {t}")


if __name__ == "__main__":
    unittest.main()
