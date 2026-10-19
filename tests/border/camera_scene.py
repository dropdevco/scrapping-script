"""A rendered bridge lane with a queue whose length and speed are known exactly.

The scene is drawn on a top-down road canvas and warped into a perspective view, the way a
pole-mounted camera sees a lane: the reader has to undo the same perspective it would face
on a real stream. Everything is seeded, so a scene is the same pixels on every run.
"""
from __future__ import annotations

import cv2
import numpy as np

from scraper.border.camera.calibration import camera_from_dict

FRAME = (640, 360)
LANE_W = 4.0              # metres across the rendered lane
VIEW_M = 60.0             # metres of lane between the near and far edges of the rectangle
CANVAS_PPM = 20
SUPERSAMPLE = 4
CAR_M, CAR_W = 4.5, 2.4
GROUND = [(160, 340), (480, 340), (345, 90), (295, 90)]   # near-left, near-right, far-right, far-left
PALETTE = [(40, 40, 160), (200, 200, 205), (30, 30, 30), (150, 90, 20), (60, 140, 60), (215, 215, 220)]


def camera(length_m: float = VIEW_M, width_m: float = LANE_W, booth_distance_m: float = 0.0,
           along=(0.0, 1.0)):
    return camera_from_dict("scene", {
        "title": "rendered", "port_number": "000000", "direction": "north", "frame_size": FRAME,
        "ground": {"image": GROUND, "width_m": width_m, "length_m": length_m, "scale_verified": True},
        "lanes": [{"lane": "car", "across": [0, 1], "along": list(along), "booth": "far",
                   "booth_distance_m": booth_distance_m}],
    })


class Scene:
    """Booth at the far edge (v = VIEW_M). Cars drive toward it in a queue of `queue_m`."""

    def __init__(self, seed: int = 7, view_m: float = VIEW_M):
        rng = np.random.default_rng(seed)
        self.view_m = view_m
        h, w = int(view_m * CANVAS_PPM), int((LANE_W + 4) * CANVAS_PPM)
        road = 92 + rng.normal(0, 7, (h, w))
        road = cv2.GaussianBlur(road.astype(np.float32), (3, 3), 0)
        self.road = np.dstack([road] * 3).clip(0, 255).astype(np.uint8)
        for u in (0.0, LANE_W):                                    # lane edge lines
            c = int((u + 2) * CANVAS_PPM)
            self.road[:, c - 2:c + 2] = 210
        canvas = np.float32([(2 * CANVAS_PPM, h), ((LANE_W + 2) * CANVAS_PPM, h),
                             ((LANE_W + 2) * CANVAS_PPM, 0), (2 * CANVAS_PPM, 0)])
        # Rendered at SUPERSAMPLE x and area-averaged down, the way a sensor pixel integrates
        # light. A plain warp point-samples a 20 px/m canvas into a pixel or two per metre at
        # the far end, so edges alias and jump between frames as no real camera's do.
        big = np.float32(GROUND) * SUPERSAMPLE
        self.to_image = cv2.getPerspectiveTransform(canvas, big)
        self.noise = np.random.default_rng(seed + 1)

    def _row(self, v: float) -> int:
        return round((self.view_m - v) * CANVAS_PPM)

    def render(self, cars: list[tuple[float, int]]) -> np.ndarray:
        """cars: (front bumper in metres from the near edge, the car's own id). The id picks
        the colour, so a car keeps its look as it moves; colouring by queue slot would
        repaint every car at once each time the queue advances a headway."""
        canvas = self.road.copy()
        c0, c1 = int((2 + (LANE_W - CAR_W) / 2) * CANVAS_PPM), int((2 + (LANE_W + CAR_W) / 2) * CANVAS_PPM)
        for front, car_id in cars:
            top, bottom = max(0, self._row(front)), min(canvas.shape[0], self._row(front - CAR_M))
            if bottom <= top:
                continue
            colour = PALETTE[car_id % len(PALETTE)]
            canvas[top:bottom, c0:c1] = colour
            shade = tuple(int(x * 0.45) for x in colour)
            w0, w1 = self._row(front - 1.1), self._row(front - 1.7)      # windscreen band
            canvas[max(top, w0):min(bottom, w1), c0 + 4:c1 - 4] = shade
            cv2.rectangle(canvas, (c0, top), (c1 - 1, bottom - 1), (15, 15, 15), 2)
        big = (FRAME[0] * SUPERSAMPLE, FRAME[1] * SUPERSAMPLE)
        frame = cv2.warpPerspective(canvas, self.to_image, big, flags=cv2.INTER_LINEAR,
                                    borderMode=cv2.BORDER_CONSTANT, borderValue=(55, 60, 58))
        frame = cv2.resize(frame, FRAME, interpolation=cv2.INTER_AREA)
        jitter = self.noise.normal(0, 2, frame.shape)
        return (frame + jitter).clip(0, 255).astype(np.uint8)


def queue_fronts(queue_m: float, travelled: float, view_m: float = VIEW_M,
                 headway: float = 6.5) -> list[tuple[float, int]]:
    """(front bumper, car id) for a queue of `queue_m` behind the booth that has advanced
    `travelled` metres in total. Cars leave at the booth and join at the tail."""
    wraps = int(travelled // headway)
    cars = []
    i = 0
    while True:
        front = view_m - (i * headway) + (travelled % headway)
        if front < view_m - queue_m:
            break
        if front <= view_m:
            cars.append((front, i + wraps))
        i += 1
    return cars
