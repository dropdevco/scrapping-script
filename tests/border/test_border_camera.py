"""The camera reader against rendered queues whose length and speed are known exactly.

Each scene is a perspective view of one lane, so these exercise the whole chain the real
streams go through: rectify, compare against the empty road, correlate frame to frame,
aggregate a window. Tolerances are what a crosser would notice, not what flatters the code.
"""
from __future__ import annotations

import unittest

import pytest

pytest.importorskip("cv2")

from camera_scene import FRAME, Scene, camera, queue_fronts

from scraper.border.camera.calibration import Strip
from scraper.border.camera.measure import (
    LaneEstimate,
    LaneWindow,
    Reference,
    image_features,
    rolling,
)

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


if __name__ == "__main__":
    unittest.main()


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
