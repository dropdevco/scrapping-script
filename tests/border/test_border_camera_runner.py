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
from scraper.border.camera.hls import Gap, Stamped
from scraper.border.camera.runner import TransitRunner

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


class Runner(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
