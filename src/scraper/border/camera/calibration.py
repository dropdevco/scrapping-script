"""A camera's road plane and its lanes, and the warp that turns a lane into a straight strip.

A calibration names four image points that are the corners of a rectangle on the road —
two lane lines and two lines across them — in the order near-left, near-right, far-right,
far-left, plus the rectangle's size in metres. That fixes a homography from the image to
the road plane. Each lane is then a strip of that plane: `across` and `along` are fractions
of the rectangle's width and length, and `along` may run past 0 or 1 to cover road the
rectangle does not, as far as the camera sees it.

Every strip is resampled at PX_PER_M with row 0 at the booth end, so row r is r / PX_PER_M
metres from the booth side of the view, whichever way the camera faces.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import cv2
import numpy as np

PX_PER_M = 8                      # strip resolution; a car is ~36 rows long
CAMERAS_FILE = Path(__file__).with_name("cameras.json")


@dataclass(frozen=True)
class Lane:
    lane: str                      # our name for the strip, unique within the camera
    across: tuple[float, float]    # fractions of the rectangle's width
    along: tuple[float, float]     # fractions of its length; may extend past [0, 1]
    booth: str                     # "far" or "near": which end of `along` the booth is at
    booth_distance_m: float | None = 0.0   # booth to the strip's booth-end row; None = unknown
    cbp_lane: str | None = None    # car | car_sentri | car_ready | walk | truck | ... once verified

    @property
    def lane_verified(self) -> bool:
        """Whether someone confirmed which CBP lane type this strip is. Until then a reading
        from it describes "these lanes", never "the SENTRI lane"."""
        return self.cbp_lane is not None


@dataclass(frozen=True)
class Camera:
    id: str
    title: str                     # the stream's title, how a stream is found on the channel
    port_number: str
    direction: str                 # north | south
    frame_size: tuple[int, int]    # the size `ground` points were picked at
    ground: tuple[tuple[float, float], ...]   # near-left, near-right, far-right, far-left
    width_m: float
    length_m: float
    scale_verified: bool
    lanes: tuple[Lane, ...]
    mask: tuple[tuple[int, int, int, int], ...] = ()   # x, y, w, h overlays to ignore
    clock: tuple[int, int, int, int] | None = None     # burned-in clock: must keep changing
    notes: str = ""
    channel: str = "https://www.youtube.com/@fpfch/streams"
    extra: dict = field(default_factory=dict)


def _lane(raw: dict) -> Lane:
    if raw["booth"] not in ("far", "near"):
        raise ValueError(f"lane {raw['lane']}: booth must be 'far' or 'near'")
    a0, a1 = raw["across"]
    b0, b1 = raw["along"]
    if not (a0 < a1 and b0 < b1):
        raise ValueError(f"lane {raw['lane']}: across/along must be increasing")
    return Lane(raw["lane"], (a0, a1), (b0, b1), raw["booth"],
                raw.get("booth_distance_m", 0.0), raw.get("cbp_lane"))


def camera_from_dict(cid: str, raw: dict) -> Camera:
    ground = tuple(tuple(p) for p in raw["ground"]["image"])
    if len(ground) != 4:
        raise ValueError(f"{cid}: ground needs exactly four points")
    if raw["direction"] not in ("north", "south"):
        raise ValueError(f"{cid}: direction must be north or south")
    return Camera(
        id=cid, title=raw["title"], port_number=raw["port_number"], direction=raw["direction"],
        frame_size=tuple(raw["frame_size"]), ground=ground,
        width_m=float(raw["ground"]["width_m"]), length_m=float(raw["ground"]["length_m"]),
        scale_verified=bool(raw["ground"].get("scale_verified", False)),
        lanes=tuple(_lane(lane) for lane in raw["lanes"]),
        mask=tuple(tuple(m) for m in raw.get("mask", ())),
        clock=tuple(raw["clock"]) if raw.get("clock") else None,
        notes=raw.get("notes", ""), channel=raw.get("channel", Camera.channel),
    )


def live_region(camera: Camera) -> tuple[float, float, float, float] | None:
    """The clock box as fractions of the frame, for stream.frames()."""
    if not camera.clock:
        return None
    x, y, w, h = camera.clock
    fw, fh = camera.frame_size
    return (x / fw, y / fh, w / fw, h / fh)


def load_cameras(path: Path = CAMERAS_FILE) -> dict[str, Camera]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return {cid: camera_from_dict(cid, raw) for cid, raw in data.items() if not cid.startswith("_")}


class Strip:
    """One lane of one camera, resampled into a straight strip with the booth at row 0."""

    def __init__(self, camera: Camera, lane: Lane, frame_size: tuple[int, int]):
        self.camera, self.lane = camera, lane
        sx = frame_size[0] / camera.frame_size[0]
        sy = frame_size[1] / camera.frame_size[1]
        image = np.float32([(x * sx, y * sy) for x, y in camera.ground])
        w, length = camera.width_m, camera.length_m
        plane = np.float32([(0, 0), (w, 0), (w, length), (0, length)])
        image_to_plane = cv2.getPerspectiveTransform(image, plane).astype(np.float64)

        (a0, a1), (b0, b1) = lane.across, lane.along
        p = PX_PER_M
        if lane.booth == "far":     # row grows from the far end toward the camera
            plane_to_strip = np.array([[p, 0, -a0 * w * p], [0, -p, b1 * length * p], [0, 0, 1]])
        else:
            plane_to_strip = np.array([[p, 0, -a0 * w * p], [0, p, -b0 * length * p], [0, 0, 1]])
        self.matrix = plane_to_strip @ image_to_plane          # image -> strip
        self.size = (max(1, round((a1 - a0) * w * p)), max(1, round((b1 - b0) * length * p)))
        self.frame_size = frame_size
        # The rectangle's centre is on the road in front of the camera by construction, so
        # its homogeneous depth has the sign every visible road point shares.
        centre = plane_to_strip @ [w / 2, length / 2, 1]
        self._front = np.sign(np.linalg.inv(self.matrix)[2] @ centre)
        self.valid = self._valid_mask(sx, sy)
        self.rows_valid = self.valid.mean(axis=1) >= 0.5      # a row counts if half of it is seen

    def _valid_mask(self, sx: float, sy: float) -> np.ndarray:
        """Strip pixels that are real road: inside the frame, not under an overlay, and on
        the camera's side of the horizon (a homography maps points behind the camera to
        plausible-looking but meaningless image positions)."""
        seen = np.full((self.frame_size[1], self.frame_size[0]), 255, np.uint8)
        for x, y, w, h in self.camera.mask:
            x0, y0 = round(x * sx), round(y * sy)
            seen[y0:y0 + round(h * sy), x0:x0 + round(w * sx)] = 0
        valid = self.warp(seen, interpolation=cv2.INTER_NEAREST) > 0
        inverse = np.linalg.inv(self.matrix)
        cols, rows = np.meshgrid(np.arange(self.size[0]), np.arange(self.size[1]))
        depth = inverse[2, 0] * cols + inverse[2, 1] * rows + inverse[2, 2]
        return valid & (np.sign(depth) == self._front)

    def warp(self, image: np.ndarray, interpolation: int = cv2.INTER_LINEAR) -> np.ndarray:
        return cv2.warpPerspective(image, self.matrix, self.size, flags=interpolation,
                                   borderMode=cv2.BORDER_CONSTANT, borderValue=0)

    def outline(self) -> np.ndarray:
        """The strip's four corners in image coordinates, for drawing a calibration check."""
        w, h = self.size
        corners = np.float32([[(0, 0), (w, 0), (w, h), (0, h)]])
        return cv2.perspectiveTransform(corners, np.linalg.inv(self.matrix))[0]

    def metres(self, rows: float) -> float:
        return rows / PX_PER_M
