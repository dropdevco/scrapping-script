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


if __name__ == "__main__":
    unittest.main()
