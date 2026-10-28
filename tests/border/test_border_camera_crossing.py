"""The shared signature form (crossing.look): what reaches transit is a finite point or nothing.
And the count zones as cameras.json draws them: their lane types (QUESTIONS.md Q-22) and their
shapes, which no two zones of one line may share."""
from __future__ import annotations

import json
import math
import shutil
import subprocess
import unittest

import pytest

pytest.importorskip("cv2")   # crossing imports hls, which imports the stream reader

import cv2
import numpy as np

from scraper.border.camera.calibration import CAMERAS_FILE, load_cameras
from scraper.border.camera.crossing import look, zones_from_raw
from scraper.border.camera.loop import BESIDE_PX

QUAD = [[0, 10], [10, 10], [10, 0], [0, 0]]


class Look(unittest.TestCase):
    def test_each_part_is_clamped_to_its_range_and_rounded(self):
        self.assertEqual(look(-1.7, 0.12345, 1.2, -0.1), (-1.0, 0.123, 1.0, 0.0))

    def test_a_part_that_is_not_a_number_gives_no_signature(self):
        # min/max pass NaN through, and transit refuses a whole method's crossings over one.
        for bad in (math.nan, math.inf, -math.inf):
            for i in range(4):
                parts = [0.1, 0.2, 0.5, 0.6]
                parts[i] = bad
                with self.subTest(part=i, value=bad):
                    self.assertIsNone(look(*parts))


class LaneTypes(unittest.TestCase):
    def test_normal_is_a_lane_type(self):
        # Q-22: the owner tells SENTRI from the rest, not standard from Ready.
        zones = zones_from_raw("cam", [{"line": "A", "lane": "1", "quad": QUAD,
                                        "lane_type": "normal"}])
        self.assertEqual(zones[0].lane_type, "normal")

    def test_an_unknown_lane_type_is_refused(self):
        for bad in ("express", "Normal", "SENTRI", ""):
            with self.subTest(lane_type=bad), self.assertRaises(ValueError):
                zones_from_raw("cam", [{"line": "A", "lane": "1", "quad": QUAD, "lane_type": bad}])


class MethodsAndMetres(unittest.TestCase):
    def test_methods_and_metres_are_read_and_default_to_none(self):
        z, plain = zones_from_raw("cam", [
            {"line": "B", "lane": "e", "quad": QUAD, "methods": ["yolox"], "metres": [3.5, 4]},
            {"line": "B", "lane": "1", "quad": QUAD}])
        self.assertEqual((z.methods, z.metres), (("yolox",), (3.5, 4.0)))
        self.assertEqual((plain.methods, plain.metres), (None, None))

    def test_bad_methods_are_refused(self):
        # A bare string would read as its letters; an empty list would count the zone by none.
        # A number or a nested list is refused with the message, not a bare TypeError; a dict
        # would read as its keys ({"yolox": false} as yolox only); a duplicate would read
        # "counted by yolox and yolox only" in runner.unmeasured's reason.
        for bad in ("yolox", [], ["yolo"], ["loop", "radar"], 5, True, [["loop"]],
                    {"yolox": True}, ["yolox", "yolox"]):
            with self.subTest(methods=bad), self.assertRaisesRegex(ValueError, "cam zone e: "):
                zones_from_raw("cam", [{"line": "B", "lane": "e", "quad": QUAD, "methods": bad}])

    def test_bad_metres_are_refused(self):
        for bad in ("3.5", [3.5], [3.5, 3.5, 3.5], [0, 3.5], [3.5, -1], [math.nan, 3.5],
                    [math.inf, 3.5], [True, 3.5], ["3.5", 3.5], 3.5, True, {"a": 1, "b": 2}):
            with self.subTest(metres=bad), self.assertRaisesRegex(ValueError, "cam zone e: "):
                zones_from_raw("cam", [{"line": "B", "lane": "e", "quad": QUAD, "metres": bad}])

    def test_bad_quads_are_refused(self):
        # float() would read "12" as (1, 2); a NaN, a bool or a third value would reach the
        # counters, which fail with no zone named.
        for bad in (None, 5, "abcd", QUAD[:3], [*QUAD, [0, 0]], ["12", "34", "56", "78"],
                    [[0, 10, 7], *QUAD[1:]], [[0], *QUAD[1:]], [[math.nan, 10], *QUAD[1:]],
                    [[math.inf, 10], *QUAD[1:]], [[True, False], *QUAD[1:]]):
            with self.subTest(quad=bad), self.assertRaisesRegex(ValueError, "cam zone e: "):
                zones_from_raw("cam", [{"line": "B", "lane": "e", "quad": bad}])

    def test_a_zone_without_a_line_is_refused_by_name(self):
        with self.assertRaisesRegex(ValueError, "cam zone e: a zone needs a line and a lane"):
            zones_from_raw("cam", [{"lane": "e", "quad": QUAD}])

    def test_tuple_quads_are_read_as_floats(self):
        z, = zones_from_raw("cam", [{"line": "B", "lane": "e", "quad": tuple(map(tuple, QUAD))}])
        self.assertEqual(z.quad, ((0.0, 10.0), (10.0, 10.0), (10.0, 0.0), (0.0, 0.0)))


class RealZones(unittest.TestCase):
    """cameras.json as committed."""

    @classmethod
    def setUpClass(cls):
        cls.cams = load_cameras()

    def test_pdn_north_line_a_is_typed_per_q22(self):
        # Owner, 2026-10-27: lane-4 (white posts to orange barrels) and lane-5 (barrels to the
        # right curb) are both the one SENTRI lane; lanes 1-3 are the normal lanes.
        types = {z.lane: z.lane_type for z in self.cams["pdn-north"].count_zones}
        self.assertEqual(types, {"lane-1": "normal", "lane-2": "normal", "lane-3": "normal",
                                 "lane-4": "sentri", "lane-5": "sentri"})

    def test_pdn_north_line_a_has_three_normal_zones_and_two_sentri_zones_on_the_right(self):
        # The road has 4 car lanes, 3 normal and 1 SENTRI on the very right (owner, 2026-10-27).
        # The SENTRI lane keeps two zones, one each side of the barrels: one zone over both would
        # be ~9 m wide, and the loop reads two cars abreast in one zone as one. The two still
        # share a side, so a car across the barrel line is judged by both (loop._beside).
        zones = [z for z in self.cams["pdn-north"].count_zones if z.line == "A"]
        self.assertEqual(sorted(z.lane_type for z in zones), ["normal"] * 3 + ["sentri"] * 2)
        normal = [z for z in zones if z.lane_type == "normal"]
        lane_4, lane_5 = sorted((z for z in zones if z.lane_type == "sentri"), key=_centre_x)
        self.assertLess(max(map(_centre_x, normal)), _centre_x(lane_4))
        self.assertEqual((lane_4.lane, lane_5.lane), ("lane-4", "lane-5"))
        # lane-4's near-right and far-right corners on lane-5's near-left and far-left ones
        for right, left in ((1, 0), (2, 3)):
            self.assertLessEqual(math.dist(lane_4.quad[right], lane_5.quad[left]), BESIDE_PX)

    def test_pdn_north_drives_north_away_from_the_camera_as_the_owner_confirmed(self):
        # Owner, 2026-10-27: cars drive away from this camera, northbound.
        cam = self.cams["pdn-north"]
        self.assertEqual((cam.direction, cam.direction_verified), ("north", True))

    def test_no_pdn_north_strip_names_a_cbp_lane(self):
        # The lane type lives on the count zones, which the runner and combine measure by. A
        # strip's number is the queue reader's (D+L)/v, which ADR-0015 rejects as a wait and
        # which under-reads at lane-5: naming car_sentri on lane-4 or lane-5 would print it on
        # the card as "SENTRI" beside the camera wait, and file it as a verified car_sentri
        # signal.
        lanes = {lane.lane: lane.cbp_lane for lane in self.cams["pdn-north"].lanes}
        self.assertEqual(lanes, dict.fromkeys(("lane-1", "lane-2", "lane-3", "lane-4", "lane-5")))
        self.assertFalse(any(lane.lane_verified for lane in self.cams["pdn-north"].lanes))

    def test_every_city_zone_has_a_lane_type_and_the_city_is_never_shown(self):
        cam = self.cams["city-pdn-north"]
        self.assertFalse(cam.display)
        self.assertTrue(cam.count_zones)
        for z in cam.count_zones:
            with self.subTest(zone=z.lane):
                self.assertEqual(z.line, "B")
                self.assertIn(z.lane_type, ("normal", "sentri"))
        self.assertEqual({z.lane_type for z in cam.count_zones}, {"normal", "sentri"})

    def test_col_e_is_counted_by_yolox_only_and_every_city_zone_has_its_metres(self):
        # A col-4 car covers col-e in the picture, so the loop counts it there again (line-B
        # review, 2026-10-26). The City has no ground rectangle: without equal metres no two
        # columns match in length and the loop's straddle de-dup (_beside) is off.
        zones = {z.lane: z for z in self.cams["city-pdn-north"].count_zones}
        self.assertEqual({k: z.methods for k, z in zones.items()},
                         {"col-1": None, "col-2": None, "col-3": None, "col-4": None,
                          "col-e": ("yolox",)})
        self.assertEqual({z.metres for z in zones.values()}, {(3.5, 3.5)})

    def test_no_two_zones_of_one_line_overlap(self):
        # A car under two zones of one line is counted twice. Zones drawn side by side may
        # share an edge to within loop.BESIDE_PX (pdn-north's lane-4 and lane-5 overlap by
        # 1.4 px); anything wider is two zones on the same road.
        for cam in self.cams.values():
            for z in cam.count_zones:
                with self.subTest(camera=cam.id, zone=z.lane):
                    self.assertTrue(_convex(z.quad), "a quad's corners are out of order")
            for i, a in enumerate(cam.count_zones):
                for b in cam.count_zones[i + 1:]:
                    if a.line != b.line:
                        continue
                    with self.subTest(camera=cam.id, a=a.lane, b=b.lane):
                        area, poly = cv2.intersectConvexConvex(np.float32(a.quad),
                                                               np.float32(b.quad))
                        if area <= 0 or poly is None:
                            continue
                        p = poly.reshape(-1, 2)
                        longest = max(np.hypot(*(p[k] - p[k - 1])) for k in range(len(p)))
                        self.assertLessEqual(area / longest, BESIDE_PX)

    def test_pdn_north_geometry_is_unchanged_since_1221c9f(self):
        # Its saved loop roads are keyed by zone and camera geometry: since then the zones were
        # given lane types (lane-4 retyped sentri on 2026-10-27) and the camera its confirmed
        # direction, and nothing else. A deliberate redraw of line A (the owner task in
        # pdn-north's notes) replaces this test.
        git = shutil.which("git")
        if git is None:
            self.skipTest("git is not installed")
        shown = subprocess.run([git, "show", "1221c9f:src/scraper/border/camera/cameras.json"],
                               cwd=CAMERAS_FILE.parent, capture_output=True, text=True,
                               encoding="utf-8", check=False)
        if shown.returncode != 0:
            self.skipTest(f"commit 1221c9f is not in this checkout: {shown.stderr.strip()}")
        before = json.loads(shown.stdout)["pdn-north"]
        now = json.loads(CAMERAS_FILE.read_text(encoding="utf-8"))["pdn-north"]
        self.assertEqual(_geometry(now), _geometry(before))


def _convex(quad) -> bool:
    q = np.float64(quad)
    turns = []
    for k in range(4):
        (ax, ay), (bx, by) = q[(k + 1) % 4] - q[k], q[(k + 2) % 4] - q[(k + 1) % 4]
        turns.append(ax * by - ay * bx)
    return all(t > 0 for t in turns) or all(t < 0 for t in turns)


def _centre_x(zone) -> float:
    """The mean x of a zone's corners: which zone is further right in the picture."""
    return sum(x for x, _ in zone.quad) / 4


def _geometry(raw: dict) -> dict:
    """A camera's entry without what may change without moving a pixel: the notes, whether
    the direction is confirmed, the zones' lane types and the strips' CBP lanes."""
    out = {k: v for k, v in raw.items() if k not in ("notes", "direction_verified")}
    out["count_zones"] = [{k: v for k, v in z.items() if k != "lane_type"}
                          for z in raw.get("count_zones", [])]
    out["lanes"] = [{k: v for k, v in lane.items() if k != "cbp_lane"}
                    for lane in raw.get("lanes", [])]
    return out


if __name__ == "__main__":
    unittest.main()
