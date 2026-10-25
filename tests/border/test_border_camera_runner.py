"""The two-camera crossing-time runner: what becomes a gap, what is kept, and what is written."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
import unittest.mock
from datetime import UTC, datetime, timedelta

import numpy as np
import pytest

pytest.importorskip("cv2")

from scraper.border.camera import runner as runner_mod
from scraper.border.camera.calibration import camera_from_dict
from scraper.border.camera.crossing import SIGNATURE_TTL_S, Crossing
from scraper.border.camera.hls import Blocked, Gap, Stamped
from scraper.border.camera.runner import TransitRunner
from scraper.border.camera.stream import FROZEN_SECONDS, StreamFrozen, StreamLost

T0 = datetime(2026, 10, 24, 18, 0, tzinfo=UTC)
QUAD = [[100, 300], [300, 300], [300, 200], [100, 200]]


def cam(cid: str, line: str):
    return camera_from_dict(cid, {
        "title": cid, "port_number": "240202", "direction": "north", "frame_size": [640, 360],
        "lanes": [], "count_zones": [{"line": line, "lane": "l1", "quad": QUAD}]})


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


if __name__ == "__main__":
    unittest.main()
