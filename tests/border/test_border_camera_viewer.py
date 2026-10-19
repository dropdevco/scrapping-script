"""The viewer: what a lane reading turns into on the page, and the two routes it serves."""
from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest
import unittest.mock
import urllib.error
import urllib.request
from datetime import UTC, datetime

import pytest

pytest.importorskip("cv2")

from scraper.border.camera.calibration import Lane
from scraper.border.camera.measure import LaneEstimate
from scraper.border.camera.viewer import serve
from scraper.border.camera.worker import lane_summary

NOW = datetime(2026, 10, 19, 23, 0, tzinfo=UTC)


def est(state, queue=40.0, speed=0.5, beyond=False):
    return LaneEstimate(camera="c", lane="l", port_number="240204", direction="south", state=state,
                        frames=120, seconds=60, queue_m=queue, beyond_view=beyond, speed_m_s=speed)


def lane(booth_distance_m):
    return Lane("l", (0, 1), (0, 1), "near", booth_distance_m, None)


class LaneSummary(unittest.TestCase):
    def test_a_measured_wait_is_shown_in_minutes(self):
        out = lane_summary(est("queue"), lane(0), {"wait_min": 1.33, "speed_m_s": 0.5}, NOW)
        self.assertEqual(out["minutes"], 1.3)
        self.assertIsNone(out["why_no_minutes"])

    def test_no_queue_with_the_booth_in_view_is_zero(self):
        out = lane_summary(est("no_queue", queue=0, speed=None), lane(0), {}, NOW)
        self.assertEqual(out["minutes"], 0.0)

    def test_no_queue_with_the_booth_out_of_view_says_why_there_is_no_number(self):
        out = lane_summary(est("no_queue", queue=0, speed=None), lane(None), {}, NOW)
        self.assertIsNone(out["minutes"])
        self.assertIn("booth out of view", out["why_no_minutes"])

    def test_a_queue_without_a_booth_distance_says_so(self):
        out = lane_summary(est("queue"), lane(None), {"wait_min": None, "speed_m_s": 0.4}, NOW)
        self.assertIsNone(out["minutes"])
        self.assertIn("booth distance", out["why_no_minutes"])
        self.assertEqual(out["speed_m_s"], 0.4)

    def test_a_queue_running_out_of_view_is_a_floor(self):
        out = lane_summary(est("queue", beyond=True), lane(0), {"wait_min": 3.0}, NOW)
        self.assertTrue(out["lower_bound"])

    def test_the_summary_serialises(self):
        json.dumps(lane_summary(est("queue"), lane(0), {"wait_min": 2.0}, NOW))


class Routes(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        patcher = unittest.mock.patch.dict(os.environ, {"CAMERA_STATE_DIR": self.dir})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.server = serve(0)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def get(self, path):
        with urllib.request.urlopen(self.base + path) as r:
            return r.read().decode("utf-8")

    def test_the_page_is_served(self):
        self.assertIn("Bridge Wait Times", self.get("/"))

    def test_status_before_the_worker_writes_any_is_empty_not_an_error(self):
        self.assertEqual(json.loads(self.get("/status.json")), {"cameras": {}})

    def test_status_is_the_workers_file(self):
        with open(os.path.join(self.dir, "status.json"), "w", encoding="utf-8") as f:
            json.dump({"cameras": {"x": {"phase": "learning"}}}, f)
        self.assertEqual(json.loads(self.get("/status.json"))["cameras"]["x"]["phase"], "learning")


if __name__ == "__main__":
    unittest.main()


class Overlay(unittest.TestCase):
    """"What it measured" is served from the worker's memory, never from disk."""

    def test_the_latest_picture_comes_from_the_worker_and_unknown_cameras_404(self):
        server = serve(0)
        server.worker = unittest.mock.Mock(overlays={"lerdo-south-booths": b"fake-jpeg"})
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        base = f"http://127.0.0.1:{server.server_address[1]}"
        with urllib.request.urlopen(base + "/overlay/lerdo-south-booths.jpg?t=1") as r:
            self.assertEqual((r.headers["Content-Type"], r.read()), ("image/jpeg", b"fake-jpeg"))
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(base + "/overlay/nope.jpg")
        self.assertEqual(caught.exception.code, 404)
        caught.exception.close()


class OneWorkerPerStateDir(unittest.TestCase):
    def test_a_second_worker_on_the_same_state_dir_refuses_to_start(self):
        from scraper.border.camera.calibration import load_cameras
        from scraper.border.camera.worker import AlreadyRunning, Worker

        with tempfile.TemporaryDirectory() as d,                 unittest.mock.patch.dict(os.environ, {"CAMERA_STATE_DIR": d}):
            cams = [load_cameras()["lerdo-south-booths"]]
            first = Worker(cams, seconds=60, every=120, alert=False)
            with self.assertRaises(AlreadyRunning):
                Worker(cams, seconds=60, every=120, alert=False)
            first._held.close()                       # the first one stops
            Worker(cams, seconds=60, every=120, alert=False)._held.close()

    def test_the_page_shows_its_own_workers_status_not_the_shared_file(self):
        with tempfile.TemporaryDirectory() as d,                 unittest.mock.patch.dict(os.environ, {"CAMERA_STATE_DIR": d}):
            with open(os.path.join(d, "status.json"), "w", encoding="utf-8") as f:
                json.dump({"cameras": {"someone-elses": {}}}, f)
            server = serve(0)
            server.worker = unittest.mock.Mock(status={"cameras": {"mine": {"phase": "ok"}}},
                                               _lock=threading.Lock())
            threading.Thread(target=server.serve_forever, daemon=True).start()
            self.addCleanup(server.server_close)
            self.addCleanup(server.shutdown)
            url = f"http://127.0.0.1:{server.server_address[1]}/status.json"
            with urllib.request.urlopen(url) as r:
                self.assertEqual(list(json.loads(r.read())["cameras"]), ["mine"])
