"""`camera estimate` (estimate.py): one process for the queue reader and both counting lines,
and what it writes, prints and serves each tick.

No stream is opened: the counters are scripted (they count the cars they are told to, in the
lanes they are told), the queue reader's lane signals are given in the shape the worker emits,
and the sure counts come from the simulated bottleneck (queue_sim) through the real
transit.measure, as in test_border_camera_combine.py.
"""
from __future__ import annotations

import contextlib
import io
import json
import math
import os
import re
import tempfile
import threading
import unittest
import unittest.mock
import urllib.error
import urllib.request
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

pytest.importorskip("cv2")

import queue_sim as qs
from test_border_camera_combine import PAST_A, measured

from scraper.border.camera import combine, estimate, reader, transit, viewer
from scraper.border.camera import runner as runner_mod
from scraper.border.camera.calibration import load_cameras
from scraper.border.camera.crossing import Crossing
from scraper.border.camera.hls import Stamped
from scraper.border.camera.runner import TransitRunner
from scraper.border.camera.worker import AlreadyRunning, Worker, hold_lock

REAL_CAMS = load_cameras()
# These tests drive one method, the loop, through both lane-type groups with fake counters.
# cameras.json counts col-e by yolox only, so the loop would not measure SENTRI
# (test_border_camera_runner covers that, and NotMeasuredTest below): here every zone is
# counted by every method.
CAMS = {**REAL_CAMS, "city-pdn-north": replace(REAL_CAMS["city-pdn-north"], count_zones=tuple(
    replace(z, methods=None) for z in REAL_CAMS["city-pdn-north"].count_zones))}
T0 = datetime(2026, 10, 26, 17, 0, tzinfo=UTC)
A_LANES = ("lane-1", "lane-2", "lane-3", "lane-4", "lane-5")   # pdn-north's strips and zones
NORMAL, SENTRI = ("lane-1", "col-1"), ("lane-5", "col-e")       # (A lane, B column), Q-22
TRIP = timedelta(minutes=5)
MINUTES_SHOWN = re.compile(r"\d+ min")


class Counter:
    """Counts a car on each frame it is told to, in the lane it is told; looks at no picture."""
    method = "loop"

    def __init__(self, camera, line: str, cars: dict[datetime, str]):
        self.camera, self.line, self.cars = camera, line, cars

    def add(self, frame):
        lane = self.cars.get(frame.at)
        if lane is None:
            return []
        return [Crossing(self.camera.id, self.line, lane, frame.at, self.method,
                         (0.0, 0.0, 0.5, 0.6))]

    def gap(self, gap):
        pass

    def gaps(self):
        return []

    def states(self):
        return {}

    def references(self):
        return {}


def cars(minutes: int) -> tuple[dict, dict]:
    """A car every 30 s at A, normal and SENTRI in turn, each at B one TRIP later."""
    at_a, at_b = {}, {}
    for k in range(minutes * 2):
        t = T0 + timedelta(seconds=30 * k)
        a_lane, b_lane = (NORMAL, SENTRI)[k % 2]
        at_a[t] = a_lane
        at_b[t + TRIP] = b_lane
    return at_a, at_b


def signal(camera: str, lane: str, at: datetime, *, queue_m: float = 40.0,
           speed: float | None = 0.9, state: str = "queue") -> dict:
    """One lane signal as the worker emits it (reader.as_signal plus the rolling wait)."""
    return {"port_number": "240202", "lane": None, "direction": "north", "kind": "measured",
            "source": f"cam:{camera}", "state": "open", "minutes": None, "low": None,
            "high": None, "observed_at": at.isoformat(), "independent": True,
            "evidence": {"camera": camera, "lane": lane, "port_number": "240202",
                         "direction": "north", "state": state, "frames": 120, "seconds": 60.0,
                         "queue_m": queue_m, "beyond_view": False, "speed_m_s": speed,
                         "detail": {"queue_head_m": 0.0,
                                    "quarter_speeds": None if speed is None else [speed] * 4}}}


def every_lane(at: datetime, lane_1_m: float = 40.0, others_m: float = 40.0) -> list[dict]:
    """A line on every pdn-north strip, lane-1's `lane_1_m` long and the others' `others_m`
    (line A is 66 m from their booth end), and the street empty."""
    out = [signal("pdn-north", lane, at, queue_m=lane_1_m if lane == "lane-1" else others_m)
           for lane in A_LANES]
    out += [signal("pdn-south-street", lane, at, queue_m=0.0, speed=None, state="no_queue")
            for lane in ("left-column", "right-column")]
    return out


def lines(directory: Path, pattern: str) -> list[dict]:
    out = []
    for path in sorted(directory.glob(pattern)):
        out += estimate.read_jsonl(path)
    return out


def walk(value):
    if isinstance(value, dict):
        for k, v in value.items():
            yield k, v
            yield from walk(v)
    elif isinstance(value, list):
        for v in value:
            yield from walk(v)


class StateDir(unittest.TestCase):
    """A temporary CAMERA_STATE_DIR, and `camera estimate`'s parts built in it with scripted
    counters."""

    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        patcher = unittest.mock.patch.dict(os.environ, {"CAMERA_STATE_DIR": d.name})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.dir = Path(d.name)
        self.pair = estimate.load_pair(CAMS)
        self.alerts: list[str] = []

    def open(self, minutes: int = 40, cams=None, **pair_changes) -> estimate.Estimate:
        with unittest.mock.patch.object(TransitRunner, "_counters", lambda self, cam: []):
            est = estimate.Estimate(cams or CAMS, {**self.pair, **pair_changes}, ["loop"], 120,
                                    notify=self.alerts.append, alert=False)
        self.addCleanup(est.close)
        at_a, at_b = cars(minutes)
        a, b = est.runner.cams
        est.runner.counters = {a.id: [Counter(a, "A", at_a)], b.id: [Counter(b, "B", at_b)]}
        return est

    @staticmethod
    def feed(est: estimate.Estimate, start_min: float, end_min: float, step_s: int = 10,
             only: str | None = None):
        """Frames from both cameras (or `only` one), every `step_s`, over [start, end)."""
        r = est.runner
        s = round(start_min * 60)
        while s < end_min * 60:
            at = T0 + timedelta(seconds=s)
            for cam in r.cams:
                if only in (None, cam.id):
                    r._frame(cam, r.lines[cam.id],
                             Stamped(np.zeros((2, 2, 3), np.uint8), at, 0))
            s += step_s


class TickTest(StateDir):
    def test_a_tick_writes_the_transit_queue_and_camera_lines(self):
        """1. One tick: the runner's records to transit-<date>.jsonl, every queue signal to
        queue-<date>.jsonl (error lines too, times to the minute), and one camera record per
        group to camera-<date>.jsonl. No frame is written."""
        est = self.open()
        self.feed(est, 0, 20)
        for sig in every_lane(T0 + timedelta(minutes=17, seconds=37)):
            est.queue_log.add(sig)
        est.queue_log.add({"camera": "pdn-south-street", "error": "StreamLost: dropped",
                           "failed_at": "2026-10-26T17:18:41.5+00:00"})
        _, records, camera = estimate.tick(est.runner, est.queue_log, est.pair)

        self.assertEqual([(r["method"], r["lane_type"]) for r in records],
                         [("loop", "normal"), ("loop", "sentri")])
        self.assertEqual(lines(self.dir, "transit-*.jsonl"), json.loads(json.dumps(records)))
        self.assertEqual(lines(self.dir, "camera-*.jsonl"), [json.loads(json.dumps(camera))])
        queue = lines(self.dir, "queue-*.jsonl")
        self.assertEqual(len(queue), 8)
        self.assertEqual({q["observed_at"] for q in queue if "observed_at" in q},
                         {"2026-10-26T17:17:00+00:00"})
        self.assertIn({"camera": "pdn-south-street", "error": "StreamLost: dropped",
                       "failed_at": "2026-10-26T17:18:00+00:00"}, queue)

        self.assertEqual(camera["at"], "2026-10-26T17:19:00+00:00")
        self.assertEqual(camera["at"], records[0]["seen_until"])
        self.assertEqual(sorted(camera["groups"]), ["normal", "sentri"])
        for group in camera["groups"].values():
            tail = group["methods"]["loop"]["segments"][0]
            # The queue signals reached combine: every strip read, none reaching A.
            self.assertEqual((tail["name"], tail["known"], tail["minutes"]),
                             ("tail_to_a", True, 0.0))
        for k, leaf in walk(lines(self.dir, "camera-*.jsonl") + queue):
            if isinstance(leaf, str) and re.fullmatch(r"\d{4}-\d\d-\d\dT.*", leaf):
                self.assertRegex(leaf, r"T\d\d:\d\d:00\+00:00$", k)
        written = {p.suffix for p in self.dir.iterdir()}
        self.assertFalse(written & {".jpg", ".jpeg", ".png", ".bmp", ".mp4", ".ts"}, written)

    def test_nothing_measured_writes_no_camera_line(self):
        est = self.open()
        self.feed(est, 0, 1)
        est.runner.last_at.pop("city-pdn-north")     # line B not seen yet
        est.runner.first_at.pop("city-pdn-north")
        _, records, camera = estimate.tick(est.runner, est.queue_log, est.pair)
        self.assertEqual((records, camera), ([], None))
        self.assertEqual(list(self.dir.glob("camera-*.jsonl")), [])
        self.assertIn("camera wait: nothing measured yet",
                      estimate.status_text(est.runner, records, camera, est.queue_log, est.pair))


class NotMeasuredTest(StateDir):
    def test_a_group_the_method_does_not_count_is_listed_not_measured(self):
        """cameras.json counts col-e by yolox only: the loop writes no SENTRI record, and the
        camera record and the status list loop SENTRI as not measured, with why, never as a
        wait; the group cannot vote."""
        est = self.open(cams=REAL_CAMS)
        why = "city-pdn-north col-e is counted by yolox only"
        self.assertEqual(est.runner.not_measured, {("loop", "sentri"): why})
        self.feed(est, 0, 20)
        _, records, camera = estimate.tick(est.runner, est.queue_log, est.pair)
        self.assertEqual([(r["method"], r["lane_type"]) for r in records], [("loop", "normal")])
        sentri = camera["groups"]["sentri"]
        self.assertEqual(sentri["methods"], {"loop": {"method": "loop", "lane_type": "sentri",
                                                       "not_measured": why}})
        self.assertFalse(sentri["would_vote"])
        self.assertIn(f"loop not measured: {why}", sentri["would_vote_reasons"])
        self.assertIsNone(sentri["agree"])
        self.assertIn(f"loop: - (not measured: {why})",
                      estimate.status_text(est.runner, records, camera, est.queue_log, est.pair))
        replayed = estimate.replay(lines(self.dir, "transit-*.jsonl"), [], est.pair,
                                   runner_mod.unmeasured(REAL_CAMS["pdn-north"],
                                                         REAL_CAMS["city-pdn-north"], ["loop"]))
        self.assertEqual(replayed[0]["groups"]["sentri"], json.loads(json.dumps(sentri)))
        # The run's not_measured goes with each transit record, for a later replay.
        self.assertEqual([r["not_measured"] for r in records], [{"loop": {"sentri": why}}])

    def test_a_run_that_measures_some_groups_waits_for_line_b_not_stops(self):
        """cameras.json as it is: the loop measures NORMAL but not SENTRI. Before line B is seen,
        the page and the status say nothing is measured yet, not that the run measures nothing
        (and do not fail looking up a reason NORMAL does not have)."""
        est = self.open(cams=REAL_CAMS)
        self.feed(est, 0, 1)
        est.runner.last_at.pop("city-pdn-north")     # line B not seen yet
        est.runner.first_at.pop("city-pdn-north")
        self.assertIsNone(estimate.measures_nothing(est.runner))
        status = est.step(now=T0 + timedelta(minutes=1))
        shown = viewer.estimate_view(est.live.snapshot())
        self.assertEqual((shown["stopped"], shown["message"]), (None, viewer.NOTHING_YET))
        self.assertIn("camera wait: nothing measured yet", status)

    def test_a_run_that_measures_no_group_says_so_not_that_nothing_is_measured_yet(self):
        """Every City zone counted by yolox only and a loop-only run: no (method, group) is
        measured, so no tick will ever make a camera record. The page and the status give the
        reasons, not "nothing measured yet (both streams must be seen first)"."""
        yolox_only = {**REAL_CAMS, "city-pdn-north": replace(
            REAL_CAMS["city-pdn-north"], count_zones=tuple(
                replace(z, methods=("yolox",)) for z in REAL_CAMS["city-pdn-north"].count_zones))}
        est = self.open(cams=yolox_only)
        self.assertEqual(sorted(est.runner.not_measured), [("loop", "normal"), ("loop", "sentri")])
        self.feed(est, 0, 20)
        status = est.step(now=T0 + timedelta(minutes=20))
        self.assertEqual(list(self.dir.glob("camera-*.jsonl")), [])
        shown = viewer.estimate_view(est.live.snapshot())
        nm = est.runner.not_measured
        self.assertEqual(shown["message"], "No camera wait: no method of this run measures any "
                         f"group (loop normal: {nm[('loop', 'normal')]}; loop sentri: "
                         f"{nm[('loop', 'sentri')]}).")
        self.assertTrue(shown["stopped"])                   # shown as a flag, not a status
        self.assertIn("camera wait: none - no method of this run measures any group", status)
        self.assertNotIn("nothing measured yet", status)


class StatusTest(StateDir):
    """2. The terminal shows each method's wait only as combine.display allows."""

    @classmethod
    def setUpClass(cls):
        run = qs.run(**PAST_A)
        cls.end = run.at(run.minutes)
        _, cls.sure = measured(run)
        _, cls.unsure = measured(run, start_min=100.0)

    def setUp(self):
        super().setUp()
        self.est = self.open()

    def camera_lines(self, records, **pair_changes) -> list[str]:
        pair = {**self.est.pair, **pair_changes}
        camera = combine.combine(records, [], pair, self.end)
        text = estimate.status_text(self.est.runner, records, camera, self.est.queue_log, pair,
                                    "cpu")
        text.encode("ascii")                       # a cp1252 console can print every line
        self.assertNotIn("now ~", text)            # the runner's own minutes are never printed
        self.assertNotIn("median transit", text)
        return [ln for ln in text.splitlines() if ln.startswith("  camera wait")]

    def test_with_show_minutes_false_no_minutes_are_printed(self):
        shown = self.camera_lines([self.sure], show_minutes=False)
        self.assertEqual(shown, [f"  camera wait: {combine.NOT_SHOWN}"])

    def test_a_sure_count_shows_its_floor_and_an_unsure_one_no_number(self):
        sure = combine.combine([self.sure], [], self.pair, self.end)
        low = sure["groups"]["sentri"]["methods"]["loop"]["low"]
        shown = self.camera_lines([self.sure])
        self.assertEqual(len(shown), 1)
        self.assertIn(f"loop: >= {math.floor(low)} min", shown[0])
        self.assertIn("B to the booths not timed", shown[0])

        unsure = self.camera_lines([self.unsure])
        self.assertIn("loop: - (count not anchored (few-anchors))", unsure[0])
        self.assertIsNone(MINUTES_SHOWN.search(unsure[0]), unsure)

    def test_the_status_names_each_count_and_queue_lane(self):
        est = self.est
        for sig in every_lane(T0, lane_1_m=70.0):
            est.queue_log.add(sig)
        text = estimate.status_text(est.runner, [self.sure, self.unsure], None, est.queue_log,
                                    est.pair)
        self.assertIn("  count loop  sentri: anchors, sd 2.0", text)
        self.assertIn("  count loop  sentri: few-anchors, sd -", text)
        self.assertIn("  queue pdn-north lane-1: queue 70 m, reaches A", text)
        self.assertIn("  queue pdn-north lane-2: queue 40 m, short of A", text)
        self.assertIn("  queue pdn-south-street left-column: no_queue 0 m", text)

    def test_an_old_reading_shows_its_age_and_a_failed_cycle_says_so(self):
        """3b. On 2026-10-28 a TimelineCut on pdn-south-street left its 07:51 reading printed
        as current for two status lines. A camera's failed cycle is now said at once, and a
        reading older than the Worker's cycle explains shows its minute and age."""
        est = self.est
        self.assertEqual(est.queue_log.late_s, 240)           # a 120-s cycle plus QUEUE_LATE_S

        def status(at):
            return estimate.status_text(est.runner, [], None, est.queue_log, est.pair, now=at)
        for sig in every_lane(T0 + timedelta(minutes=51, seconds=30)):
            est.queue_log.add(sig)
        quiet = status(T0 + timedelta(minutes=54, seconds=59))   # 239 s past 17:51
        self.assertNotIn("min ago", quiet)
        self.assertNotIn("failed", quiet)
        est.queue_log.add({"camera": "pdn-south-street",
                           "error": "TimelineCut: timestamps jumped +9.0 s at segment 7",
                           "failed_at": (T0 + timedelta(minutes=54, seconds=2)).isoformat()})
        for sig in every_lane(T0 + timedelta(minutes=53, seconds=30))[:5]:   # pdn-north read
            est.queue_log.add(sig)
        text = status(T0 + timedelta(minutes=55, seconds=10)).splitlines()
        street = [ln for ln in text if "pdn-south-street" in ln]
        self.assertEqual(street, [
            ("  queue pdn-south-street: last cycle failed at 17:54 UTC: TimelineCut: timestamps "
             "jumped +9.0 s at segment 7"),
            "  queue pdn-south-street left-column: no_queue 0 m, read 17:51 UTC, 4 min ago",
            "  queue pdn-south-street right-column: no_queue 0 m, read 17:51 UTC, 4 min ago"])
        self.assertIn("  queue pdn-north lane-1: queue 40 m, short of A", text)
        self.assertFalse([ln for ln in text if "pdn-north" in ln and "ago" in ln], text)
        for sig in every_lane(T0 + timedelta(minutes=55, seconds=20))[5:]:   # the street reads
            est.queue_log.add(sig)
        self.assertNotIn("pdn-south-street: last cycle failed",
                         status(T0 + timedelta(minutes=56)))

    def test_a_camera_that_failed_before_any_reading_is_said_too(self):
        self.est.queue_log.add({"camera": "pdn-north", "error": "StreamDown: offline",
                                "failed_at": "2026-10-26T17:01:12+00:00"})
        text = estimate.status_text(self.est.runner, [], None, self.est.queue_log, self.est.pair)
        self.assertIn("  queue pdn-north: last cycle failed at 17:01 UTC: StreamDown: offline",
                      text)


class LockTest(StateDir):
    def test_it_refuses_while_either_lock_is_held_and_the_others_refuse_while_it_runs(self):
        """3. transit.lock and worker.lock: it takes both or neither."""
        held = hold_lock(self.dir / "transit.lock")
        with self.assertRaises(AlreadyRunning):
            self.open()
        held.close()
        worker = Worker([CAMS["pdn-north"]], seconds=60, every=120, alert=False)
        with self.assertRaises(AlreadyRunning):
            self.open()
        hold_lock(self.dir / "transit.lock").close()      # let go when the Worker refused
        worker._held.close()

        self.open()
        args = SimpleNamespace(replay=None, queue=None, methods="loop", queue_every=120, port=0,
                               every=60)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(estimate.cmd_estimate(args, CAMS), 1)
            self.assertEqual(runner_mod.cmd_transit(SimpleNamespace(
                a="pdn-north", b="city-pdn-north", methods="loop", every=60, minutes=None),
                CAMS), 1)
            self.assertEqual(viewer.cmd_view(SimpleNamespace(
                cameras=None, seconds=60, every=120, json=False, port=0), CAMS), 1)
            self.assertEqual(reader.cmd_run(SimpleNamespace(
                cameras="pdn-north", seconds=60, every=300), CAMS), 1)
        self.assertEqual(out.getvalue().count("already running"), 4)


class AlertTest(StateDir):
    def test_the_city_blocked_gives_no_total_and_exactly_one_alert(self):
        """4. Line B ends for good: no camera record from then on, on the page or in the file,
        and one alert, however many ticks follow."""
        est = self.open()
        self.feed(est, 0, 20)
        now = T0 + timedelta(minutes=20)
        est.step(now=now)
        self.assertIsNotNone(est.live.snapshot()["record"])
        written = lines(self.dir, "camera-*.jsonl")

        est.runner.blocked.add("city-pdn-north")
        est.runner.errors["city-pdn-north"] = "BLOCKED 403 Forbidden"
        for k in range(3):
            self.feed(est, 20 + k, 21 + k, only="pdn-north")     # line A goes on
            text = est.step(now=now + timedelta(minutes=k + 1))
        self.assertEqual(len(self.alerts), 1, self.alerts)
        self.assertIn("city-pdn-north refused us (BLOCKED 403 Forbidden)", self.alerts[0])
        self.assertIn("never retried", self.alerts[0])
        self.assertEqual(lines(self.dir, "camera-*.jsonl"), written)
        self.assertIn("camera wait: none - line B stopped", text)
        snap = est.live.snapshot()
        self.assertIsNone(snap["record"])
        shown = viewer.estimate_view(snap)
        self.assertEqual(shown["groups"], [])
        self.assertIn("never retried", shown["message"])

    def test_a_line_far_behind_live_alerts_once_and_once_more_when_it_catches_up(self):
        est = self.open()
        self.feed(est, 0, 20)
        last = T0 + timedelta(minutes=20) - timedelta(seconds=10)
        texts = [est.step(now=last + timedelta(seconds=estimate.LAG_ALERT_S + 60 + k))
                 for k in range(3)]
        self.assertEqual(len(self.alerts), 2, self.alerts)        # line A and line B
        self.assertTrue(all("behind live" in a for a in self.alerts))
        # The terminal prints each alert once too: without Telegram it is the only place.
        for text in self.alerts:
            self.assertIn(f"  alert: {text}", texts[0])
        self.assertNotIn("alert:", texts[1] + texts[2])
        texts = [est.step(now=last + timedelta(seconds=5)) for _ in range(2)]
        self.assertEqual(len(self.alerts), 4, self.alerts)
        self.assertTrue(all("caught up" in a for a in self.alerts[2:]))
        self.assertIn("caught up", texts[0])
        self.assertNotIn("alert:", texts[1])

    def test_a_frozen_line_b_is_flagged_stale_on_the_page(self):
        """Line B's stream stops without a refusal: each tick re-measures B's last minute,
        and the page says how far behind live that is."""
        est = self.open()
        self.feed(est, 0, 20)
        est.step(now=T0 + timedelta(minutes=20))
        self.assertFalse(viewer.estimate_view(est.live.snapshot())["stale"])
        self.feed(est, 20, 26, only="pdn-north")         # line A goes on, line B does not
        est.step(now=T0 + timedelta(minutes=26))
        snap = est.live.snapshot()
        self.assertEqual(snap["record"]["at"], "2026-10-26T17:19:00+00:00")
        shown = viewer.estimate_view(snap)
        self.assertTrue(shown["stale"])
        self.assertIn("7 min behind live", shown["message"])

    def test_a_failed_tick_leaves_no_old_record_on_the_page(self):
        """A tick that raises (a full disk) is logged and retried by cmd_estimate; the page
        says there is no wait this minute instead of keeping the last one."""
        est = self.open()
        self.feed(est, 0, 20)
        est.step(now=T0 + timedelta(minutes=20))
        self.assertIsNotNone(est.live.snapshot()["record"])
        self.feed(est, 20, 21)
        with unittest.mock.patch.object(est.runner, "write", side_effect=OSError(28, "full")), \
                self.assertRaises(OSError):
            est.step(now=T0 + timedelta(minutes=21))
        snap = est.live.snapshot()
        self.assertIsNone(snap["record"])
        self.assertEqual(snap["updated_at"], "2026-10-26T17:21:00+00:00")
        shown = viewer.estimate_view(snap)
        self.assertEqual(shown["groups"], [])
        self.assertIn("the tick failed (OSError)", shown["message"])


class HiddenCameraTest(StateDir):
    def test_the_worker_never_reads_a_hidden_camera_and_its_picture_is_never_served(self):
        """5. The Worker reads pdn-north and the street only; the City's picture does not
        exist to serve, and the page's estimate section carries no picture."""
        est = self.open()
        self.assertEqual([c.id for c in est.worker.cameras], ["pdn-north", "pdn-south-street"])
        self.assertTrue(all(c.display for c in est.worker.cameras))
        self.assertEqual([c.id for c in estimate.queue_cameras(
            CAMS, {"a": "pdn-north", "street": "city-pdn-north"})], ["pdn-north"])
        self.feed(est, 0, 20)
        est.step(now=T0 + timedelta(minutes=20))

        server = viewer.serve(0)
        server.worker, server.estimate = est.worker, est.live
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        base = f"http://127.0.0.1:{server.server_address[1]}"
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(base + "/overlay/city-pdn-north.jpg")
        self.assertEqual(caught.exception.code, 404)
        caught.exception.close()
        with urllib.request.urlopen(base + "/estimate.json") as r:
            body = r.read().decode("utf-8")
        shown = json.loads(body)
        self.assertTrue(shown["running"])
        self.assertEqual([g["group"] for g in shown["groups"]], ["normal", "sentri"])
        self.assertNotIn(".jpg", body)
        self.assertNotIn("overlay", body)


class ReplayTest(StateDir):
    def test_a_replay_with_a_bom_gives_the_live_records_when_every_reading_was_logged_first(self):
        """6. The logged transit and queue lines, read back with a BOM, give exactly the camera
        records the live ticks wrote, a line reaching back over A included, when every reading
        reached the log before its tick (see the next test for when one did not)."""
        est = self.open()
        start = 0
        for minute, lane_1_m in ((20, 40.0), (26, 80.0), (32, 90.0)):
            self.feed(est, start, minute)
            start = minute
            for back in (5, 3):
                for sig in every_lane(T0 + timedelta(minutes=minute - back, seconds=20),
                                      lane_1_m=lane_1_m, others_m=lane_1_m):
                    est.queue_log.add(sig)
            est.queue_log.add({"camera": "pdn-north", "error": "StreamLost: dropped"})
            est.step(now=T0 + timedelta(minutes=minute))
        live = lines(self.dir, "camera-*.jsonl")
        self.assertEqual(len(live), 3)
        tails = [g["methods"]["loop"]["segments"][0] for rec in live
                 for g in rec["groups"].values()]
        self.assertTrue(any(t["known"] and t["minutes"] > 0 for t in tails), tails)

        bom = b"\xef\xbb\xbf"
        transit_log, queue_log = self.dir / "t.jsonl", self.dir / "q.jsonl"
        transit_log.write_bytes(bom + b"".join(p.read_bytes()
                                               for p in self.dir.glob("transit-*.jsonl")))
        queue_log.write_bytes(bom + b"".join(p.read_bytes()
                                             for p in self.dir.glob("queue-*.jsonl")))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = estimate.cmd_replay(SimpleNamespace(replay=str(transit_log),
                                                       queue=str(queue_log), methods="loop"),
                                       CAMS)
        self.assertEqual(code, 0)
        self.assertEqual(lines(self.dir, "camera-replay-t.jsonl"), live)
        self.assertIn("self-check", out.getvalue())
        self.assertIn("3 camera records from 6 transit records", out.getvalue())

    def late_reading(self) -> estimate.Estimate:
        """One tick at seen_until 17:19, its lines 40 m (short of A, read at 17:14 and 17:16)
        and the street's read at 17:19:05, before the tick. Then pdn-north's reading of the
        tick's own minute (17:19:40, 90 m: past A) reaches the log after the tick, as on the
        night of 2026-10-28, when the Worker's cycle ran a different phase of the minute."""
        est = self.open()
        self.feed(est, 0, 20)                                   # seen_until 17:19
        for back in (5, 3):                                     # 40 m: short of A
            for sig in every_lane(T0 + timedelta(minutes=19 - back, seconds=20)):
                est.queue_log.add(sig)
        for sig in every_lane(T0 + timedelta(minutes=19, seconds=5))[5:]:   # the street
            est.queue_log.add(sig)
        est.step(now=T0 + timedelta(minutes=20))
        for sig in every_lane(T0 + timedelta(minutes=19, seconds=40), 90.0, 90.0)[:5]:
            est.queue_log.add(sig)                              # logged as 17:19, after the tick
        return est

    @staticmethod
    def tail(rec):
        return rec["groups"]["sentri"]["methods"]["loop"]["segments"][0]

    def test_a_reading_of_the_ticks_minute_logged_after_it_is_left_out_of_its_replay(self):
        """6b. The tick logs which readings it had (`queue_seen`: per lane, the newest one's
        minute), so the replay leaves out the reading that came after it, keeps the street's of
        the same minute that came before, and equals the live record."""
        est = self.late_reading()
        live = lines(self.dir, "camera-*.jsonl")
        logged = lines(self.dir, "transit-*.jsonl")
        seen = {"pdn-north": dict.fromkeys(A_LANES, "2026-10-26T17:16:00+00:00"),
                "pdn-south-street": dict.fromkeys(("left-column", "right-column"),
                                                  "2026-10-26T17:19:00+00:00")}
        self.assertEqual(live[0]["at"], "2026-10-26T17:19:00+00:00")
        self.assertEqual(live[0]["queue_seen"], seen)
        self.assertEqual([r["queue_seen"] for r in logged], [seen] * len(logged))
        self.assertEqual(self.tail(live[0])["minutes"], 0.0)
        queue = lines(self.dir, "queue-*.jsonl")
        self.assertIn("2026-10-26T17:19:00+00:00",
                      {q["observed_at"] for q in queue if q["evidence"]["camera"] == "pdn-north"})
        again = estimate.replay(logged, queue, est.pair)
        self.assertEqual(json.loads(json.dumps(again, ensure_ascii=False, default=str)), live)

    def test_a_record_without_queue_seen_replays_as_before(self):
        """A log written before `queue_seen` keeps the old rule: every reading not after `at`,
        so the late reading is used, as the live tick did not (estimate.py's docstring)."""
        est = self.late_reading()
        old = [{k: v for k, v in r.items() if k != "queue_seen"}
               for r in lines(self.dir, "transit-*.jsonl")]
        again = estimate.replay(old, lines(self.dir, "queue-*.jsonl"), est.pair)
        self.assertNotIn("queue_seen", again[0])
        self.assertGreater(self.tail(again[0])["minutes"], 0.0)

    def test_two_ticks_at_one_seen_until_replay_apart_when_the_first_lacks_a_group(self):
        """Line B frozen, so two ticks share a seen_until, and the first lost its normal group
        (transit.measure refused it): each logged record names its tick, so the replay makes
        the same two records the live ticks did, not one mixing the two ticks."""
        est = self.open()
        self.feed(est, 0, 20)
        real = est.runner.measure

        def without_normal():
            out = real()
            out.pop(("loop", "normal"), None)
            return out
        with unittest.mock.patch.object(est.runner, "measure", without_normal):
            est.step(now=T0 + timedelta(minutes=20))
        self.feed(est, 20, 21, only="pdn-north")                # line B does not move on
        est.step(now=T0 + timedelta(minutes=21))
        live = lines(self.dir, "camera-*.jsonl")
        self.assertEqual([sorted(r["groups"]) for r in live], [["sentri"], ["normal", "sentri"]])
        self.assertEqual(live[0]["at"], live[1]["at"])
        logged = lines(self.dir, "transit-*.jsonl")
        self.assertEqual([r["tick"] for r in logged], [1, 2, 2])
        self.assertEqual(len({r["run"] for r in logged}), 1)
        again = estimate.replay(logged, [], est.pair)
        self.assertEqual(json.loads(json.dumps(again, ensure_ascii=False, default=str)), live)

    def live_and_replayed(self, method: str, replay_methods: str,
                          logged: bool = True) -> tuple[list[dict], list[dict]]:
        """One live tick on the real cameras (col-e counted by yolox only) by scripted `method`
        counters, and its transit log replayed by cmd_replay with `replay_methods` as
        --methods. Without `logged`, the log's records do not carry the run's not_measured,
        as `camera transit`'s and older ones do not."""
        with unittest.mock.patch.object(TransitRunner, "_counters", lambda self, cam: []):
            est = estimate.Estimate(REAL_CAMS, self.pair, [method], 120,
                                    notify=self.alerts.append, alert=False)
        self.addCleanup(est.close)
        counter = type("Scripted", (Counter,), {"method": method})
        at_a, at_b = cars(40)
        a, b = est.runner.cams
        est.runner.counters = {a.id: [counter(a, "A", at_a)], b.id: [counter(b, "B", at_b)]}
        self.feed(est, 0, 20)
        est.step(now=T0 + timedelta(minutes=20))
        live = lines(self.dir, "camera-*.jsonl")      # before camera-replay-* exists
        records = lines(self.dir, "transit-*.jsonl")
        if not logged:
            for rec in records:
                del rec["not_measured"]
        source = self.dir / "t.jsonl"
        source.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(estimate.cmd_replay(
                SimpleNamespace(replay=str(source), queue=None, methods=replay_methods),
                REAL_CAMS), 0)
        return live, lines(self.dir, "camera-replay-t.jsonl")

    def test_a_replay_lists_as_not_measured_what_the_log_says_whatever_its_methods(self):
        """A loop-only run did not measure loop SENTRI, and its log says so: a replay given
        other --methods still gives back the live record."""
        live, again = self.live_and_replayed("loop", "yolox")
        self.assertIn("not_measured", live[0]["groups"]["sentri"]["methods"]["loop"])
        self.assertEqual(again, live)

    def test_a_replay_lists_nothing_not_measured_when_the_log_says_nothing_was(self):
        """A yolox-only run measured every group, and its log says so (an empty not_measured):
        a replay given loop as well adds no loop card."""
        live, again = self.live_and_replayed("yolox", "loop,yolox")
        self.assertEqual([r.get("not_measured") for r in lines(self.dir, "transit-*.jsonl")],
                         [{}, {}])
        self.assertEqual(list(live[0]["groups"]["sentri"]["methods"]), ["yolox"])
        self.assertEqual(again, live)

    def test_cmd_replay_rebuilds_not_measured_for_a_log_that_does_not_carry_it(self):
        """With the live run's --methods, the rebuilt not_measured gives back the live record:
        loop SENTRI listed as not measured, with why."""
        live, again = self.live_and_replayed("loop", "loop", logged=False)
        self.assertIn("not_measured", live[0]["groups"]["sentri"]["methods"]["loop"])
        self.assertEqual(again, live)

    def test_cmd_replay_adds_no_method_the_live_run_did_not_run(self):
        live, again = self.live_and_replayed("yolox", "yolox", logged=False)
        self.assertEqual(list(live[0]["groups"]["sentri"]["methods"]), ["yolox"])
        self.assertEqual(again, live)

    def test_a_log_from_before_lane_types_gains_no_typed_group_in_a_replay(self):
        """A log written before lane types (every record "mixed", no not_measured) replayed
        with today's typed cameras: today's loop-SENTRI entry is not added to its ticks."""
        est = self.open()
        self.feed(est, 0, 20)
        est.step(now=T0 + timedelta(minutes=20))
        old = [dict(r, lane_type="mixed") for r in lines(self.dir, "transit-*.jsonl")
               if r["lane_type"] == "normal"]
        for rec in old:
            del rec["not_measured"]
        today = runner_mod.unmeasured(REAL_CAMS["pdn-north"], REAL_CAMS["city-pdn-north"],
                                      ["loop", "yolox"])
        self.assertIn(("loop", "sentri"), today)
        again = estimate.replay(old, [], est.pair, today)
        self.assertEqual([sorted(r["groups"]) for r in again], [["mixed"]])
        self.assertEqual(list(again[0]["groups"]["mixed"]["methods"]), ["loop"])


class DeviceTest(StateDir):
    """Where YOLOX ran, in every yolox record, the camera record, the status and the page; and a
    replay that never grades two devices' yolox records together (gpu.py: the same frames give
    the same counts per path, not across paths)."""
    GPU = "gpu:NVIDIA GeForce RTX 4070"

    def yolox_run(self, device: str) -> tuple[estimate.Estimate, str]:
        with unittest.mock.patch.object(TransitRunner, "_counters", lambda self, cam: []):
            est = estimate.Estimate(CAMS, self.pair, ["yolox"], 120, notify=self.alerts.append,
                                    alert=False)
        self.addCleanup(est.close)
        record = {"device": device}
        if device != "cpu":
            record.update(cpu_checked=3, cpu_differed=0)
        counter = type("Scripted", (Counter,), {"method": "yolox",
                                                "device_record": lambda self: dict(record)})
        at_a, at_b = cars(40)
        a, b = est.runner.cams
        est.runner.counters = {a.id: [counter(a, "A", at_a)], b.id: [counter(b, "B", at_b)]}
        self.feed(est, 0, 20)
        return est, est.step(now=T0 + timedelta(minutes=20))

    def test_the_device_is_in_each_yolox_record_the_camera_record_the_status_and_the_page(self):
        est, text = self.yolox_run(self.GPU)
        logged = lines(self.dir, "transit-*.jsonl")
        self.assertEqual([r["lane_type"] for r in logged], ["normal", "sentri"])
        devices = {"pdn-north": {"device": self.GPU, "cpu_checked": 3, "cpu_differed": 0},
                   "city-pdn-north": {"device": self.GPU, "cpu_checked": 3, "cpu_differed": 0}}
        camera = lines(self.dir, "camera-*.jsonl")[0]
        for rec in logged:
            self.assertEqual(rec["devices"], devices)
            self.assertEqual(combine.record_device(rec), self.GPU)
            kept = camera["groups"][rec["lane_type"]]["methods"]["yolox"]
            self.assertEqual((kept["devices"], kept["anchoring"]),
                             (rec["devices"], rec["anchoring"]))
        self.assertIn(f"  yolox device: pdn-north {self.GPU} (checked on the CPU: 3 frames, 0 "
                      f"differed); city-pdn-north {self.GPU}", text)
        shown = text.splitlines()
        for rec in logged:      # each count line's bar is its own record's anchoring
            count = shown.index(next(ln for ln in shown
                                     if ln.startswith(f"  count yolox {rec['lane_type']:6}:")))
            self.assertEqual(shown[count + 1], "  " + transit.anchoring_text(
                "yolox", rec["lane_type"], rec["anchoring"]))
        self.assertNotIn("not recorded", text)
        card = viewer.estimate_view(est.live.snapshot())["groups"][0]["methods"][0]
        self.assertEqual(card["device_text"],
                         f"YOLOX ran on {self.GPU} (pdn-north, city-pdn-north), 6 frames also "
                         "run on the CPU, 0 of them differing.")
        self.assertEqual(card["device_stopped"], "")
        self.assertIn(card["anchoring"]["state"], ("waiting", "anchored"))

    def test_a_replay_never_grades_two_devices_together(self):
        self.yolox_run("cpu")
        live = lines(self.dir, "camera-*.jsonl")
        cpu = lines(self.dir, "transit-*.jsonl")
        gpu = [dict(r, run=r["run"] + "-gpu",       # never the live run's own minute
                    devices={cid: {"device": self.GPU, "cpu_checked": 1, "cpu_differed": 0}
                             for cid in r["devices"]}) for r in cpu]
        with self.assertRaisesRegex(ValueError, f"counted on cpu, {self.GPU}"):
            estimate.replay(cpu + gpu, [], self.pair)
        for wanted, graded, other in (("cpu", "cpu", self.GPU), ("gpu", self.GPU, "cpu")):
            again = estimate.replay(cpu + gpu, [], self.pair, device=wanted)
            self.assertEqual(len(again), 2)
            kept, left = (again[0], again[1]) if graded == "cpu" else (again[1], again[0])
            for g in left["groups"].values():
                self.assertEqual(g["methods"]["yolox"]["not_measured"],
                                 f"counted on {other}; this replay grades {graded} only "
                                 "(--device)")
            for g in kept["groups"].values():
                self.assertNotIn("not_measured", g["methods"]["yolox"])
        def plain(records):
            return json.loads(json.dumps(records, ensure_ascii=False, default=str))
        self.assertEqual(plain(estimate.replay(cpu + gpu, [], self.pair, device="cpu")[:1]),
                         live)
        self.assertEqual(plain(estimate.replay(cpu, [], self.pair)), live)   # one device

        source = self.dir / "t.jsonl"
        source.write_text("".join(json.dumps(r) + "\n" for r in cpu + gpu), encoding="utf-8")
        args = SimpleNamespace(replay=str(source), queue=None, methods="yolox", device=None)
        with self.assertRaisesRegex(SystemExit, "give --device cpu or --device gpu"):
            estimate.cmd_replay(args, CAMS)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(estimate.cmd_replay(dict_args(args, device="gpu"), CAMS), 0)
        self.assertIn(f"yolox records by device: cpu 2, {self.GPU} 2; graded: {self.GPU} only",
                      out.getvalue())

    def test_a_replay_picks_its_device_by_device_only(self):
        # "auto" picks none; "gpu" never picks one of two adapters; a device the log lacks is
        # still the one named; and $CHISME_YOLOX_DEVICE is never read (it says where a live
        # run's YOLOX runs, not what a log holds).
        def on(device: str) -> dict:
            return {"kind": "transit", "method": "yolox", "devices": {"a": {"device": device}}}
        self.assertEqual(estimate.replay_device([on("cpu")], "auto"), "cpu")
        with self.assertRaisesRegex(ValueError, "counted on gpu:A, gpu:B"):
            estimate.replay_device([on("gpu:A"), on("gpu:B")], "gpu")
        self.yolox_run("cpu")
        cpu = lines(self.dir, "transit-*.jsonl")
        for g in estimate.replay(cpu, [], self.pair, device="gpu")[0]["groups"].values():
            self.assertEqual(g["methods"]["yolox"]["not_measured"],
                             "counted on cpu; this replay grades gpu only (--device)")
        source = self.dir / "t.jsonl"
        source.write_text("".join(json.dumps(r) + "\n" for r in cpu), encoding="utf-8")
        args = SimpleNamespace(replay=str(source), queue=None, methods="yolox", device=None)
        out = io.StringIO()
        with unittest.mock.patch.dict(os.environ, {"CHISME_YOLOX_DEVICE": "gpu"}), \
                contextlib.redirect_stdout(out):
            self.assertEqual(estimate.cmd_replay(args, CAMS), 0)
        self.assertIn("yolox records by device: cpu 2; graded: cpu only", out.getvalue())

    def test_a_replay_can_grade_the_records_that_name_no_device(self):
        # A log that spans the change that named each record's device: its older yolox records
        # name none (combine.NO_DEVICE), and `--device unlogged` grades those alone.
        self.yolox_run("cpu")
        new = lines(self.dir, "transit-*.jsonl")
        old = [{k: v for k, v in dict(r, run=r["run"] + "-before").items() if k != "devices"}
               for r in new]
        with self.assertRaisesRegex(ValueError, "give --device cpu or --device gpu, or --device "
                                                "unlogged for the records that name none"):
            estimate.replay_device(old + new)
        self.assertEqual(estimate.replay_device(old + new, "unlogged"), combine.NO_DEVICE)
        again = estimate.replay(old + new, [], self.pair, device="unlogged")
        self.assertEqual(len(again), 2)
        for g in again[0]["groups"].values():
            self.assertNotIn("not_measured", g["methods"]["yolox"])
        for g in again[1]["groups"].values():
            self.assertEqual(g["methods"]["yolox"]["not_measured"],
                             "counted on cpu; this replay grades device not logged only "
                             "(--device)")

        def plain(records):
            return json.loads(json.dumps(records, ensure_ascii=False, default=str))
        self.assertEqual(plain(again[:1]), plain(estimate.replay(old, [], self.pair)))

        source = self.dir / "t.jsonl"
        source.write_text("".join(json.dumps(r) + "\n" for r in old + new), encoding="utf-8")
        args = SimpleNamespace(replay=str(source), queue=None, methods="yolox",
                               device="unlogged")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(estimate.cmd_replay(args, CAMS), 0)
        self.assertIn(f"yolox records by device: {combine.NO_DEVICE} 2, cpu 2; graded: "
                      f"{combine.NO_DEVICE} only", out.getvalue())
        # where a live run's YOLOX runs is never "unlogged"
        live = SimpleNamespace(replay=None, queue=None, methods="loop,yolox", queue_every=120,
                               port=0, every=60, device="unlogged")
        with self.assertRaisesRegex(SystemExit, "YOLOX device 'unlogged'"):
            estimate.cmd_estimate(live, CAMS)

    def test_a_stopped_gpu_paths_bar_says_it_stays_broken_for_the_run(self):
        est, _ = self.yolox_run(self.GPU)
        why = "a DirectML run on NVIDIA GeForce RTX 4070 failed: test"
        est.runner.yolox_stopped["pdn-north"] = (T0 + timedelta(minutes=10), why,
                                                 {"device": self.GPU})
        text = est.step(now=T0 + timedelta(minutes=21))
        for group in ("normal", "sentri"):
            self.assertIn(f"  anchoring yolox {group:6} [..........] broken for the rest of this "
                          "run: YOLOX stopped", text)
        self.assertNotIn("a line is unseen now", text)

    def test_a_stopped_gpu_path_alerts_once_and_the_page_flags_it(self):
        est = self.open()
        why = "a DirectML run on NVIDIA GeForce RTX 4070 failed: test"
        est.runner.yolox_stopped["pdn-north"] = (T0, why, {"device": self.GPU})
        now = T0 + timedelta(minutes=1)
        self.assertEqual(est.alerts.check(est.runner, now),
                         [(f"Border camera estimate: YOLOX stopped at line A (pdn-north): {why}. "
                           "Its yolox count there is a gap for the rest of this run, never made "
                           "on the CPU instead; the loop goes on.")])
        self.assertEqual(est.alerts.check(est.runner, now), [])
        self.assertEqual(len(self.alerts), 1)
        devices = est.runner.devices()
        self.assertEqual(devices["pdn-north"], {"device": self.GPU, "stopped": why,
                                                "stopped_at": T0.isoformat()})
        text, stopped = viewer._device_view(devices)
        self.assertEqual(text, f"YOLOX ran on {self.GPU} (pdn-north).")
        self.assertEqual(stopped, f"YOLOX STOPPED at pdn-north ({why}): its count there is a gap "
                                  "from 17:00 UTC on, never made on the CPU instead")

    def test_gpu_asked_for_and_refused_stops_before_reading_and_lets_go_of_both_locks(self):
        from scraper.border.camera.gpu import GpuRefused

        def refuse(runner, cam):
            raise GpuRefused("no NVIDIA GPU among the DirectML devices: none")
        args = SimpleNamespace(replay=None, queue=None, methods="loop,yolox", queue_every=120,
                               port=0, every=60, device="gpu")
        out = io.StringIO()
        with unittest.mock.patch.object(TransitRunner, "_counters", refuse), \
                unittest.mock.patch.object(estimate.Estimate, "start") as start, \
                contextlib.redirect_stdout(out):
            self.assertEqual(estimate.cmd_estimate(args, CAMS), 1)
        start.assert_not_called()
        self.assertIn("YOLOX on the GPU was asked for", out.getvalue())
        self.assertIn("refused: no NVIDIA GPU", out.getvalue())
        hold_lock(self.dir / "transit.lock").close()
        hold_lock(self.dir / "worker.lock").close()

    def test_the_device_is_the_option_else_the_environment_else_auto(self):
        """`camera estimate --device` reaches the runner, as `camera transit`'s does: dropped on
        the way, YOLOX would run on "auto", the GPU on the owner's machine."""
        from scraper.border.camera.gpu import GpuRefused

        seen = []

        def refuse(runner, cam):        # where YOLOX would run, then stop before any reading
            seen.append(runner.device)
            raise GpuRefused("no NVIDIA GPU among the DirectML devices: none")
        for device, env, want in (("cpu", "gpu", "cpu"), (None, "gpu", "gpu"), (None, "", "auto")):
            args = SimpleNamespace(replay=None, queue=None, methods="loop,yolox", queue_every=120,
                                   port=0, every=60, device=device)
            with unittest.mock.patch.dict(os.environ, {"CHISME_YOLOX_DEVICE": env}), \
                    unittest.mock.patch.object(TransitRunner, "_counters", refuse), \
                    unittest.mock.patch.object(estimate.Estimate, "start") as start, \
                    contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(estimate.cmd_estimate(args, CAMS), 1)
            start.assert_not_called()
            self.assertEqual(seen[-1], want, (device, env))
            hold_lock(self.dir / "transit.lock").close()
            hold_lock(self.dir / "worker.lock").close()

    def test_a_device_in_the_environment_that_is_none_of_the_three_stops_with_its_message(self):
        args = SimpleNamespace(replay=None, queue=None, methods="loop,yolox", queue_every=120,
                               port=0, every=60, device=None)
        with unittest.mock.patch.dict(os.environ, {"CHISME_YOLOX_DEVICE": "tpu"}), \
                self.assertRaisesRegex(SystemExit, "YOLOX device 'tpu'"):
            estimate.cmd_estimate(args, CAMS)


def dict_args(args: SimpleNamespace, **changes) -> SimpleNamespace:
    return SimpleNamespace(**{**vars(args), **changes})


class CommandTest(StateDir):
    def test_the_command_ticks_serves_the_page_and_lets_go_of_both_locks_at_ctrl_c(self):
        """cmd_estimate as the owner runs it, with no stream started: OpenCV at 4 threads, the
        page served with this process's Worker and estimate, one tick printed, and both locks
        let go at Ctrl+C."""
        import cv2

        self.addCleanup(cv2.setNumThreads, cv2.getNumThreads())
        ticks = iter([None])

        def sleep(_):
            if next(ticks, "stop") == "stop":
                raise KeyboardInterrupt
        served = {}

        def serve(port):
            served["server"] = server = real_serve(0)
            return server
        real_serve = viewer.serve
        args = SimpleNamespace(replay=None, queue=None, methods="loop", queue_every=120, port=0,
                               every=60)
        out = io.StringIO()
        with unittest.mock.patch.object(TransitRunner, "_counters", lambda self, cam: []), \
                unittest.mock.patch.object(estimate.Estimate, "start", lambda self: None), \
                unittest.mock.patch.object(estimate.time, "sleep", sleep), \
                unittest.mock.patch.object(viewer, "serve", serve), \
                contextlib.redirect_stdout(out):
            self.assertEqual(estimate.cmd_estimate(args, CAMS), 0)
        self.assertIn("Page: http://127.0.0.1:0", out.getvalue())
        self.assertIn("camera wait: nothing measured yet", out.getvalue())
        self.assertEqual(cv2.getNumThreads(), estimate.THREADS)
        server = served["server"]
        self.assertIsInstance(server.estimate, estimate.Live)
        self.assertEqual([c.id for c in server.worker.cameras], ["pdn-north", "pdn-south-street"])
        hold_lock(self.dir / "transit.lock").close()
        hold_lock(self.dir / "worker.lock").close()


class QueueLogTest(StateDir):
    def test_it_keeps_readings_only_and_hands_combine_what_was_observed_by_then(self):
        log_ = estimate.QueueLog(keep_s=1800)
        old = signal("pdn-north", "lane-1", T0 - timedelta(minutes=40))
        log_.add(old)
        log_.add({"camera": "pdn-north", "error": "StreamLost: dropped"})
        for minute in (0, 10, 16):
            log_.add(signal("pdn-north", "lane-1", T0 + timedelta(minutes=minute, seconds=50)))
        at = T0 + timedelta(minutes=15)
        got = [s["observed_at"] for s in log_.recent(at)]
        # 40 min before the newest is past keep_s; 16 min is after `at`; error lines are not
        # readings. What is left is cut to the minute.
        self.assertEqual(got, ["2026-10-26T17:00:00+00:00", "2026-10-26T17:10:00+00:00"])
        self.assertEqual(len(lines(self.dir, "queue-*.jsonl")), 5)

    def test_an_error_line_has_the_minute_it_came_and_marks_its_cameras_last_cycle(self):
        log_ = estimate.QueueLog()
        before = datetime.now(UTC).replace(second=0, microsecond=0)
        log_.add({"camera": "pdn-north", "error": "StreamLost: dropped"})    # no time given
        log_.add({"camera": "pdn-south-street", "error": "TimelineCut: timestamps jumped",
                  "failed_at": "2026-10-28T07:54:31.250000+00:00"})        # the Worker's
        written = lines(self.dir, "queue-*.jsonl")
        stamped = datetime.fromisoformat(written[0]["failed_at"])
        self.assertEqual((stamped.second, stamped.microsecond), (0, 0))
        self.assertTrue(before <= stamped <= datetime.now(UTC), stamped)
        self.assertEqual(written[1], {"camera": "pdn-south-street",
                                      "error": "TimelineCut: timestamps jumped",
                                      "failed_at": "2026-10-28T07:54:00+00:00"})
        self.assertEqual(log_.failed()["pdn-south-street"],
                         ("2026-10-28T07:54:00+00:00", "TimelineCut: timestamps jumped"))
        self.assertEqual(log_.recent(T0), [])                 # never a reading
        log_.add(signal("pdn-south-street", "left-column", T0))   # its next cycle read
        self.assertEqual(sorted(log_.failed()), ["pdn-north"])

    def test_the_worker_times_a_failed_cycle(self):
        sent = []
        worker = SimpleNamespace(_set=lambda *a, **k: None, _notify=sent.append,
                                 _emit=sent.append)
        cam = SimpleNamespace(id="pdn-south-street")
        Worker._fail(worker, cam, "down", RuntimeError("x"), was_failing=True)
        self.assertEqual(sent[0]["camera"], "pdn-south-street")
        self.assertLess(abs((datetime.fromisoformat(sent[0]["failed_at"])
                             - datetime.now(UTC)).total_seconds()), 60)

    def test_upto_takes_each_lanes_readings_up_to_its_logged_minute(self):
        logged = [{**s, "observed_at": estimate._minute(s["observed_at"])} for s in (
            *[signal("pdn-north", "lane-1", T0 + timedelta(minutes=m, seconds=10))
              for m in (0, 2, 4)],
            *[signal("pdn-north", "lane-2", T0 + timedelta(minutes=m, seconds=50))
              for m in (0, 2)],
            signal("pdn-south-street", "left-column", T0))]
        seen = {"pdn-north": {"lane-1": "2026-10-26T17:02:00+00:00",
                              "lane-2": "2026-10-26T17:02:00+00:00"}}
        got = [(s["evidence"]["lane"], s["observed_at"][11:16])
               for s in estimate.upto(logged, seen)]
        # lane-1's 17:04 came after the tick; the street had no reading by then
        self.assertEqual(got, [("lane-1", "17:00"), ("lane-1", "17:02"), ("lane-2", "17:00"),
                               ("lane-2", "17:02")])
        # Two readings of one lane in its logged minute: both are taken, and that is said.
        with self.assertLogs(estimate.log, "WARNING") as said:
            self.assertEqual(len(estimate.upto([*logged, dict(logged[1])], seen)), 5)
        self.assertIn("pdn-north lane-1: 2 readings at 2026-10-26T17:02:00+00:00",
                      said.output[0])

    def test_line_a_lies_where_the_strips_and_zones_put_it(self):
        """a_m and r_m come from cameras.json through the strips' ground homography: line A's
        far edge 66 m from the strips' booth end, 9 m before the last metre they see (the
        design's nominal 66 and 9.3 m)."""
        self.assertEqual(self.pair["a_lanes"], {"lane-1": "normal", "lane-2": "normal",
                                                "lane-3": "normal", "lane-4": "sentri",
                                                "lane-5": "sentri"})
        self.assertEqual(self.pair["a_m"], dict.fromkeys(A_LANES, 66.0))
        self.assertEqual(self.pair["r_m"], dict.fromkeys(A_LANES, 9.0))
        self.assertTrue(self.pair["show_minutes"])


if __name__ == "__main__":
    unittest.main()
