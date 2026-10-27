"""The two-camera crossing-time runner: what becomes a gap, what is kept, and what is written."""
from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
import unittest.mock
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import numpy as np
import pytest

pytest.importorskip("cv2")

from test_border_camera_transit import T0 as SIM_T0
from test_border_camera_transit import simulate, wheel

from scraper.border.camera import runner as runner_mod
from scraper.border.camera import transit
from scraper.border.camera.calibration import camera_from_dict, load_cameras
from scraper.border.camera.crossing import SIGNATURE_TTL_S, Crossing
from scraper.border.camera.hls import Blocked, Gap, Stamped
from scraper.border.camera.runner import TransitRunner
from scraper.border.camera.stream import FROZEN_SECONDS, StreamFrozen, StreamLost

T0 = datetime(2026, 10, 24, 18, 0, tzinfo=UTC)
QUAD = [[100, 300], [300, 300], [300, 200], [100, 200]]
LOOP = ("loop", "mixed")     # the loop method's measurement of a pair with no lane type


def cam(cid: str, line: str, lane_type: str | None = None, quad=QUAD, frame_size=(640, 360),
        mask=()):
    return camera_from_dict(cid, {
        "title": cid, "port_number": "240202", "direction": "north",
        "frame_size": list(frame_size), "lanes": [], "mask": [list(m) for m in mask],
        "count_zones": [{"line": line, "lane": "l1", "quad": quad, "lane_type": lane_type}]})


def typed(cid: str, line: str, lanes: dict[str, str | None],
          methods: dict[str, list[str]] | None = None):
    """A camera whose line has one zone per lane, each of the lane type given, and counted by
    the methods `methods` gives for its lane (cameras.json "methods"; every method if none)."""
    return camera_from_dict(cid, {
        "title": cid, "port_number": "240202", "direction": "north", "frame_size": [640, 360],
        "lanes": [], "count_zones": [
            {"line": line, "lane": lane, "quad": [[x + 40 * i, y] for x, y in QUAD],
             "lane_type": lane_type,
             **({"methods": methods[lane]} if lane in (methods or {}) else {})}
            for i, (lane, lane_type) in enumerate(lanes.items())]})


def zone_lanes(counter) -> list[str]:
    """The lanes of the zones a real counter counts at."""
    zones = counter.zones if counter.method == "yolox" else [lp.zone for lp in counter._loops]
    return [z.lane for z in zones]


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

    def test_a_road_saved_for_a_zone_since_redrawn_is_not_seeded(self):
        # seed() checks only the patch's shape, and a zone moved along the road (or a camera now
        # read at another size) keeps it: the old road would read the new place as a car. So
        # does a changed mask: the pixels it covered were learnt as 0, and uncovered they would.
        runner_mod._save_refs(self.a, self.ca.references())
        moved = cam("cam-a", "A", quad=[[x, y - 40] for x, y in QUAD])
        resized = cam("cam-a", "A", frame_size=(1280, 720))
        masked = cam("cam-a", "A", mask=[(0, 240, 640, 10)])
        for changed in (moved, resized, masked):
            with self.assertLogs(runner_mod.log, "WARNING"):
                self.assertEqual(runner_mod._load_refs(changed), {})
        self.assertEqual(list(runner_mod._load_refs(self.a)), [("A", "l1")])

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
            half = TransitRunner(cam("s-a", "A", "sentri"), cam("u-b", "B"))   # B untyped
        self.assertEqual(general.groups, ("general",))
        self.assertEqual(split.groups, ("mixed",))
        self.assertEqual(half.groups, ("mixed",))

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
        self.assertIn(LOOP, self.run_.previous)
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
        start, prior = self.run_._start(LOOP, T0, [gap])
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

    def play(self, minutes, ticks, b_down=(), night=None, seed=1, miss_b=0.0):
        """Frames at both cameras every STEP_S for `minutes`, but none at B over each (from, to)
        minutes in `b_down`; the first frame after reports the reconnect gap. measure() at each
        minute in `ticks`. From minute `night` on no car has a colour, so none can anchor. B's
        counter misses a share `miss_b` of the cars."""
        sim = simulate(seed=seed, minutes=minutes, steady=True, colourless_after_min=night,
                       miss_b=miss_b)
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
        self.assertEqual([t for t, measured in out.items() if LOOP not in measured], [])

    def test_after_a_long_outage_at_one_line_the_count_starts_afresh(self):
        # B is down from minute 200 to 375.5. By then the 3 h horizon has slid up to the outage,
        # so the start moves past it, beyond anything the last measurement saw: no count can be
        # carried there from it.
        _, out = self.play(380, ticks=[195, 300, 370, 380], b_down=[(200, 375.5)])
        self.assertEqual(out[195][LOOP][0].epochs[0].source, "anchors")
        self.assertIn(LOOP, out[380])
        self.assertNotIn(out[380][LOOP][0].epochs[0].source, transit._SURE)

    def test_after_a_long_outage_at_night_nothing_comes_back_sure(self):
        # B is down from minute 150 to 345, and from minute 300 no car has a colour. Before the
        # audit's fixes (e2df7e2) no tick was measured from 340 to 520, and from 530 the count
        # from before the outage came back "continued": unflagged, 1313-1330 cars between
        # against a truth of 307-324.
        _, out = self.play(560, ticks=range(140, 561, 10), b_down=[(150, 345)], night=300)
        self.assertEqual(out[340], {})                  # B more than the TTL behind
        for t in range(350, 561, 10):
            self.assertIn(LOOP, out[t], t)
            measured, now = out[t][LOOP]
            self.assertEqual([e.source for e in measured.epochs if e.source in transit._SURE], [],
                             t)
            self.assertTrue(now is None or now.flagged, t)

    def test_an_unchanged_start_is_measured_afresh_not_from_its_own_last_guess(self):
        # In the first 3 h the start stays put: each tick must refit from all the anchors so far,
        # as one measurement over the same crossings does.
        self.assert_measured_afresh(*self.play(130, ticks=range(20, 131, 10)))

    def test_an_unchanged_start_is_not_disputed_by_its_own_last_measurement(self):
        # With 3 % of cars missed at B the anchored count moves between ticks by more than its
        # tolerance. Opened from its own last output, the tick at minute 145 called the anchors
        # "disputed", flagged, where one measurement over the same crossings has them sure at
        # 338 against a truth of 337.
        self.assert_measured_afresh(*self.play(145, ticks=range(10, 146, 5), seed=3,
                                               miss_b=0.03))

    def test_a_tick_that_cannot_be_measured_leaves_nothing_for_the_next_to_carry(self):
        # A ValueError (here a stand-in for a count carried to a start a padded gap covers) drops
        # the last measurement: the next tick starts afresh, not from the one before it, which
        # could raise the same error tick after tick.
        real, priors = transit.measure, {}

        def measure(*args, **kw):
            priors[kw["end"]] = kw["prior"]
            if kw["end"] == SIM_T0 + timedelta(minutes=195):
                raise ValueError("a gap covers `start`, so a count given there cannot be continued")
            return real(*args, **kw)

        with unittest.mock.patch.object(transit, "measure", measure), \
                self.assertLogs(runner_mod.log, "WARNING"):
            _, out = self.play(200, ticks=[190, 195, 200])
        self.assertEqual(out[195], {})
        self.assertIn(LOOP, out[200])
        self.assertIsNone(priors[SIM_T0 + timedelta(minutes=200)])

    def assert_measured_afresh(self, sim, out):
        for t, measured in out.items():
            end = SIM_T0 + timedelta(minutes=t)
            alone = transit.measure([c for c in sim.a if c.at <= end],
                                    [c for c in sim.b if c.at <= end],
                                    runner_mod.LO_MIN, runner_mod.HI_MIN, start=SIM_T0, end=end)
            got = measured[LOOP][0].epochs[0]
            self.assertEqual((got.source, got.offset),
                             (alone.epochs[0].source, alone.epochs[0].offset), f"minute {t}")


def replay(crossings, method="loop"):
    """A Replay counter for `method`, its crossings relabelled to it."""
    counter = Replay([replace(c, method=method) for c in crossings])
    counter.method = method
    return counter


TRIPS = {"sentri": 8.0, "normal": 40.0}     # minutes from A to B, per lane type
LANES = {"sentri": ("lane-5", "col-e"), "normal": ("lane-1", "col-1")}     # (A lane, B column)


class LaneTypeGroups(unittest.TestCase):
    """Each lane type that zones at both lines carry is measured on its own (QUESTIONS.md Q-22):
    the k-th car at A is the (offset + k)-th at B only among cars of one lane type."""
    STEP_S = 5

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        patcher = unittest.mock.patch.dict(os.environ, {"CAMERA_STATE_DIR": self.dir.name})
        patcher.start()
        self.addCleanup(patcher.stop)

    def runner(self, a, b, methods=("loop",)):
        with unittest.mock.patch.object(TransitRunner, "_counters", lambda self, c: []):
            return TransitRunner(a, b, methods=methods)

    def two_types(self, methods=("loop",)):
        return self.runner(typed("cam-a", "A", {"lane-1": "normal", "lane-5": "sentri"}),
                           typed("cam-b", "B", {"col-1": "normal", "col-e": "sentri"}), methods)

    def play(self, run, counters, minutes, ticks):
        """Frames at both cameras every STEP_S for `minutes`; measure() at each minute in
        `ticks`."""
        run.counters = counters
        due = {round(t * 60): t for t in ticks}
        out = {}
        for s in range(0, int(minutes * 60) + 1, self.STEP_S):
            for camera in run.cams:
                run._frame(camera, run.lines[camera.id],
                           SimpleNamespace(at=SIM_T0 + timedelta(seconds=s)))
            if s in due:
                out[due[s]] = run.measure()
        return out

    def test_each_lane_type_is_measured_on_its_own_with_its_own_trip(self):
        # SENTRI cars take 8 min from A to B and the others 40. Pooled, as the runner measured
        # before, the one "mixed" record had every window's median at 7.8-11.2 min, anchored and
        # unflagged: the 40-min cars were paired with SENTRI cars.
        minutes, sims, a, b = 200, {}, [], []
        for seed, (group, (lane_a, lane_b)) in enumerate(LANES.items(), 21):
            sims[group] = wheel(seed, TRIPS[group], minutes=minutes)
            a += [replace(c, camera="cam-a", lane=lane_a) for c in sims[group][0]]
            b += [replace(c, camera="cam-b", lane=lane_b) for c in sims[group][1]]
        run = self.two_types()
        out = self.play(run, {"cam-a": [replay(a)], "cam-b": [replay(b)]}, minutes,
                        [180, 190, 200])
        for t, measured in out.items():
            self.assertEqual(list(measured), [("loop", "normal"), ("loop", "sentri")], t)
            records = run.records(measured)
            self.assertEqual([(r["method"], r["lane_type"]) for r in records],
                             [("loop", "normal"), ("loop", "sentri")], t)
            for rec in records:
                trip, at = TRIPS[rec["lane_type"]], (t, rec["lane_type"])
                medians = [w["by_exit"]["median"] for w in rec["windows"]
                           if not w["flagged"] and w["by_exit"]["median"] is not None]
                self.assertGreaterEqual(len(medians), 8, at)
                for median in medians:
                    self.assertAlmostEqual(median, trip, delta=1.0, msg=at)
        # From minute 190 the 3 h horizon moves each start, and each type carries its own count
        # there from its own last measurement: about 50 cars between for SENTRI, 240 for the rest.
        for group, (sa, sb, at_t0) in sims.items():
            prior = run.previous[("loop", group)][1]
            self.assertIsNotNone(prior, group)
            truth = (at_t0 + sum(1 for c in sa if c.at < prior.at)
                     - sum(1 for c in sb if c.at < prior.at))
            self.assertTrue(prior.sure, group)
            self.assertAlmostEqual(prior.between, truth, delta=3, msg=group)
        # The status line has each type's own median transit.
        lines = run.status(out[200]).splitlines()[1:]
        self.assertEqual([line.split(":")[0].split() for line in lines],
                         [["loop", "normal"], ["loop", "sentri"]])
        for line, group in zip(lines, ("normal", "sentri")):
            median = float(line.split("median transit ")[1].split()[0])
            self.assertAlmostEqual(median, TRIPS[group], delta=1.0, msg=line)

    def test_a_pair_with_no_lane_type_pools_every_lane_as_one_mixed_group_per_method(self):
        # Cameras typed nowhere are measured as before Q-22: every lane at once, "mixed".
        sa, sb, _ = wheel(23, 30.0, minutes=60)
        a = [replace(c, camera="cam-a", lane=f"l{i % 2}") for i, c in enumerate(sa)]
        b = [replace(c, camera="cam-b", lane="c1") for c in sb]
        run = self.runner(typed("cam-a", "A", {"l0": None, "l1": None}),
                          typed("cam-b", "B", {"c1": None}), methods=("loop", "yolox"))
        self.assertEqual(run.groups, ("mixed",))
        out = self.play(run, {"cam-a": [replay(a), replay(a, "yolox")],
                              "cam-b": [replay(b), replay(b, "yolox")]}, 60, [60])
        measured = out[60]
        self.assertEqual(list(measured), [("loop", "mixed"), ("yolox", "mixed")])
        self.assertEqual([(r["method"], r["lane_type"]) for r in run.records(measured)],
                         [("loop", "mixed"), ("yolox", "mixed")])
        for key, (m, _) in measured.items():
            self.assertEqual((len(m.a_times), len(m.b_times)), (len(a), len(b)), key)

    def test_a_lane_whose_type_the_other_line_lacks_is_not_measured(self):
        # lane-2 has no type, lane-3's type is at A only and col-2's at B only: none is in a
        # group, and a warning names each.
        with self.assertLogs(runner_mod.log, "WARNING") as logs:
            run = self.runner(
                typed("cam-a", "A", {"lane-1": "normal", "lane-2": None, "lane-3": "ready"}),
                typed("cam-b", "B", {"col-1": "normal", "col-2": "sentri"}))
        self.assertEqual(run.groups, ("normal",))
        for lane in ("lane-2", "lane-3", "col-2"):
            self.assertIn(lane, "\n".join(logs.output))
        sa, sb, _ = wheel(24, 20.0, minutes=60)
        a = [replace(c, camera="cam-a", lane=f"lane-{1 + i % 3}") for i, c in enumerate(sa)]
        b = [replace(c, camera="cam-b", lane=f"col-{1 + i % 2}") for i, c in enumerate(sb)]
        out = self.play(run, {"cam-a": [replay(a)], "cam-b": [replay(b)]}, 60, [60])
        self.assertEqual(list(out[60]), [("loop", "normal")])
        m = out[60][("loop", "normal")][0]
        self.assertEqual(len(m.a_times), sum(1 for c in a if c.lane == "lane-1"))
        self.assertEqual(len(m.b_times), sum(1 for c in b if c.lane == "col-1"))

    def test_pdn_norths_pair_is_measured_as_sentri_and_normal(self):
        # Q-22: SENTRI is A's lane-5 and B's column E; every other lane and column is normal.
        # Every lane is in a group. The one warning is that the loop does not measure SENTRI
        # (the next test says why).
        cameras = load_cameras()
        with self.assertLogs(runner_mod.log, "WARNING") as logs:
            run = self.runner(cameras["pdn-north"], cameras["city-pdn-north"],
                              methods=("loop", "yolox"))
        self.assertEqual(len(logs.output), 1, logs.output)
        self.assertIn("loop sentri (city-pdn-north col-e", logs.output[0])
        self.assertEqual(run.groups, ("normal", "sentri"))
        self.assertEqual(list(run.not_measured), [("loop", "sentri")])
        for line, sentri in (("A", {"lane-5"}), ("B", {"col-e"})):
            self.assertEqual({lane for lane, t in run.types[line].items() if t == "sentri"},
                             sentri)
            self.assertEqual(set(run.types[line].values()), {"normal", "sentri"})

    def test_pdn_norths_loop_leaves_column_e_out_and_judges_the_city_columns_together(self):
        # Line-B review, 2026-10-26: in both daylight City frames a col-4 car covers col-e, and
        # in a rendered pass the loop counted such cars there, so col-e is counted by yolox only
        # and the loop's SENTRI is never measured. col-1..col-4 share their sides and are given
        # equal metres, so a car across a column line is judged by both columns (loop._beside),
        # as line A's lanes are. With lengths read from square pixels (1.4-3.0 m) none were.
        cameras = load_cameras()
        with self.assertLogs(runner_mod.log, "WARNING"):
            run = TransitRunner(cameras["pdn-north"], cameras["city-pdn-north"],
                                methods=("loop",))     # no saved roads: a fresh state dir
        city, pdn = run.counters["city-pdn-north"][0], run.counters["pdn-north"][0]
        self.assertEqual(zone_lanes(city), ["col-1", "col-2", "col-3", "col-4"])
        self.assertEqual([(zone_lanes(city)[i], zone_lanes(city)[j]) for i, j in city._beside],
                         [("col-1", "col-2"), ("col-2", "col-3"), ("col-3", "col-4")])
        # lane-5 stays in A's loop: a car across lane-4 and lane-5 is still judged by both.
        self.assertEqual(zone_lanes(pdn), [f"lane-{i}" for i in range(1, 6)])
        self.assertIn(("lane-4", "lane-5"),
                      [(zone_lanes(pdn)[i], zone_lanes(pdn)[j]) for i, j in pdn._beside])
        self.assertEqual(list(run.not_measured), [("loop", "sentri")])

    def test_a_group_with_a_zone_its_method_does_not_count_is_not_measured_by_it(self):
        # col-e is counted by yolox only. A loop count of SENTRI at B would miss col-e's cars or
        # hold others, and pair A's SENTRI cars with them: (loop, sentri) is not measured, not
        # recorded, and the status says why. Crossings a loop counter reports at col-e anyway
        # are never measured. The other three are measured as before.
        b = typed("cam-b", "B", {"col-1": "normal", "col-e": "sentri"},
                  methods={"col-e": ["yolox"]})
        with self.assertLogs(runner_mod.log, "WARNING") as logs:
            run = self.runner(typed("cam-a", "A", {"lane-1": "normal", "lane-5": "sentri"}), b,
                              methods=("loop", "yolox"))
        a_cars = [Crossing("cam-a", "A", lane, SIM_T0 + timedelta(seconds=s), "loop")
                  for s, lane in ((0, "lane-1"), (30, "lane-5"), (60, "lane-1"))]
        b_cars = [Crossing("cam-b", "B", lane, SIM_T0 + timedelta(seconds=s), "loop")
                  for s, lane in ((120, "col-1"), (150, "col-e"), (180, "col-1"))]
        out = self.play(run, {"cam-a": [replay(a_cars), replay(a_cars, "yolox")],
                              "cam-b": [replay(b_cars), replay(b_cars, "yolox")]}, 5, [5])
        kept = [("loop", "normal"), ("yolox", "normal"), ("yolox", "sentri")]
        self.assertEqual(list(out[5]), kept)
        self.assertEqual([(r["method"], r["lane_type"]) for r in run.records(out[5])], kept)
        self.assertNotIn(("loop", "sentri"), run.previous)
        self.assertEqual(run.not_measured,
                         {("loop", "sentri"): "cam-b col-e is counted by yolox only"})
        self.assertIn("loop sentri (cam-b col-e is counted by yolox only)", "\n".join(logs.output))
        lines = run.status(out[5]).splitlines()[1:]
        self.assertEqual([line.split(":")[0].split() for line in lines],
                         [["loop", "normal"], ["loop", "sentri"], ["yolox", "normal"],
                          ["yolox", "sentri"]])
        self.assertEqual(lines[1].split(":", 1)[1].strip(),
                         "not measured: cam-b col-e is counted by yolox only")
        self.assertIn("B 1 (", lines[3])           # yolox counts col-e

    def test_each_counter_counts_only_the_zones_that_name_its_method(self):
        # The loop counter leaves col-e out and YOLOX keeps it. lane-5 stays in A's loop
        # although the loop does not measure SENTRI: a zone is left out by its own "methods".
        a = typed("cam-a", "A", {"lane-1": "normal", "lane-5": "sentri"})
        b = typed("cam-b", "B", {"col-1": "normal", "col-e": "sentri"},
                  methods={"col-e": ["yolox"]})
        with unittest.mock.patch("scraper.border.camera.detect.Yolox.load",
                                 return_value=object()), \
                self.assertLogs(runner_mod.log, "WARNING"):
            run = TransitRunner(a, b, methods=("loop", "yolox"))
        self.assertEqual({cid: {c.method: zone_lanes(c) for c in counters}
                          for cid, counters in run.counters.items()},
                         {"cam-a": {"loop": ["lane-1", "lane-5"], "yolox": ["lane-1", "lane-5"]},
                          "cam-b": {"loop": ["col-1"], "yolox": ["col-1", "col-e"]}})

    def test_a_camera_whose_zones_no_method_of_the_run_counts_gets_no_counter(self):
        # A LoopCounter with no zones refuses to start: the run goes on without one, and every
        # loop group is not measured.
        b = typed("cam-b", "B", {"col-1": "normal"}, methods={"col-1": ["yolox"]})
        with self.assertLogs(runner_mod.log, "WARNING"):
            run = TransitRunner(typed("cam-a", "A", {"lane-1": "normal"}), b, methods=("loop",))
        self.assertEqual([c.method for c in run.counters["cam-b"]], [])
        self.assertEqual(list(run.not_measured), [("loop", "normal")])

    def test_the_status_has_one_line_per_method_and_lane_type_with_its_own_counts(self):
        run = self.two_types(methods=("loop", "yolox"))
        a = [Crossing("cam-a", "A", lane, SIM_T0 + timedelta(seconds=s), "loop")
             for s, lane in ((0, "lane-1"), (30, "lane-1"), (60, "lane-1"), (90, "lane-5"),
                             (120, "lane-5"), (150, "lane-9"))]       # lane-9 has no zone
        self.play(run, {"cam-a": [replay(a)], "cam-b": []}, 3, [])
        lines = run.status({}).splitlines()[1:]
        self.assertEqual([line.split(":")[0].split() for line in lines],
                         [["loop", "normal"], ["loop", "sentri"], ["yolox", "normal"],
                          ["yolox", "sentri"]])
        self.assertIn("A 3 cars", lines[0])
        self.assertIn("A 2 cars", lines[1])
        self.assertIn("A 0 cars", lines[2])


if __name__ == "__main__":
    unittest.main()
