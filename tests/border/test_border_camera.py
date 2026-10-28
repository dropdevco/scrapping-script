"""The camera reader against rendered queues whose length and speed are known exactly.

Each scene is a perspective view of one lane, so these exercise the whole chain the real
streams go through: rectify, compare against the empty road, correlate frame to frame,
aggregate a window. Tolerances are what a crosser would notice, not what flatters the code.
"""
from __future__ import annotations

import copy
import dataclasses
import json
import os
import tempfile
import unittest
import unittest.mock

import numpy as np
import pytest

pytest.importorskip("cv2")

from camera_scene import FRAME, Scene, camera, queue_fronts

from scraper.border.camera.calibration import (
    CAMERAS_FILE,
    Camera,
    Strip,
    camera_from_dict,
    load_cameras,
)
from scraper.border.camera.measure import (
    SCALE_SLACK,
    LaneEstimate,
    LaneWindow,
    Reference,
    image_features,
    rolling,
    wait_bounds,
)
from scraper.border.camera.reader import CameraReader, _state_path

DT = 0.5
FRAMES = 120


def run(cam, frames, empty=None):
    strip = Strip(cam, cam.lanes[0], FRAME)
    empty = empty if empty is not None else Scene().render([])
    ref = Reference.from_images(strip, [image_features(empty)])
    window = LaneWindow(strip, ref, DT)
    for f in frames:
        window.add(f)
    return window.result()


def moving_queue(queue_m, speed_of, frames=FRAMES, scene=None):
    scene = scene or Scene()
    travelled, out = 0.0, []
    for n in range(frames):
        t = n * DT
        out.append(scene.render(queue_fronts(queue_m, travelled, scene.view_m)))
        travelled += speed_of(t) * DT
    return out


class Rectification(unittest.TestCase):
    def test_strip_runs_booth_first_and_covers_the_whole_view(self):
        strip = Strip(camera(), camera().lanes[0], FRAME)
        self.assertEqual(strip.size, (32, 480))          # 4 m x 60 m at 8 px/m
        self.assertGreater(strip.rows_valid.mean(), 0.95)
        far_end = strip.outline()[0]                      # strip row 0, left edge
        self.assertAlmostEqual(far_end[1], 90, delta=2)   # the far edge of the rectangle

    def test_road_beyond_the_frame_is_not_treated_as_seen(self):
        cam = camera(along=(-1.0, 1.0))                   # 60 m behind the camera's view
        strip = Strip(cam, cam.lanes[0], FRAME)
        seen_m = strip.rows_valid.sum() / 8
        self.assertLess(seen_m, 75)


class Queues(unittest.TestCase):
    def test_empty_lane_has_no_queue_and_no_wait(self):
        est = run(camera(), [Scene().render([]) for _ in range(FRAMES)])
        self.assertEqual(est.state, "no_queue")
        self.assertEqual(est.wait_min, 0.0)

    def test_steady_queue_length_speed_and_wait(self):
        est = run(camera(), moving_queue(30, lambda t: 0.6))
        self.assertEqual(est.state, "queue", est.detail)
        self.assertAlmostEqual(est.queue_m, 32, delta=5)
        self.assertAlmostEqual(est.speed_m_s, 0.6, delta=0.08)
        self.assertAlmostEqual(est.wait_min, 32 / 0.6 / 60, delta=0.25)
        self.assertFalse(est.beyond_view)
        self.assertLessEqual(est.low_min, est.wait_min)
        self.assertGreaterEqual(est.high_min, est.wait_min)

    def test_stop_and_go_averages_to_the_mean_speed(self):
        # 1.2 m/s for five seconds, stopped for five: the queue covers 0.6 m/s on average.
        est = run(camera(), moving_queue(30, lambda t: 1.2 if int(t // 5) % 2 == 0 else 0.0))
        self.assertEqual(est.state, "queue", est.detail)
        self.assertAlmostEqual(est.speed_m_s, 0.6, delta=0.1)

    def test_a_queue_that_never_moves_is_stalled_not_absorbed_into_the_road(self):
        est = run(camera(), moving_queue(30, lambda t: 0.0))
        self.assertEqual(est.state, "stalled", est.detail)
        self.assertAlmostEqual(est.queue_m, 32, delta=5)
        self.assertIsNone(est.wait_min)

    def test_a_queue_longer_than_the_view_is_a_floor(self):
        est = run(camera(), moving_queue(90, lambda t: 0.6))
        self.assertTrue(est.beyond_view, est.detail)
        self.assertTrue(est.lower_bound)
        self.assertIsNone(est.high_min)
        self.assertGreaterEqual(est.queue_m, 55)

    def test_a_queue_longer_than_the_view_with_the_booth_out_of_view_is_not_a_floor(self):
        # The wait assumes the unseen road to the booth moves at the visible speed; if it moves
        # faster the true wait is shorter, so the range's low end is no "at least".
        est = run(camera(booth_distance_m=120), moving_queue(90, lambda t: 0.6))
        self.assertTrue(est.beyond_view, est.detail)
        self.assertFalse(est.lower_bound)
        self.assertIsNone(est.high_min)
        self.assertIsNotNone(est.wait_min)

    def test_the_wait_includes_the_unseen_stretch_to_the_booth(self):
        est = run(camera(booth_distance_m=120), moving_queue(30, lambda t: 0.6))
        self.assertAlmostEqual(est.wait_min, (120 + 32) / 0.6 / 60, delta=0.6)

    def test_no_queue_here_says_nothing_about_an_unseen_booth(self):
        est = run(camera(booth_distance_m=120), [Scene().render([]) for _ in range(FRAMES)])
        self.assertEqual(est.state, "no_queue")
        self.assertIsNone(est.wait_min)

    def test_too_few_frames_is_reported_not_guessed(self):
        est = run(camera(), moving_queue(30, lambda t: 0.6, frames=10))
        self.assertEqual(est.state, "insufficient")
        self.assertIsNone(est.wait_min)


class Rolling(unittest.TestCase):
    def window(self, state, speed, queue=40.0, seconds=60.0, beyond=False):
        return LaneEstimate(camera="c", lane="l", port_number="p", direction="north",
                            state=state, frames=120, seconds=seconds, queue_m=queue,
                            beyond_view=beyond, speed_m_s=speed)

    def test_stop_and_go_windows_average_including_the_stops(self):
        history = [(0, self.window("stalled", 0.0)), (300, self.window("queue", 0.3)),
                   (600, self.window("queue", 0.15))]
        out = rolling(history, now=600, booth_distance_m=100)
        self.assertAlmostEqual(out["speed_m_s"], 0.15)
        self.assertAlmostEqual(out["wait_min"], (100 + 40) / 0.15 / 60)

    def test_old_windows_and_empty_lanes_do_not_count(self):
        history = [(0, self.window("queue", 2.0)), (1000, self.window("no_queue", None)),
                   (1200, self.window("queue", 0.2))]
        out = rolling(history, now=1200, booth_distance_m=0)
        self.assertEqual(out["windows"], 1)
        self.assertAlmostEqual(out["speed_m_s"], 0.2)

    def test_no_booth_distance_means_speed_but_no_wait(self):
        out = rolling([(0, self.window("queue", 0.2))], now=0, booth_distance_m=None)
        self.assertIsNone(out["wait_min"])
        self.assertAlmostEqual(out["speed_m_s"], 0.2)


class Determinism(unittest.TestCase):
    def test_same_frames_same_answer(self):
        frames = moving_queue(30, lambda t: 0.6)
        self.assertEqual(run(camera(), frames).to_dict(), run(camera(), frames).to_dict())

    def test_a_wrong_scale_cancels_out_of_the_wait_when_the_booth_is_in_view(self):
        frames = moving_queue(30, lambda t: 0.6)
        right = run(camera(), frames)
        wrong = run(camera(length_m=90, width_m=6), frames)   # every distance 1.5x too long
        self.assertAlmostEqual(wrong.queue_m / right.queue_m, 1.5, delta=0.15)
        self.assertAlmostEqual(wrong.wait_min, right.wait_min, delta=0.1 * right.wait_min)




class EmptiestReference(unittest.TestCase):
    """The road is learnt from the moments the lane was empty, even when it was queued most
    of the time, and an all-queued hour never overwrites a good reference."""

    def setUp(self):
        from scraper.border.camera.measure import strip_features

        self.cam = camera()
        self.strip = Strip(self.cam, self.cam.lanes[0], FRAME)
        self.empty = Scene().render([])
        self.features = lambda f: strip_features(self.strip, image_features(f))

    def test_a_mostly_queued_hour_still_learns_the_road(self):
        from scraper.border.camera.reader import emptiest_reference

        # Twelve moments of a moving queue (different cars each time), five of an empty lane.
        # A queue that never moved all hour is indistinguishable from paint, so not tested.
        queued = [self.features(f) for f in moving_queue(40, lambda t: 0.6, frames=96)[::8]]
        empty = [self.features(self.empty) for _ in range(5)]
        ref = emptiest_reference(queued + empty, self.strip.valid, None)
        window = LaneWindow(self.strip, ref, DT)
        for f in [self.empty] * 30:
            window.add(f)
        self.assertEqual(window.result().state, "no_queue")

    def test_an_hour_with_no_empty_moment_keeps_the_reference_held(self):
        from scraper.border.camera.reader import emptiest_reference

        held = Reference.from_images(self.strip, [image_features(self.empty)])
        queued = [self.features(f) for f in moving_queue(40, lambda t: 0.0, frames=12)]
        self.assertIsNone(emptiest_reference(queued, self.strip.valid, held))


class QueueRuns(unittest.TestCase):
    """The rules that decide whether a profile of per-metre occupancy is a queue, each from a
    case seen on real footage on 2026-10-19."""

    def run_of(self, share, booth_in_view):
        import numpy as np

        from scraper.border.camera.measure import _queue_run

        share = np.array(share, float)
        return _queue_run(share, np.ones(len(share), bool), booth_in_view)

    def test_a_clear_head_does_not_hide_a_queue_when_the_booth_is_out_of_view(self):
        # Lerdo, 07:16 local: the first 15 m had just cleared, the rest of the lane was full.
        share = [0.1] * 15 + [0.95] * 50
        self.assertEqual(self.run_of(share, booth_in_view=False)[:2], (15, 64))

    def test_with_the_booth_in_view_the_queue_must_reach_it(self):
        share = [0.1] * 15 + [0.95] * 50
        self.assertIsNone(self.run_of(share, booth_in_view=True))

    def test_islands_of_clutter_are_not_a_queue(self):
        # PDN south, 02:36 local: a cart, people standing; dense spots with gaps between.
        share = ([0.9] * 3 + [0.1] * 4) * 6
        self.assertIsNone(self.run_of(share, booth_in_view=False))

    def test_a_stretch_held_just_over_the_line_is_not_a_queue(self):
        # PDN south, 02:56 local: 0.5-0.6 for 10 m, someone crossing now and then.
        self.assertIsNone(self.run_of([0.0] * 20 + [0.55] * 11 + [0.0] * 20, booth_in_view=False))

    def test_one_parked_object_is_not_a_queue_when_the_booth_is_out_of_view(self):
        self.assertIsNone(self.run_of([0.0] * 20 + [1.0] * 5 + [0.0] * 20, booth_in_view=False))


class HeadGap(unittest.TestCase):
    """cameras.json's max_head_gap_m: with the booth out of view, a run that starts further
    into the view than this is no line. The cases are from PDN's free-flow hour, 2026-10-27."""

    def run_of(self, share, max_head_gap_m=18, seen=None):
        from scraper.border.camera.measure import _queue_run

        share = np.array(share, float)
        seen = np.ones(len(share), bool) if seen is None else np.array(seen, bool)
        return _queue_run(share, seen, booth_in_view=False, max_head_gap_m=max_head_gap_m)

    def test_a_still_blob_far_into_an_empty_street_is_no_line(self):
        # The street's left column, 19:41 UTC: a 10 m blob 41-50 m in, 41 m of free road before it.
        self.assertIsNone(self.run_of([0.05] * 41 + [1.0] * 10))

    def test_without_a_head_gap_the_rule_is_unchanged(self):
        # Off unless the camera sets it: Lerdo and BOTA read the same blob as a line, as before.
        self.assertEqual(self.run_of([0.05] * 41 + [1.0] * 10, max_head_gap_m=None)[:2], (41, 50))

    def test_a_run_starting_within_the_head_gap_is_still_a_line(self):
        self.assertEqual(self.run_of([0.1] * 18 + [0.95] * 30)[:2], (18, 47))

    def test_lerdos_clear_head_is_still_a_line(self):
        self.assertEqual(self.run_of([0.1] * 15 + [0.95] * 50)[:2], (15, 64))

    def test_the_gap_is_counted_from_the_first_metre_in_view(self):
        # 10 metres out of the picture at the booth end, then 15 m of free road: 15 m in.
        share = [0.0] * 25 + [0.95] * 20 + [0.0] * 15
        self.assertEqual(self.run_of(share, seen=[False] * 10 + [True] * 50)[:2], (25, 44))

    def test_a_parked_object_near_the_head_does_not_carry_a_far_run(self):
        # The 5 m object is no line on its own, so the head gap is the far run's: 37 m.
        share = [0.0] * 2 + [1.0] * 5 + [0.0] * 30 + [1.0] * 15
        self.assertIsNone(self.run_of(share))
        self.assertEqual(self.run_of(share, max_head_gap_m=None)[:2], (37, 51))

    def still_blob(self, cam):
        # Three cars standing 30-47.5 m from the booth end, nothing between them and it.
        scene = Scene()
        frame = scene.render([(scene.view_m - 30 - 6.5 * i, i) for i in range(3)])
        return run(cam, [frame] * 40)

    def test_a_reading_on_a_camera_without_a_head_gap_keeps_the_far_blob(self):
        est = self.still_blob(camera(booth_distance_m=120))
        self.assertNotEqual(est.state, "no_queue", est.detail)
        self.assertNotIn("far_run_m", est.detail)

    def test_a_reading_on_a_camera_with_a_head_gap_drops_it_and_logs_where_it_was(self):
        est = self.still_blob(with_head_gap(camera(booth_distance_m=120), 18))
        self.assertEqual(est.state, "no_queue", est.detail)
        self.assertIsNone(est.wait_min)
        # Whole metres, end exclusive as visible_m: the cars stand 30-47.5 m in, past the 18 m.
        self.assertEqual(est.detail["far_run_m"], [29.0, 48.0])

    def test_with_the_booth_in_view_a_head_gap_changes_nothing(self):
        # There the queue must already reach the booth (GAP_M).
        est = self.still_blob(with_head_gap(camera(), 18))
        self.assertEqual(est.state, "no_queue", est.detail)
        self.assertNotIn("far_run_m", est.detail)

    def test_cameras_json_sets_it_on_the_pdn_pair_only(self):
        with open(CAMERAS_FILE, encoding="utf-8") as f:
            raw = json.load(f)
        gaps = {cid: c["max_head_gap_m"] for cid, c in raw.items()
                if not cid.startswith("_") and "max_head_gap_m" in c}
        self.assertEqual(gaps, {"pdn-north": 18, "pdn-south-street": 18})


def with_head_gap(cam: Camera, gap: float) -> Camera:
    return dataclasses.replace(cam, max_head_gap_m=gap)


class HeadGapField(unittest.TestCase):
    def test_it_is_read_from_cameras_json_and_absent_means_no_limit(self):
        self.assertEqual(camera_from_dict("x", {**raw_camera(), "max_head_gap_m": 18})
                         .max_head_gap_m, 18)
        self.assertIsNone(camera_from_dict("x", raw_camera()).max_head_gap_m)
        cams = load_cameras()
        self.assertEqual((cams["pdn-north"].max_head_gap_m, cams["lerdo-north"].max_head_gap_m),
                         (18, None))

    def test_a_negative_head_gap_is_refused(self):
        with self.assertRaisesRegex(ValueError, "max_head_gap_m cannot be negative"):
            camera_from_dict("x", {**raw_camera(), "max_head_gap_m": -1})


class Serialisation(unittest.TestCase):
    def test_a_real_reading_serialises_to_json(self):
        # The live worker's first output line once crashed on a numpy bool.
        import json

        from scraper.border.camera.reader import as_signal

        cam = camera()
        est = run(cam, moving_queue(90, lambda t: 0.6))
        sig = as_signal(est, cam.lanes[0], __import__("datetime").datetime(2026, 10, 19))
        sig["evidence"]["rolling"] = rolling([(0, est)], now=0, booth_distance_m=0)
        decoded = json.loads(json.dumps(sig))
        self.assertIs(decoded["evidence"]["beyond_view"], True)


class ReadCommand(unittest.TestCase):
    def test_a_floor_is_printed_at_its_low_end_not_the_central_wait(self):
        import contextlib
        import io
        from types import SimpleNamespace

        from scraper.border.camera import reader as reader_module

        cam = camera()
        sig = {"minutes": 9, "low": 7, "high": None,
               "evidence": {"lane": "car", "state": "queue", "queue_m": 60.0, "beyond_view": True,
                            "speed_m_s": 0.11, "lower_bound": True}}
        args = SimpleNamespace(camera=cam.id, source=None, seconds=60, debug=None, json=False)
        out = io.StringIO()
        with unittest.mock.patch.object(reader_module, "_read_once", return_value=[sig]), \
                contextlib.redirect_stdout(out):
            reader_module.cmd_read(args, {cam.id: cam})
        self.assertIn("wait at least 7 min", out.getvalue())


class HiddenFrames(unittest.TestCase):
    """A display: false camera (the City's) is never shown and its frames are not written:
    `snapshot` and `read --debug` refuse it before opening any stream, unless --calibration,
    which prints the rule the frame is written under (owner, 2026-10-26; Q-21)."""

    def setUp(self):
        import contextlib
        import io
        from pathlib import Path

        from scraper.border.camera import reader as reader_module
        from scraper.border.camera.calibration import load_cameras

        self.reader = reader_module
        self.assertFalse(load_cameras()["city-pdn-north"].display)
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.out = Path(d.name) / "frame.png"
        self.printed = io.StringIO()
        self.redirect = contextlib.redirect_stdout(self.printed)

    def opened(self):
        """Every way a frame could be opened, each failing the test if called."""
        never = AssertionError("a stream or clip was opened")
        return (unittest.mock.patch.object(self.reader, "live_frames", side_effect=never),
                unittest.mock.patch.object(self.reader, "frames", side_effect=never),
                unittest.mock.patch.object(self.reader, "_read_once", side_effect=never))

    def refused(self, argv):
        live, clip, read = self.opened()
        with live, clip, read, self.redirect, self.assertRaises(SystemExit) as caught:
            self.reader.main(argv)
        self.assertIn("ADR-0014 decision 5", str(caught.exception.code))
        self.assertIn("--calibration", str(caught.exception.code))
        self.assertFalse(self.out.exists())

    def test_a_snapshot_of_a_hidden_camera_is_refused_before_any_stream(self):
        self.refused(["snapshot", "--camera", "city-pdn-north", "--out", str(self.out)])
        self.refused(["snapshot", "--camera", "city-pdn-north", "--out", str(self.out),
                      "--source", "clip.ts"])

    def test_read_debug_on_a_hidden_camera_is_refused_before_any_stream(self):
        self.refused(["read", "--camera", "city-pdn-north", "--debug", str(self.out)])

    def test_read_without_debug_writes_no_frame_and_is_not_refused(self):
        with unittest.mock.patch.object(self.reader, "_read_once", return_value=[]) as read, \
                self.redirect:
            self.assertEqual(self.reader.main(["read", "--camera", "city-pdn-north"]), 0)
        self.assertIsNone(read.call_args.args[3])

    def test_with_calibration_the_rule_is_printed_and_the_frame_written(self):
        from datetime import UTC, datetime

        from scraper.border.camera.hls import Stamped

        black = Stamped(np.zeros((720, 1280, 3), np.uint8), datetime(2026, 10, 26, tzinfo=UTC), 0)
        with unittest.mock.patch.object(self.reader, "live_frames", return_value=iter([black])), \
                self.redirect:
            self.reader.main(["snapshot", "--camera", "city-pdn-north", "--out", str(self.out),
                              "--calibration"])
        self.assertTrue(self.out.exists())
        self.assertIn(self.reader.CALIBRATION_RULE, self.printed.getvalue())
        # The rule printed says when a frame may be written; it does not claim it never is.
        self.assertNotIn("never written", self.printed.getvalue())
        with unittest.mock.patch.object(self.reader, "_read_once", return_value=[]) as read, \
                self.redirect:
            self.reader.main(["read", "--camera", "city-pdn-north", "--debug", str(self.out),
                              "--calibration"])
        self.assertEqual(read.call_args.args[3], str(self.out))
        self.assertEqual(self.printed.getvalue().count(self.reader.CALIBRATION_RULE), 2)

    def refused_in_repo(self, argv, path):
        live, clip, read = self.opened()
        with live, clip, read, self.redirect, self.assertRaises(SystemExit) as caught:
            self.reader.main(argv)
        self.assertIn("inside the repository", str(caught.exception.code))
        self.assertFalse(path.exists())

    def test_with_calibration_a_frame_bound_for_the_repository_is_refused(self):
        """A City frame never enters the repository: with --calibration, an --out inside it,
        given whole or relative to a run from the repository root, is refused before any
        stream opens."""
        from pathlib import Path

        repo = Path(__file__).resolve().parents[2]
        inside = repo / "tests" / "border" / "_never.png"
        self.refused_in_repo(["snapshot", "--camera", "city-pdn-north", "--out", str(inside),
                              "--calibration"], inside)
        here = os.getcwd()
        os.chdir(repo)
        self.addCleanup(os.chdir, here)
        self.refused_in_repo(["snapshot", "--camera", "city-pdn-north", "--out", "check.jpg",
                              "--calibration"], repo / "check.jpg")

    def test_with_calibration_a_debug_frame_bound_for_the_repository_is_refused(self):
        from pathlib import Path

        inside = Path(__file__).resolve().parents[2] / "tests" / "border" / "_never.png"
        self.refused_in_repo(["read", "--camera", "city-pdn-north", "--debug", str(inside),
                              "--calibration"], inside)

    def test_a_displayed_camera_needs_no_flag_and_prints_no_rule(self):
        from datetime import UTC, datetime

        from scraper.border.camera.hls import Stamped

        frame = Stamped(np.zeros((480, 854, 3), np.uint8), datetime(2026, 10, 26, tzinfo=UTC), 0)
        with unittest.mock.patch.object(self.reader, "live_frames", return_value=iter([frame])), \
                self.redirect:
            self.reader.main(["snapshot", "--camera", "pdn-north", "--out", str(self.out)])
        self.assertTrue(self.out.exists())
        self.assertNotIn(self.reader.CALIBRATION_RULE, self.printed.getvalue())


class WaitBounds(unittest.TestCase):
    def test_the_low_end_is_the_short_distance_at_the_fastest_speed(self):
        low, high = wait_bounds(100, 0, 20, fastest=0.4, slowest=0.2, scale_verified=True)
        self.assertAlmostEqual(low, 120 / 0.4 / 60)
        self.assertAlmostEqual(high, 120 / 0.2 / 60)

    def test_the_distance_uncertainty_widens_both_ends(self):
        low, high = wait_bounds(400, 40, 20, 0.5, 0.5, scale_verified=True)
        self.assertAlmostEqual(low, (360 + 20) / 0.5 / 60)
        self.assertAlmostEqual(high, (440 + 20) / 0.5 / 60)

    def test_an_uncertainty_larger_than_the_distance_never_goes_below_the_visible_line(self):
        low, _ = wait_bounds(10, 30, 20, 0.5, 0.5, scale_verified=True)
        self.assertAlmostEqual(low, 20 / 0.5 / 60)

    def test_an_unverified_scale_widens_the_range_only_when_the_booth_is_out_of_view(self):
        low, high = wait_bounds(400, 0, 20, 0.5, 0.5, scale_verified=False)
        wait = 420 / 0.5 / 60
        self.assertAlmostEqual(low, wait / (1 + SCALE_SLACK))
        self.assertAlmostEqual(high, wait * (1 + SCALE_SLACK))
        # With the booth in view the scale cancels out of the wait, so no slack.
        self.assertEqual(wait_bounds(0, 0, 20, 0.5, 0.5, scale_verified=False),
                         wait_bounds(0, 0, 20, 0.5, 0.5, scale_verified=True))


class RollingRange(unittest.TestCase):
    def window(self, beyond=False):
        return LaneEstimate(camera="c", lane="l", port_number="p", direction="north",
                            state="queue", frames=120, seconds=60, queue_m=40.0,
                            beyond_view=beyond, speed_m_s=0.2)

    def test_the_range_carries_the_distance_uncertainty_and_contains_the_wait(self):
        out = rolling([(0, self.window())], now=0, booth_distance_m=400, uncertainty_m=40)
        self.assertAlmostEqual(out["wait_min"], 440 / 0.2 / 60)
        self.assertAlmostEqual(out["low_min"], 400 / 0.2 / 60)
        self.assertAlmostEqual(out["high_min"], 480 / 0.2 / 60)

    def test_a_line_running_out_of_view_has_a_low_end_and_no_high_end(self):
        out = rolling([(0, self.window(beyond=True))], now=0, booth_distance_m=400,
                      uncertainty_m=40, scale_verified=False)
        self.assertIsNone(out["high_min"])
        self.assertAlmostEqual(out["low_min"], 400 / 0.2 / 60 / (1 + SCALE_SLACK))
        # Not a floor: the low end still assumes the 400 m out of view moves like the visible
        # line, and a line that merges and opens gaps out there moves faster.
        self.assertFalse(out["lower_bound"])

    def test_a_line_running_out_of_view_from_a_booth_in_view_is_a_floor(self):
        out = rolling([(0, self.window(beyond=True))], now=0, booth_distance_m=0)
        self.assertTrue(out["lower_bound"])
        self.assertIsNone(out["high_min"])
        self.assertAlmostEqual(out["low_min"], 40 / 0.2 / 60)


def raw_camera(**lane):
    return {"title": "t", "port_number": "000000", "direction": "north", "frame_size": FRAME,
            "ground": {"image": [(160, 340), (480, 340), (345, 90), (295, 90)],
                       "width_m": 4.0, "length_m": 60.0},
            "lanes": [{"lane": "car", "across": [0, 1], "along": [0, 1], "booth": "far", **lane}]}


class BoothDistanceFields(unittest.TestCase):
    def test_a_null_uncertainty_means_none_given(self):
        cam = camera_from_dict("x", raw_camera(booth_distance_m=400,
                                               booth_distance_uncertainty_m=None))
        self.assertEqual(cam.lanes[0].booth_distance_uncertainty_m, 0.0)

    def test_negative_distances_are_refused(self):
        for fields in ({"booth_distance_m": -5}, {"booth_distance_uncertainty_m": -1}):
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                camera_from_dict("x", raw_camera(**fields))

    def test_only_a_booth_in_view_counts_as_verified_unless_said(self):
        self.assertTrue(camera_from_dict("x", raw_camera(booth_distance_m=0)).lanes[0]
                        .booth_distance_verified)
        self.assertFalse(camera_from_dict("x", raw_camera(booth_distance_m=400)).lanes[0]
                         .booth_distance_verified)


class SavedReferences(unittest.TestCase):
    """What the worker loads at start: good lanes kept, stale ones dropped, never a crash."""

    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        patcher = unittest.mock.patch.dict(os.environ, {"CAMERA_STATE_DIR": d.name})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.raw = raw_camera()
        self.raw["lanes"].append({**self.raw["lanes"][0], "lane": "car-2"})
        self.cam = camera_from_dict("two", self.raw)

    def learnt(self):
        reader = CameraReader(self.cam)
        reader.learn([image_features(Scene().render([]))])
        return reader

    def test_a_stale_reference_is_deleted_and_relearnt_even_on_windows(self):
        # The archive used to stay open while the stale file was deleted: on Windows that
        # raised PermissionError and stopped the worker on every restart.
        self.learnt().save()
        stale = _state_path(self.cam, "car.reference")
        with np.load(stale) as saved:
            arrays = {k: saved[k] for k in saved.files}
        np.savez_compressed(stale, **{**arrays, "size": np.array([1, 1])})
        reader = CameraReader(self.cam)
        self.assertEqual(reader.prepare(), ["car"])
        self.assertFalse(stale.exists())
        self.assertIn("car-2", reader.references)

    def test_a_road_redrawn_to_a_strip_of_the_same_size_is_relearnt(self):
        # Only the strip's pixel size was compared, so a road learnt for one stretch was reused
        # for another of the same size. A changed mask is the same: pixels it used to cover were
        # learnt black, and uncovered they read as a car.
        moved, masked = copy.deepcopy(self.raw), copy.deepcopy(self.raw)
        moved["lanes"][0]["along"] = [0.1, 1.1]                # car, 6 m further along the road
        masked["mask"] = [[0, 300, 640, 40]]
        for raw, relearnt in ((moved, ["car"]), (masked, ["car", "car-2"])):
            with self.subTest(relearnt=relearnt):
                self.learnt().save()
                cam = camera_from_dict("two", raw)
                self.assertEqual(Strip(cam, cam.lanes[0], FRAME).size,
                                 Strip(self.cam, self.cam.lanes[0], FRAME).size)
                self.assertEqual(CameraReader(cam).prepare(), relearnt)

    def test_one_missing_lane_leaves_the_others_learnt(self):
        self.learnt().save()
        _state_path(self.cam, "car-2.reference").unlink()
        reader = CameraReader(self.cam)
        self.assertEqual(reader.prepare(), ["car-2"])
        self.assertEqual(list(reader.references), ["car"])

    def test_an_unreadable_reference_is_dropped_and_relearnt(self):
        # A worker killed mid-save could leave garbage; it must not fail every cycle for ever.
        self.learnt().save()
        _state_path(self.cam, "car.reference").write_bytes(b"not an archive")
        reader = CameraReader(self.cam)
        self.assertEqual(reader.prepare(), ["car"])
        self.assertFalse(_state_path(self.cam, "car.reference").exists())

    def test_a_read_holds_a_few_images_not_one_per_frame(self):
        # Every frame's float32 grey and edges were kept for the whole read only to sample three
        # at the end: ~400 MB per 480p camera-minute, five cameras at once in the worker.
        import weakref

        from scraper.border.camera import reader as reader_module
        from scraper.border.camera.reader import SAMPLES_PER_WINDOW

        reader = self.learnt()
        banked = len(reader.bank)
        alive, sampled, frames = [], [], 100
        features = reader_module.image_features

        def tracked(frame):
            image = features(frame)
            object.__setattr__(image, "n", len(alive))
            alive.append(weakref.ref(image))
            return image

        def source():
            empty = Scene().render([])
            for n in range(frames):
                if n == frames - 1:
                    held = sum(ref() is not None for ref in alive)
                    self.assertLessEqual(held, 2 * SAMPLES_PER_WINDOW + 2)
                yield empty

        sample = reader._sample
        with unittest.mock.patch.object(reader_module, "image_features", tracked), \
                unittest.mock.patch.object(reader, "_sample",
                                           lambda im: sampled.append(im.n) or sample(im)):
            reader.read(source(), save=False)
        self.assertEqual(len(reader.bank) - banked, SAMPLES_PER_WINDOW)
        self.assertEqual(len(set(sampled)), SAMPLES_PER_WINDOW)
        self.assertGreaterEqual(sampled[-1] - sampled[0], frames // 2)

    def test_learning_one_lane_leaves_the_others_road_untouched(self):
        self.learnt().save()
        saved = _state_path(self.cam, "car.reference").read_bytes()
        reader = CameraReader(self.cam)
        reader.prepare()
        kept = reader.references["car"]
        # A car standing in every sample: learning car-2 from these must not touch car.
        reader.bank.extend(reader._sample(image_features(Scene().render([(30.0, 0)])))
                           for _ in range(6))
        reader._rebuild(["car-2"])
        reader.save(["car-2"])
        self.assertIs(reader.references["car"], kept)
        self.assertEqual(_state_path(self.cam, "car.reference").read_bytes(), saved)
        self.assertFalse(list(_state_path(self.cam, "x").parent.glob("*.tmp")))


if __name__ == "__main__":
    unittest.main()
