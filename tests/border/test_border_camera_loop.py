"""The loop counter (loop.py) against rendered lanes whose cars are known exactly.

A count is scored against truth one to one: a car's rear crossing the zone's far edge, matched
to a crossing within MATCH_S. The zone is 3 m of lane across the rendered lane, 9.2-12.2 m from
the near edge of the view, where the render has ~7 image rows per metre along the lane: about
line A on pdn-north read at 854x480 (1.5 m in 11 rows). Its edges are off the 0.5 m grid that
queued rears stop on in these scenes, so no car stands exactly on the far edge (where either
answer is right and the truth would hinge on a rounding error).

Lanes renders pdn-north's own widths (WideScene): lanes of 4.25 and 4.75 m, cars of 1.6-2.2 m
off the lane's centre or across the line between two zones, at 1280x720 as well as 640x360.

The longer evaluation (20 minutes of stop-and-go per gap, flows up to 10 m/s, 108 lane, car and
frame-size cases, and where it fails) is in loop.py's docstring; these are the same cases cut
to run in seconds.
"""
from __future__ import annotations

import math
import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import numpy as np
import pytest

cv2 = pytest.importorskip("cv2")

from camera_scene import (
    CANVAS_PPM,
    CAR_M,
    CAR_W,
    FRAME,
    GROUND,
    LANE_W,
    PALETTE,
    SUPERSAMPLE,
    VIEW_M,
    Scene,
    camera,
    queue_fronts,
)

from scraper.border.camera import transit
from scraper.border.camera.calibration import camera_from_dict
from scraper.border.camera.crossing import Zone
from scraper.border.camera.hls import Gap, Stamped
from scraper.border.camera.loop import ASSUMED_LANE_M, WIDTH_SCALE_M, LoopCounter, _Look

DT = 0.5
T0 = datetime(2026, 10, 24, 18, 0, tzinfo=UTC)
NEAR, FAR = 9.2, 12.2
WARM_S = 10              # empty road before traffic: enough for the test-sized bank below
MATCH_S = 3.0
RED, WHITE = 0, 1        # camera_scene.PALETTE indexes: the car id picks the paint


class _Still:
    def normal(self, loc, scale, size):
        return 0.0


class WideScene(Scene):
    """camera_scene.Scene with the road's width, the cars' width and each car's place across
    the road chosen, for pdn-north's wide lanes (lane-4 4.25 m, lane-5 4.75 m), cars of
    1.6-2.2 m, cars off the lane's centre and cars between two lanes.

    The ground rectangle (GROUND) spans the whole road, `road_w` metres; `lines` are the painted
    lines across it. `centre(car_id)` is a car's centre in metres from the road's left edge."""

    def __init__(self, seed: int = 7, road_w: float = LANE_W, car_w: float = CAR_W,
                 centre=None, lines: tuple[float, ...] | None = None):
        rng = np.random.default_rng(seed)
        self.view_m, self.road_w, self.car_w = VIEW_M, road_w, car_w
        self.centre = centre or (lambda car_id: road_w / 2)
        h, w = int(VIEW_M * CANVAS_PPM), int((road_w + 4) * CANVAS_PPM)
        road = cv2.GaussianBlur((92 + rng.normal(0, 7, (h, w))).astype(np.float32), (3, 3), 0)
        self.road = np.dstack([road] * 3).clip(0, 255).astype(np.uint8)
        for u in (0.0, road_w) if lines is None else lines:
            c = int((u + 2) * CANVAS_PPM)
            self.road[:, c - 2:c + 2] = 210
        canvas = np.float32([(2 * CANVAS_PPM, h), ((road_w + 2) * CANVAS_PPM, h),
                             ((road_w + 2) * CANVAS_PPM, 0), (2 * CANVAS_PPM, 0)])
        self.to_image = cv2.getPerspectiveTransform(canvas, np.float32(GROUND) * SUPERSAMPLE)
        self.noise = np.random.default_rng(seed + 1)

    def across(self, car_id: int, front: float) -> float:
        """A car's centre in metres from the road's left edge, with its front at `front`."""
        return self.centre(car_id)

    def render(self, cars: list[tuple[float, int]]) -> np.ndarray:
        canvas = self.road.copy()
        for front, car_id in cars:
            top = max(0, self._row(front))
            bottom = min(canvas.shape[0], self._row(front - CAR_M))
            if bottom <= top:
                continue
            mid = 2 + self.across(car_id, front)
            c0 = int((mid - self.car_w / 2) * CANVAS_PPM)
            c1 = int((mid + self.car_w / 2) * CANVAS_PPM)
            colour = PALETTE[car_id % len(PALETTE)]
            canvas[top:bottom, c0:c1] = colour
            shade = tuple(int(x * 0.45) for x in colour)
            w0, w1 = self._row(front - 1.1), self._row(front - 1.7)       # windscreen band
            canvas[max(top, w0):max(top, min(bottom, w1)), c0 + 4:c1 - 4] = shade   # as Scene
            cv2.rectangle(canvas, (c0, top), (c1 - 1, bottom - 1), (15, 15, 15), 2)
        big = (FRAME[0] * SUPERSAMPLE, FRAME[1] * SUPERSAMPLE)
        frame = cv2.warpPerspective(canvas, self.to_image, big, flags=cv2.INTER_LINEAR,
                                    borderMode=cv2.BORDER_CONSTANT, borderValue=(55, 60, 58))
        frame = cv2.resize(frame, FRAME, interpolation=cv2.INTER_AREA)
        return (frame + self.noise.normal(0, 2, frame.shape)).clip(0, 255).astype(np.uint8)


class Changing(WideScene):
    """Two 3.5 m lanes; a car moves from the left one to the right one at 1 m/s sideways while
    driving on at `speed`, its centre on the line between them when its rear is at `on_line`."""

    def __init__(self, speed: float, on_line: float):
        super().__init__(road_w=7.0, car_w=1.8, lines=(0.0, 3.5, 7.0))
        self.speed, self.on_line = speed, on_line

    def across(self, car_id: int, front: float) -> float:
        return min(max(3.5 + (front - CAR_M - self.on_line) / self.speed, 1.75), 5.25)


class Frames:
    """Scene frames with the scene's own sensor noise, rendered once per arrangement of cars.

    A queue standing for 25 s is 50 identical arrangements; the warp is the slow part, so it
    is done once and fresh noise (N(0, 2), as Scene.render adds) comes from a seeded bank."""

    def __init__(self, seed: int = 7, scene: Scene | None = None):
        self.scene = scene or Scene(seed)
        rng = np.random.default_rng(seed + 100)
        self.noise = [rng.normal(0, 2, (FRAME[1], FRAME[0], 3)).astype(np.float32)
                      for _ in range(7)]
        self.cache: dict = {}
        self.n = 0

    def render(self, cars, blobs=()) -> np.ndarray:
        key = (tuple((round(f, 4), i) for f, i in cars), tuple(blobs))
        base = self.cache.get(key)
        if base is None:
            real, road = self.scene.noise, self.scene.road
            self.scene.noise = _Still()
            if blobs:                         # drawn on the road, so the warp puts them in view
                self.scene.road = road.copy()
                for u, v, size, colour in blobs:
                    c, r = (u + 2) * CANVAS_PPM, (self.scene.view_m - v) * CANVAS_PPM
                    h = size * CANVAS_PPM / 2
                    self.scene.road[max(0, int(r - h)):int(r + h),
                                    max(0, int(c - h)):int(c + h)] = colour
            try:
                base = self.scene.render(cars)
            finally:
                self.scene.noise, self.scene.road = real, road
            if len(self.cache) > 64:
                self.cache.clear()
            self.cache[key] = base
        self.n += 1
        return (base + self.noise[self.n % len(self.noise)]).clip(0, 255).astype(np.uint8)


def zone_quad(v0: float, v1: float, u0: float = 0.0, u1: float | None = None,
              road_w: float = LANE_W) -> tuple:
    """The image quad of the stretch v0-v1 m along the road and u0-u1 m across it."""
    u1 = road_w if u1 is None else u1
    plane = np.float32([(0, 0), (road_w, 0), (road_w, VIEW_M), (0, VIEW_M)])
    to_image = cv2.getPerspectiveTransform(plane, np.float32(GROUND))
    pts = np.float32([[(u0, v0), (u1, v0), (u1, v1), (u0, v1)]])
    return tuple(tuple(float(c) for c in p) for p in cv2.perspectiveTransform(pts, to_image)[0])


def counter(v0: float = NEAR, v1: float = FAR, cam=None, road_w: float = LANE_W,
            lanes: tuple[tuple[float, float], ...] | None = None) -> LoopCounter:
    """A counter on one zone across the whole road, or one zone per (u0, u1) in `lanes`."""
    cam = cam or camera(width_m=road_w)
    zones = tuple(Zone(cam.id, "A", f"lane-{k + 1}", zone_quad(v0, v1, u0, u1, road_w))
                  for k, (u0, u1) in enumerate(lanes or ((0.0, road_w),)))
    if lanes is None:
        zones = (Zone(cam.id, "A", "car", zones[0].quad),)
    # A test-sized bank: one sample a frame, a reference after 4 s of road.
    return LoopCounter(cam, zones, sample_every_s=0.5, bank_min=8)


class Run:
    """Feeds frames at 2 fps with capture times, the way hls.HlsReader stamps them."""

    def __init__(self, loop: LoopCounter | None = None, frames: Frames | None = None,
                 size: tuple[int, int] | None = None):
        self.loop = loop or counter()
        self.frames = frames or Frames()
        self.size = size
        self.at = T0
        self.crossings = []

    def add(self, cars=(), blobs=(), image=None) -> list:
        image = self.frames.render(list(cars), blobs) if image is None else image
        if self.size:
            image = cv2.resize(image, self.size, interpolation=cv2.INTER_AREA)
        out = self.loop.add(Stamped(image, self.at, 0))
        self.crossings += out
        self.at += timedelta(seconds=DT)
        return out

    def warm(self, seconds: float = WARM_S) -> Run:
        for _ in range(int(seconds / DT)):
            self.add()
        return self


def score(positions, v1: float = FAR, run: Run | None = None, settle: int = 20):
    """(cars that crossed, missed, false) after an empty-road warm-up. A car crosses when its
    rear passes the far edge. Events in the first and last `settle` frames are matched but not
    scored: the queue appears all at once when the scene starts."""
    run = run or Run().warm()
    start = run.at
    truth, prev, before = [], {}, len(run.crossings)
    for cars in positions:
        run.add(cars)
        cur = {i: f for f, i in cars}
        truth += [run.at - timedelta(seconds=DT) for i, f in cur.items()
                  if i in prev and prev[i] - CAR_M <= v1 < f - CAR_M]
        prev = cur
    counted = [c.at for c in run.crossings[before:]]
    lo = start + timedelta(seconds=settle * DT)
    hi = run.at - timedelta(seconds=settle * DT)
    used, false = set(), 0
    for c in counted:
        near = [(abs((c - t).total_seconds()), j) for j, t in enumerate(truth) if j not in used]
        near = [x for x in near if x[0] <= MATCH_S]
        if near:
            used.add(min(near)[1])
        elif lo <= c <= hi:
            false += 1
    missed = sum(1 for j, t in enumerate(truth) if j not in used and lo <= t <= hi)
    return sum(1 for t in truth if lo <= t <= hi), missed, false


def steady(seconds: float, speed: float, headway: float):
    return [queue_fronts(60, speed * k * DT, headway=headway) for k in range(int(seconds / DT))]


def stop_and_go(seconds: float, headway: float, advance: float = CAR_M, move_s: float = 5.0,
                stand_s: float = 25.0):
    """Advance `advance` metres in `move_s`, stand `stand_s`, repeat."""
    out, travelled = [], 0.0
    for k in range(int(seconds / DT)):
        out.append(queue_fronts(60, travelled, headway=headway))
        if (k * DT) % (move_s + stand_s) < move_s:
            travelled += advance / move_s * DT
    return out


def staggered(seconds: float, gap: float, delay: float, stand_s: float = 20.0,
              accel: float = 1.5, top: float = 2.0):
    """Stop-and-go as queues really start: each car repeats the path of the car ahead `delay`
    seconds later (Newell's car-following), so it still stands while that one pulls away. The
    first car moves one headway a cycle, at `accel` up to `top` and down again, then stands
    `stand_s`. With 1.5 m gaps the rears stand at 11.5 m, 0.7 m short of the far edge."""
    headway = CAR_M + gap
    ramp_s = min(top / accel, math.sqrt(headway / accel))       # to full speed, or half way
    ramp_m = accel * ramp_s ** 2 / 2
    move_s = 2 * ramp_s + (headway - 2 * ramp_m) / (accel * ramp_s)

    def gone(t: float) -> float:                                 # metres the first car has gone
        cycles, r = divmod(t, stand_s + move_s)
        r = min(max(r - stand_s, 0.0), move_s)
        if r <= ramp_s:
            m = accel * r * r / 2
        elif r >= move_s - ramp_s:
            m = headway - accel * (move_s - r) ** 2 / 2
        else:
            m = ramp_m + accel * ramp_s * (r - ramp_s)
        return cycles * headway + m

    out = []
    for k in range(int(seconds / DT)):
        cars, i = [], 0
        while (front := VIEW_M + 10 + gone(k * DT - i * delay) - i * headway) >= -5:
            if front <= VIEW_M:
                cars.append((front, i))
            i += 1
        out.append(cars)
    return out


def one_car(car_id: int, speed: float = 1.5, start: float = 2.0, end: float = 22.0):
    return [[(start + speed * k * DT, car_id)]
            for k in range(int((end - start) / (speed * DT)) + 1)]


class Counting(unittest.TestCase):
    """In the 20-minute runs (loop.py) stop-and-go with 2.0, 1.5 and 1.0 m gaps, a crawl with
    1 m gaps and steady flows of 2-6 m/s counted every car with no false count."""

    def assert_counts(self, positions, at_least: int):
        cars, missed, false = score(positions)
        self.assertGreaterEqual(cars, at_least)
        self.assertEqual((missed, false), (0, 0), f"{cars} cars: {missed} missed, {false} false")

    def test_steady_flow(self):
        self.assert_counts(steady(90, 3.0, headway=9.0), at_least=20)

    def test_stop_and_go_counts_each_car_once(self):
        # A car length in 5 s, then 25 s standing: stops land at a new place every cycle, so
        # rears stand on the zone, on its edges and in its margins in turn.
        self.assert_counts(stop_and_go(240, headway=6.5), at_least=4)

    def test_stop_and_go_with_one_metre_gaps(self):
        self.assert_counts(stop_and_go(300, headway=5.5), at_least=7)

    def test_stop_and_go_where_each_car_starts_after_the_one_ahead(self):
        # A car pulling away 1.5 s before the one behind it moves 0.6-0.9 m a frame while the
        # patch's one shift, which the standing car behind fills, reads far less. Its rear was
        # not matched, its own was born uncounted: 3 of these cars were missed.
        self.assert_counts(staggered(240, gap=1.5, delay=1.5), at_least=7)

    def test_a_dense_crawl_with_one_metre_gaps(self):
        # Rendered, a 1 m gap leaves 2-3 rows (0.3 m) of clear road at this range: the gap is
        # seen while it crosses the zone and followed when it is not.
        self.assert_counts(steady(60, 1.0, headway=5.5), at_least=6)

    def test_crossing_time_is_when_the_rear_passes_the_far_edge(self):
        run = Run().warm()
        positions = one_car(RED, speed=2.0)
        for cars in positions:
            run.add(cars)
        self.assertEqual(len(run.crossings), 1)
        rear_at_far = (FAR + CAR_M - 2.0) / 2.0          # seconds after the car's first frame
        crossed = (run.crossings[0].at - (T0 + timedelta(seconds=WARM_S))).total_seconds()
        self.assertAlmostEqual(crossed, rear_at_far, delta=1.0)


class Lanes(unittest.TestCase):
    """pdn-north's line A: lane-1 to lane-3 3.5 m (equal thirds of the carriageway, not painted
    lanes), lane-4 4.25 m and lane-5 4.75 m, read at 854x480 or 1280x720; cars 1.6-2.2 m wide,
    anywhere across their lane, some across a lane line."""

    def assert_counts(self, road_w: float, car_w: float, v0: float, v1: float, *, size=None,
                      centre=None, lanes=None, seconds: float = 60):
        scene = WideScene(road_w=road_w, car_w=car_w, centre=centre,
                          lines=None if lanes is None else (0.0, *(b for _, b in lanes)))
        run = Run(counter(v0, v1, road_w=road_w, lanes=lanes), Frames(scene=scene), size).warm()
        cars, missed, false = score(steady(seconds, 3.0, headway=9.0), v1, run=run)
        self.assertGreaterEqual(cars, seconds / 6)       # one car every 3 s, settling cut
        self.assertEqual((missed, false), (0, 0),
                         f"lane {road_w} m, car {car_w} m: {cars} cars, {missed} missed, {false} false")
        return run

    def test_wide_lanes_count_cars_of_every_width(self):
        # Asked for 8 % of the lane's columns, a 4.75 m lane read 18 and 24 of 40 cars of 1.7
        # and 1.9 m near the camera at 1280x720.
        for road_w, car_w in ((4.75, 1.7), (4.75, 1.9), (4.25, 2.2)):
            self.assert_counts(road_w, car_w, 1.6, 4.6, size=(1280, 720))

    def test_a_line_a_length_zone_at_1280x720(self):
        # 1.5 m, like line A. Here the bank's refresh learnt a car's front as road (3 missed and
        # 3 false of 33 in 2 minutes) until each row was learnt only from moments it was clear.
        self.assert_counts(4.25, 1.8, 10.2, 11.7, size=(1280, 720), seconds=120)

    def test_cars_off_the_lane_centre_are_counted(self):
        # Asked for foreground on both sides of the lane's centre, a 4.25 m lane read 0 of 40
        # cars 1.0 m right of it and 3 of 40 at 1.0 m left.
        for off in (-1.0, 1.0):
            self.assert_counts(4.25, 1.8, NEAR, FAR, centre=lambda car_id, o=off: 4.25 / 2 + o)

    def test_a_car_across_two_lane_zones_is_counted_once(self):
        # Two 3.5 m zones side by side. Judged alone, a 1.8 m car on the line was counted in
        # neither about half the time, and a 2.2 m car within 0.15 m of it in both.
        lanes = ((0.0, 3.5), (3.5, 7.0))
        for car_w, x in ((1.8, -0.05), (1.8, 0.0), (2.2, 0.1), (1.6, -0.15)):
            run = self.assert_counts(7.0, car_w, NEAR, FAR, lanes=lanes,
                                     centre=lambda car_id, x=x: 3.5 + x)
            self.assertEqual({c.line for c in run.crossings}, {"A"})

    def test_a_wide_vehicle_across_a_painted_line_is_counted_once(self):
        # A 2.55 m bus reads 2.9-3.0 m from outline to outline. With CAR_MAX_M at 2.8 m and
        # bounding cars joined across the line too, it was judged by each zone alone and
        # counted 88 and 100 times in 50 (the audit's run, 2026-10-26).
        for x in (0.1, -0.3):
            self.assert_counts(7.0, 2.55, NEAR, FAR, lanes=((0.0, 3.5), (3.5, 7.0)),
                               centre=lambda car_id, x=x: 3.5 + x)

    def test_two_cars_side_by_side_near_the_line_are_two_cars(self):
        # Each 0.2 m from the line between their zones: their outlines meet across it, and the
        # pair was taken for one car across the line (14 cars missed in this minute).
        scene = WideScene(road_w=7.0, car_w=1.8, lines=(0.0, 3.5, 7.0),
                          centre=lambda car_id: 3.5 + (1.1 if car_id % 2 else -1.1))
        run = Run(counter(road_w=7.0, lanes=((0.0, 3.5), (3.5, 7.0))), Frames(scene=scene)).warm()
        pairs = [[(f, 2 * i + side) for f, i in cars for side in (0, 1)]
                 for cars in steady(60, 3.0, headway=9.0)]
        cars, missed, false = score(pairs, run=run)
        self.assertGreaterEqual(cars, 20)
        self.assertEqual((missed, false), (0, 0), f"{cars} cars: {missed} missed, {false} false")
        lanes = [c.lane for c in run.crossings]
        self.assertEqual(lanes.count("lane-1"), lanes.count("lane-2"))

    def test_a_car_changing_lanes_is_counted_once(self):
        # Its centre crosses the line with its rear just short of the far edge: counted in the
        # first zone, then again in the second, empty the frame before, as if it came in there.
        for speed, on_line in ((1.0, 12.1), (2.0, 11.6), (3.0, 11.3)):
            run = Run(counter(road_w=7.0, lanes=((0.0, 3.5), (3.5, 7.0))),
                      Frames(scene=Changing(speed, on_line))).warm()
            for cars in one_car(RED, speed=speed, start=-2.0, end=30.0):
                run.add(cars)
            self.assertEqual(len(run.crossings), 1, (speed, on_line))


class NotCars(unittest.TestCase):
    def test_an_empty_road_counts_nothing(self):
        run = Run().warm()
        for _ in range(240):
            run.add()
        self.assertEqual(run.crossings, [])
        self.assertEqual(run.loop.states(), {("A", "car"): "counting"})

    def test_a_person_crossing_sideways_is_not_a_car(self):
        run = Run().warm()
        for v in (NEAR, (NEAR + FAR) / 2, FAR, NEAR + 0.3, FAR - 0.3):
            for k in range(12):                           # 1.4 m/s across a 4 m lane and beyond
                run.add(blobs=((-1.5 + 1.4 * k * DT, v, 0.5, (40, 40, 60)),))
        self.assertEqual(run.crossings, [])

    def test_a_hard_shadow_swept_along_the_lane_is_not_a_car(self):
        # A 3 m shadow across the road, 45 % darker, at 1 m/s. It darkens both painted edge
        # lines, which then read as a 3 m car between them unless they are two things.
        frames = Frames()
        run = Run(frames=frames).warm()
        scene, road = frames.scene, frames.scene.road
        try:
            for _ in range(10):
                for k in range(40):
                    near = scene._row(NEAR - 5 + k * DT)
                    scene.road = road.copy()
                    band = scene.road[max(0, near - 3 * CANVAS_PPM):max(0, near)]
                    band[:] = (band * 0.55).astype(np.uint8)
                    frames.cache.clear()
                    run.add()
                scene.road = road
                frames.cache.clear()
                run.warm(5)
        finally:
            scene.road = road
        self.assertEqual(run.crossings, [])

    def test_dusk_on_an_empty_road_is_not_a_car(self):
        frames = Frames()
        run = Run(frames=frames).warm()
        empty = frames.render([]).astype(np.int16)
        for k in range(120):                              # 60 grey levels darker in a minute
            run.add(image=(empty - k // 2).clip(0, 255).astype(np.uint8))
        self.assertEqual(run.crossings, [])


class Learning(unittest.TestCase):
    def test_nothing_is_counted_until_the_road_is_learnt(self):
        run = Run()
        self.assertEqual(run.loop.states(), {("A", "car"): "learning"})
        for cars in one_car(WHITE, speed=6.0):            # crosses before 8 samples are in
            run.add(cars)
        self.assertEqual(run.crossings, [])
        run.warm()
        self.assertEqual(run.loop.states(), {("A", "car"): "counting"})

    def test_a_lane_busy_the_whole_time_is_not_learnt_as_road(self):
        run = Run()
        for cars in steady(20, 1.0, headway=5.5):
            run.add(cars)
        self.assertEqual(run.loop.states(), {("A", "car"): "learning"})
        self.assertEqual(run.crossings, [])

    def production(self) -> LoopCounter:
        # The real bank: a sample every 10 s, a first road after two minutes of them.
        cam = camera()
        return LoopCounter(cam, (Zone(cam.id, "A", "car", zone_quad(NEAR, FAR)),))

    def test_a_road_kept_from_before_counts_from_the_first_frame(self):
        # Restarted in a queue, the bank alone would stay "learning" until the lane clears.
        kept = Run().warm().loop.references()
        loop = self.production()
        self.assertEqual(loop.seed(kept), [])
        self.assertEqual(loop.states(), {("A", "car"): "counting"})
        cars, missed, false = score(stop_and_go(240, headway=6.5), run=Run(loop))
        self.assertGreaterEqual(cars, 4)
        self.assertEqual((missed, false), (0, 0))

    def test_a_road_learnt_for_another_patch_is_refused(self):
        (grey, edges), = Run().warm().loop.references().values()
        loop = self.production()
        self.assertEqual(loop.seed({("A", "car"): (grey[1:], edges[1:])}), [("A", "car")])
        self.assertEqual(loop.seed({("A", "other"): (grey, edges)}), [("A", "car")])

    def test_a_queue_learnt_as_road_is_unlearnt_after_a_long_quiet_spell(self):
        # A queue standing through the first learning window is learnt as road, and no moment
        # looks empty against it again. Kept, it read 33 of 48 patch rows as a car on an empty
        # road 10 minutes later, and 3 m/s flow then counted 27 of 54.
        run = Run()
        for _ in range(int(8 / DT)):
            run.add(queue_fronts(60, 0.0, headway=6.5))
        self.assertEqual(run.loop.states(), {("A", "car"): "counting"})
        run.warm(120)            # this bank's quiet spell: 18 s to fill, 13 refreshes of 6 s
        cars, missed, false = score(steady(60, 3.0, headway=9.0), run=run)
        self.assertGreaterEqual(cars, 10)
        self.assertEqual((missed, false), (0, 0), f"{cars} cars: {missed} missed, {false} false")

    def test_a_queue_standing_a_minute_is_not_learnt_as_road(self):
        # The road held is replaced only after the whole bank has looked one way for
        # AGREE_REFRESHES refreshes running: with this bank about 100 s, with the real one 32
        # minutes. One car a cycle, each standing 60 s inside the zone.
        cars, missed, false = score(stop_and_go(260, headway=6.5, advance=6.5, stand_s=60.0))
        self.assertGreaterEqual(cars, 3)
        self.assertEqual((missed, false), (0, 0), f"{cars} cars: {missed} missed, {false} false")


class Gaps(unittest.TestCase):
    def half_way(self, run: Run):
        positions = one_car(WHITE)
        middle = next(k for k, cars in enumerate(positions) if cars[0][0] - CAR_M > NEAR + 1)
        for cars in positions[:middle]:
            run.add(cars)
        return positions[middle:]

    def test_a_stamp_that_does_not_move_forward_is_a_gap(self):
        # hls and the runner drop such a frame before any counter sees it. Were one to come,
        # the loop treats it as the detector does: the car in flight is forgotten and the line
        # has a gap there, so both methods cut their epochs at the same frames.
        for back in (0.0, 0.1, 0.9):
            run = Run().warm()
            rest = self.half_way(run)
            last = run.at - timedelta(seconds=DT)
            run.at -= timedelta(seconds=DT + back)        # stamped at, or before, the last frame
            for cars in rest:
                run.add(cars)
            self.assertEqual(run.crossings, [], back)
            gap = run.loop.gaps()[0]
            self.assertEqual((gap.after, gap.seconds), (last, 0.0), back)

    def test_a_car_in_flight_across_a_gap_is_not_counted(self):
        run = Run().warm()
        rest = self.half_way(run)
        run.loop.gap(Gap(run.at, 30.0, "segment expired"))
        for cars in rest:
            run.add(cars)
        self.assertEqual(run.crossings, [])               # its entry across the near edge was lost
        self.assertEqual(run.loop.states(), {("A", "car"): "counting"})

    def test_frames_far_apart_are_a_gap_too(self):
        run = Run().warm()
        rest = self.half_way(run)
        last = run.at - timedelta(seconds=DT)
        run.at += timedelta(seconds=30)
        for cars in rest:
            run.add(cars)
        self.assertEqual(run.crossings, [])
        # ...and reported, so transit cuts its epoch there as at the reader's own gaps. The car
        # that was in the zone at the resume is not a gap of its own: transit sets the new
        # epoch's offset afresh, and in simulation a car missed at its start changed no
        # unflagged number (loop.py).
        self.assertEqual(run.loop.gaps(), [Gap(last, 30.5, "frames +30.5 s apart")])
        self.assertEqual(run.loop.gaps(), [])

    def test_a_stamp_that_goes_well_back_is_a_gap(self):
        run = Run().warm()
        rest = self.half_way(run)
        run.at -= timedelta(seconds=3)
        for cars in rest:
            run.add(cars)
        self.assertEqual(run.crossings, [])
        self.assertEqual(run.loop.gaps()[0].seconds, 0.0)

    def test_without_the_gap_the_same_car_is_counted(self):
        run = Run().warm()
        for cars in self.half_way(run):
            run.add(cars)
        self.assertEqual(len(run.crossings), 1)


class Signature(unittest.TestCase):
    def crossing(self, car_id: int):
        run = Run().warm()
        for cars in one_car(car_id):
            run.add(cars)
        self.assertEqual(len(run.crossings), 1)
        return run.crossings[0]

    def test_it_tells_a_red_car_from_a_white_one(self):
        red, white = self.crossing(RED), self.crossing(WHITE)
        x, y, val, width = red.signature
        self.assertGreater(x, 0.4)                        # red: hue 0, strongly coloured
        self.assertLess(abs(y), 0.1)
        self.assertLess(math.hypot(*white.signature[:2]), 0.1)    # white: no colour to speak of
        self.assertGreater(white.signature[2], val)       # brighter than the red
        self.assertAlmostEqual(width * WIDTH_SCALE_M, CAR_W, delta=0.6)
        self.assertEqual((red.method, red.line, red.lane), ("loop", "A", "car"))

    def test_signatures_are_on_the_scale_transit_compares(self):
        # transit.measure refuses a signature with a component beyond +-SIGNATURE_LIMIT (hue in
        # degrees, 0-255 channels): loop crossings at A and B must go straight in.
        run = Run().warm()
        for cars in steady(60, 3.0, headway=9.0):
            run.add(cars)
        a = run.crossings
        self.assertGreater(len(a), 10)
        for c in a:
            self.assertEqual(len(c.signature), 4)
            self.assertTrue(all(-1.0 <= v <= 1.0 for v in c.signature), c.signature)
        b = [replace(c, camera="city", line="B", at=c.at + timedelta(minutes=40)) for c in a]
        transit.measure(a, b, 30.0, 50.0)                 # raised ValueError before

    def test_one_car_reads_alike_at_a_line_whose_width_is_assumed(self):
        # Line B's camera has no ground rectangle, so its zones are read ASSUMED_LANE_M wide
        # whatever they really are, and a car's width share there is not the one at A. Rendered,
        # in a calibrated 3.5 m zone at A and a 4.5 m one read as 3.5 m at B, a red car is
        # 0.129 apart on all four parts at 1.8 m wide and 0.164 at 2.0 m, against MATCH_DISTANCE
        # 0.15. transit compares colour and value only (colour_distance): 0.005 and 0.020.
        def read(road_w: float, car_w: float, metres=None):
            cam = camera(width_m=road_w)
            zone = Zone(cam.id, "A", "car", zone_quad(NEAR, FAR, road_w=road_w))
            loop = LoopCounter(cam, (zone,), metres=metres, sample_every_s=0.5, bank_min=8)
            run = Run(loop, Frames(scene=WideScene(road_w=road_w, car_w=car_w))).warm()
            for cars in one_car(RED):
                run.add(cars)
            (crossing,) = run.crossings
            return crossing.signature

        assumed = {("A", "car"): (ASSUMED_LANE_M, (FAR - NEAR) * ASSUMED_LANE_M / 4.5)}
        for car_w in (1.8, 2.0):
            a, b = read(3.5, car_w), read(4.5, car_w, assumed)
            self.assertLessEqual(transit.colour_distance(a, b), transit.MATCH_DISTANCE, car_w)

    def test_a_red_car_reads_alike_on_both_sides_of_the_hue_wrap(self):
        # Raw hue put a red car at 359.9 degrees and another at 0.0, as far apart as two cars
        # can be, and rounded one to 360.0.
        look = _Look()
        for hue in (179, 1):                              # OpenCV half degrees: -2 and +2
            look.add(np.array([[hue, 200, 120]] * 10, np.uint8), 1.8)
        x, y, _, _ = look.signature()
        self.assertAlmostEqual(x, 200 / 255 * math.cos(math.radians(2)), places=2)
        self.assertAlmostEqual(y, 0.0, places=3)


class Geometry(unittest.TestCase):
    def test_a_zone_of_another_camera_is_refused(self):
        with self.assertRaises(ValueError):
            LoopCounter(camera(), (Zone("elsewhere", "A", "car", zone_quad(NEAR, FAR)),))

    def test_a_zone_too_short_to_have_two_ends_is_refused(self):
        with self.assertRaises(ValueError):
            counter(NEAR, NEAR + 0.5)

    def test_a_zone_off_the_picture_is_refused(self):
        cam = camera()
        quad = ((700.0, 400.0), (760.0, 400.0), (760.0, 380.0), (700.0, 380.0))
        with self.assertRaises(ValueError):
            LoopCounter(cam, (Zone(cam.id, "A", "car", quad),))

    def test_frames_at_another_size_count_the_same(self):
        positions = steady(45, 3.0, headway=9.0)
        small = score(positions)
        big = score(positions, run=Run(size=(1280, 720)).warm())
        self.assertEqual(small, big)
        self.assertEqual(small[1:], (0, 0))

    def city(self):
        # Like the City's bridgepdn1: no ground rectangle, the zone drawn in pixels only.
        return camera_from_dict("city", {
            "title": "rendered", "port_number": "000000", "direction": "north",
            "frame_size": FRAME, "source": {"kind": "hls", "url": "https://example.invalid/x.m3u8",
                                             "clock": "last-modified"},
            "display": False,
            "count_zones": [{"line": "B", "lane": "car", "quad": zone_quad(NEAR, FAR)}]})

    def test_without_a_ground_rectangle_a_foreshortened_zone_is_refused(self):
        # The zone is taken as one lane, ASSUMED_LANE_M wide, with square pixels. This low
        # rendered camera sees 6x fewer pixels per metre along the lane than across it, so
        # the 3 m zone reads 0.5 m: refused, rather than counted at the wrong scale.
        with self.assertRaisesRegex(ValueError, "calibrate"):
            LoopCounter(self.city())

    def test_without_a_ground_rectangle_given_metres_count(self):
        loop = LoopCounter(self.city(), metres={("B", "car"): (LANE_W, FAR - NEAR)},
                           sample_every_s=0.5, bank_min=8)
        cars, missed, false = score(steady(45, 3.0, headway=9.0), run=Run(loop).warm())
        self.assertGreaterEqual(cars, 8)
        self.assertEqual((missed, false), (0, 0))

    def test_without_a_ground_rectangle_a_zones_own_metres_count(self):
        # cameras.json "metres" on the zone, so every way of building the counter gets them.
        cam = camera_from_dict("city", {
            "title": "rendered", "port_number": "000000", "direction": "north",
            "frame_size": FRAME, "source": {"kind": "hls", "url": "https://example.invalid/x.m3u8",
                                             "clock": "last-modified"},
            "display": False,
            "count_zones": [{"line": "B", "lane": "car", "quad": zone_quad(NEAR, FAR),
                             "metres": [LANE_W, FAR - NEAR]}]})
        loop = LoopCounter(cam, sample_every_s=0.5, bank_min=8)
        cars, missed, false = score(steady(45, 3.0, headway=9.0), run=Run(loop).warm())
        self.assertGreaterEqual(cars, 8)
        self.assertEqual((missed, false), (0, 0))

    def test_without_a_ground_rectangle_equal_metres_judge_a_car_across_two_columns_once(self):
        # The City's line B: columns side by side, no ground rectangle. Read with square pixels
        # no two columns matched in length, so _beside paired none and a 2.2 m car 0.1 m over
        # the line was judged by each zone alone (the line-B review, 2026-10-26).
        lanes = ((0.0, 3.5), (3.5, 7.0))
        cam = camera_from_dict("city", {
            "title": "rendered", "port_number": "000000", "direction": "north",
            "frame_size": FRAME, "source": {"kind": "hls", "url": "https://example.invalid/x.m3u8",
                                             "clock": "last-modified"},
            "display": False,
            "count_zones": [{"line": "B", "lane": f"col-{k + 1}",
                             "quad": zone_quad(NEAR, FAR, u0, u1, 7.0),
                             "metres": [3.5, FAR - NEAR]} for k, (u0, u1) in enumerate(lanes)]})

        def run(metres=None):
            scene = WideScene(road_w=7.0, car_w=2.2, lines=(0.0, 3.5, 7.0),
                              centre=lambda car_id: 3.6)
            loop = LoopCounter(cam, metres=metres, sample_every_s=0.5, bank_min=8)
            r = Run(loop, Frames(scene=scene)).warm()
            return loop, score(steady(60, 3.0, headway=9.0), run=r)

        loop, (cars, missed, false) = run()
        self.assertEqual(len(loop._beside), 1)
        self.assertGreaterEqual(cars, 10)
        self.assertEqual((missed, false), (0, 0), f"{cars} cars: {missed} missed, {false} false")
        unequal = {("B", "col-1"): (3.5, FAR - NEAR), ("B", "col-2"): (3.5, FAR - NEAR + 0.5)}
        loop, (cars, missed, false) = run(unequal)
        self.assertEqual(loop._beside, [])
        self.assertGreater(false, 0, "the case no longer exercises the straddle de-dup")


class Scenes(unittest.TestCase):
    def test_a_car_driving_out_of_view_paints_only_itself(self):
        # Its windscreen band, once past the far edge, wrapped round the canvas and shaded the
        # lane down to the near end: a scene with cars leaving the view was silently wrong.
        bound = max(y for _, y in zone_quad(50.0, VIEW_M))      # the image row 50 m along
        for scene in (Scene(), WideScene()):
            scene.noise = _Still()
            empty = scene.render([]).astype(np.int16)
            for front in (60.0, 61.0, 61.8, 62.5):
                changed = np.abs(scene.render([(front, WHITE)]) - empty).max(axis=(1, 2)) > 0
                self.assertLess(np.flatnonzero(changed).max(), bound, (type(scene), front))


class Determinism(unittest.TestCase):
    def test_same_frames_same_crossings(self):
        frames = Frames()
        images = [frames.render([])] * 20 + [frames.render(c) for c in steady(30, 3.0, 9.0)]
        runs = []
        for _ in range(2):
            run = Run()
            for image in images:
                run.add(image=image)
            runs.append([(c.at, c.signature) for c in run.crossings])
        self.assertTrue(runs[0])
        self.assertEqual(runs[0], runs[1])


if __name__ == "__main__":
    unittest.main()
