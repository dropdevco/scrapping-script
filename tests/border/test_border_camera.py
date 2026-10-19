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
from scraper.border.camera.measure import LaneWindow, Reference, image_features

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
