"""The YOLOX counter: decoding, letterboxing, NMS, tiles, tracking and line counting.

Everything except the last class runs without the model: a crafted output tensor stands in for
the network, or a stand-in network that "detects" bright rectangles in its own input, so the
whole path from frame pixels to the model's input and back is checked. Tracking and counting
are fed box sequences whose crossings are known by construction, and a simulated road: exact
boxes of 3-D cars seen through pinhole cameras like the two at PDN, with seeded misses, jitter
and scores. The model tests are skipped when the pinned file is absent (CI has none).
"""
from __future__ import annotations

import math
import os
import tempfile
import unittest
import unittest.mock
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import pytest

pytest.importorskip("cv2")

import cv2

from scraper.border.camera import detect
from scraper.border.camera.calibration import camera_from_dict
from scraper.border.camera.detect import (
    Detection,
    DetectorCounter,
    ModelRefused,
    Tile,
    Yolox,
    decode,
    nms,
    plan_tiles,
    preprocess,
    signature,
    vehicles,
)
from scraper.border.camera.hls import Gap, Stamped

T0 = datetime(2026, 10, 24, 3, 0, 0, tzinfo=UTC)
DT = 0.5
FRAME = (640, 360)
CAR, TRUCK, PERSON = 2, 7, 0

# Two lanes side by side; each zone is 30 px deep: near edge y=250, far edge y=220.
LANE_1 = [(100, 250), (200, 250), (200, 220), (100, 220)]
LANE_2 = [(200, 250), (300, 250), (300, 220), (200, 220)]


def cam(zones=None, frame=FRAME):
    zones = zones if zones is not None else [("A", "1", LANE_1), ("A", "2", LANE_2)]
    return camera_from_dict("cam", {
        "title": "test", "port_number": "000000", "direction": "north", "frame_size": list(frame),
        "count_zones": [{"line": ln, "lane": lane, "quad": q} for ln, lane, q in zones],
    })


def car(x: float, bottom: float, w: float = 60, h: float = 40, score: float = 0.8,
        cls: int = CAR) -> Detection:
    """A box whose bottom centre is (x, bottom)."""
    return Detection((x - w / 2, bottom - h, x + w / 2, bottom), score, cls)


class Scripted:
    """A detector that returns prepared boxes, one list per frame."""

    def __init__(self, frames):
        self.frames = list(frames)
        self.calls = 0

    def detect(self, image, tiles):
        out = self.frames[self.calls]
        self.calls += 1
        return out


def run(frames, camera=None, image=None):
    """Feed box lists frame by frame; return the crossings with the index of their frame."""
    counter = DetectorCounter(camera or cam(), detector=Scripted(frames))
    image = image if image is not None else np.zeros((FRAME[1], FRAME[0], 3), np.uint8)
    found = []
    for n in range(len(frames)):
        for c in counter.add(Stamped(image, T0 + timedelta(seconds=n * DT), n * 45000)):
            found.append((n, c))
    return found, counter


def frame_at(n: int) -> datetime:
    return T0 + timedelta(seconds=n * DT)


# --- the network's output ---------------------------------------------------------------------
def encode(boxes, size=640):
    """A raw (1, 8400, 85) output holding `boxes` = [(cx, cy, w, h, cls, obj, cls_prob)], each
    put in the stride-8 cell under its centre, the way the network's head writes it."""
    out = np.zeros((1, sum((size // s) ** 2 for s in detect.STRIDES), 85), np.float32)
    out[0, :, 2:4] = math.log(1 / 8)            # empty anchors: tiny boxes with no score
    cells = size // 8
    for cx, cy, w, h, cls, obj, prob in boxes:
        gx, gy = int(cx // 8), int(cy // 8)
        i = gy * cells + gx
        out[0, i, 0:4] = (cx / 8 - gx, cy / 8 - gy, math.log(w / 8), math.log(h / 8))
        out[0, i, 4] = obj
        out[0, i, 5 + cls] = prob
    return out


class Decoding(unittest.TestCase):
    def test_a_crafted_output_decodes_to_its_boxes_and_scores(self):
        out = encode([(100.0, 200.0, 64.0, 48.0, CAR, 0.9, 0.8),
                      (400.5, 300.25, 120.0, 90.0, TRUCK, 0.7, 0.5)])
        boxes, scores = decode(out)
        self.assertEqual(boxes.shape, (8400, 4))
        self.assertEqual(scores.shape, (8400, 80))
        found = vehicles(boxes, scores)
        self.assertEqual(len(found), 2)
        first, second = found
        np.testing.assert_allclose(first.box, (68, 176, 132, 224), atol=1e-3)
        self.assertAlmostEqual(first.score, 0.72, places=5)
        self.assertEqual(first.cls, CAR)
        np.testing.assert_allclose(second.box, (340.5, 255.25, 460.5, 345.25), atol=1e-3)
        self.assertAlmostEqual(second.score, 0.35, places=5)
        self.assertEqual(second.cls, TRUCK)

    def test_grid_runs_x_fastest_and_strides_coarsen_after_6400_anchors(self):
        out = np.zeros((1, 8400, 85), np.float32)
        boxes, _ = decode(out)
        # raw zeros decode to a box of one stride centred on the cell's corner
        np.testing.assert_allclose(boxes[1], (8 - 4, 0 - 4, 8 + 4, 0 + 4))       # x = 1, y = 0
        np.testing.assert_allclose(boxes[80], (0 - 4, 8 - 4, 0 + 4, 8 + 4))      # x = 0, y = 1
        np.testing.assert_allclose(boxes[6400 + 1], (16 - 8, -8, 16 + 8, 8))     # stride 16
        np.testing.assert_allclose(boxes[8000 + 21], (32 - 16, 32 - 16, 48, 48))  # stride 32

    def test_decoding_leaves_the_networks_output_untouched(self):
        out = encode([(100.0, 200.0, 64.0, 48.0, CAR, 0.9, 0.8)])
        before = out.copy()
        decode(out)
        np.testing.assert_array_equal(out, before)

    def test_only_vehicle_classes_above_the_demo_threshold_survive(self):
        out = encode([(100.0, 100.0, 40.0, 80.0, PERSON, 0.9, 0.9),   # a person
                      (300.0, 100.0, 60.0, 40.0, 3, 0.9, 0.9),        # a motorcycle
                      (500.0, 100.0, 60.0, 40.0, 5, 0.9, 0.9),        # a bus
                      (300.0, 400.0, 60.0, 40.0, CAR, 1.0, 0.0999),   # below 0.1: dropped
                      (500.0, 400.0, 60.0, 40.0, CAR, 1.0, 0.1001)])  # above: kept
        found = vehicles(*decode(out))
        self.assertEqual(sorted(d.cls for d in found), [CAR, 3, 5])

    def test_each_anchor_keeps_its_best_class(self):
        out = encode([(300.0, 300.0, 60.0, 40.0, TRUCK, 0.9, 0.6)])
        out[0, 37 * 80 + 37, 5 + CAR] = 0.3          # the same anchor also says "car", weaker
        (only,) = vehicles(*decode(out))
        self.assertEqual(only.cls, TRUCK)


class Letterbox(unittest.TestCase):
    def test_pad_channels_and_scale_follow_the_demo(self):
        image = np.zeros((720, 1280, 3), np.uint8)
        image[..., 0], image[..., 1], image[..., 2] = 10, 20, 30           # B, G, R
        blob, r = preprocess(image)
        self.assertEqual(blob.shape, (1, 3, 640, 640))
        self.assertEqual(blob.dtype, np.float32)
        self.assertEqual(r, 0.5)
        np.testing.assert_array_equal(blob[0, :, 100, 100], (10, 20, 30))  # BGR, raw 0-255
        np.testing.assert_array_equal(blob[0, :, 359, 639], (10, 20, 30))  # last image row
        np.testing.assert_array_equal(blob[0, :, 360, 0], (114, 114, 114)) # padding below

    def test_a_box_comes_back_where_it_was_drawn(self):
        for size, box in (((1280, 720), (400, 300, 520, 390)), ((854, 480), (300, 200, 360, 250)),
                          ((300, 500), (50, 100, 150, 180))):
            image = np.zeros((size[1], size[0], 3), np.uint8)
            x0, y0, x1, y1 = box
            image[y0:y1, x0:x1] = 255
            (found,) = Yolox(BrightBoxes()).detect(image, [Tile(0, 0, *size)])
            np.testing.assert_allclose(found.box, box, atol=1.5 / min(1, 640 / max(size)))


class BrightBoxes:
    """A stand-in network: every bright rectangle in its input comes out as a car, through
    the same output tensor the real network writes."""

    def setInput(self, blob):  # OpenCV's name
        self.blob = blob

    def forward(self):
        mask = (self.blob[0].min(axis=0) > 128).astype(np.uint8)
        n, _, stats, _ = cv2.connectedComponentsWithStats(mask)
        boxes = [(x + w / 2, y + h / 2, w, h, CAR, 0.9, 0.9) for x, y, w, h, _ in stats[1:n]]
        return encode(boxes)


class Nms(unittest.TestCase):
    def test_overlap_counts_both_edge_pixels_as_the_demo_does(self):
        # IoU 6.5/13.5 = 0.48 with the demo's +1, 5.5/12.5 = 0.44 without: suppressed.
        boxes = np.array([[0, 0, 9, 9], [3.5, 0, 12.5, 9]], float)
        self.assertEqual(nms(boxes, np.array([0.9, 0.8])), [0])
        # IoU 6/14 = 0.43: both kept, best first
        boxes = np.array([[0, 0, 9, 9], [4, 0, 13, 9]], float)
        self.assertEqual(nms(boxes, np.array([0.8, 0.9])), [1, 0])

    def test_one_vehicle_seen_as_car_and_truck_survives_once(self):
        out = encode([(300.0, 300.0, 60.0, 40.0, CAR, 0.9, 0.8),
                      (304.0, 302.0, 60.0, 40.0, TRUCK, 0.9, 0.7)])
        (only,) = vehicles(*decode(out))
        self.assertEqual(only.cls, CAR)

    def test_equal_scores_keep_the_first_box(self):
        boxes = np.array([[0, 0, 10, 10], [1, 1, 11, 11], [100, 100, 110, 110]], float)
        self.assertEqual(nms(boxes, np.array([0.5, 0.5, 0.5])), [0, 2])
        self.assertEqual(nms(np.zeros((0, 4)), np.zeros(0)), [])


# Four 100 px lanes across a 1280x720 frame: the region is 700 x 330, wider than 2:1.
WIDE_QUADS = tuple(np.array([(340 + 100 * i, 430), (440 + 100 * i, 430), (440 + 100 * i, 400),
                             (340 + 100 * i, 400)], float) for i in range(4))


class Tiles(unittest.TestCase):

    def test_no_zones_means_the_whole_frame(self):
        self.assertEqual(plan_tiles([], (1280, 720)), [Tile(0, 0, 1280, 720)])

    def test_a_wide_region_is_cut_into_overlapping_tiles(self):
        tiles = plan_tiles(WIDE_QUADS, (1280, 720))
        self.assertEqual(len(tiles), 2)
        a, b = tiles
        self.assertEqual((a.x0, a.y0, b.x1, b.y1), (190, 250, 890, 580))   # zones + 150 px
        self.assertGreaterEqual(a.x1 - b.x0, 100)                          # a lane overlaps
        self.assertEqual((a.cut_left, a.cut_right, b.cut_left, b.cut_right),
                         (False, True, True, False))
        for t in tiles:                                                     # magnified
            self.assertGreater(640 / max(t.x1 - t.x0, t.y1 - t.y0), 1.4)

    def test_a_wide_region_whose_cars_are_already_large_is_one_pass(self):
        # 250 px lanes: at half scale a lane is still 125 px in the model's input
        quads = [np.array([(140 + 250 * i, 640), (390 + 250 * i, 640), (390 + 250 * i, 600),
                           (140 + 250 * i, 600)], float) for i in range(4)]
        self.assertEqual(plan_tiles(quads, (1280, 720)), [Tile(0, 225, 1280, 720)])

    def test_a_square_region_is_one_tile_and_clipped_to_the_frame(self):
        quad = [np.array([(10, 700), (110, 700), (110, 600), (10, 600)], float)]
        self.assertEqual(plan_tiles(quad, (1280, 720)), [Tile(0, 450, 260, 720)])

    def test_a_car_across_a_cut_is_found_once_and_whole(self):
        tiles = plan_tiles(WIDE_QUADS, (1280, 720))
        for box in ((560, 380, 660, 450),     # across tile 0's right cut, whole in tile 1
                    (500, 380, 600, 450),     # inside the overlap: seen whole by both
                    (300, 380, 400, 450)):    # tile 0 only
            image = np.zeros((720, 1280, 3), np.uint8)
            image[box[1]:box[3], box[0]:box[2]] = 255
            found = Yolox(BrightBoxes()).detect(image, tiles)
            self.assertEqual(len(found), 1, box)
            np.testing.assert_allclose(found[0].box, box, atol=1.5)

    def test_a_vehicle_wider_than_the_overlap_is_joined_from_both_tiles(self):
        tiles = plan_tiles(WIDE_QUADS, (1280, 720))
        a, b = tiles
        self.assertLess(a.x1 - b.x0, 160)                                   # the overlap
        for box in ((460, 380, 620, 470), (445, 380, 635, 470), (440, 300, 700, 470)):
            image = np.zeros((720, 1280, 3), np.uint8)
            image[box[1]:box[3], box[0]:box[2]] = 255
            found = Yolox(BrightBoxes()).detect(image, tiles)
            self.assertEqual(len(found), 1, box)
            np.testing.assert_allclose(found[0].box, box, atol=1.5)

    def test_two_wide_vehicles_at_different_heights_are_each_joined_with_their_own_parts(self):
        tiles = plan_tiles(WIDE_QUADS, (1280, 720))
        image = np.zeros((720, 1280, 3), np.uint8)
        image[300:340, 440:620] = 255           # both wider than the overlap, so both are cut
        image[400:440, 460:700] = 255           # in each tile; their rows do not agree
        found = Yolox(BrightBoxes()).detect(image, tiles)
        self.assertEqual(len(found), 2)
        np.testing.assert_allclose(sorted(d.box for d in found),
                                   [(440, 300, 620, 340), (460, 400, 700, 440)], atol=1.5)


# --- tracking and counting --------------------------------------------------------------------
def path(bottoms, x=150.0, **kw):
    return [[car(x, b, **kw)] if b is not None else [] for b in bottoms]


class Counting(unittest.TestCase):
    def test_a_steady_car_counts_once_in_its_lane_when_its_rear_leaves_the_far_edge(self):
        found, _ = run(path(range(305, 150, -10)))
        self.assertEqual(len(found), 1)
        n, c = found[0]
        self.assertEqual(n, 9)                     # bottom 225 -> 215: across the far edge
        self.assertEqual((c.camera, c.line, c.lane, c.method), ("cam", "A", "1", "yolox"))
        self.assertEqual(c.at, frame_at(9))
        self.assertAlmostEqual(c.confidence, 0.8)

    def test_stop_and_go_counts_once_when_it_finally_leaves(self):
        bottoms = [295, 285, 275, 265, 255, 245] + [235, 236, 235, 234, 235, 236] * 3 \
            + [235, 228, 221, 214, 207, 200]
        found, _ = run(path(bottoms))
        self.assertEqual([n for n, _ in found], [27])          # 221 -> 214

    def test_a_box_jittering_on_the_far_edge_counts_once(self):
        bottoms = [295, 285, 275, 265, 255, 245, 235] + [221, 219] * 10 + [210, 200]
        found, _ = run(path(bottoms))
        self.assertEqual([n for n, _ in found], [8])

    def test_two_cars_nose_to_tail_count_twice_at_their_own_times(self):
        frames = [[car(150, 305 - 10 * n), car(150, 348 - 10 * n)] for n in range(20)]
        found, counter = run(frames)
        self.assertEqual([n for n, _ in found], [9, 13])        # 3 px apart, 10 px/frame
        self.assertEqual({c.lane for _, c in found}, {"1"})
        self.assertEqual(len({t.id for t in counter.tracker.tracks}), 2)

    def test_two_cars_side_by_side_count_in_their_own_lanes(self):
        frames = [[car(150, 305 - 10 * n), car(250, 305 - 10 * n)] for n in range(15)]
        found, _ = run(frames)
        self.assertEqual(sorted((n, c.lane) for n, c in found), [(9, "1"), (9, "2")])

    def test_a_track_lost_for_one_frame_still_counts_once(self):
        for missing in (5, 9):                     # entering the zone; on the crossing frame
            bottoms = [b if n != missing else None for n, b in enumerate(range(305, 150, -10))]
            found, counter = run(path(bottoms))
            self.assertEqual([n for n, _ in found], [9 if missing != 9 else 10], missing)
            self.assertEqual(counter.tracker._next_id, 2, "no second track")

    def test_a_car_that_skips_the_zone_in_one_step_still_counts(self):
        found, _ = run(path([300, 280, 255, 215, 175]))         # 40 px step over a 30 px zone
        self.assertEqual([n for n, _ in found], [3])

    def test_a_car_first_seen_inside_the_zone_is_not_counted(self):
        found, _ = run(path([240, 230, 220, 210, 200]))
        self.assertEqual(found, [])

    def test_a_car_going_the_wrong_way_is_not_counted(self):
        found, _ = run(path(range(150, 300, 10)))
        self.assertEqual(found, [])

    def test_a_lane_change_counts_once_in_the_lane_it_leaves_from(self):
        xs = [150, 150, 150, 150, 150, 170, 190, 210, 230, 240, 240, 240]
        frames = [[car(x, 295 - 10 * n)] for n, x in enumerate(xs)]
        found, _ = run(frames)
        self.assertEqual([(n, c.lane) for n, c in found], [(8, "2")])

    def test_a_reading_gap_ends_every_track(self):
        counter = DetectorCounter(cam(), detector=Scripted(path([300, 290, 280, 260, 240,
                                                                 230, 220, 210])))
        image = np.zeros((FRAME[1], FRAME[0], 3), np.uint8)
        crossings = []
        for n in range(8):
            if n == 4:
                counter.gap(Gap(frame_at(3), 30.0, "test"))
                self.assertEqual(counter.tracker.tracks, [])
            crossings += counter.add(Stamped(image, frame_at(n), n))
        self.assertEqual(crossings, [], "after the gap it is a new car first seen in the zone")

    def test_a_track_unseen_longer_than_the_maximum_age_is_dropped(self):
        unseen = int(detect.MAX_AGE_S / DT) + 1
        bottoms = [300, 290, 280, 270, 260, 250] + [None] * unseen + [235, 225, 215, 205]
        found, counter = run(path(bottoms))
        self.assertEqual(found, [])
        self.assertEqual(counter.tracker._next_id, 3)

    def test_weak_boxes_keep_a_track_alive_but_never_start_one(self):
        weak = detect.HIGH_SCORE / 2
        frames = path([305, 295, 285]) + path([275, 265, 255, 245, 235, 225, 215], score=weak)
        found, counter = run(frames)
        self.assertEqual([n for n, _ in found], [9])
        self.assertEqual(counter.tracker._next_id, 2)
        found, _ = run(path(range(300, 150, -10), score=weak))
        self.assertEqual(found, [])

    def test_the_same_boxes_give_the_same_crossings(self):
        rng = np.random.default_rng(3)
        frames = []
        for n in range(40):
            boxes = [car(150 + rng.normal(0, 2), 400 - 8 * n + rng.normal(0, 2)),
                     car(250 + rng.normal(0, 2), 380 - 7 * n + rng.normal(0, 2), cls=TRUCK),
                     car(150 + rng.normal(0, 2), 450 - 8 * n + rng.normal(0, 2))]
            frames.append(boxes)
        first, _ = run(frames)
        second, _ = run(frames)
        self.assertEqual(first, second)
        self.assertEqual(sorted(c.lane for _, c in first), ["1", "1", "2"])

    def test_a_perspective_zone_counts_at_its_far_edge(self):
        quad = [(100, 300), (260, 300), (230, 200), (130, 200)]
        found, _ = run(path(range(345, 150, -10), x=180), camera=cam([("B", "x", quad)]))
        self.assertEqual([(n, c.line, c.lane) for n, c in found], [(15, "B", "x")])

    def test_zones_are_scaled_to_the_frames_read(self):
        big = cam([("A", "1", [(x * 2, y * 2) for x, y in LANE_1])], frame=(1280, 720))
        found, _ = run(path(range(305, 150, -10)), camera=big)
        self.assertEqual([n for n, _ in found], [9])

    def test_a_zone_whose_far_edge_is_above_the_frame_is_refused(self):
        # No box can reach that far edge: the line would read as seen with no cars, forever.
        quad = [(100, 60), (200, 60), (200, -40), (100, -40)]
        with self.assertRaisesRegex(ValueError, "A/1"):
            DetectorCounter(cam([("A", "1", quad)]), detector=Scripted([]))

    def test_a_zone_wholly_outside_the_frame_is_refused(self):
        quad = [(700, 250), (800, 250), (800, 220), (700, 220)]
        with self.assertRaisesRegex(ValueError, "A/1"):
            DetectorCounter(cam([("A", "1", quad)]), detector=Scripted([]))

    def test_a_camera_without_zones_never_runs_the_model(self):
        detector = Scripted([[car(150, 300)]] * 20)
        counter = DetectorCounter(cam(zones=[]), detector=detector)
        image = np.zeros((FRAME[1], FRAME[0], 3), np.uint8)
        for n in range(20):
            self.assertEqual(counter.add(Stamped(image, frame_at(n), n)), [])
        self.assertEqual(detector.calls, 0)

    def test_the_heading_follows_the_lane_sides_toward_the_far_edge(self):
        quad = np.array([(100, 300), (260, 300), (230, 200), (130, 200)], float)
        zone = detect.ZoneGeometry(cam([("B", "x", quad.tolist())]).count_zones[0], quad)
        side = (30 / math.hypot(30, 100), -100 / math.hypot(30, 100))
        np.testing.assert_allclose(zone.heading((100, 300)), side, atol=1e-6)   # the left side
        np.testing.assert_allclose(zone.heading((180, 250)), (0, -1), atol=1e-6)  # the middle
        self.assertLess(zone.heading((115, 400))[1], -0.95)                    # before the zone too


# --- a simulated road --------------------------------------------------------------------------
CAR_LENGTH_M, CAR_WIDTH_M, CAR_HEIGHT_M = 4.5, 1.8, 1.5


class Pinhole:
    """A pole camera looking along one lane at cars driving away from it, seen from behind."""

    def __init__(self, size: tuple[int, int], focal: float, height_m: float, pitch_deg: float):
        self.size, self.focal, self.height_m = size, focal, height_m
        self.pitch = math.radians(pitch_deg)

    def project(self, x: float, y: float, z: float) -> tuple[float, float]:
        """Image point of (x across, y up, z along the road from the pole's foot), metres."""
        yc = y - self.height_m
        depth = z * math.cos(self.pitch) - yc * math.sin(self.pitch)
        up = z * math.sin(self.pitch) + yc * math.cos(self.pitch)
        return (self.size[0] / 2 + self.focal * x / depth, self.size[1] / 2 - self.focal * up / depth)

    def car_box(self, rear_m: float) -> tuple[float, float, float, float]:
        points = [self.project(sx * CAR_WIDTH_M / 2, y, rear_m + z) for sx in (-1, 1)
                  for y in (0, CAR_HEIGHT_M) for z in (0, CAR_LENGTH_M)]
        xs, ys = [p[0] for p in points], [p[1] for p in points]
        return (min(xs), min(ys), max(xs), max(ys))

    def zone(self, near_m: float, far_m: float, lane_m: float = 3.5) -> list:
        return [self.project(-lane_m / 2, 0, near_m), self.project(lane_m / 2, 0, near_m),
                self.project(lane_m / 2, 0, far_m), self.project(-lane_m / 2, 0, far_m)]


HIGH = Pinhole((1280, 720), 900.0, 6.0, 22)   # like bridgepdn1: a car 12 m out is 126 x 178 px
LOW = Pinhole((854, 480), 600.0, 4.0, 7)      # like pdn-north: a car 25 m out is 43 x 44 px


class Road:
    """A detector that returns every car's exact box, clipped to the tiles it is shown, with
    seeded misses, jitter (a share of the box's width, or `jitter_px` pixels whatever its size)
    and scores. It keeps which car each box it returned came from, so a count can be traced to
    a car."""

    def __init__(self, camera: Pinhole, seed: int, missed: float = 0.0, jitter: float = 0.0,
                 scores: tuple[float, float] = (0.8, 0.8), jitter_px: float = 0.0):
        self.camera, self.missed, self.jitter, self.scores = camera, missed, jitter, scores
        self.jitter_px = jitter_px
        self.rng = np.random.default_rng(seed)
        self.cars: list[tuple[float, int]] = []     # (rear bumper, metres from the pole; car id)
        self.owner: dict = {}

    def detect(self, image, tiles):
        x0, y0 = min(t.x0 for t in tiles), min(t.y0 for t in tiles)
        x1, y1 = max(t.x1 for t in tiles), max(t.y1 for t in tiles)
        found = []
        for rear, car_id in self.cars:
            if rear < 1.0:
                continue                            # under the pole, out of the picture
            b = self.camera.car_box(rear)
            c = (max(b[0], x0), max(b[1], y0), min(b[2], x1 - 1), min(b[3], y1 - 1))
            if c[2] - c[0] < 4 or c[3] - c[1] < 4:
                continue
            if (c[2] - c[0]) * (c[3] - c[1]) < 0.4 * (b[2] - b[0]) * (b[3] - b[1]):
                continue                            # mostly outside what the model sees
            if self.rng.random() < self.missed:
                continue
            j = (self.rng.normal(0, self.jitter_px, 4) if self.jitter_px
                 else self.rng.normal(0, self.jitter, 4) * (c[2] - c[0]))
            found.append((Detection(tuple(float(c[i] + j[i]) for i in range(4)),
                                    float(self.rng.uniform(*self.scores)), CAR), car_id))
        found.sort(key=lambda f: -f[0].score)
        self.owner = {d.box: car_id for d, car_id in found}
        return [d for d, _ in found]


def queue(travelled: float, headway: float, first: float, last: float) -> list[tuple[float, int]]:
    """(rear bumper, car id) of a queue with fixed spacing that has moved `travelled` metres."""
    cars, wraps = [], int(travelled // headway)
    rear = first - headway + travelled % headway
    i = 0
    while rear + i * headway <= last + headway:
        cars.append((rear + i * headway, wraps - i))
        i += 1
    return cars


def drive(camera: Pinhole, near: float, far: float, speeds: list[tuple[float, float]], *,
          seed: int = 11, minutes: float = 3.0, **noise) -> tuple[set, set, Counter]:
    """Drive a queue through one zone at 2 fps. `speeds` repeats (seconds, m/s); the spacing is
    a car length plus 2 m, or plus 1.5 s at the top speed. Returns the cars that must be
    counted (rear bumper across the far edge, first seen a metre or more before the near
    edge), the cars that may be (any whose rear bumper reached the far edge, give or take
    half a metre of box jitter) and how often each car was counted."""
    top = max(v for _, v in speeds)
    headway = CAR_LENGTH_M + max(2.0, 1.5 * top)
    period = sum(s for s, _ in speeds)
    zone_cam = camera_from_dict("sim", {
        "title": "sim", "port_number": "000000", "direction": "north",
        "frame_size": list(camera.size),
        "count_zones": [{"line": "A", "lane": "1", "quad": [list(p) for p in camera.zone(near, far)]}]})
    road = Road(camera, seed, **noise)
    counter = DetectorCounter(zone_cam, detector=road)
    image = np.zeros((camera.size[1], camera.size[0], 3), np.uint8)
    first_rear, last_rear, counted = {}, {}, Counter()
    for n in range(int(minutes * 120)):
        t = n * DT
        cycles, rest = divmod(t, period)
        travelled = cycles * sum(s * v for s, v in speeds)
        for s, v in speeds:
            travelled += min(s, rest) * v
            rest = max(0.0, rest - s)
        road.cars = queue(travelled, headway, 1.0, 130.0)
        for rear, car_id in road.cars:
            first_rear.setdefault(car_id, rear)
            last_rear[car_id] = rear
        before = {t.id: set(t.counted) for t in counter.tracker.tracks}
        if counter.add(Stamped(image, frame_at(n), n)):
            for track in counter.tracker.tracks:
                if track.counted - before.get(track.id, set()):
                    counted[road.owner[track.box]] += 1
    must = {c for c in first_rear if first_rear[c] < near - 1 and last_rear[c] >= far}
    may = {c for c in first_rear if first_rear[c] < far and last_rear[c] >= far - 0.5}
    return must, may, counted


class SimulatedRoad(unittest.TestCase):
    """Seeded runs on the simulated road. The bounds are what these seeds gave, with room."""

    def assert_counts(self, run: tuple[set, set, Counter], missed_at_most: int):
        must, may, counted = run
        self.assertEqual([c for c, k in counted.items() if k > 1], [], "a car counted twice")
        self.assertEqual(set(counted) - may, set(), "a car counted that did not cross")
        self.assertLessEqual(len(must - set(counted)), missed_at_most)

    def test_exact_boxes_count_every_car_at_12_m_s_near_the_camera(self):
        # The first tracker counted 80 of 160 here: its predicted box kept its size, and a car
        # going away shrinks 2.1-3.2x in area per frame this close to the pole.
        run = drive(HIGH, 12.0, 15.0, [(10, 12.0)])
        self.assertGreater(len(run[0]), 90)
        self.assert_counts(run, 0)

    def test_free_flow_with_missed_boxes_is_counted_where_the_zone_obeys_the_rule(self):
        noise = {"missed": 0.1, "jitter": 0.015, "scores": (0.2, 0.9)}
        for camera, near, far in ((HIGH, 30.0, 34.0), (LOW, 25.0, 29.0)):
            for speed, share in ((8.0, 0.02), (12.0, 0.1)):
                with self.subTest(camera=camera.size, speed=speed):
                    run = drive(camera, near, far, [(10, speed)], **noise)
                    self.assert_counts(run, int(share * len(run[0])))

    def test_a_slow_queue_with_missed_boxes_counts_no_car_twice(self):
        # The first tracker counted cars twice here: a track that took the box of the car
        # ahead learnt one car a frame and walked on.
        noise = {"missed": 0.1, "jitter": 0.015, "scores": (0.2, 0.9)}
        for camera, near, far in ((LOW, 25.0, 29.0), (HIGH, 30.0, 34.0)):
            for speeds in ([(10, 0.5)], [(6, 3.0), (10, 0.0)]):
                for seed in (11, 12, 13):
                    with self.subTest(camera=camera.size, speeds=speeds, seed=seed):
                        run = drive(camera, near, far, speeds, seed=seed, minutes=5, **noise)
                        self.assert_counts(run, 1)

    def test_with_one_box_in_five_missed_a_slow_queue_still_counts_few_cars_twice(self):
        # Not none: 1 car twice in 295 crossings over these 10 runs (2 in 595 with the same
        # runs at bridgepdn1). The first tracker counted 11 twice in the creeping 5. This bounds it.
        noise = {"missed": 0.2, "jitter": 0.02, "scores": (0.15, 0.9)}
        for speeds in ([(10, 0.5)], [(6, 3.0), (10, 0.0)]):
            for seed in (11, 12, 13, 14, 15):
                with self.subTest(speeds=speeds, seed=seed):
                    must, may, counted = drive(LOW, 25.0, 29.0, speeds, seed=seed, minutes=5,
                                               **noise)
                    self.assertLessEqual(sum(k - 1 for k in counted.values()), 1)
                    self.assertEqual(set(counted) - may, set())
                    self.assertLessEqual(len(must - set(counted)), 5)


class Tracking(unittest.TestCase):
    def test_the_motion_model_is_exact_for_a_car_driving_away_near_the_camera(self):
        boxes = [HIGH.car_box(8.0 + 6.0 * n) for n in range(4)]             # 12 m/s, 2 fps
        track = detect.Track(1, boxes[0], T0, T0)
        for n in (1, 2):
            track.update(Detection(boxes[n], 0.9, CAR), frame_at(n))
        p, truth = track.predicted(frame_at(3)), boxes[3]
        size = detect.box_size(truth)
        miss = math.hypot((p[0] + p[2] - truth[0] - truth[2]) / 2,
                          (p[1] + p[3] - truth[1] - truth[3]) / 2) / size
        self.assertLess(miss, 0.15)
        self.assertAlmostEqual((p[2] - p[0]) / (truth[2] - truth[0]), 1.0, delta=0.02)
        self.assertLess(detect.box_size(p), 0.8 * detect.box_size(boxes[2]))   # it shrank

    def test_a_creeping_track_does_not_take_the_box_of_the_car_ahead(self):
        # pdn-north-like, 6.5 m apart: the car ahead overlaps the car's box (IoU 0.39) a third
        # of a box further on, where the first tracker took it. Taking it now would mean
        # 13 m/s with the next car 6.5 m ahead of it: no room.
        tracker = detect.Tracker(heading=lambda point: (0.0, -1.0))
        creep = [LOW.car_box(25.0 + 0.25 * n) for n in range(7)]
        ahead = [LOW.car_box(31.5 + 0.25 * n) for n in range(7)]
        beyond = [LOW.car_box(38.0 + 0.25 * n) for n in range(7)]
        for n in range(6):                      # the car ahead is never seen until frame 6
            tracker.update([Detection(creep[n], 0.9, CAR), Detection(beyond[n], 0.9, CAR)],
                           frame_at(n))
        track = next(t for t in tracker.tracks if t.box == creep[5])
        seen = tracker.update([Detection(ahead[6], 0.9, CAR), Detection(beyond[6], 0.9, CAR)],
                              frame_at(6))
        new = [t for t, before in seen if before is None]
        self.assertEqual([t.box for t in new], [ahead[6]], "the car ahead starts its own track")
        self.assertEqual(track.box, creep[5], "the creeping track waits for its own car")

    def test_a_box_cut_by_the_edge_of_what_was_seen_teaches_no_motion(self):
        tracker = detect.Tracker(bounds=(0, 0, 640, 360))
        for n, top in enumerate((330, 300, 270)):                            # coming into view
            tracker.update([Detection((300, top, 360, 359), 0.9, CAR)], frame_at(n))
        (track,) = tracker.tracks
        self.assertTrue(track.truncated)
        self.assertEqual(track.steps, 0)
        tracker.update([Detection((302, 250, 358, 300), 0.9, CAR)], frame_at(3))
        tracker.update([Detection((304, 230, 356, 276), 0.9, CAR)], frame_at(4))
        self.assertEqual(track.steps, 1, "only the step between two whole boxes")


class Signature(unittest.TestCase):
    """The shared form (crossing.look): (s cos h, s sin h, value, width share)."""

    def test_colour_comes_from_the_upper_body_and_ignores_the_bottom_half(self):
        image = np.zeros((FRAME[1], FRAME[0], 3), np.uint8)
        box = (120, 180, 180, 220)                              # 60 x 40
        image[180:200, 120:180] = (40, 40, 200)                 # body: red in BGR
        image[200:220, 120:180] = (255, 255, 255)               # lower half: a white panel
        sig = signature(image, box, 100.0)
        self.assertGreater(sig[0], 0.7)                         # red: saturated, at 0 degrees
        self.assertAlmostEqual(sig[1], 0.0, places=2)
        self.assertAlmostEqual(sig[2], 200 / 255, places=2)
        self.assertAlmostEqual(sig[3], 0.6)
        image[200:220, 120:180] = (0, 0, 0)
        self.assertEqual(signature(image, box, 100.0), sig)

    def test_a_red_that_straddles_the_hue_wrap_stays_red(self):
        image = np.zeros((40, 60, 3), np.uint8)
        image[:, :30] = (0, 10, 200)                            # hue ~2 degrees
        image[:, 30:] = (10, 0, 200)                            # hue ~356 degrees
        x, y = signature(image, (0, 0, 60, 40), 60)[:2]
        self.assertGreater(x, 0.8)
        self.assertLess(abs(y), 0.05)

    def test_a_counted_crossing_carries_the_signature_and_the_class(self):
        image = np.zeros((FRAME[1], FRAME[0], 3), np.uint8)
        image[:, :, 0] = 200                                    # everything blue
        found, _ = run(path(range(305, 150, -10)), image=image)
        crossing = found[0][1]
        x, y, _, width = crossing.signature
        self.assertAlmostEqual(x, -0.5, places=2)               # blue: 240 degrees
        self.assertAlmostEqual(y, -0.866, places=2)
        self.assertAlmostEqual(width, 0.6)                      # 60 px car, 100 px zone
        self.assertEqual(crossing.vehicle, "car")


class ModelFile(unittest.TestCase):
    def test_a_file_with_another_hash_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake = Path(tmp) / detect.MODEL_FILE
            fake.write_bytes(b"not yolox" * 1000)
            with self.assertRaises(ModelRefused) as err:
                detect.read_model(fake)
            self.assertIn(detect.MODEL_SHA256, str(err.exception))
            with unittest.mock.patch.dict(os.environ, {detect.MODEL_DIR_ENV: tmp}):
                self.assertEqual(detect.default_model_path(), fake)
                with self.assertRaises(ModelRefused):
                    Yolox.load()

    def test_a_missing_file_is_refused_and_nothing_is_fetched(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ModelRefused) as err:
                detect.read_model(Path(tmp) / "absent.onnx")
            self.assertIn("Nothing is downloaded", str(err.exception))


@unittest.skipUnless(detect.default_model_path().is_file(), "the pinned model is not installed")
class Model(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.yolo = Yolox.load()

    def test_the_pinned_model_loads_and_writes_8400_anchors(self):
        blob, _ = preprocess(np.full((360, 640, 3), 90, np.uint8))
        self.yolo.net.setInput(blob)
        out = self.yolo.net.forward()
        self.assertEqual(out.shape, (1, 8400, 85))
        self.assertTrue(((out[0, :, 4:] >= 0) & (out[0, :, 4:] <= 1)).all())  # probabilities

    def test_the_same_frame_gives_the_same_boxes(self):
        rng = np.random.default_rng(5)
        image = rng.integers(0, 255, (360, 640, 3), dtype=np.uint8)
        tiles = [Tile(0, 0, 640, 360)]
        self.assertEqual(self.yolo.detect(image, tiles), self.yolo.detect(image, tiles))


if __name__ == "__main__":
    unittest.main()
