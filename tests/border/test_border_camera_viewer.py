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


def far(distance=400, uncertainty=40.0, verified=False):
    """A lane whose booth is out of view, its distance taken from a map unless `verified`."""
    return Lane("l", (0, 1), (0, 1), "far", distance, None, uncertainty, verified)


class LaneSummary(unittest.TestCase):
    def test_a_measured_wait_is_shown_in_minutes(self):
        roll = {"wait_min": 1.33, "low_min": 1.33, "high_min": 1.33, "speed_m_s": 0.5}
        out = lane_summary(est("queue"), lane(0), roll, NOW)
        self.assertEqual((out["minutes"], out["display_value"], out["kind"]), (1.3, "1", "estimate"))
        self.assertIsNone(out["why_no_minutes"])
        self.assertFalse(out["approx"])

    def test_no_queue_with_the_booth_in_view_is_zero(self):
        out = lane_summary(est("no_queue", queue=0, speed=None), lane(0), {}, NOW)
        self.assertEqual((out["minutes"], out["display_value"]), (0.0, "0"))

    def test_no_queue_with_an_unmeasured_booth_distance_says_why_there_is_no_number(self):
        out = lane_summary(est("no_queue", queue=0, speed=None), lane(None), {}, NOW)
        self.assertIsNone(out["minutes"])
        self.assertIn("booth distance not measured", out["why_no_minutes"])
        self.assertEqual(out["display_detail"].count("no line in view"), 1)

    def test_no_queue_with_the_booth_out_of_view_is_probably_under_at_the_slowest_speed(self):
        # 400 m +/- 40 left to the booth and the slowest recent lane moving 0.2 m/s: a line
        # hidden in those 440 m would probably take under 440 / 0.2 s. "Probably": a hidden
        # line could move slower still, so the page never prints a strict "<".
        out = lane_summary(est("no_queue", queue=0, speed=None), far(), {}, NOW,
                           slowest_speed=0.2, scale_verified=True)
        self.assertTrue(out["upper_bound"])
        self.assertAlmostEqual(out["minutes"], round(440 / 0.2 / 60, 1))
        self.assertEqual(out["display_value"], "under ~37")
        self.assertIn("probably", out["display_detail"])
        self.assertNotIn("<", out["display_value"])

    def test_an_unverified_scale_stretches_the_probably_under_figure(self):
        out = lane_summary(est("no_queue", queue=0, speed=None), far(), {}, NOW,
                           slowest_speed=0.2, scale_verified=False)
        self.assertAlmostEqual(out["minutes"], round(440 / 0.2 / 60 * 1.25, 1))
        self.assertIn("camera scale unverified", out["approx_reasons"])

    def test_no_queue_with_no_recent_speed_gives_no_number(self):
        out = lane_summary(est("no_queue", queue=0, speed=None), far(), {}, NOW,
                           slowest_speed=None)
        self.assertIsNone(out["minutes"])
        self.assertIn("no recent line", out["why_no_minutes"])

    def test_a_map_estimated_distance_marks_the_minutes_approximate_with_their_range(self):
        roll = {"wait_min": 20.0, "low_min": 15.0, "high_min": 27.0, "speed_m_s": 0.4}
        out = lane_summary(est("queue"), far(), roll, NOW)
        self.assertTrue(out["approx"])
        self.assertEqual((out["low_min"], out["high_min"]), (15.0, 27.0))
        self.assertEqual(out["display_value"], "≈ 20")
        self.assertIn("range 15–27 min", out["display_detail"])
        self.assertEqual(out["approx_reasons"], ["booth distance is a map estimate"])

    def test_an_unverified_scale_is_named_as_the_reason_not_the_map(self):
        roll = {"wait_min": 20.0, "low_min": 16.0, "high_min": 25.0, "speed_m_s": 0.4}
        out = lane_summary(est("queue"), far(uncertainty=0.0, verified=True), roll, NOW,
                           scale_verified=False)
        self.assertEqual(out["approx_reasons"], ["camera scale unverified"])
        self.assertNotIn("map estimate", out["display_detail"])

    def test_a_booth_in_view_is_never_approximate(self):
        roll = {"wait_min": 3.0, "low_min": 2.0, "high_min": 4.0, "speed_m_s": 0.4}
        out = lane_summary(est("queue"), lane(0), roll, NOW, scale_verified=False)
        self.assertFalse(out["approx"])

    def test_a_queue_running_out_of_view_is_a_floor_stated_at_its_low_end(self):
        # The floor is the range's low end (short distance, scale slack), not the central wait.
        roll = {"wait_min": 16.0, "low_min": 13.0, "high_min": None, "speed_m_s": 0.5,
                "lower_bound": True}
        out = lane_summary(est("queue", beyond=True), far(), roll, NOW)
        self.assertTrue(out["lower_bound"])
        self.assertEqual(out["display_value"], "≥ 13")
        self.assertIsNone(out["high_min"])
        self.assertIn("at least", out["display_detail"])

    def test_a_minute_too_short_to_time_uses_this_minutes_line_not_an_older_wait(self):
        # 15 minutes ago a long line gave 12 min; now a 30 m line sits at the booth, too short
        # to time. The page must show this line at the recent speed, not the old 12.
        roll = {"wait_min": 12.0, "low_min": 12.0, "high_min": 12.0, "speed_m_s": 0.5}
        out = lane_summary(est("insufficient", queue=30, speed=None), lane(0), roll, NOW)
        self.assertEqual(out["minutes"], 1.0)
        self.assertIn("this minute's line", out["note"])

    def test_a_minute_too_short_to_time_whose_line_runs_out_of_view_is_still_a_floor(self):
        roll = {"wait_min": 20.0, "speed_m_s": 0.5}
        out = lane_summary(est("insufficient", queue=60, speed=None, beyond=True), far(), roll, NOW)
        self.assertTrue(out["lower_bound"])
        self.assertEqual(out["display_value"], f"≥ {round((360 + 60) / 0.5 / 60)}")

    def test_a_minute_with_no_line_and_too_few_frames_says_so(self):
        out = lane_summary(est("insufficient", queue=0, speed=None), lane(0), {"speed_m_s": 0.5},
                           NOW)
        self.assertIsNone(out["minutes"])
        self.assertIn("too few frames", out["why_no_minutes"])

    def test_a_queue_without_a_booth_distance_says_so(self):
        out = lane_summary(est("queue"), lane(None), {"wait_min": None, "speed_m_s": 0.4}, NOW)
        self.assertIsNone(out["minutes"])
        self.assertIn("booth distance", out["why_no_minutes"])
        self.assertEqual(out["speed_m_s"], 0.4)

    def test_a_queue_that_has_not_moved_in_15_minutes_says_so(self):
        out = lane_summary(est("stalled", speed=0.0), lane(0), {"wait_min": None, "speed_m_s": 0.0},
                           NOW)
        self.assertIsNone(out["minutes"])
        self.assertIn("not moving", out["why_no_minutes"])

    def test_the_summary_serialises(self):
        json.dumps(lane_summary(est("queue"), lane(0), {"wait_min": 2.0, "speed_m_s": 0.5}, NOW))


class WorkerReadings(unittest.TestCase):
    """The worker around the reader: what it publishes, and that nothing stops it."""

    def setUp(self):
        from camera_scene import camera

        from scraper.border.camera.worker import Worker

        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        patcher = unittest.mock.patch.dict(os.environ, {"CAMERA_STATE_DIR": d.name})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.cam = camera(booth_distance_m=100)
        self.signals = []
        self.worker = Worker([self.cam], seconds=60, every=120, emit=self.signals.append,
                             alert=False)
        self.addCleanup(self.worker._held.close)

    def window(self, speed):
        wait = (100 + 40) / speed / 60
        return LaneEstimate(camera=self.cam.id, lane="car", port_number="000000",
                            direction="north", state="queue", frames=120, seconds=60,
                            queue_m=40.0, speed_m_s=speed, wait_min=wait, low_min=wait,
                            high_min=wait)

    def test_the_signal_carries_the_15_minute_wait_with_its_own_range(self):
        self.worker._summarise(self.cam, [self.window(0.1)], NOW)
        self.worker._summarise(self.cam, [self.window(0.5)], NOW)
        sig = self.signals[-1]
        rolled = round((100 + 40) / 0.3 / 60)              # 60 s at 0.1 and 60 s at 0.5
        self.assertEqual((sig["minutes"], sig["low"], sig["high"]), (rolled, rolled, rolled))
        lane = self.worker.status["cameras"][self.cam.id]["lanes"][0]
        self.assertEqual(round(lane["minutes"]), rolled)

    def test_an_unexpected_failure_marks_the_camera_and_does_not_stop_the_worker(self):
        with unittest.mock.patch("scraper.border.camera.worker.find",
                                 side_effect=RuntimeError("yt-dlp timed out")):
            self.worker.cycle()
        cam = self.worker.status["cameras"][self.cam.id]
        self.assertEqual(cam["phase"], "error")
        self.assertIn("yt-dlp timed out", cam["message"])
        self.assertIn("error", self.signals[-1])

    def test_cbp_is_fetched_alongside_the_cameras_not_before_them(self):
        camera_started = threading.Event()
        seen = []
        self.worker.compare_cbp = True
        with unittest.mock.patch.object(self.worker, "_cbp",
                                        lambda: seen.append(camera_started.wait(5))), \
                unittest.mock.patch.object(self.worker, "_one",
                                           lambda cam, started: camera_started.set()):
            self.worker.cycle()
        self.assertEqual(seen, [True])


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

    def test_the_page_is_served_and_prints_what_the_worker_decided(self):
        page = self.get("/")
        self.assertIn("Bridge Wait Times", page)
        self.assertIn("l.display_value", page)      # the sign, number and range come from Python
        self.assertIn("l.display_detail", page)

    def test_status_before_the_worker_writes_any_is_empty_not_an_error(self):
        self.assertEqual(json.loads(self.get("/status.json")), {"cameras": {}})

    def test_status_is_the_workers_file(self):
        with open(os.path.join(self.dir, "status.json"), "w", encoding="utf-8") as f:
            json.dump({"cameras": {"x": {"phase": "learning"}}}, f)
        self.assertEqual(json.loads(self.get("/status.json"))["cameras"]["x"]["phase"], "learning")


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


if __name__ == "__main__":
    unittest.main()
