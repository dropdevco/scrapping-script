"""Count cars across a line without a model: a virtual loop on each count zone (ADR-0014).

The deterministic one of the two counters that run side by side on the same stamped frames
(crossing.py). Fixed computations only, no randomness: the same frames give the same crossings.

What it looks at. Each zone is rectified into a small patch at PX_PER_M (8 px per metre, so no
plate or face is legible in anything this module holds), with MARGIN_M of road beyond each end.
Each patch row is judged against the empty road the way the queue reader judges its strips
(measure.py): grey structure plus edge strength, against a reference learnt only from
near-empty moments (emptiest(), after reader.emptiest_reference). A row holds a car when, lone
pixels dropped, one thing in it spans ROW_SPAN_M or more anywhere across the lane
(occupied_rows): at this module's structure scale a car is mostly its outline. Every test is in
metres, never a share of the lane, so a 4.75 m lane is judged like a 3.5 m one, and a car need
not sit on the lane's centre. A car across the line between two zones of one line that share a
side is judged by both and given to the zone holding its centre (_across), so it counts once.

The learnt road. The counter learns it from its own frames, a bank of moments SAMPLE_EVERY_S
apart, each patch row from the moments it was clear (_clear_median), and keeps it in memory.
Until a zone has one it reports "learning" (states()) and counts nothing: its line's total is
short, so the caller must treat that time as a gap, never as no cars. Learning needs
EMPTY_MIN_SAMPLES near-empty moments, and a lane queued since the counter started has none:
rendered, 30 minutes of stop-and-go with 1-3 m gaps never learnt it. So references() hands the
learnt roads to a caller that keeps them across restarts and seed() takes them back (checked
only against the shape of the zone's patch: see seed()). This module writes nothing itself.
ADR-0014 decision 2 says the loop is judged against "the learnt empty road the queue reader
already keeps"; this counter keeps its own, at a finer structure scale (zone_features) and on
its own patches, and whether the worker saves it the way reader.py saves strips is the lead's
call against that decision.

How a car is counted. The textbook loop counts a car when its zone goes from occupied to clear.
Stopped queues leave 1-3 m between cars, so a zone longer than the gap never clears between two
of them, and a zone short enough to clear must catch the gap in a frame: at 2 fps a 1 m gap
creeping at 1 m/s leaves one frame or none. So this follows the cars' rears instead (_track):
a rear is the near end of a run of car rows with road behind it, carried from frame to frame by
the patch's own motion (displacement()), and a car is counted once, as its rear crosses the
zone's far edge. A rear standing on the edge cannot count twice, a gap need only show once
somewhere in the patch, and a rear hidden for a while (the queue closed the gap) is carried
along until it shows again or leaves. Direction: only a rear first seen at the zone's near edge
(within a frame's motion), behind every known rear, is counted, so something that appears
inside the zone (a person, a shadow, a car changing lanes into it where cars are in view) is
never counted there. Frames must come in capture order (hls and the runner drop any other): a
stamp that does not move forward, or frames more than MAX_STEP_S apart, drop the cars in
flight and are reported by gaps(), to be passed to transit with the reader's own gaps. A car
already in a zone when counting starts or resumes after any gap came in unseen and is never
counted, as with the detector.

Each crossing carries a signature of how the car looked while it was in the zone: its body's
mean colour as a point on the colour wheel (saturation x cos and x sin of hue), its median
value and its width, each on a 0-1 scale (_Look.signature). It is for matching a few
distinctive cars between the two lines, lives in memory only, and is never logged or written
(ADR-0014).

Rendered evaluation, 2026-10-24, re-run after the review (tests/border/camera_scene.py, a 4 m
lane and 2.4 m cars unless said; truth is a car's rear crossing the far edge, matched one to
one to a crossing within 3 s; steady flows keep a 2 s gap: headway 4.5 m + 2 s x speed). On
these default cases the first version gave the same numbers, case for case:
- 3 m zone at 9.2-12.2 m, ~7 image rows per metre along the lane (about line A on pdn-north at
  854x480): stop-and-go (a car length in 5 s, then 25 s standing; 20 min each) with 2.0, 1.5
  and 1.0 m gaps, 27/30/32 cars; a crawl with 1 m gaps at 0.6/1.0/1.5 m/s, 31/51/76 cars
  (5 min each); steady flow at 2/4/6 m/s, 66/90/102 cars: none missed, none false. At 8 m/s 15
  of 110 missed (-14 %), at 10 m/s 19 of 115 (-17 %).
- 1.5 m zone (line A's length) at 10.2-11.7 m: the same stop-and-go, crawl and 2-4 m/s cases,
  none missed or false. At 6 m/s 51 of 102 missed, at 8 m/s 23 of 110, at 10 m/s 39 of 115:
  the patch is shorter than two frames of travel (at 6 m/s every other car's rear falls
  between the frames that show its gap).
- pdn-north's lane widths: lanes of 3.5, 4.25 and 4.75 m, cars 1.6-2.2 m wide, zones at
  1.6-4.6, 9.2-12.2 and 10.2-11.7 m, frames at 640x360 and 1280x720, steady 3 m/s for 2
  minutes (33-34 cars a case, 108 cases): none missed, none false. The first version counted
  18-48 of 40 in 4.75 m lanes, by car width and frame size, and 0 of 40 cars 1.0 m off the
  centre of a 4.25 m lane; here every car counts up to 1.2 m off the centre of a 4.25 m lane and
  1.4 m off a 4.75 m one.
- Across a lane line: a 7 m road split into two 3.5 m zones, cars of 1.6, 1.8 and 2.2 m centred
  0-0.3 m either side of the line, 3 m and 1.5 m zones: every car counted once on the line.
- Not cars: a 10-minute empty road, a person-sized blob crossing sideways 140 times at 7 places
  in and around the zone, a hard 3 m shadow across the lane swept along it 10 times: 0 counted.
Added 2026-10-26, after the audit's fixes (the 3 m zone's flows, stop-and-go for 10 minutes and
crawl, and the 1.5 m zone's flows, re-run on the code before and after: the same numbers):
- Staggered starts, as queues really start: each car repeats the path of the one ahead 1, 1.5
  or 2 s later, at 1.5 or 2.5 m/s^2 up to 2 m/s, 20 s stands, 2.0, 1.5 and 1.0 m gaps, 7-8 stop
  places, 5 minutes each: 2 of 1522 cars missed, 5 false; before, 230 missed. Starting
  together, the same cases: 0 of 541 missed.
- Two 1.8 m cars side by side, one in each of two 3.5 m zones, each 0.1-0.6 m from the line
  between them, painted or not, 3 m/s for 150 s: 50 of 50 a lane (before: 22-66 within 0.3 m).
  Vehicles of 2.2-2.55 m across that line, painted or not: once each (before, a 2.55 m one
  across an unpainted line: 15 and 24 of 50).
- A car changing lanes across a shared line at 1-6 m/s, its centre on the line with its rear
  anywhere from 9.8 to 12.8 m: counted once, in one zone (before, twice in 11 of those 36
  cases, all at 1-3 m/s with the centre crossing within 1.2 m of the far edge).
- A road learnt wrong (test-sized bank): a standing queue learnt as road, a 1.6 m dark patch
  and a 0.3 m dark stripe laid on the road after learning read 33, 17 and 6 of 48 patch rows
  as a car on the empty road; after 2 minutes of empty road, 0, and 3 m/s flow counted 54 of
  54 (before: unchanged after 10 minutes, and 27, 9 and 0 missed). With the real bank, the
  queue learnt as road was replaced after 30-35 minutes of empty road; a road learnt right
  held through a 10-minute standstill (0 of 54 missed in the flow after) and through 30
  minutes of 1 m stop-and-go (0 of 48 missed).

Where it fails, plainly:
- 0.5 m gaps: 0 of 35 cars counted. The image's own 3x3 smoothing and Sobel spread each car
  ~0.3 m at ~7 rows per metre, so the queue reads as one long car.
- Half that resolution (3 m zone at 18.2-21.2 m, ~3.3 rows per metre): 1 m gaps are not seen;
  30 of 31 cars missed in stop-and-go and 51 of 51 in a crawl (the first version missed 25 and
  34, by flicker more than by sight). 2 m gaps miss 3 of 27 with 3 false; steady flow counts.
- A queue that stands still through the whole first learning window (BANK_MIN samples,
  SAMPLE_EVERY_S apart) is learnt as road, and so is a road changed for good (a repair patch,
  paint, a nudged camera): the bank's refresh starts from the road held, and no moment looks
  empty against a wrong one. Until the whole bank has looked like one picture for
  AGREE_REFRESHES refreshes running (about 32 minutes of empty road), the zone counts short
  while states() says "counting": rendered, 27 of 54 cars missed. A queue moving through the
  window is refused. A queue standing wholly still that long is learnt as road (ASSUMPTION:
  none does).
- The bank learns slowly in traffic. With the real bank (a sample every 10 s) on a 1.5 m zone,
  free flow at 3-4 m/s with random 3-8 m gaps learnt after 330-470 s (the first version, whose
  looser row test did not see the leftover cars in the median it starts from: 130-350 s); free
  flow at 2 m/s with 2-5 m gaps, regular 4.5 m gaps sampled in step with the bank, and
  stop-and-go with 1-3 m gaps did not learn in 20-30 minutes. Hence seed(). The bank does not
  start from the sample most others look empty against, which would learn faster: its samples
  are 10 s apart, and a queue standing for 40 s gives four that agree.
- A car whose centre crosses a lane line while its rear is in the zone changes zones mid-way:
  rendered with the other zone empty, it is counted once; with cars in the other zone it may
  be counted in neither (not rendered). A car straddling two zones carries only its own
  zone's part of itself into its signature (a narrower width, part of its colour).
- After any gap, a car already in a zone is not counted (above), and no gap is reported for
  it, as detect.py reports none. transit starts a new epoch at every gap, whose offset is set
  afresh there, so such a car shifts only that offset. Simulated (the runner on a steady 55-min
  queue, the cars missed in the 45 s after a 30 s outage, 2026-10-26 review): a gap for them
  made no unflagged number closer to the truth at either line, and at B it dropped, flagged or
  thinned 25-61 outputs that were right without it. ASSUMPTION: synthetic crossings, not
  rendered.
- A car seen from behind hides about h_car x d / (h_cam - h_car) of road ahead of it, so gaps
  shrink with distance from the camera. The rendered cars are flat; with the road ahead of each
  bumper painted as part of the car (2026-10-26 audit, stop-and-go), cars were counted with
  1.1 m of road showing between them and missed with 0.5 m. At line A a stopped car hides
  roughly 1.5-2.5 m (ASSUMPTION: pdn-north's pose from its unverified 10.5 x 30 m rectangle,
  typical car shapes), so stopped gaps under about 2.5-3.5 m may read as one car there.
- Not rendered, so not measured: cars with no visible outline or shadow against the road;
  motorbikes (narrower than ROW_SPAN_M), and two side by side (one car); night (headlight
  pools, no colour for the signature); compression noise; zones of one line drawn with unequal
  lengths (no lane-line rule between them).
- A camera with no ground rectangle is read with each zone's own "metres" (cameras.json) or,
  without them, an assumed lane width and square pixels (_zone_metres): a foreshortened zone
  then reads short, and under MIN_ZONE_M is refused. The City's bridgepdn1 zones all carry
  3.5 x 3.5 m (ASSUMPTION: ASSUMED_LANE_M and the rough model's length, not measured), so
  _zone_metres is not used for them. On a camera with a ground rectangle, the rectangle
  measures every zone and a zone's own metres are not used (LoopCounter warns).
"""
from __future__ import annotations

import itertools
import logging
import math
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime

import cv2
import numpy as np

from .calibration import PX_PER_M, Camera
from .crossing import Crossing, Zone, look
from .hls import Gap, Stamped
from .measure import Image, Reference, foreground, image_features
from .reader import EMPTY_MIN_SAMPLES

log = logging.getLogger(__name__)

MARGIN_M = 1.5           # road followed beyond each end of the zone: a rear is seen in more frames
HIGHPASS_M = 0.25        # structure scale: see zone_features()
ROW_SPAN_M = 1.1         # a row holds a car when its foreground spans this far across, anywhere in
                         # the lane: see occupied_rows()
INSIDE_M = 2.6           # the widest clear stretch within one car's row (a 2.55 m bus between its
                         # outlines): foreground further apart is two things: see _things()
EDGE_M = 0.25            # foreground this close to a lane line on both sides of it runs across it
CAR_MAX_M = 3.2          # outer edge to outer edge, the widest one car can read: rendered, a 2.55 m
                         # bus reads 2.9-3.0 m with its outline's spread. ASSUMPTION: two cars side
                         # by side read wider (each 1.6 m or more, apart by 0.1 m or more): see
                         # _across()
HOLD_M = 0.25            # a car centred this close to a lane line stays in the zone that had it
BESIDE_PX = 2.0          # zones whose shared corners are this close (frame_size pixels) are beside
                         # each other: pdn-north's lane-4 and lane-5 overlap by 1.4 px
CLOSE_M = 0.25           # a clear stretch shorter than this between car rows is a missed outline
GAP_M = 0.25             # clear road behind a car before its rear counts as seen
MATCH_M = 0.4            # a rear seen this close to where the motion put it is the same rear
BIRTH_M = 1.0            # car ahead of a newly seen rear, so a flickering bumper is not a new car
MIN_ZONE_M = 1.0         # shortest zone that still has a near end and a far end to tell apart
MAX_SPEED_M_S = 6.0      # fastest queue motion searched for between two frames
FAST_M_S = 12.0          # ASSUMPTION: no car approaches the booths faster (43 km/h)
MIN_OVERLAP = 0.6        # share of the patch the motion search compares between two frames
MOTION_FRAMES = 3        # the motion used to carry rears is the median of this many frames'
MIN_PEAK = 0.55          # weaker correlation than this is not the same cars moving
MIN_STRUCTURE = 0.4      # profile spread (grey levels) with nothing to follow: see departure()
PROFILE_SIGMA_M = 0.25   # smoothing of the profile along the lane before the motion search
LOST_S = 120.0           # a rear hidden this long (its gap closed while the queue stood) is dropped
SEEN_ROW = 0.5           # a patch row counts as seen when half of it is in the frame and unmasked
ASSUMED_LANE_M = 3.5     # ASSUMPTION: a zone's width on a camera with no ground calibration
EMPTY_ROWS = 0.12        # share of the patch's rows that may hold a car in an "empty" sample
SAMPLE_EVERY_S = 10.0    # one reference sample per this much capture time
BANK_SIZE = 36           # samples kept: six minutes of moments to learn the empty road from
BANK_MIN = 12            # two minutes of moments before a first reference is trusted
REFRESH_SAMPLES = 12     # relearn from the bank every two minutes of new samples
AGREE_REFRESHES = 13     # ASSUMPTION: no queue stands wholly still in a zone for BANK_SIZE x
                         # SAMPLE_EVERY_S + AGREE_REFRESHES x REFRESH_SAMPLES x SAMPLE_EVERY_S
                         # (about 32 min). A road learnt wrong, or a road that changed, is replaced
                         # after that long a quiet spell; until then it counts short, silently
MAX_STEP_S = 5.0         # frames further apart than this are a gap even if nobody said so
PAD_PX = 4               # crop margin, so the blur and Sobel kernels see real pixels at the zone
WIDTH_SCALE_M = 3.0      # ASSUMPTION: no one car is wider (a bus is 2.55 m); the signature gives a
                         # car's width as a share of it


@dataclass
class _Geometry:
    """One zone rectified at one frame size: crop pixels -> patch, far end at the top."""
    matrix: np.ndarray
    size: tuple[int, int]          # (columns, rows) of the patch, margins included
    valid: np.ndarray
    zone: slice                    # the zone's own rows within the patch

    def warp(self, image: np.ndarray, interpolation: int = cv2.INTER_LINEAR) -> np.ndarray:
        return cv2.warpPerspective(image, self.matrix, self.size, flags=interpolation,
                                   borderMode=cv2.BORDER_CONSTANT, borderValue=0)


@dataclass
class _Layout:
    crop: tuple[int, int, int, int]                 # x0, y0, x1, y1 in frame pixels
    zones: list[_Geometry]


@dataclass
class _Look:
    """Running colour and size of one car while it is in the zone. In memory only."""
    chroma_x: float = 0.0           # sums of saturation x cos(hue) and x sin(hue), saturation
    chroma_y: float = 0.0           # as a share of its range
    pixels: int = 0
    val: np.ndarray = field(default_factory=lambda: np.zeros(256, np.int64))
    width: float = 0.0
    frames: int = 0

    def add(self, hsv: np.ndarray, width_m: float) -> None:
        h = hsv[:, 0].astype(np.float64) * (math.pi / 90.0)    # OpenCV hue is 0-179, half degrees
        s = hsv[:, 1].astype(np.float64) / 255.0
        self.chroma_x += float((s * np.cos(h)).sum())
        self.chroma_y += float((s * np.sin(h)).sum())
        self.pixels += len(hsv)
        self.val += np.bincount(hsv[:, 2], minlength=256)
        self.width += width_m
        self.frames += 1

    def signature(self) -> tuple[float, ...] | None:
        """(saturation x cos hue, saturation x sin hue, value, width share), each on a 0-1 scale
        (the first two -1 to 1), the form crossing.look() gives; transit.colour_distance compares
        its first three parts.

        The first two are the body's mean colour on the colour wheel: hue as an angle, how
        coloured as the distance from the centre. Grey, white and black cars, whose hue is noise,
        sit together near (0, 0), and a red car reads the same at 359 degrees as at 1. Value is
        the body's median brightness, width the car's mean width over WIDTH_SCALE_M. How long
        the car took to cross is not part of it: that depends on the queue, not on the car, and
        differs between the two lines."""
        if not self.frames or not self.pixels:
            return None
        return look(self.chroma_x / self.pixels, self.chroma_y / self.pixels,
                    _median(self.val) / 255.0, self.width / self.frames / WIDTH_SCALE_M)


@dataclass
class _Rear:
    """The back of one car, in patch rows counted from the patch's near end."""
    pos: float
    from_near: bool                 # came in across the zone's near edge: only these are counted
    counted: bool = False
    step: float = 0.0               # rows it moved last frame, for when the motion is not known
    lost: datetime | None = None    # since when the gap behind it has been closed
    look: _Look = field(default_factory=_Look)


@dataclass
class _Loop:
    zone: Zone
    width_m: float
    length_m: float
    reference: Reference | None = None
    bank: deque = field(default_factory=lambda: deque(maxlen=BANK_SIZE))
    last_sample: datetime | None = None
    new_samples: int = 0
    agreeing: int = 0                        # refreshes running whose whole bank agreed: _sample()
    rears: list[_Rear] = field(default_factory=list)
    last_profile: np.ndarray | None = None   # last frame's structure along the patch
    last_rows: np.ndarray | None = None      # last frame's occupied patch rows, near end first
    moves: deque = field(default_factory=lambda: deque(maxlen=MOTION_FRAMES))


def _median(hist: np.ndarray) -> int:
    return int(np.searchsorted(np.cumsum(hist), (hist.sum() + 1) // 2))


def _zone_metres(camera: Camera, quad: np.ndarray) -> tuple[float, float]:
    """(width, length) of a zone in metres, from the camera's ground rectangle. Without one,
    ASSUMPTION: the zone is one lane ASSUMED_LANE_M wide and its pixels are square in metres,
    so its length is its width scaled by the quad's aspect. That ignores foreshortening, which
    makes a zone seen at a slant read short; the thresholds in metres then mean somewhat more
    road than they say. Calibrating a ground rectangle on the camera removes the assumption, and
    the rectangle then wins over a zone's own metres too (LoopCounter)."""
    def side(a, b):
        return float(np.hypot(*(b - a)))

    if camera.width_m > 0 and camera.length_m > 0:
        plane = np.float32([(0, 0), (camera.width_m, 0), (camera.width_m, camera.length_m),
                            (0, camera.length_m)])
        to_plane = cv2.getPerspectiveTransform(np.float32(camera.ground), plane)
        p = cv2.perspectiveTransform(quad.reshape(1, 4, 2), to_plane)[0]
        return (side(p[0], p[1]) + side(p[3], p[2])) / 2, (side(p[0], p[3]) + side(p[1], p[2])) / 2
    across = (side(quad[0], quad[1]) + side(quad[3], quad[2])) / 2
    along = (side(quad[0], quad[3]) + side(quad[1], quad[2])) / 2
    return ASSUMED_LANE_M, ASSUMED_LANE_M * along / max(across, 1e-6)


def zone_features(geo: _Geometry, image: Image) -> tuple[np.ndarray, np.ndarray]:
    """measure.strip_features with a finer structure scale: grey level minus a HIGHPASS_M blur
    of itself, and edge strength, in patch pixels.

    The queue reader's 1 m blur spreads a car's contrast about a metre onto the road around it.
    Rendered, with the rear-following below, both scales counted queues alike (stop-and-go and
    crawls with 1 m gaps: no miss, no false count), but in free flow the 1 m scale missed 25 of
    80 cars at 6 m/s and 29 of 86 at 8 m/s, against 0 and 2 at a quarter metre. A quarter-metre
    blur still drops light that changes smoothly (a rendered dusk, 60 grey levels darker in a
    minute, counted nothing); it keeps more of a headlight pool's edge than the reader's metre,
    and night is not tested."""
    g = geo.warp(image.grey)
    weight = geo.valid.astype(np.float32)
    sigma = HIGHPASS_M * PX_PER_M
    smooth = cv2.GaussianBlur(g * weight, (0, 0), sigma) / np.maximum(
        cv2.GaussianBlur(weight, (0, 0), sigma), 1e-3)
    return (g - smooth + 128.0) * weight, geo.warp(image.edges)


def occupied_rows(fg: np.ndarray, valid: np.ndarray) -> np.ndarray:
    """Per patch row: True when its foreground spans ROW_SPAN_M or more, anywhere across the
    lane, once lone pixels (no foreground among their eight neighbours) are dropped as noise.

    Everything here is in metres, never in shares of the lane. Across a car's plain body the
    foreground is its two side outlines, 1-2 px each at PX_PER_M whatever the lane's width, so
    a share of the lane's columns is a different test in every lane: the first version asked
    for 8 % of them, and in a 4.75 m lane (38 columns) a body row's 3 px fell short. Cars broke
    into short runs, and a rendered steady flow counted 18-48 of 40 cars depending on their
    width and the frame size. Asking for foreground on both sides of the lane's centre missed
    every car whose near side was past the centre (0 of 40 at 1.0 m off the centre of a 4.25 m
    lane). A lone pixel is dropped instead of asking for a share of pixels: an outline is a line
    of pixels, noise is speckle.

    ROW_SPAN_M sits between a person and half a car: a rendered 0.5 m person spans 0.62 m with
    its outline's spread, a car of 1.6-2.2 m its width plus about 0.2 m. A car across the line
    between two lane zones is judged by both together: see _across()."""
    return _close(_car_rows(_despeckled(fg), valid), round(CLOSE_M * PX_PER_M))


def _despeckled(fg: np.ndarray) -> np.ndarray:
    """The foreground without lone pixels: each kept pixel has another among its 8 neighbours."""
    neighbours = cv2.boxFilter(fg.astype(np.uint8), -1, (3, 3), normalize=False,
                               borderType=cv2.BORDER_CONSTANT)
    return fg & (neighbours >= 2)


def _things(kept: np.ndarray) -> np.ndarray:
    """Per row, the things in it: runs of foreground columns with no clear stretch wider than
    INSIDE_M within them. Returns [widest, leftmost, rightmost] x rows x (first, last) column,
    -1 where a row holds nothing; ties for the widest go to the leftmost.

    One car's row is one thing: across its plain body, its two side outlines lie at most a
    bus's width apart. A lane's two painted edge lines lie further apart, so they are two: when
    a shadow darkened them (rendered: a hard 3 m shadow swept along the lane), the first span
    test read the shadow as a 3 m car between them and counted it 3 times in 10 sweeps."""
    gap = round(INSIDE_M * PX_PER_M)
    out = np.full((3, kept.shape[0], 2), -1, np.int64)
    for r, row in enumerate(kept):
        cols = np.flatnonzero(row)
        if not len(cols):
            continue
        cut = np.flatnonzero(np.diff(cols) > gap + 1)
        firsts = np.concatenate((cols[:1], cols[cut + 1]))
        lasts = np.concatenate((cols[cut], cols[-1:]))
        w = int(np.argmax(lasts - firsts))
        out[:, r] = (firsts[w], lasts[w]), (firsts[0], lasts[0]), (firsts[-1], lasts[-1])
    return out


def _car_rows(kept: np.ndarray, valid: np.ndarray) -> np.ndarray:
    """occupied_rows before its gaps are closed."""
    first, last = _things(kept)[0].T
    seen = valid.sum(axis=1) >= SEEN_ROW * kept.shape[1]
    return (first >= 0) & ((last - first + 1) >= ROW_SPAN_M * PX_PER_M) & seen


def _across(left: np.ndarray, right: np.ndarray, rows_l: np.ndarray, rows_r: np.ndarray,
            seen: np.ndarray, held_l: np.ndarray | None, held_r: np.ndarray | None) -> None:
    """Give a car across the line between two adjacent lane zones to the zone that holds its
    centre, in place: its rows become car rows there and stop being car rows in the other.
    `held_l` / `held_r`: each zone's car rows in the frame before (patch order), or None.

    Judged alone, each zone sees only its side of such a car. Rendered (a 7 m road split into
    two 3.5 m zones, steady flow), that counted a 1.8 m car centred within 0.05 m of the line in
    neither zone about half the time, and a 2.2 m car within 0.15 m of it in both. On pdn-north
    lane-1 to lane-3 are equal thirds of the carriageway, not painted lanes, so cars need not
    keep off their lines. A row holds a car across the line when its foreground runs on across
    it (within EDGE_M on both sides), or when each zone holds only a sliver of it (its side
    outline); either way only when its two sides lie a car's width apart (ROW_SPAN_M to
    CAR_MAX_M, outer edge to outer edge). Two cars side by side, each near the line, meet across
    it too, and read wider: rendered, while the bound held for slivers only, two 1.8 m cars each
    0.1-0.3 m from the line were taken for one, and a lane counted 15-63 of 50. Each run of such
    rows goes whole to one zone, by its median centre, so a car does
    not flicker between the two row by row; and a car within HOLD_M of the line stays with the
    zone that held those rows the frame before, so it does not flicker frame by frame either
    (its centre is read to half a pixel: rendered, a car 0.05 m off the line changed zones from
    frame to frame, and its rear was lost in both)."""
    cl = left.shape[1]
    edge = max(1, round(EDGE_M * PX_PER_M))
    first_l, last_l = _things(left)[2].T                # the thing nearest the line on each side
    first_r, last_r = _things(right)[1].T
    wide = ROW_SPAN_M * PX_PER_M
    outer = (cl - first_l) + (last_r + 1)               # px from its left side to its right side
    joined = (last_l >= cl - edge) & (first_r >= 0) & (first_r < edge)
    sliver = ((first_l >= 0) & (first_r >= 0) & (last_l - first_l + 1 < wide)
              & (last_r - first_r + 1 < wide))
    across = (joined | sliver) & (outer >= wide) & (outer <= CAR_MAX_M * PX_PER_M) & seen
    centre = ((last_r + 1) - (cl - first_l)) / 2.0      # px right of the line
    for a, b in _runs(across):
        c = float(np.median(centre[a:b + 1]))
        had_l = held_l is not None and bool(held_l[a:b + 1].any())
        had_r = held_r is not None and bool(held_r[a:b + 1].any())
        if abs(c) <= HOLD_M * PX_PER_M and had_l != had_r:
            c = 1.0 if had_r else -1.0
        rows_r[a:b + 1], rows_l[a:b + 1] = c >= 0, c < 0


def _close(rows: np.ndarray, rows_max: int) -> np.ndarray:
    """Fill clear stretches shorter than `rows_max` rows that have a car row on both sides."""
    out = rows.copy()
    idx = np.flatnonzero(rows)
    for a, b in itertools.pairwise(idx):
        if 1 < b - a <= rows_max:
            out[a + 1:b] = True
    return out


def _runs(occupied: np.ndarray) -> list[tuple[int, int]]:
    """Occupied runs as (first, last) row, in order."""
    edges = np.diff(np.concatenate(([0], occupied.astype(np.int8), [0])))
    return [(int(a), int(b)) for a, b in zip(np.flatnonzero(edges == 1),
                                              np.flatnonzero(edges == -1) - 1)]


def departure(g: np.ndarray, ref: Reference, valid: np.ndarray) -> np.ndarray:
    """Each patch row's mean absolute departure of structure from the empty road.

    measure.profile averages the signed departure, which suits its 1 m structure: there a car
    body stands out as a whole. At this module's quarter-metre scale a car is its outline, whose
    two sides depart in opposite directions and cancel in a signed mean (rendered: a profile
    standard deviation of ~1 grey level with a car in view). The absolute departure keeps them."""
    diff = np.where(valid, np.abs(g - ref.image), 0.0)
    counts = valid.sum(axis=1)
    rows = np.divide(diff.sum(axis=1), counts, out=np.zeros(len(counts)), where=counts > 0)
    return cv2.GaussianBlur(rows.reshape(-1, 1), (1, 0), PROFILE_SIGMA_M * PX_PER_M).ravel()


def displacement(cur: np.ndarray, prev: np.ndarray, step_s: float) -> float | None:
    """Rows the patch content moved toward its far end between two frames, or None.

    One shift for the whole patch, and a stopped queue reads 0. Queued cars do not all move
    together: each starts a second or two after the one ahead, so a car pulling away from one
    still standing behind it moves further than this says (_track allows for it).
    Normalised correlation over integer shifts, refined to a fraction of a row by a parabola
    through the peak. None when either frame is flat (nothing to follow) or nothing matches."""
    n = len(cur)
    k = min(math.ceil(MAX_SPEED_M_S * step_s * PX_PER_M), math.floor(n * (1 - MIN_OVERLAP)))
    scores = {}
    for d in range(-1, k + 1):
        a, b = (cur[:n - d], prev[d:]) if d >= 0 else (cur[-d:], prev[:n + d])
        if a.std() < MIN_STRUCTURE or b.std() < MIN_STRUCTURE:
            continue
        scores[d] = float(np.corrcoef(a, b)[0, 1])
    if not scores:
        return None
    best = max(scores, key=lambda d: (scores[d], -abs(d)))      # ties go to the smaller shift
    if scores[best] < MIN_PEAK:
        return None
    lo, hi = scores.get(best - 1), scores.get(best + 1)
    if lo is not None and hi is not None and lo - 2 * scores[best] + hi < 0:
        return best + 0.5 * (lo - hi) / (lo - 2 * scores[best] + hi)
    return float(best)


class LoopCounter:
    """A virtual loop on every count zone of one camera. Implements crossing.Counter."""

    method = "loop"

    def __init__(self, camera: Camera, zones: tuple[Zone, ...] | None = None, *,
                 metres: dict[tuple[str, str], tuple[float, float]] | None = None,
                 sample_every_s: float = SAMPLE_EVERY_S, bank_min: int = BANK_MIN):
        """`metres`: (width, length) of a zone, by (line, lane), measured some other way (a map,
        a tape); it comes first. Otherwise a camera's ground rectangle measures the zone, and
        only on a camera with none do the zone's own `metres` (cameras.json) count: they are an
        ASSUMPTION a calibration replaces, so on a calibrated camera they are not used and a
        warning says so. Otherwise see _zone_metres().
        `sample_every_s` and `bank_min` exist so tests can learn the road in seconds."""
        zones = camera.count_zones if zones is None else zones
        if not zones:
            raise ValueError(f"{camera.id}: no count zones")
        self.camera = camera
        self.sample_every_s, self.bank_min = sample_every_s, bank_min
        self._loops: list[_Loop] = []
        for z in zones:
            if z.camera != camera.id:
                raise ValueError(f"zone {z.line}/{z.lane} belongs to {z.camera}, not {camera.id}")
            calibrated = camera.width_m > 0 and camera.length_m > 0     # as _zone_metres
            if z.metres and calibrated:
                log.warning("%s zone %s/%s: its metres are not used, the camera's ground "
                            "rectangle measures it", camera.id, z.line, z.lane)
            given = (metres or {}).get((z.line, z.lane)) or (None if calibrated else z.metres)
            width_m, length_m = given or _zone_metres(camera, np.float32(z.quad))
            if length_m < MIN_ZONE_M:
                raise ValueError(f"{camera.id} zone {z.line}/{z.lane}: {length_m:.2f} m is too "
                                 f"short to see a car come in and go out"
                                 + ("" if given or camera.width_m > 0 else
                                    " (no ground rectangle: the length assumes square pixels;"
                                    " calibrate the camera or give the zone's metres)"))
            self._loops.append(_Loop(z, width_m, length_m))
        self._beside = _beside(self._loops)
        self._layouts: dict[tuple[int, int], _Layout] = {}
        self._last_at: datetime | None = None
        self._gaps: list[Gap] = []
        self._layout(camera.frame_size)            # a zone off the picture fails here, not later

    def states(self) -> dict[tuple[str, str], str]:
        """(line, lane) -> "learning" until the zone's empty road is known, then "counting".
        A learning zone counts nothing: its line's total is short for as long as it learns, so
        the caller must treat that time as a gap at the line, never as no cars."""
        return {(lp.zone.line, lp.zone.lane): "learning" if lp.reference is None else "counting"
                for lp in self._loops}

    def references(self) -> dict[tuple[str, str], tuple[np.ndarray, np.ndarray]]:
        """(line, lane) -> the zone's learnt empty road, as (grey structure, edge strength)
        arrays of its patch, copied. For a caller that keeps them across restarts and hands
        them back with seed(): learnt in memory alone, a restart in a queue means "learning"
        until the lane next clears. Each is a median of near-empty moments at PX_PER_M, the
        same kind of record as reader.CameraReader's strip references; no frame is kept."""
        return {(lp.zone.line, lp.zone.lane): (lp.reference.image.copy(),
                                                lp.reference.edges.copy())
                for lp in self._loops if lp.reference is not None}

    def seed(self, references: dict[tuple[str, str], tuple[np.ndarray, np.ndarray]]
             ) -> list[tuple[str, str]]:
        """Take roads learnt earlier (references()) and return the zones still learning. A road
        whose shape is not its zone's patch any more (the zone's metres, or the camera's ground
        rectangle, changed since) is refused. Only the shape is checked, as reader.py checks a
        strip reference: a zone redrawn elsewhere with the same metres takes the old road
        (pdn-north's lane-1 to lane-3 share one patch shape), so the caller must keep each road
        with its zone's quad and the camera's geometry and drop it when those changed. The
        counter's own bank keeps refreshing a seeded road like one it learnt."""
        for lp, geo in zip(self._loops, self._layout(self.camera.frame_size).zones):
            given = references.get((lp.zone.line, lp.zone.lane))
            if given is None:
                continue
            grey, edges = (np.asarray(x, np.float32) for x in given)
            shape = geo.size[::-1]                         # (rows, columns)
            if (grey.shape == shape and edges.shape == shape and np.isfinite(grey).all()
                    and np.isfinite(edges).all()):
                lp.reference = Reference(grey.copy(), edges.copy())
                lp.new_samples = lp.agreeing = 0
        return [key for key, state in self.states().items() if state == "learning"]

    def gap(self, gap: Gap) -> None:
        """Missed frames: forget every car in flight. The learnt road stays: it does not move."""
        self._forget()
        self._last_at = None

    def gaps(self) -> list[Gap]:
        """The gaps this counter found since the last call, and cleared: frames more than
        MAX_STEP_S apart, or a stamp that did not move forward, where every car in flight was
        forgotten as at a gap the reader reports. They belong with the reader's gaps for this
        line (transit.measure's gaps_a / gaps_b)."""
        out, self._gaps = self._gaps, []
        return out

    def add(self, frame: Stamped) -> list[Crossing]:
        """One frame's crossings.

        Frames come in capture order: hls and the runner drop a frame stamped no later than
        the last. A stamp that does not move forward, or frames more than MAX_STEP_S apart,
        forget the cars in flight and are recorded as a gap (gaps()), as detect.py does."""
        h, w = frame.image.shape[:2]
        layout = self._layout((w, h))
        step = None
        if self._last_at is not None:
            step = (frame.at - self._last_at).total_seconds()
            if not 0 < step <= MAX_STEP_S:
                self._forget()
                self._gaps.append(Gap(self._last_at, max(step, 0.0),
                                      f"frames {step:+.1f} s apart"))
                step = None
        self._last_at = frame.at
        x0, y0, x1, y1 = layout.crop
        crop = frame.image[y0:y1, x0:x1]
        image = image_features(crop)
        seen: dict[int, tuple] = {}            # zone index -> (g, e, fg, kept, car rows)
        for i, (lp, geo) in enumerate(zip(self._loops, layout.zones)):
            g, e = zone_features(geo, image)
            self._sample(lp, g, e, geo.valid, frame.at)
            if lp.reference is not None:
                fg = foreground(g, e, lp.reference, geo.valid)
                kept = _despeckled(fg)
                seen[i] = (g, e, fg, kept, _car_rows(kept, geo.valid))
        for i, j in self._beside:
            if i in seen and j in seen:
                both = (layout.zones[i].valid.mean(axis=1) >= SEEN_ROW) & (
                    layout.zones[j].valid.mean(axis=1) >= SEEN_ROW)
                held = [None if lp.last_rows is None else lp.last_rows[::-1]
                        for lp in (self._loops[i], self._loops[j])]
                _across(seen[i][3], seen[j][3], seen[i][4], seen[j][4], both, *held)
        out = []
        for i, (g, e, fg, _, car_rows) in seen.items():
            lp, geo = self._loops[i], layout.zones[i]
            rows = _close(car_rows, round(CLOSE_M * PX_PER_M))
            if not rows.any():
                lp.reference.update(g, e, geo.valid & ~fg)      # follow the light on an empty zone
            along = departure(g, lp.reference, geo.valid)
            moved = None
            if step is not None and lp.last_profile is not None:
                moved = displacement(along, lp.last_profile, step)
            occ = rows[::-1]                                    # near end first
            colour = None
            if crop.ndim == 3 and occ[_zone(geo)].any():
                colour = cv2.cvtColor(geo.warp(crop), cv2.COLOR_BGR2HSV)[::-1]
            out += self._track(lp, geo, occ, moved, step, frame.at, fg[::-1], colour)
            lp.last_profile, lp.last_rows = along, occ
        return out

    def _track(self, lp: _Loop, geo: _Geometry, occ: np.ndarray, moved: float | None,
               step: float | None, at: datetime, fg: np.ndarray,
               colour: np.ndarray | None) -> list[Crossing]:
        """Follow the rears of the cars through the patch; count each once, as it crosses the
        zone's far edge. Everything here is near end first: row 0 is the patch's near end.

        A rear is the near end of a run of car rows with seen road behind it. Each frame every
        known rear is carried on by the patch's displacement and matched to a rear seen within
        MATCH_M of there (twice that ahead of it). A known rear not seen again has left when it
        was carried past the patch's far end, or when the patch ahead of it is clear and nothing
        says it stood still; it vanished, uncounted, when the patch ahead is clear but the
        motion says it cannot have reached the far edge. Otherwise the gap behind it has closed,
        and it is carried along hidden until it shows again or leaves. A rear first seen behind
        every known one, at or within one frame's motion of the zone's near edge, came in across
        that edge: only those are counted."""
        n = len(occ)
        zone = _zone(geo)
        near_edge, far_edge = zone.start, zone.stop
        seen_rows = geo.valid[::-1].mean(axis=1) >= SEEN_ROW
        tol = MATCH_M * PX_PER_M
        gap_rows = round(GAP_M * PX_PER_M)
        birth_rows = min(BIRTH_M * PX_PER_M, (far_edge - near_edge) / 2)
        lp.moves.append(None if moved is None else max(0.0, moved))
        known = [m for m in lp.moves if m is not None]
        move = float(np.median(known)) if known else None      # one bad correlation is outvoted
        raw = lp.moves[-1]
        seen, behind_end = [], -1
        for a, b in _runs(occ):
            if a - behind_end - 1 >= gap_rows and seen_rows[a - gap_rows:a].all():
                seen.append((a, b))
            behind_end = b
        predicted = [r.pos + (move if move is not None else (0.0 if r.lost else r.step))
                     for r in lp.rears]
        # Where a rear may be now: carried by the steady motion, by this frame's own (a start
        # from standing shows here a frame before the median follows), by its own last step,
        # or standing. The nearest seen rear within MATCH_M of any of them is the same rear;
        # within twice that when it is ahead of all of them, as a car pulling away from one
        # still standing behind it outruns the patch's one shift (rendered, staggered starts
        # lost 230 of 1522 cars without this), or when it was lost.
        guesses = [{p, r.pos + (raw if raw is not None else r.step), r.pos + r.step, r.pos}
                   for r, p in zip(lp.rears, predicted)]
        matched: dict[int, tuple[int, int]] = {}
        for a, b in sorted(seen, key=lambda run: -run[0]):
            best = None
            for i, r in enumerate(lp.rears):
                off = min(abs(a - q) for q in guesses[i])
                loose = r.lost or a > max(guesses[i])
                if i not in matched and off <= tol * (2 if loose else 1) and (
                        best is None or off < best[0]):
                    best = (off, i)
            if best is not None:
                matched[best[1]] = (a, b)
        if move is None and step:
            # Nothing to correlate (the patch empty in one of the two frames): free-flowing
            # traffic, a car or two in view. A rear not found where it was may have gone on as
            # far as FAST_M_S allows; the nearest unclaimed rear ahead of it is it.
            reach = FAST_M_S * step * PX_PER_M
            free = sorted(set(seen) - set(matched.values()))
            ahead_limit = math.inf
            unmatched = sorted(set(range(len(lp.rears))) - set(matched),
                               key=lambda i: -lp.rears[i].pos)
            for i in unmatched:
                r = lp.rears[i]
                ahead = [(a, b) for a, b in free
                         if r.pos + tol < a <= r.pos + reach and a < ahead_limit]
                if ahead:
                    matched[i] = ahead[0]
                    free.remove(ahead[0])
                    ahead_limit = ahead[0][0]
        live: list[tuple[_Rear, tuple[int, int] | None]] = []
        gone: list[_Rear] = []
        for i, (r, p) in enumerate(zip(lp.rears, predicted)):
            if i in matched:
                a, b = matched[i]
                r.step, r.pos, r.lost = a - r.pos, float(a), None
                live.append((r, (a, b)))
            elif p >= n - 1 - tol:
                r.pos = p
                gone.append(r)                                  # carried out of the patch
            elif not occ[max(0, math.ceil(p)):].any():
                if move is None or p >= far_edge:
                    r.pos = max(p, float(far_edge))             # left faster than was followed
                    gone.append(r)
            else:
                r.pos, r.step = p, (move if move is not None else 0.0)
                r.lost = r.lost or at
                if (at - r.lost).total_seconds() <= LOST_S:
                    live.append((r, None))
        taken = set(matched.values())
        nearest = min((r.pos for r, _ in live), default=math.inf)
        # With no motion to go by, a car that was nowhere in the patch, or was just coming in
        # at its near end with its rear still behind it, came in across the near edge too,
        # however far on its rear is first seen (free flow moves 4-5 m between frames). One
        # that was nowhere in the patch has its rear short of the far edge: past it, it would
        # have gone the near margin, the zone and its own length in one frame, 6.5 m or more for
        # a 3.5 m car in line A's 1.5 m zone, over FAST_M_S at reader.FPS. A rear first seen
        # there is a car changing lanes into the zone, counted in its own (rendered: counted in
        # both in 11 of 36 lane changes, at 1-3 m/s, its centre crossing within 1.2 m of it).
        empty = lp.last_rows is not None and not lp.last_rows.any()
        entering = lp.last_rows is not None and move is None and bool(lp.last_rows[0])
        reach = near_edge + (move or 0.0) + tol + gap_rows
        for a, b in sorted(seen):
            if (a, b) in taken:
                continue
            long = b - a + 1 >= birth_rows               # a car's length ahead of it, not a bumper
            near = a <= reach and (long or b == n - 1)
            unfollowed = entering or empty and a < far_edge
            from_near = a < nearest - tol and (near or (unfollowed and long))
            live.append((_Rear(float(a), from_near), (a, b)))
            nearest = min(nearest, a)
        out = []
        for r, run in live + [(r, None) for r in gone]:
            if r.from_near and colour is not None and run is not None:
                a, b = max(run[0], near_edge), min(run[1], far_edge - 1)
                if a <= b:
                    _look(r.look, fg[a:b + 1], colour[a:b + 1])
            if r.from_near and not r.counted and r.pos >= far_edge:
                r.counted = True
                out.append(Crossing(self.camera.id, lp.zone.line, lp.zone.lane, at, self.method,
                                    r.look.signature()))
        lp.rears = sorted((r for r, _ in live), key=lambda r: r.pos)
        return out

    def _forget(self) -> None:
        """Forget every car in flight."""
        for lp in self._loops:
            lp.rears, lp.last_profile, lp.last_rows = [], None, None
            lp.moves.clear()

    def _sample(self, lp: _Loop, g: np.ndarray, e: np.ndarray, valid: np.ndarray,
                at: datetime) -> None:
        since = None if lp.last_sample is None else (at - lp.last_sample).total_seconds()
        if since is not None and since < self.sample_every_s:
            return
        lp.bank.append((g, e))
        lp.last_sample = at
        lp.new_samples += 1
        if lp.reference is None:
            if len(lp.bank) >= self.bank_min:
                lp.reference = emptiest(list(lp.bank), valid, None)
                lp.new_samples = 0
        elif lp.new_samples >= REFRESH_SAMPLES:
            # The refresh starts from the road held, so a wrong one (a queue learnt as road, a
            # road changed for good) is never replaced from it: no moment looks empty against
            # it. A full bank in which every moment looks empty against the bank's own road,
            # for AGREE_REFRESHES refreshes running, replaces it.
            bank = list(lp.bank)
            fresh = emptiest(bank, valid, lp.reference)
            whole = None
            if fresh is None and len(bank) == BANK_SIZE:
                whole = emptiest(bank, valid, None)
                if whole is not None and len(_empty(bank, whole, valid)[1]) < len(bank):
                    whole = None
            lp.agreeing = lp.agreeing + 1 if whole is not None else 0
            if lp.agreeing >= AGREE_REFRESHES:
                fresh, lp.agreeing = whole, 0
            if fresh is not None:
                lp.reference = fresh
            lp.new_samples = 0

    def _layout(self, size: tuple[int, int]) -> _Layout:
        """Each zone's patch at one frame size, and the crop of the frame they all lie in."""
        if size in self._layouts:
            return self._layouts[size]
        sx, sy = size[0] / self.camera.frame_size[0], size[1] / self.camera.frame_size[1]
        margin = round(MARGIN_M * PX_PER_M)
        parts, corners = [], []
        for lp in self._loops:
            quad = np.float32([(x * sx, y * sy) for x, y in lp.zone.quad])
            cols = max(8, round(lp.width_m * PX_PER_M))
            rows = max(1, round(lp.length_m * PX_PER_M))
            rect = np.float32([(0, margin + rows), (cols, margin + rows), (cols, margin),
                               (0, margin)])
            to_patch = cv2.getPerspectiveTransform(quad, rect).astype(np.float64)
            patch = (cols, rows + 2 * margin)
            corners.append(_visible_corners(to_patch, patch, quad))
            parts.append((to_patch, patch, slice(margin, margin + rows)))
        pts = np.concatenate(corners)
        x0 = int(max(0, math.floor(pts[:, 0].min()) - PAD_PX))
        y0 = int(max(0, math.floor(pts[:, 1].min()) - PAD_PX))
        x1 = int(min(size[0], math.ceil(pts[:, 0].max()) + PAD_PX + 1))
        y1 = int(min(size[1], math.ceil(pts[:, 1].max()) + PAD_PX + 1))
        if x1 - x0 < 2 or y1 - y0 < 2:
            raise ValueError(f"{self.camera.id}: count zones are outside a {size} frame")
        seen = np.full((y1 - y0, x1 - x0), 255, np.uint8)
        for x, y, w, h in self.camera.mask:
            seen[max(0, round(y * sy) - y0):max(0, round((y + h) * sy) - y0),
                 max(0, round(x * sx) - x0):max(0, round((x + w) * sx) - x0)] = 0
        shift = np.array([[1, 0, x0], [0, 1, y0], [0, 0, 1]], np.float64)
        geometries = []
        ends = math.ceil(BIRTH_M * PX_PER_M / 2)
        for lp, (to_patch, patch, zone_rows) in zip(self._loops, parts):
            geo = _Geometry(to_patch @ shift, patch, np.zeros(0, bool), zone_rows)
            geo.valid = (geo.warp(seen, cv2.INTER_NEAREST) > 0) & _in_front(to_patch, patch)
            rows_seen = geo.valid[zone_rows].mean(axis=1) >= SEEN_ROW
            if not (rows_seen[:ends].all() and rows_seen[-ends:].all()):
                raise ValueError(f"{self.camera.id} zone {lp.zone.line}/{lp.zone.lane}: its ends "
                                 f"are not in view at {size} (outside the frame or masked)")
            geometries.append(geo)
        self._layouts[size] = _Layout((x0, y0, x1, y1), geometries)
        return self._layouts[size]


def _beside(loops: list[_Loop]) -> list[tuple[int, int]]:
    """(left, right) zone indexes of the zones of one line that share a side: the left one's
    near-right and far-right corners are the right one's near-left and far-left, within
    BESIDE_PX. ASSUMPTION: zones drawn that way, with the same length, cover the same stretch
    of road, so their patches' rows line up (pdn-north's line A is drawn so; the City's
    col-1..col-4 share sides and pair only because cameras.json gives them equal metres,
    ASSUMPTION, not measured). Zones of unequal length are left to judge cars alone."""
    pairs = []
    for i, a in enumerate(loops):
        for j, b in enumerate(loops):
            qa, qb = np.float32(a.zone.quad), np.float32(b.zone.quad)
            if (i != j and a.zone.line == b.zone.line
                    and round(a.length_m * PX_PER_M) == round(b.length_m * PX_PER_M)
                    and np.hypot(*(qa[1] - qb[0])) <= BESIDE_PX
                    and np.hypot(*(qa[2] - qb[3])) <= BESIDE_PX):
                pairs.append((i, j))
    return pairs


def _zone(geo: _Geometry) -> slice:
    """The zone's rows in near-end-first order (the patch itself has the far end at the top)."""
    rows = geo.size[1]
    return slice(rows - geo.zone.stop, rows - geo.zone.start)


def emptiest(samples: list[tuple[np.ndarray, np.ndarray]], valid: np.ndarray,
             current: Reference | None) -> Reference | None:
    """reader.emptiest_reference, with "empty" judged the way the counter judges a car, and no
    fallback when nothing is held.

    The same three rounds: start from the median (or the reference held), keep the samples that
    are nearly empty against it, take their median, row by row from the samples in which the
    row was clear (_clear_median). A sample is empty here when at most
    EMPTY_ROWS of its rows hold a car (occupied_rows), not when few of its pixels differ: a
    rendered standing queue holds a car in 94 % of the patch's rows but differs in only 26 % of
    its pixels, the outline being most of what differs at this structure scale. With nothing
    held, reader.emptiest_reference falls back to the least busy samples; rendered, a lane
    crawling with 1 m gaps from the first frame was learnt that way, from samples that all
    held cars. Here that returns None and the zone stays "learning" until EMPTY_MIN_SAMPLES
    near-empty moments are in the bank. None also means "keep what is held" on a refresh."""
    if not samples:
        return None
    ref = current or Reference.from_strips(samples)
    for _ in range(3):
        kept, empty = _empty(samples, ref, valid)
        if len(empty) < EMPTY_MIN_SAMPLES:
            return None
        ref = _clear_median([samples[k] for k in empty], [kept[k].any(axis=1) for k in empty],
                            current)
    return ref


def _empty(samples: list[tuple[np.ndarray, np.ndarray]], ref: Reference,
           valid: np.ndarray) -> tuple[list[np.ndarray], list[int]]:
    """Each sample's foreground against `ref`, lone pixels dropped, and the indexes of the
    samples nearly empty against it: at most EMPTY_ROWS of their rows hold a car."""
    fgs = [foreground(g, e, ref, valid) for g, e in samples]
    return ([_despeckled(fg) for fg in fgs],
            [k for k, fg in enumerate(fgs) if occupied_rows(fg, valid).mean() <= EMPTY_ROWS])


def _clear_median(samples: list[tuple[np.ndarray, np.ndarray]], busy: list[np.ndarray],
                  held: Reference | None) -> Reference:
    """Reference.from_strips over near-empty samples, each patch row taken only from the
    samples in which it, and GAP_M around it, held no foreground at all (`busy`: per sample,
    the rows that held some). A row clear in fewer than EMPTY_MIN_SAMPLES of them keeps the
    reference `held`; with none held (a first learning), it is taken from all of them.

    "Near-empty" lets a car into EMPTY_ROWS of a sample's rows, and lets through anything
    narrower than a car. A whole-sample median then learnt as road whatever the near-empty
    samples kept catching in the same rows. Rendered: a steady flow whose period matched the
    sampling put a car's front in the patch's first rows of every near-empty sample, and a
    1.5 m zone then read 3-6 false counts (and up to 3 missed) in 33 cars; a car straddling two
    lane zones left its side outline, too narrow to be a car in either, in every sample it was
    in, the refreshed road learnt it, and about half of those cars were lost."""
    pad = round(GAP_M * PX_PER_M)
    clear = ~np.stack([np.convolve(rows, np.ones(2 * pad + 1), "same") > 0 for rows in busy])
    enough = clear.sum(axis=0) >= EMPTY_MIN_SAMPLES
    keep = np.where(enough[None, :], clear, True)[:, :, None]
    out = []
    for stack, kept in ((np.stack([g for g, _ in samples]), None if held is None else held.image),
                        (np.stack([e for _, e in samples]), None if held is None else held.edges)):
        median = np.nanmedian(np.where(keep, stack.astype(np.float32), np.nan), axis=0)
        out.append(median if kept is None else np.where(enough[:, None], median, kept))
    return Reference(*out)


def _look(look: _Look, fg: np.ndarray, colour: np.ndarray) -> None:
    """Add one frame's view of a car to its signature: in each of its rows of the zone, the
    pixels between its outermost foreground columns, which is the car's body between its sides.
    The foreground alone is mostly outline and windscreen edges, dark whatever the paint."""
    has = fg.any(axis=1)
    if not has.any():
        return
    cols = np.arange(fg.shape[1])
    first = fg.argmax(axis=1)
    last = fg.shape[1] - 1 - fg[:, ::-1].argmax(axis=1)
    body = has[:, None] & (cols >= first[:, None]) & (cols <= last[:, None])
    look.add(colour[body], float((last - first + 1)[has].mean()) / PX_PER_M)


def _visible_corners(to_patch: np.ndarray, size: tuple[int, int], quad: np.ndarray) -> np.ndarray:
    """Image points bounding a patch: its corners in front of the camera, and the zone itself."""
    w, h = size
    inverse = np.linalg.inv(to_patch)
    front = _front_sign(inverse, size)
    pts = [quad]
    for c in ((0, 0), (w, 0), (w, h), (0, h)):
        p = inverse @ (c[0], c[1], 1.0)
        if np.sign(p[2]) == front and abs(p[2]) > 1e-9:
            pts.append(np.float32([(p[0] / p[2], p[1] / p[2])]))
    return np.concatenate(pts)


def _front_sign(inverse: np.ndarray, size: tuple[int, int]) -> float:
    """The homogeneous depth sign of the patch's middle, which is on the road by definition."""
    return float(np.sign(inverse[2] @ (size[0] / 2, size[1] / 2, 1.0)))


def _in_front(to_patch: np.ndarray, size: tuple[int, int]) -> np.ndarray:
    """Patch pixels on the camera's side of the horizon (calibration.Strip does the same)."""
    inverse = np.linalg.inv(to_patch)
    cols, rows = np.meshgrid(np.arange(size[0]), np.arange(size[1]))
    depth = inverse[2, 0] * cols + inverse[2, 1] * rows + inverse[2, 2]
    return np.sign(depth) == _front_sign(inverse, size)
