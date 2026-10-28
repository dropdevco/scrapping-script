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
from datetime import UTC, datetime, timedelta

import pytest

pytest.importorskip("cv2")

import queue_sim as qs
from test_border_camera_combine import PAST_A, measured, no_line_at_a, pair, timed_k

from scraper.border.camera import combine, viewer
from scraper.border.camera.calibration import Lane
from scraper.border.camera.hls import Gap
from scraper.border.camera.measure import LaneEstimate
from scraper.border.camera.viewer import serve
from scraper.border.camera.worker import lane_summary

NOW = datetime(2026, 10, 19, 23, 0, tzinfo=UTC)
HIDDEN = "road out of view assumed to move like the line in view"
TAIL = "the line past the view not counted"


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
        self.assertEqual(out["display_value"], "no line")
        self.assertIn("booth distance not measured", out["why_no_minutes"])
        self.assertEqual(out["display_detail"].count("no line in view"), 1)

    def test_no_queue_with_the_booth_out_of_view_is_no_line_and_no_minutes(self):
        # No ceiling at a queue speed, however slow a lane moved lately: that was (D + L) / v
        # with L = 0, which read "under ~18" on an empty street (2026-10-27).
        for roll in ({}, {"speed_m_s": 0.2, "windows": 3}):
            for scale_verified in (True, False):
                with self.subTest(roll=roll, scale_verified=scale_verified):
                    out = lane_summary(est("no_queue", queue=0, speed=None), far(), roll, NOW,
                                       scale_verified=scale_verified)
                    self.assertEqual((out["minutes"], out["low_min"], out["high_min"]),
                                     (None, None, None))
                    self.assertEqual((out["display_value"], out["kind"]), ("no line", "clear"))
                    self.assertFalse(out["approx"])
                    self.assertFalse(out["lower_bound"])
                    self.assertNotIn("upper_bound", out)
                    self.assertIsNone(out["why_no_minutes"])
                    self.assertTrue(out["display_detail"].startswith("no line in view · "))
                    self.assertNotIn("under", out["display_value"] + out["display_detail"])

    def test_a_road_no_camera_wait_times_is_said_to_be_timed_by_nothing(self):
        # Lerdo and BOTA: no pair, so nothing times the 400 m on to the booth.
        out = lane_summary(est("no_queue", queue=0, speed=None), far(), {}, NOW)
        self.assertIn("the 400 m to the booth are out of view; nothing here times them",
                      out["display_detail"])
        self.assertNotIn("camera wait", out["display_detail"])

    def test_a_road_the_camera_wait_times_says_so(self):
        # PDN's camera A and its street: the camera wait times the road on to the booth.
        out = lane_summary(est("no_queue", queue=0, speed=None), far(510, 75), {}, NOW,
                           camera_wait=True)
        self.assertEqual(out["display_detail"], "no line in view · the 510 m to the booth are "
                                                "out of view: the camera wait times them")
        self.assertEqual(out["display_value"], "no line")

    def test_no_queue_with_the_booth_in_view_is_zero_on_a_camera_of_the_camera_wait_too(self):
        out = lane_summary(est("no_queue", queue=0, speed=None), lane(0), {}, NOW,
                           camera_wait=True)
        self.assertEqual((out["minutes"], out["display_value"]), (0.0, "0"))

    def test_a_line_on_a_camera_of_the_camera_wait_shows_its_length_and_speed_not_minutes(self):
        # The street's right column, 19:57 UTC: shown as "≈ 22" until 2026-10-28. With the booth
        # out of view the wait is the camera wait's, never (D + L) / v.
        roll = {"wait_min": 22.0, "low_min": 15.0, "high_min": None, "speed_m_s": 1.2,
                "windows": 3}
        out = lane_summary(est("queue", queue=51, speed=1.2, beyond=True), far(510, 75), roll,
                           NOW, scale_verified=False, camera_wait=True)
        self.assertEqual((out["minutes"], out["low_min"], out["high_min"]), (None, None, None))
        self.assertEqual((out["display_value"], out["kind"]), ("line 51 m+", "line"))
        self.assertEqual(out["display_detail"], "moving 1.20 m/s · the 510 m to the booth are "
                                                "out of view: the camera wait times them")
        self.assertFalse(out["approx"])
        self.assertFalse(out["lower_bound"])
        self.assertIsNone(out["why_no_minutes"])
        self.assertEqual(out["speed_m_s"], 1.2)

    def test_the_same_line_elsewhere_keeps_its_approximate_minutes(self):
        # Lerdo and BOTA have no camera wait, so their cards keep (D + L) / v (owner question).
        roll = {"wait_min": 22.0, "low_min": 15.0, "high_min": None, "speed_m_s": 1.2,
                "windows": 3}
        out = lane_summary(est("queue", queue=51, speed=1.2, beyond=True), far(510, 75), roll,
                           NOW, scale_verified=False)
        self.assertEqual((out["minutes"], out["display_value"], out["kind"]),
                         (22.0, "≈ 22", "estimate"))

    def test_a_line_not_moving_on_a_camera_of_the_camera_wait_says_so(self):
        roll = {"wait_min": None, "speed_m_s": 0.0, "windows": 3}
        out = lane_summary(est("stalled", speed=0.0), far(), roll, NOW, camera_wait=True)
        self.assertEqual((out["minutes"], out["display_value"]), (None, "line 40 m"))
        self.assertEqual(out["display_detail"],
                         "not moving over the last 3 readings · not moving this minute · the "
                         "400 m to the booth are out of view: the camera wait times them")
        self.assertIsNone(out["why_no_minutes"])

    def test_a_minute_too_short_to_time_on_a_camera_of_the_camera_wait_is_not_timed(self):
        roll = {"wait_min": 20.0, "speed_m_s": 0.5}
        out = lane_summary(est("insufficient", queue=30, speed=None), far(), roll, NOW,
                           camera_wait=True)
        self.assertEqual((out["minutes"], out["display_value"]), (None, "line 30 m"))
        self.assertIn("the camera wait times them", out["display_detail"])
        self.assertNotIn("this minute's line", out["display_detail"])

    def test_a_line_at_a_booth_in_view_keeps_its_minutes_on_a_camera_of_the_camera_wait(self):
        # Only a booth out of view hands the wait to the camera wait.
        roll = {"wait_min": 3.0, "low_min": 2.0, "high_min": 4.0, "speed_m_s": 0.4}
        out = lane_summary(est("queue"), lane(0), roll, NOW, camera_wait=True)
        self.assertEqual((out["minutes"], out["display_value"]), (3.0, "3"))

    def test_a_map_estimated_distance_marks_the_minutes_approximate_with_their_range(self):
        roll = {"wait_min": 20.0, "low_min": 15.0, "high_min": 27.0, "speed_m_s": 0.4}
        out = lane_summary(est("queue"), far(), roll, NOW)
        self.assertTrue(out["approx"])
        self.assertEqual((out["low_min"], out["high_min"]), (15.0, 27.0))
        self.assertEqual(out["display_value"], "≈ 20")
        self.assertIn("range 15–27 min", out["display_detail"])
        self.assertEqual(out["approx_reasons"], ["booth distance is a map estimate", HIDDEN])

    def test_an_unverified_scale_is_named_as_the_reason_not_the_map(self):
        roll = {"wait_min": 20.0, "low_min": 16.0, "high_min": 25.0, "speed_m_s": 0.4}
        out = lane_summary(est("queue"), far(uncertainty=0.0, verified=True), roll, NOW,
                           scale_verified=False)
        self.assertEqual(out["approx_reasons"], ["camera scale unverified", HIDDEN])
        self.assertNotIn("map estimate", out["display_detail"])

    def test_a_surveyed_booth_out_of_view_is_still_approximate(self):
        # Distance and scale known, the road out of view is still timed at the visible speed.
        roll = {"wait_min": 20.0, "low_min": 18.0, "high_min": 22.0, "speed_m_s": 0.4}
        out = lane_summary(est("queue"), far(uncertainty=0.0, verified=True), roll, NOW)
        self.assertEqual((out["display_value"], out["approx_reasons"]), ("≈ 20", [HIDDEN]))

    def test_a_booth_in_view_is_never_approximate(self):
        roll = {"wait_min": 3.0, "low_min": 2.0, "high_min": 4.0, "speed_m_s": 0.4}
        out = lane_summary(est("queue"), lane(0), roll, NOW, scale_verified=False)
        self.assertFalse(out["approx"])

    def test_a_queue_running_out_of_view_is_a_floor_stated_at_its_low_end(self):
        # The floor is the range's low end (fastest speed), not the central wait.
        roll = {"wait_min": 16.0, "low_min": 13.0, "high_min": None, "speed_m_s": 0.5,
                "lower_bound": True}
        out = lane_summary(est("queue", beyond=True), lane(0), roll, NOW)
        self.assertTrue(out["lower_bound"])
        self.assertEqual(out["display_value"], "≥ 13")
        self.assertIsNone(out["high_min"])
        self.assertIn("at least", out["display_detail"])
        self.assertNotIn(TAIL, out["display_detail"])   # the "at least" already says it

    def test_a_queue_running_out_of_view_with_the_booth_out_of_view_is_no_floor(self):
        # 365 m out of view timed at the visible line's speed: if that stretch moves faster
        # (merges, gaps), the true wait is below the range's low end, so no "at least".
        roll = {"wait_min": 70.8, "low_min": 52.0, "high_min": None, "speed_m_s": 0.1,
                "lower_bound": False}
        out = lane_summary(est("queue", queue=60, speed=0.1, beyond=True), far(365, 35), roll,
                           NOW, scale_verified=False)
        self.assertEqual((out["kind"], out["lower_bound"]), ("estimate", False))
        self.assertEqual(out["display_value"], "≈ 71")
        self.assertIsNone(out["high_min"])
        self.assertNotIn("≥", out["display_value"])
        self.assertNotIn("at least", out["display_detail"])
        self.assertIn(HIDDEN, out["display_detail"])
        # With no "at least", the page must say the minutes stop at the edge of the view.
        self.assertIn(TAIL, out["approx_reasons"])

    def test_a_minute_too_short_to_time_uses_this_minutes_line_not_an_older_wait(self):
        # 15 minutes ago a long line gave 12 min; now a 30 m line sits at the booth, too short
        # to time. The page must show this line at the recent speed, not the old 12.
        roll = {"wait_min": 12.0, "low_min": 12.0, "high_min": 12.0, "speed_m_s": 0.5}
        out = lane_summary(est("insufficient", queue=30, speed=None), lane(0), roll, NOW)
        self.assertEqual(out["minutes"], 1.0)
        self.assertIn("this minute's line", out["note"])

    def test_a_minute_too_short_to_time_whose_line_runs_out_of_view_is_still_a_floor(self):
        roll = {"wait_min": 20.0, "speed_m_s": 0.05}
        out = lane_summary(est("insufficient", queue=60, speed=None, beyond=True), lane(0), roll,
                           NOW)
        self.assertTrue(out["lower_bound"])
        self.assertEqual(out["display_value"], f"≥ {round(60 / 0.05 / 60)}")

    def test_a_minute_too_short_to_time_with_the_booth_out_of_view_is_an_estimate(self):
        # No floor, and no high end either: the line runs on past the view.
        roll = {"wait_min": 20.0, "speed_m_s": 0.5}
        out = lane_summary(est("insufficient", queue=60, speed=None, beyond=True), far(), roll, NOW)
        self.assertEqual((out["kind"], out["lower_bound"]), ("estimate", False))
        self.assertEqual(out["display_value"], f"≈ {round((400 + 60) / 0.5 / 60)}")
        self.assertIsNone(out["high_min"])
        self.assertNotIn("range", out["display_detail"])
        self.assertIn(TAIL, out["display_detail"])

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

    def test_a_camera_of_the_camera_wait_shows_no_minutes_but_publishes_the_same_signal(self):
        # The card hands the wait to the camera wait; the signal is unchanged (it does not vote
        # while the strip has no cbp_lane: see CameraWaitCameras).
        self.worker.camera_wait = frozenset({self.cam.id})
        self.worker._summarise(self.cam, [self.window(0.5)], NOW)
        lane = self.worker.status["cameras"][self.cam.id]["lanes"][0]
        self.assertEqual((lane["minutes"], lane["display_value"]), (None, "line 40 m"))
        self.assertIn("the camera wait times them", lane["display_detail"])
        self.assertEqual(self.signals[-1]["minutes"], round((100 + 40) / 0.5 / 60))

    def test_the_worker_knows_the_camera_waits_cameras_from_pairs_json(self):
        self.assertEqual(self.worker.camera_wait, frozenset({"pdn-north", "pdn-south-street"}))
        self.assertNotIn(self.cam.id, self.worker.camera_wait)

    def test_an_unexpected_failure_marks_the_camera_and_does_not_stop_the_worker(self):
        with unittest.mock.patch("scraper.border.camera.worker.find",
                                 side_effect=RuntimeError("yt-dlp timed out")):
            self.worker.cycle()
        cam = self.worker.status["cameras"][self.cam.id]
        self.assertEqual(cam["phase"], "error")
        self.assertIn("yt-dlp timed out", cam["message"])
        self.assertIn("error", self.signals[-1])

    def test_output_that_has_gone_away_does_not_stop_the_cycle(self):
        def closed(_):
            raise BrokenPipeError("stdout closed")
        self.worker.emit = closed
        with unittest.mock.patch("scraper.border.camera.worker.find",
                                 side_effect=RuntimeError("yt-dlp timed out")):
            self.worker.cycle()
        self.assertIn("cycle_at", self.worker.status)
        self.assertEqual(self.worker.status["cameras"][self.cam.id]["phase"], "error")

    def test_a_full_disk_does_not_stop_the_cycle_and_the_page_still_updates(self):
        with unittest.mock.patch("scraper.border.camera.worker.find",
                                 side_effect=RuntimeError("yt-dlp timed out")), \
                unittest.mock.patch("pathlib.Path.replace", side_effect=OSError("disk full")):
            self.worker.cycle()
        self.assertIn("cycle_at", self.worker.status)
        self.assertEqual(self.worker.status["cameras"][self.cam.id]["phase"], "error")

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


class CameraWaitCameras(unittest.TestCase):
    """Which cards hand the wait to the camera wait: each pair's camera A and its street."""

    def test_camera_a_and_the_street_of_every_pair_and_nothing_else(self):
        from scraper.border.camera.worker import camera_wait_cameras

        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "pairs.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"_readme": {"a": "not-a-camera"},
                           "x": {"a": "x", "b": "x-b", "street": "x-street"},
                           "y": {"a": "y", "b": "y-b", "street": None}}, f)
            self.assertEqual(camera_wait_cameras(path), frozenset({"x", "x-street", "y"}))

    def test_their_strips_are_untyped_so_their_signals_never_vote(self):
        # Their cards show no (D + L) / v, but signal_from_roll still publishes it: harmless only
        # while no strip of theirs has a cbp_lane. Typing one must drop its signal's minutes too.
        from scraper.border.camera.calibration import load_cameras
        from scraper.border.camera.worker import camera_wait_cameras

        cams = load_cameras()
        typed = [(cid, lane.lane) for cid in camera_wait_cameras() for lane in cams[cid].lanes
                 if lane.cbp_lane is not None]
        self.assertEqual(typed, [])


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


class HiddenFeeds(unittest.TestCase):
    """The City of El Paso feeds are read at the owner's risk and never displayed (ADR-0014)."""

    def test_the_viewer_never_takes_a_camera_marked_not_to_display(self):
        from types import SimpleNamespace

        from scraper.border.camera import viewer
        from scraper.border.camera.calibration import load_cameras

        cams = load_cameras()
        self.assertFalse(cams["city-pdn-north"].display)
        made = {}

        class Stop(Exception):
            pass

        def fake_worker(chosen, **kw):
            made["chosen"] = [c.id for c in chosen]
            raise Stop

        args = SimpleNamespace(cameras="city-pdn-north,lerdo-south-booths", seconds=60,
                               every=120, json=False, port=0)
        with unittest.mock.patch("scraper.border.camera.worker.Worker", fake_worker),                 self.assertRaises(Stop):
            viewer.cmd_view(args, cams)
        self.assertEqual(made["chosen"], ["lerdo-south-booths"])


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


class EstimateSection(unittest.TestCase):
    """The page's camera-estimate section (owner decision 2026-10-26, ADR-0015 decision 2):
    per lane-type group, each counting method's wait for PDN northbound side by side, exactly as
    combine.display allows, with CBP's published wait beside it for comparison only."""

    @classmethod
    def setUpClass(cls):
        run = qs.run(**PAST_A)
        end = run.at(run.minutes)
        m, sure = measured(run)
        _, unsure = measured(run, start_min=100.0)
        cls.sure_a_to_b = combine.a_to_b(sure).minutes
        cls.pair = pair()
        records = [sure, dict(unsure, method="yolox"), dict(sure, lane_type="normal"),
                   dict(unsure, method="yolox", lane_type="normal")]
        cls.floor = combine.combine(records, no_line_at_a(end), cls.pair, end)
        timed = pair(k_eff={"loop": [timed_k(run, m)], "yolox": []}, primary_booths=10)
        cls.whole = combine.combine([sure], no_line_at_a(end), timed, end,
                                    q_b={("loop", "sentri"): True})
        cls.cbp = {"240202": {"car": {"state": "open", "minutes": 25, "label": None},
                              "car_ready": {"state": "open", "minutes": 20, "label": None},
                              "car_sentri": {"state": "closed", "minutes": None, "label": None}}}

    def snapshot(self, record, show=True):
        return {"pair": {"a": "pdn-north", "b": "city-pdn-north", "port_number": "240202",
                         "direction": "north", "show_minutes": show, "camera_votes": False},
                "record": record, "stopped": None, "updated_at": None}

    def test_each_value_and_reason_is_what_combine_display_gives(self):
        shown = viewer.estimate_view(self.snapshot(self.floor), self.cbp)
        self.assertEqual(shown["title"], "Paso del Norte northbound - camera estimate")
        self.assertEqual([g["group"] for g in shown["groups"]], ["normal", "sentri"])
        for g in shown["groups"]:
            self.assertEqual([m["method"] for m in g["methods"]], ["loop", "yolox"])
            self.assertFalse({"minutes", "low", "high", "mean", "average"} & set(g))
            for m in g["methods"]:
                d = combine.display(self.floor["groups"][g["group"]]["methods"][m["method"]])
                self.assertEqual((m["value"], m["detail"]), (d["value"], d["detail"]))
        loop, yolox = shown["groups"][1]["methods"]
        self.assertRegex(loop["value"], r"^≥ \d+$")
        self.assertEqual(loop["unit"], " min")
        self.assertEqual((yolox["value"], yolox["unit"]), ("—", ""))
        self.assertIn("count not anchored", yolox["detail"])
        # Each stretch by the same rule on its own, B->booth untimed. So the sure loop's A->B
        # shows its minutes beside a floor total (asked in Q-26), and the page's note says so.
        # The yolox count here has no sd, which already makes its A->B unknown: that the stretch
        # follows the count's sureness is shown by the carried count's test below.
        self.assertIn("beside a floor", shown["note"])
        loop_segs = {s["name"]: s for s in loop["segments"]}
        yolox_segs = {s["name"]: s for s in yolox["segments"]}
        self.assertEqual(loop_segs["a_to_b"]["value"], str(round(self.sure_a_to_b)))
        self.assertEqual(yolox_segs["a_to_b"]["value"], "—")
        self.assertIn("count not anchored", yolox_segs["a_to_b"]["detail"])
        self.assertEqual(loop_segs["tail_to_a"]["value"], "0")
        self.assertEqual(loop_segs["b_to_booth"]["value"], "—")
        self.assertIn("not timed", loop_segs["b_to_booth"]["detail"])

    def test_a_count_with_its_sd_known_but_not_sure_shows_no_a_to_b_minutes(self):
        """CountTest 6b's carried count: A->B has minutes in the record (sd known), but the
        count is not sure, so that stretch shows "—" like the total, not its minutes."""
        run = qs.run(**PAST_A)
        g0 = run.at(192.0)
        b = [c for c in run.b if not g0 < c.at <= g0 + timedelta(seconds=30)]
        _, rec = measured(run, b=b, gaps_b=(Gap(g0, 30.0, "test"),))
        end = run.at(200.0)
        out = combine.combine([rec], no_line_at_a(end), self.pair, end)
        w = out["groups"]["sentri"]["methods"]["loop"]
        self.assertTrue(next(s for s in w["segments"] if s["name"] == "a_to_b")["known"])
        self.assertFalse(w["count"]["sure"])
        loop = viewer.estimate_view(self.snapshot(out))["groups"][0]["methods"][0]
        a_b = {s["name"]: s for s in loop["segments"]}["a_to_b"]
        self.assertEqual((a_b["value"], a_b["unit"]), ("—", ""))
        self.assertIn("count not anchored (carried)", a_b["detail"])

    def test_a_record_far_behind_live_is_flagged_stale(self):
        """When the slower line has stopped moving on (a stream dropped without a refusal),
        each tick re-measures the same old minute: the page says how far behind live it is."""
        at = datetime.fromisoformat(self.floor["at"])
        fresh = dict(self.snapshot(self.floor), updated_at=(at + timedelta(minutes=2)).isoformat())
        shown = viewer.estimate_view(fresh)
        self.assertEqual((shown["message"], shown["stale"]), ("", False))
        old = dict(fresh, updated_at=(at + timedelta(minutes=7)).isoformat())
        shown = viewer.estimate_view(old)
        self.assertTrue(shown["stale"])
        self.assertIn("7 min behind live", shown["message"])
        self.assertEqual(len(shown["groups"]), 2)          # still shown, under the flag
        page = viewer.PAGE
        self.assertIn("estimate.stopped || estimate.stale", page)

    def test_minutes_only_for_a_sure_count_with_every_stretch_known(self):
        shown = viewer.estimate_view(self.snapshot(self.whole))
        loop = shown["groups"][0]["methods"][0]
        w = self.whole["groups"]["sentri"]["methods"]["loop"]
        self.assertTrue(loop["minutes_shown"])
        self.assertEqual((loop["value"], loop["unit"]), (str(round(w["minutes"])), " min"))
        self.assertTrue(all(s["unit"] == " min" for s in loop["segments"]), loop["segments"])

    def test_with_show_minutes_false_no_number_is_shown(self):
        shown = viewer.estimate_view(self.snapshot(self.whole, show=False))
        for g in shown["groups"]:
            for m in g["methods"]:
                for item in (m, *m["segments"]):
                    self.assertEqual((item["value"], item["unit"], item["detail"]),
                                     ("", "", combine.NOT_SHOWN))

    def test_cbp_sits_beside_each_group_for_its_own_lane_type_and_feeds_nothing(self):
        shown = viewer.estimate_view(self.snapshot(self.floor), self.cbp)
        normal, sentri = shown["groups"]
        self.assertEqual(normal["cbp_text"],
                         "For comparison only, CBP publishes: standard 25 min · Ready 20 min.")
        self.assertEqual(sentri["cbp_text"], "For comparison only, CBP publishes: SENTRI closed.")
        other = viewer.estimate_view(self.snapshot(self.floor),
                                     {"240202": {"car_sentri": {"state": "open", "minutes": 90}}})
        for got in (shown, other):
            for g in got["groups"]:
                g.pop("cbp_text")
        self.assertEqual(shown, other)
        none = viewer.estimate_view(self.snapshot(self.floor))
        self.assertIn("not fetched yet", none["groups"][0]["cbp_text"])
        failed = viewer.estimate_view(self.snapshot(self.floor), None, "TimeoutError: x")
        self.assertIn("unavailable", failed["groups"][0]["cbp_text"])

    def test_each_group_names_its_cbp_lane_and_why_it_does_not_vote(self):
        normal, sentri = viewer.estimate_view(self.snapshot(self.floor))["groups"]
        self.assertEqual(sentri["lane_text"], "CBP lane car_sentri")
        self.assertEqual(normal["lane_text"], "No CBP lane id: this group never votes.")
        for g in (normal, sentri):
            self.assertTrue(g["vote_text"].startswith("Votes in the hidden estimate: no ("))
            self.assertIn("ADR-0015 decision 3 proposed", g["vote_text"])
        self.assertEqual(sentri["agree_text"], "Agreement: — (both methods need minutes).")

    def test_a_method_not_measured_shows_why_and_no_count_line(self):
        """A method the runner does not measure (cameras.json counts col-e by yolox only) is a
        card with its reason: no wait, no stretches, and no 'Cars between the lines' line. It
        keeps its place before yolox, and the group says agreement is not possible, not that
        it waits on minutes."""
        run = qs.run(**PAST_A)
        end = run.at(run.minutes)
        _, sure = measured(run)
        why = "city-pdn-north col-e is counted by yolox only"
        rec = combine.combine([dict(sure, method="yolox")], no_line_at_a(end), self.pair, end,
                              not_measured={("loop", "sentri"): why})
        sentri = next(g for g in viewer.estimate_view(self.snapshot(rec))["groups"]
                      if g["group"] == "sentri")
        self.assertEqual([m["method"] for m in sentri["methods"]], ["loop", "yolox"])
        loop = sentri["methods"][0]
        self.assertEqual((loop["value"], loop["unit"], loop["detail"], loop["count_text"],
                          loop["segments"], loop["transient"], loop["info"]),
                         ("—", "", f"not measured: {why}", "", [], [], []))
        self.assertIn("if (m.count_text)", viewer.PAGE)
        self.assertEqual(sentri["agree_text"],
                         "Agreement: — (not possible: loop not measured here).")

    def test_under_camera_view_the_section_says_the_estimate_is_not_running(self):
        server = serve(0)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        base = f"http://127.0.0.1:{server.server_address[1]}"
        with urllib.request.urlopen(base + "/estimate.json") as r:
            shown = json.loads(r.read())
        self.assertEqual(shown, {"running": False, "message": viewer.NOT_RUNNING,
                                 "title": "Paso del Norte northbound - camera estimate"})
        with urllib.request.urlopen(base + "/") as r:
            page = r.read().decode("utf-8")
        self.assertIn('id="estimate"', page)
        self.assertIn('fetch("estimate.json"', page)

    def test_the_running_estimate_is_served_with_cbp_from_its_worker(self):
        from scraper.border.camera.estimate import Live

        live = Live(self.pair)
        live.update(self.floor)
        server = serve(0)
        server.estimate = live
        server.worker = unittest.mock.Mock(status={"cameras": {}, "cbp": self.cbp},
                                           _lock=threading.Lock())
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        url = f"http://127.0.0.1:{server.server_address[1]}/estimate.json"
        with urllib.request.urlopen(url) as r:
            shown = json.loads(r.read())
        self.assertEqual(shown, viewer.estimate_view(live.snapshot(), self.cbp))
        self.assertEqual(shown["at"], self.floor["at"])


class LaneLabels(unittest.TestCase):
    """A queue card names a lane by its count zone's lane type, not "(lane type unconfirmed)":
    a label only, so cbp_lane stays null and nothing is filed as SENTRI (owner, 2026-10-27:
    pdn-north's lane-4 and lane-5 are one SENTRI lane, split by barrels)."""

    @classmethod
    def setUpClass(cls):
        from scraper.border.camera.calibration import load_cameras

        cls.cams = load_cameras()

    def test_pdn_north_names_three_normal_lanes_and_two_sentri_ones_said_once_to_be_one(self):
        got = viewer.lane_types([self.cams["pdn-north"], self.cams["pdn-south-street"]])
        self.assertEqual(got["pdn-north"]["lane_types"],
                         {"lane-1": "normal", "lane-2": "normal", "lane-3": "normal",
                          "lane-4": "SENTRI", "lane-5": "SENTRI"})
        self.assertEqual(got["pdn-north"]["lanes_note"],
                         "lane-4 and lane-5 are one SENTRI lane, split by barrels near the "
                         "camera: each is read as its own strip.")
        # No count zone on the street camera: its lanes keep "(lane type unconfirmed)".
        self.assertEqual(got["pdn-south-street"], {"lane_types": {}, "lanes_note": None})
        # A label only: no strip is filed as a CBP lane.
        self.assertEqual({lane.cbp_lane for lane in self.cams["pdn-north"].lanes}, {None})

    def test_the_split_lanes_are_what_cameras_json_types_them(self):
        for cid, (lanes, kind, _) in viewer.SPLIT_LANES.items():
            zones = {z.lane: z.lane_type for z in self.cams[cid].count_zones}
            self.assertEqual({zones[lane] for lane in lanes}, {kind}, cid)
        # Retyped, the note is no longer said, and each lane shows its own zone's type.
        from dataclasses import replace

        cam = self.cams["pdn-north"]
        retyped = replace(cam, count_zones=tuple(
            replace(z, lane_type="normal") if z.lane == "lane-4" else z
            for z in cam.count_zones))
        got = viewer.lane_types([retyped])["pdn-north"]
        self.assertEqual((got["lane_types"]["lane-4"], got["lanes_note"]), ("normal", None))

    def test_status_json_carries_the_labels_and_leaves_the_workers_status_alone(self):
        server = serve(0)
        cams = [self.cams["pdn-north"], self.cams["pdn-south-street"]]
        status = {"cameras": {c.id: {"id": c.id, "lanes": []} for c in cams}}
        server.worker = unittest.mock.Mock(status=status, cameras=cams, _lock=threading.Lock())
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        url = f"http://127.0.0.1:{server.server_address[1]}/status.json"
        with urllib.request.urlopen(url) as r:
            served = json.loads(r.read())["cameras"]
        self.assertEqual(served["pdn-north"]["lane_types"]["lane-5"], "SENTRI")
        self.assertIn("one SENTRI lane", served["pdn-north"]["lanes_note"])
        self.assertEqual(served["pdn-south-street"]["lane_types"], {})
        self.assertEqual(status["cameras"]["pdn-north"], {"id": "pdn-north", "lanes": []})

    def test_the_page_prints_the_type_said_once_and_keeps_unconfirmed_for_untyped_lanes(self):
        page = viewer.PAGE
        self.assertIn("laneRow(l, stale, c.lane_types)", page)
        self.assertIn("const type = l.cbp_lane ? null : (types || {})[l.lane];", page)
        self.assertIn('type ? ` · ${type}` : " (lane type unconfirmed)"', page)
        self.assertIn('if (c.lanes_note && (c.lanes || []).length) box.append(el("div", "detail", '
                      'c.lanes_note));', page)
        self.assertEqual(page.count("c.lanes_note"), 2)      # tested and printed, once a camera


class AnchoringBars(unittest.TestCase):
    """Each method's card shows its anchoring as a bar (transit.anchoring, carried by the camera
    record), 100 % only once anchored, with one line saying what it is."""

    def a(self, **changes):
        return {"state": "waiting", "unbroken_min": 101.6, "needed_min": 173.2,
                "transit_min": 6.8, "transit_assumed": False, "percent": 58, "enclosed": 1,
                "min_anchors": 3, **changes}

    def test_each_state_says_what_the_bar_is_and_never_shows_more_than_there_is(self):
        view = viewer._anchoring_view
        self.assertEqual(view(self.a()), {
            "state": "waiting", "percent": 58,
            "text": "Anchoring: 58 % · 101 of 174 min unbroken at both lines · anchors 1 of 3."})
        self.assertEqual(view(self.a(unbroken_min=180.0, percent=99, transit_min=None,
                                     transit_assumed=True, needed_min=180.0))["text"],
                         "Anchoring: 99 % · 180 of 180 min unbroken at both lines · anchors 1 "
                         "of 3 · long enough, waiting for distinctive cars at both lines (no trip "
                         "measured yet: taken as 0 min, the longest stretch).")
        self.assertTrue(view(self.a(state="anchored", percent=100, enclosed=4))["text"]
                        .startswith("Anchored: 100 % · "))
        broken = view(self.a(state="broken", percent=0, unbroken_min=0.0, enclosed=0))
        self.assertEqual((broken["state"], broken["percent"]), ("broken", 0))
        self.assertIn("a line is unseen now", broken["text"])
        self.assertIsNone(view(None))
        # transit._epoch: a count carried in and confirmed by the anchors that follow it is
        # anchored with fewer enclosed than MIN_ANCHORS, and says so, not "anchors 0 of 3"
        carried = view(self.a(state="anchored", percent=100, enclosed=0, unbroken_min=30.0))
        self.assertEqual(carried["text"], "Anchored: 100 % · the count carried in (across a gap, "
                         "or from the measurement before), confirmed by distinctive cars that "
                         "follow it.")
        # YOLOX stopped: its gap stays open for the rest of the run (runner._gaps_for)
        stopped = view(self.a(state="broken", percent=0, unbroken_min=0.0, enclosed=0),
                       stopped=True)
        self.assertIn("broken for the rest of this run: YOLOX stopped", stopped["text"])
        self.assertNotIn("starts again", stopped["text"])
        self.assertNotIn("YOLOX stopped", view(self.a(), stopped=True)["text"])

    def test_the_card_carries_the_bar_and_the_section_its_note(self):
        rec = {"at": "2026-10-28T17:00:00+00:00", "groups": {"sentri": {
            "lane": "car_sentri", "agree": None, "would_vote": False, "would_vote_reasons": [],
            "methods": {"yolox": {"count": {"source": "few-anchors", "offset_sd": None,
                                            "sure": False},
                                  "segments": [], "floor": True, "anchoring": self.a(),
                                  "transient": [], "info": []}}}}}
        snapshot = {"pair": {"port_number": "240202", "direction": "north",
                             "show_minutes": False}, "record": rec, "stopped": None,
                    "updated_at": rec["at"]}
        shown = viewer.estimate_view(snapshot)
        card = shown["groups"][0]["methods"][0]
        self.assertEqual(card["anchoring"]["percent"], 58)
        self.assertEqual(card["value"], "")                  # show_minutes false: no number
        self.assertEqual((card["device_text"], card["device_stopped"]), ("", ""))
        self.assertIn("reads 100 % only once anchors set the count", shown["anchor_note"])
        self.assertIn("or once 3 that follow a count carried in", shown["anchor_note"])
        self.assertIn("once none has checked the count for 60 min", shown["anchor_note"])
        self.assertIn('setAttribute("role", "progressbar")', viewer.PAGE)
        self.assertIn("fill.style.width = `${m.anchoring.percent}%`;", viewer.PAGE)
        self.assertIn('if (m.device_text) card.append(el("div", "detail", m.device_text));',
                      viewer.PAGE)
        self.assertIn('if (m.device_stopped) card.append(el("div", "flag", m.device_stopped));',
                      viewer.PAGE)
        self.assertIn("estimate.anchor_note", viewer.PAGE)
        # A card whose YOLOX stopped says its broken bar stays broken this run.
        rec["groups"]["sentri"]["methods"]["yolox"].update(
            anchoring=self.a(state="broken", percent=0, unbroken_min=0.0, enclosed=0),
            devices={"pdn-north": {"device": "gpu:test", "stopped": "test",
                                   "stopped_at": rec["at"]}})
        card = viewer.estimate_view(snapshot)["groups"][0]["methods"][0]
        self.assertTrue(card["device_stopped"].startswith("YOLOX STOPPED at pdn-north"))
        self.assertIn("broken for the rest of this run", card["anchoring"]["text"])

    def test_a_broken_bar_and_its_card_name_the_line_unseen_now_and_why(self):
        """transit.anchoring's `unseen`, as on 2026-10-28 from 11:04 UTC: line B's loop zones
        learning the empty road, the span wholly inside that gap, so the count has no epoch and
        no source. The bar says which line and why, and the card says there is no count and
        why, never "count not anchored (None)". YOLOX stopped keeps its own words."""
        broken = self.a(state="broken", percent=0, unbroken_min=0.0, enclosed=0, unseen=[
            {"line": "B", "since": "2026-10-28T11:04:00+00:00",
             "reasons": ["loop zones learning"]}])
        bar = ("Anchoring broken: line B unseen now (loop zones learning) since 11:04 UTC; the "
               "unbroken stretch starts again once both are seen.")
        self.assertEqual(viewer._anchoring_view(broken),
                         {"state": "broken", "percent": 0, "text": bar})
        self.assertEqual(viewer._anchoring_view(broken, stopped=True)["text"],
                         "Anchoring broken for the rest of this run: YOLOX stopped (flagged "
                         "below), so its line stays unseen.")
        why = ("no count of the cars between the lines: line B unseen now (loop zones learning) "
               "since 11:04 UTC")                  # combine.a_to_b's reason, from the record
        a_to_b = {"name": "a_to_b", "minutes": None, "low": None, "high": None, "known": False,
                  "sensor": "loop counts at lines A and B", "why": why}
        rec = {"at": "2026-10-28T11:40:00+00:00", "groups": {"normal": {
            "lane": None, "agree": None, "would_vote": False, "would_vote_reasons": [],
            "methods": {"loop": {"count": {"source": None, "offset_sd": None, "sure": False},
                                 "segments": [a_to_b], "floor": True, "anchoring": broken,
                                 "transient": [], "info": []}}}}}
        snapshot = {"pair": {"port_number": "240202", "direction": "north",
                             "show_minutes": True}, "record": rec, "stopped": None,
                    "updated_at": rec["at"]}
        card = viewer.estimate_view(snapshot)["groups"][0]["methods"][0]
        self.assertEqual((card["value"], card["detail"], card["anchoring"]["text"]),
                         ("—", why, bar))
        # the A->B stretch alone (_segment_view) carries no anchoring: its own reason says it
        self.assertEqual((card["segments"][0]["value"], card["segments"][0]["detail"]),
                         ("—", why))


if __name__ == "__main__":
    unittest.main()
