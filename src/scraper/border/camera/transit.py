"""Crossing time from the running counts at two lines (ADR-0014).

Every car that crosses line A (FPFCH `pdn-north`, the Mexican half of Paso del Norte) later
crosses line B (City `bridgepdn1`, the US half). Draw each line's running count against time: the
horizontal gap between the two curves is the transit, and the vertical gap is the cars between
the lines. Put as indices: the k-th car to cross A after the start is the (offset + k)-th to cross
B, where `offset` is the cars already between the lines at the start. No car has to be recognised
for this, which matters here. Cars are about 30-38 px wide at line A as it is read, at 854x480
(detect.py; nominal scale, ASSUMPTION), about 3 in 4 are white, grey, black or silver, and
published re-identification across viewpoints manages 34-68 % even at full resolution
(ADR-0014, Context).

ASSUMPTION: the stretch from A to B is first-in-first-out with no entries or exits. Nobody joins
or leaves between the cameras, and nobody moves between lane types. Overtaking breaks "the k-th
car" but not the curves: the area between them is still the sum of every car's time between the
lines, so the mean transit holds, and the median holds while the order only changes locally.
Anchors tolerate less reordering than that (below).

The pairing holds for one lane type at a time (`of_lane_type`). The runner measures each lane
type that zones at both lines carry on its own (at PDN, SENTRI and normal: QUESTIONS.md Q-22),
and pools every lane as "mixed" only when the lines share no type. What follows is that pooled
case. Pooled lane types whose trips differ are paired across types, and p10-p90
is not the spread of trips. Given the true count, a window's median is near the flow-weighted
mean of their trips, the trip of none of them: half the cars at 7 min and half at 45 gave
medians of 23.5-29.1 min, each within 2.5 min of its window's mean trip. An anchored offset
follows the type that supplies most enclosed anchors, and enclosure favours long trips: over the
runner's 2 x hi_min a trip must span the epoch's middle. A quarter at 12 min and the rest at 45
gave medians of 41-45 min and `between` 11-42 cars high, unflagged, even where 20 of 28 anchors
were 12-min cars, none of them enclosed (offset 247 against 204). Which type wins still varies:
over 12 seeds with the 12-min cars 4 times as likely distinctive, the offset followed the 45-min
type in 4 (42-56 cars high) and the 12-min type in 3 (140-160 low, window medians 7-11 min),
unflagged each time; 5 came out few-anchors. With fewer than MIN_ANCHORS
enclosed, the offset follows the type with the most anchors and is flagged "few-anchors"
(simulations, 2026-10-26).

Three things keep the counts honest.
- Anchors. A few cars look rare enough (a yellow car among the greys) to be matched between the
  lines with some certainty. Each gives the offset directly, as its index at B minus its index at
  A. A pairing with a look-alike is only outvoted where the true partner is in view too. Near an
  epoch's edges it is not: every B car of the first transit belongs to an A car from before the
  epoch, every A car of the last transit leaves after it, and both pair with cars of the same
  colour inside it. In an epoch not much longer than one transit those are all the anchors there
  are. Review on 2026-10-24 found 5-27 of them in 40-80 min epochs, every one a look-alike, and
  they set offsets about 200 cars out (transits about 30 min out), unflagged. So an anchor counts
  as evidence on its own only when its epoch encloses it: its A crossing more than hi_min before
  the epoch's end and its B crossing at least hi_min after its start. Whichever car each one
  really is, its partner then crossed inside the epoch, and the true pairing competed with the
  look-alike for a place in the chain. ASSUMPTION: true anchors agree with their own trend line
  to a car or two. That holds only while cars change places little between A and B, since each
  anchor's offset is the true one plus the places its car gained or lost. Simulated at 8 cars/min
  with 45-min trips and exits jittered per car or per lane (2026-10-26), a fresh measurement
  anchored in 18 of 18 runs where the order changed by up to about 5 places (sd of B place minus
  A place), in 4 of 15 at 5-9 (up to 6 cars out where it did), and in 0 of 12 at 10 or more. The
  rest came out "few-anchors", flagged, 0-9 cars from the truth. How far cars reorder at PDN is
  not measured. A chain of look-alikes does not agree: where the true partners cannot match at
  all (a trip longer than hi_min, or a camera at B that renders colour its own way), it scattered
  by 100 cars and more in review runs (2026-10-26), and was taken as sure. So the epoch's offset
  is set by at least MIN_ANCHORS enclosed anchors within OUTLIER_CARS of the enclosed anchors'
  line, and only when they are at least half of the enclosed ones; the other anchors near that
  line are kept too. Without them, an epoch's anchors can only confirm an offset carried into it,
  never set one unflagged. Where they confirm it, the cars' offsets rest on the anchors that agree
  with it and with each other, and on the others only near those anchors' line: near the epoch's
  edges the others are look-alikes, and a line fitted to all of them tilted (EdgeLookAlikeTest).
- Drift. A counter that misses or double counts drifts. If A counts a share bias_A too many and B
  a share bias_B, a transit read with a fixed offset is off by about
  bias_A x (minutes since the start, at A) - bias_B x (minutes since the start, at B). Once the
  elapsed time is long against the transit, that is the ADR's (bias_A - bias_B) x elapsed. So
  `measure` fits a robust trend line to the anchors' offsets, keeps the anchors near it, and
  gives each car the median offset of those kept anchors near it in time (the line where there
  are too few). The line's slope is the drift indicator; it estimates bias_A - bias_B.
- Gaps. When either reader misses stream time (hls.Gap), the cars that crossed in it are
  unknown, so neither count is continued across it. The run is cut into epochs at every gap at
  either line, and no transit spans a gap. The count is carried across: the cars seen, plus each
  line's rate over the seen part of the RATE_WINDOW_MIN before the gap times the time it did not
  see. That estimate is good to about sqrt(n) for n estimated cars (Poisson), and the
  uncertainty adds up from gap to gap. The new epoch's anchors then confirm the carried offset
  ("anchors"), or dispute it ("disputed": enclosed anchors that disagree, used but flagged), or
  cannot speak to it, and the carried offset is used, flagged ("carried").

The signatures' TTL. ADR-0014 decision 4 keeps a signature for at most SIGNATURE_TTL_S (3 h), and
`measure` finds every epoch's anchors afresh from signatures each time it runs. Run over a longer
stretch with the older signatures dropped, its early epochs would lose their anchors and with
them their transits (review, 2026-10-24: 162 transits and 13 windows changed at 6 h). So a live
caller measures over a horizon no longer than the TTL and starts each horizon from the last:
`prior_at(previous, at)` gives the cars between the lines at `at` (none past the end of what was
measured), a count with no per-vehicle data, and `measure(..., prior=...)` continues from it,
its own anchors checking it. A sure count stays sure for CHECK_MAX_MIN after anchors (as the
last of them crossed B) or a given count last set it, and no longer: a count's error grows with
every car counted (Drift above), and nothing else would flag it. A prior older than that which
the next measurement's anchors cannot check (at night, with no colour) is "carried" there.
Inside a measurement, a car that crossed A, or a count read, past that time is flagged, even
while the old anchors are still measured and keep their epoch "anchors". A window's by-exit
numbers are complete in a horizon once every car that left in it entered after the horizon's
start and the window is over (window start - hi_min >= start, window end <= end), its by-entry
numbers once the window lies in the horizon and every car that entered in it has left (window
start >= start, window end + hi_min <= end). Each Window, and each window of the record, says
which of the two it is; over a horizon of 2 x hi_min no window is both, so a reader takes each
half from a record that marks it complete. Right after a gap they rest on the carried offset,
flagged, until enough of the new epoch is seen for its anchors to confirm it (about one transit
plus hi_min): a caller that writes a window once must either wait that long after a gap or
accept a flag a later horizon would clear. LiveTest runs this. `anchoring` says how far the
latest epoch has come towards anchors (each record's progress, for the owner's bars).

Signatures are crossing.look()'s form, (saturation x cos hue, saturation x sin hue, value, width
share), which both counters emit. Two are compared by `colour_distance` against MATCH_DISTANCE,
on colour and value only: the width share is a share of each zone's assumed or pixel width, and
those differ between the cameras, so one car's two readings could differ by more than
MATCH_DISTANCE on width alone. A signature in any other form (hue in degrees, 0-255 channels, a
class id) or with a part that is not a number is refused (ValueError) rather than turned into
silent look-alike anchors. A crossing's `vehicle` class, where the counter knows it, must agree
too (`_same_class`).

Everything here is a fixed computation: the same crossings give the same output, whatever order
they come in, with deterministic tie-breaks and no randomness. Signatures, anchors and single
transits stay in memory; `record` writes window and epoch numbers and each line's unseen
stretches only (ADR-0014 decision 4).
Window and epoch statistics over few cars can give those cars' times or offsets back, though
never which car had which.
"""
from __future__ import annotations

import math
from bisect import bisect_left, bisect_right
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

if TYPE_CHECKING:            # the maths needs no image or network library
    from .crossing import Crossing, Zone
    from .hls import Gap

WINDOW_MIN = 15              # summaries per quarter hour, aligned to the clock
RATE_WINDOW_MIN = 10         # rates (the exit rate behind "now", a line's rate before a gap) are
                             # counted over the last 10 minutes
MIN_RATE_SPAN_MIN = 2.0      # a rate over less seen time than this is noise, so none is given
MIN_EXIT_RATE = 0.05         # cars/min: slower is a stopped line, and between / rate means nothing
GAP_PAD_S = 5.0              # ASSUMPTION: a reader resumes mid-segment; one 5 s City segment more
                             # past a gap is treated as unseen, so a half-read segment is not
                             # counted as if it were whole
MATCH_DISTANCE = 0.15        # ASSUMPTION: two readings of one car, or two cars of one shade,
                             # fall within this in look()'s colour and value (see above)
SIGNATURE_LIMIT = 1.5        # a part beyond +-1.5 is not look()'s 0-1 form (hue in degrees,
                             # 0-255 channels, a class id): refused
RARITY_HALF_MIN = 15         # a car's look is judged against its line's cars within 15 min of it
MIN_POPULATION = 20          # fewer neighbours than this cannot say that a look is rare
RARE_SHARE = 0.05            # distinctive: at most 1 in 20 neighbours look alike. ASSUMPTION
                             # (ADR-0014): about 3 in 4 cars are white, grey, black or silver,
                             # roughly 19 % per colour; a vivid colour is 1-2 % of cars
REANCHOR_HALF_MIN = 30       # a car's offset is the median of the kept anchors within 30 min
REANCHOR_MIN_ANCHORS = 5     # with fewer kept anchors near it, the trend line gives the offset;
                             # with fewer in the epoch, the line is flat at their median
OUTLIER_CARS = 3             # an anchor further than this from the median offset disagrees.
                             # It is also the band in which enclosed anchors must agree with
                             # their own line (`_epoch`), which assumes cars reorder by a few
                             # places at most (ASSUMPTION, module docstring)
MIN_ANCHORS = 3              # fewer enclosed anchors than this do not set an offset, and fewer
                             # anchors than this do not confirm one: a single look-alike would
INLIER_MIN_CARS = 3          # an anchor this close to the anchors' trend line is always kept,
INLIER_MADS = 4              # and one further than 4 median deviations from it never is
ANCHORED_SD = 2.0            # ASSUMPTION: an anchored offset is good to about 2 cars. Calibrated
                             # on the test simulator only (AnchorTest and GapTest find it within
                             # 2 of the truth there), which is strictly first-in-first-out, has
                             # one lane type and 1 car in 10 distinctive, and whose cameras agree
                             # to 0.03. Not yet measured against a hand count, so the offset_sd
                             # written for an anchored epoch is this assumption, not a measurement
AGREE_SDS = 3.0              # anchors agree with a carried offset within OUTLIER_CARS + 3 of its
                             # standard deviations
CHECK_MAX_MIN = 60           # ASSUMPTION: a sure count stays sure this long after anchors (or a
                             # given count) last set it. A flat offset is then off by about
                             # bias_A x (60, or 60 + trip after anchors) - bias_B x (60 + trip)
                             # min (Drift above): with a 55-min trip, 1.2 min for 1 % at B alone
                             # and 2.7-3.0 for 5 % at both. An anchor line with a slope can carry
                             # the drift on (0.3 min out at a 4 % difference after 4 h of
                             # anchors), but after 2.5 h of them the last sure transits' medians
                             # were up to 2.5 min out at 1 % (simulations, 2026-10-26)
DRIFT_MIN_DT_MIN = 10        # Theil-Sen pairs closer in time than this say nothing about drift
DRIFT_MIN_ENDS = 5           # and a slope needs this many anchors at each end of those pairs
DRIFT_MAX_ANCHORS = 400      # Theil-Sen looks at every pair: thin evenly to this many anchors
ANCHOR_BAR = 10              # characters in the terminal's anchoring bar (`anchoring_text`)
_TICK = timedelta(microseconds=1)
_SURE = ("given", "anchors", "continued")   # offset sources whose transits are not flagged,
                                            # for CHECK_MAX_MIN after the offset's last check


@dataclass(frozen=True)
class Transit:
    entered_at: datetime         # capture time of the crossing at A
    exited_at: datetime          # capture time of the crossing at B it pairs with
    minutes: float
    flagged: bool = False        # its epoch's offset is not sure (not given, anchored or
                                 # continued), or was last checked more than CHECK_MAX_MIN
                                 # before the car crossed A


@dataclass(frozen=True)
class Anchor:
    """One distinctive car seen at both lines. Per-vehicle: in memory only, never written."""
    a_index: int                 # 1-based place among line A's crossings since the start
    b_index: int
    entered_at: datetime
    exited_at: datetime
    confidence: float

    @property
    def offset(self) -> int:
        return self.b_index - self.a_index

    @property
    def minutes(self) -> float:
        return _minutes(self.exited_at - self.entered_at)


@dataclass(frozen=True)
class OffsetEstimate:
    offset: int                  # median of the anchors' offsets, rounded
    spread: float                # median absolute deviation of those offsets, cars
    n: int
    outliers: int                # anchors more than OUTLIER_CARS from the offset
    low: float                   # 10th and 90th percentile of the anchors' offsets
    high: float


@dataclass(frozen=True)
class Drift:
    cars_per_hour: float         # trend of the anchors' offsets (Theil-Sen)
    minutes_per_hour: float | None   # what that trend does to a transit read with a fixed offset
    bias_difference: float | None    # its estimate of bias_A - bias_B, as a share of cars
    n: int


@dataclass(frozen=True)
class Now:
    at: datetime
    between: int | None          # cars between the lines
    exit_rate: float | None      # cars/min crossing B over the last RATE_WINDOW_MIN
    minutes: float | None        # between / exit rate
    flagged: bool = False


@dataclass(frozen=True)
class Prior:
    """The cars between the lines at `at`, from an earlier measurement (`prior_at`), to start the
    next one from (`measure(prior=...)`). A count: no per-vehicle data."""
    at: datetime
    between: int
    sure: bool                   # its epoch's offset was given, anchored, or continued from one
    uncertainty: float | None    # cars, about one standard deviation; None when not known
    checked_at: datetime | None = None   # when anchors or a given count last set it; None: `at`


@dataclass(frozen=True)
class Epoch:
    """A stretch with no gap at either line, so both counts run unbroken through it."""
    start: datetime
    end: datetime
    offset: int | None           # the offset at `start`: the cars between the lines then, as the
                                 # two counts see them; offset_at() gives it per car
    source: str                  # "given" | "anchors" | "continued" | "carried" | "disputed"
                                 # | "few-anchors" | "unknown" (see measure)
    estimate: OffsetEstimate | None  # what all this epoch's anchors say, even when not used
    drift: Drift | None
    anchors: tuple[Anchor, ...]  # in memory only
    inliers: tuple[Anchor, ...] = ()            # the anchors near their line that its offsets
                                                # rest on ("given": shown against it, unused)
    line: tuple[float, float] | None = None     # that line (offset at `start`, cars per minute),
                                                # when the offsets come from the anchors
    enclosed: int = 0            # enclosed anchors that agree with their line (module docstring)
    incoming: int | None = None  # the offset carried into it (across a gap, or a prior)
    uncertainty: float | None = None   # of `offset`, cars, about one sd; None when not known
    checked_at: datetime | None = None # when anchors or a given count last set the offset;
                                       # None when it is not sure


@dataclass(frozen=True)
class Measurement:
    method: str
    start: datetime
    end: datetime
    epochs: tuple[Epoch, ...]
    transits: tuple[Transit, ...]
    a_times: tuple[datetime, ...]                    # every crossing at A in [start, end), in order
    b_times: tuple[datetime, ...]
    gaps_a: tuple[tuple[datetime, datetime], ...]    # unseen stretches at A, padded
    gaps_b: tuple[tuple[datetime, datetime], ...]
    hi_min: float                                    # the longest trip it allowed for
    # Why each stretch of gaps_a / gaps_b was unseen: the reasons of the gaps merged into it,
    # each once, in the order the gaps began
    why_a: tuple[tuple[str, ...], ...] = ()
    why_b: tuple[tuple[str, ...], ...] = ()


@dataclass(frozen=True)
class Stats:
    n: int
    flagged: int
    median: float | None
    p10: float | None
    p90: float | None


@dataclass(frozen=True)
class Window:
    start: datetime
    end: datetime
    by_exit: Stats               # transits that crossed B in the window
    by_entry: Stats              # transits that crossed A in it; n < entered while cars are between
    by_exit_complete: bool       # every car that left in it entered after the measurement's start
                                 # (start - hi_min >= m.start), and it is over (end <= m.end)
    by_entry_complete: bool      # it lies in the measurement (start >= m.start), and every car
                                 # that entered in it has had hi_min to leave (end + hi_min <= m.end)
    entered: int                 # crossings at A in the window
    exited: int                  # crossings at B
    exit_rate: float | None      # cars/min at B over the window's seen time
    between: int | None          # cars between the lines at the window's end
    gap: bool                    # a reading gap at either line overlaps the window
    flagged: bool                # a gap, a flagged transit, or an offset not anchored after the
                                 # last gap or last checked over CHECK_MAX_MIN before its end
    anchors: int                 # kept anchors that crossed A in the window
    anchor_residual: float | None    # their median offset minus the epoch's offset at its start:
                                     # drift shows as a trend


# --- counts ---------------------------------------------------------------------------------

def lane_types(zones: Sequence[Zone]) -> dict[str, str | None]:
    """lane -> lane type for one camera's zones."""
    out: dict[str, str | None] = {}
    for z in zones:
        if z.lane in out and out[z.lane] != z.lane_type:
            raise ValueError(f"{z.camera} lane {z.lane}: its zones disagree on the lane type")
        out[z.lane] = z.lane_type
    return out


def of_lane_type(crossings: Sequence[Crossing], types: Mapping[str, str | None],
                 lane_type: str | None) -> list[Crossing]:
    """The crossings in lanes of one type. A lane missing from `types` is in none."""
    return [c for c in crossings if c.lane in types and types[c.lane] == lane_type]


def ordered(crossings: Sequence[Crossing], start: datetime | None = None,
            end: datetime | None = None) -> list[Crossing]:
    """Crossings in capture order within [start, end). Which of two cars in one frame came first
    is not knowable, so any fixed rule will do; this one sorts them by lane, then by signature,
    and does not depend on the order the counter gave them (that order moved anchor indices and
    changed 38 transits in a review run)."""
    return sorted((c for c in crossings
                   if (start is None or c.at >= start) and (end is None or c.at < end)),
                  key=lambda c: (c.at, c.lane, c.signature is None, c.signature or (),
                                 c.confidence))


def cumulative(crossings: Sequence[Crossing], start: datetime | None = None,
               end: datetime | None = None) -> list[datetime]:
    """The running count as the capture times of its steps: count_at(times, t) is N(t)."""
    return [c.at for c in ordered(crossings, start, end)]


def count_at(times: Sequence[datetime], at: datetime) -> int:
    """Crossings up to and including `at`."""
    return bisect_right(times, at)


# --- transits and now -----------------------------------------------------------------------

def transits(a: Sequence[Crossing], b: Sequence[Crossing], offset: int,
             start: datetime | None = None) -> list[Transit]:
    """Pair the k-th crossing at A after `start` with the (offset + k)-th at B.

    Both lists must cover the same time from `start` (None: the lists are already cut to it).
    A transit that comes out negative is kept: it says the offset is wrong."""
    ta, tb = cumulative(a, start), cumulative(b, start)
    return _pair(ta, tb, [offset] * len(ta), [False] * len(ta))


def now(a: Sequence[Crossing], b: Sequence[Crossing], offset: int, at: datetime,
        start: datetime | None = None) -> Now:
    """Cars between the lines at `at` = offset + N_A(at) - N_B(at), the exit rate at B over the
    last RATE_WINDOW_MIN, and between / rate (None when the rate is about zero)."""
    return _now(cumulative(a, start), cumulative(b, start), offset, at, start, flagged=False)


def _pair(ta: Sequence[datetime], tb: Sequence[datetime], offsets: Sequence[int],
          flags: Sequence[bool]) -> list[Transit]:
    out = []
    for k, (entered, off, flagged) in enumerate(zip(ta, offsets, flags), start=1):
        j = off + k
        if 1 <= j <= len(tb):
            exited = tb[j - 1]
            out.append(Transit(entered, exited, _minutes(exited - entered), flagged))
    return out


def _now(ta: Sequence[datetime], tb: Sequence[datetime], offset: int, at: datetime,
         start: datetime | None, flagged: bool) -> Now:
    between = offset + count_at(ta, at) - count_at(tb, at)
    since = at - timedelta(minutes=RATE_WINDOW_MIN)
    if start is not None and start > since:
        since = start
    span = _minutes(at - since)
    rate = None
    if span >= MIN_RATE_SPAN_MIN:
        rate = (count_at(tb, at) - count_at(tb, since)) / span
    minutes = None
    if rate is not None and rate >= MIN_EXIT_RATE and between >= 0:
        minutes = between / rate
    return Now(at, between, rate, minutes, flagged)


# --- anchors --------------------------------------------------------------------------------

def colour_distance(p: Sequence[float], q: Sequence[float]) -> float:
    """How unlike two signatures look: Euclidean over look()'s colour and value, its first three
    parts. The width share is left out. It is a share of each zone's assumed or pixel width, and
    those differ between the cameras (B's zones have no ground rectangle; detect.py's share is of
    the zone's far edge in pixels), so one car's two readings could differ by more than
    MATCH_DISTANCE on width alone: 0.34 for a car filling 80 % of a zone (review, 2026-10-26)."""
    return math.dist(p[:3], q[:3])


def look_alike_shares(crossings: Sequence[Crossing]) -> list[float | None]:
    """For each crossing (in capture order), the share of its line's cars within RARITY_HALF_MIN
    that look like it: within MATCH_DISTANCE, of a class it could be matched to. None when it has
    no signature or too few neighbours to judge."""
    signed = [(i, c) for i, c in enumerate(crossings) if c.signature is not None]
    times = [c.at for _, c in signed]
    half = timedelta(minutes=RARITY_HALF_MIN)
    shares: list[float | None] = [None] * len(crossings)
    for n, (i, c) in enumerate(signed):
        lo, hi = bisect_left(times, c.at - half), bisect_right(times, c.at + half)
        population = hi - lo - 1
        if population < MIN_POPULATION:
            continue
        alike = sum(1 for m in range(lo, hi)
                    if m != n and _same_class(c, signed[m][1])
                    and colour_distance(c.signature, signed[m][1].signature) <= MATCH_DISTANCE)
        shares[i] = alike / population
    return shares


@dataclass(frozen=True)
class _Candidate:
    ia: int                      # 1-based indices, as in Anchor
    ib: int
    distance: float
    share: float                 # the larger look-alike share of the two
    confidence: float            # the lower of the two crossings' confidences


def anchors(a: Sequence[Crossing], b: Sequence[Crossing], lo_min: float, hi_min: float,
            start: datetime | None = None) -> list[Anchor]:
    """Distinctive cars matched between A and B by signature.

    A pair is a candidate when both cars are distinctive on their own line (look-alike share at
    most RARE_SHARE), it reached B lo_min to hi_min minutes after A, the two signatures are
    within MATCH_DISTANCE (`colour_distance`), and their classes agree. Of the candidates, the
    largest set in which no two pairs cross is kept: first-in-first-out allows no other. Ties go
    to the higher total confidence, then to the earlier pairs. Each anchor's confidence is the
    product of
    - how alike the two readings are (1 at the same reading, 0 at MATCH_DISTANCE);
    - how rare the look is (1 for a look nobody shares, 0 at RARE_SHARE);
    - 1 / (1 + the other candidates of either car that look at least as alike);
    - the lower of the two crossings' own confidences."""
    oa, ob = ordered(a, start), ordered(b, start)
    return _chain(_scored(oa, ob, lo_min, hi_min), oa, ob)


def _scored(oa: Sequence[Crossing], ob: Sequence[Crossing], lo_min: float,
            hi_min: float) -> list[tuple[_Candidate, float]]:
    """Every candidate pair with its confidence, sorted by (ia, ib). See `anchors`."""
    if not 0 <= lo_min < hi_min:
        raise ValueError("anchors need 0 <= lo_min < hi_min")
    _check_scale(oa)
    _check_scale(ob)
    sa, sb = look_alike_shares(oa), look_alike_shares(ob)
    rare_b = [(j, c) for j, c in enumerate(ob) if _rare(sb[j])]
    tb = [c.at for _, c in rare_b]
    lo, hi = timedelta(minutes=lo_min), timedelta(minutes=hi_min)
    cands: list[_Candidate] = []
    for i, ca in enumerate(oa):
        if not _rare(sa[i]):
            continue
        for m in range(bisect_left(tb, ca.at + lo), bisect_right(tb, ca.at + hi)):
            j, cb = rare_b[m]
            d = colour_distance(ca.signature, cb.signature)
            if d <= MATCH_DISTANCE and _same_class(ca, cb):
                cands.append(_Candidate(i + 1, j + 1, d, max(sa[i], sb[j]),
                                        min(ca.confidence, cb.confidence)))
    by_a: dict[int, list[float]] = defaultdict(list)
    by_b: dict[int, list[float]] = defaultdict(list)
    for c in cands:
        by_a[c.ia].append(c.distance)
        by_b[c.ib].append(c.distance)
    scored = []
    for c in cands:
        rivals = max(sum(1 for d in by_a[c.ia] if d <= c.distance),
                     sum(1 for d in by_b[c.ib] if d <= c.distance)) - 1
        confidence = ((1 - c.distance / MATCH_DISTANCE) * (1 - c.share / RARE_SHARE)
                      * c.confidence / (1 + rivals))
        scored.append((c, max(0.0, confidence)))
    return scored


def _chain(scored: Sequence[tuple[_Candidate, float]], oa: Sequence[Crossing],
           ob: Sequence[Crossing]) -> list[Anchor]:
    """The anchors: the largest non-crossing chain of the candidates."""
    if not scored:
        return []
    return [Anchor(c.ia, c.ib, oa[c.ia - 1].at, ob[c.ib - 1].at, round(conf, 4))
            for c, conf in _largest_chain(scored)]


def _enclosed(found: Sequence[Anchor], s: datetime, e: datetime,
              hi_min: float) -> list[Anchor]:
    """The anchors [s, e) encloses: whichever cars they really are, their partners crossed
    inside it, as no transit is longer than hi_min."""
    hi = timedelta(minutes=hi_min)
    return [x for x in found if x.entered_at + hi < e and x.exited_at - hi >= s]


def _check_scale(crossings: Sequence[Crossing]) -> None:
    """Signatures must be look()'s 0-1 form. Nothing of the signature is put in the message: it
    is per-vehicle data."""
    for c in crossings:
        if c.signature is None:
            continue
        if not all(math.isfinite(v) for v in c.signature):
            raise ValueError(f"{c.camera} ({c.method}): a signature component is not finite")
        if not all(-SIGNATURE_LIMIT <= v <= SIGNATURE_LIMIT for v in c.signature):
            raise ValueError(
                f"{c.camera} ({c.method}): a signature component lies beyond "
                f"+-{SIGNATURE_LIMIT}, so it is not crossing.look()'s 0-1 form")


def _same_class(p: Crossing, q: Crossing) -> bool:
    """Whether two crossings may be one vehicle by class: an unknown class matches anything.
    ASSUMPTION: YOLOX gives a vehicle the same class at A and at B, which both see it from
    behind but at very different sizes (about 30-38 px wide at A, 150-400 px at B). A pickup
    read as a car at one and a truck at the other loses its anchor."""
    return p.vehicle is None or q.vehicle is None or p.vehicle == q.vehicle


def _rare(share: float | None) -> bool:
    return share is not None and share <= RARE_SHARE


def _largest_chain(scored: Sequence[tuple[_Candidate, float]]) -> list[tuple[_Candidate, float]]:
    """The largest set of candidates strictly increasing in both indices.

    `scored` is sorted by (ia, ib). A Fenwick tree over ib holds, for every prefix of B, the best
    chain ending there, ranked by (length, total confidence, earliest). Candidates sharing an ia
    are all looked up before any is stored, so no chain uses one A car twice."""
    size = max(c.ib for c, _ in scored)
    tree: list[tuple[int, float, int] | None] = [None] * (size + 1)
    best: list[tuple[int, float, int]] = []      # per candidate: (length, total, -position)
    prev: list[int | None] = []

    def query(i: int) -> tuple[int, float, int] | None:
        found = None
        while i > 0:
            if tree[i] is not None and (found is None or tree[i] > found):
                found = tree[i]
            i -= i & -i
        return found

    def store(i: int, item: tuple[int, float, int]) -> None:
        while i <= size:
            if tree[i] is None or item > tree[i]:
                tree[i] = item
            i += i & -i

    start = 0
    while start < len(scored):
        stop = start
        while stop < len(scored) and scored[stop][0].ia == scored[start][0].ia:
            stop += 1
        for q in range(start, stop):
            cand, conf = scored[q]
            before = query(cand.ib - 1)
            if before is None:
                best.append((1, round(conf, 9), -q))
                prev.append(None)
            else:
                best.append((before[0] + 1, round(before[1] + conf, 9), -q))
                prev.append(-before[2])
        for q in range(start, stop):
            store(scored[q][0].ib, best[q])
        start = stop
    q: int | None = -max(best)[2]
    chain = []
    while q is not None:
        chain.append(scored[q])
        q = prev[q]
    return chain[::-1]


def offset_from_anchors(found: Sequence[Anchor]) -> OffsetEstimate | None:
    """The anchors' median offset, with their spread so that disagreeing anchors show."""
    if not found:
        return None
    offs = sorted(x.offset for x in found)
    middle = _quantile(offs, 0.5)
    offset = _round(middle)
    spread = _quantile(sorted(abs(o - middle) for o in offs), 0.5)
    outliers = sum(1 for o in offs if abs(o - offset) > OUTLIER_CARS)
    return OffsetEstimate(offset, spread, len(offs), outliers,
                          _quantile(offs, 0.1), _quantile(offs, 0.9))


def drift(found: Sequence[Anchor], exit_rate: float | None,
          entry_rate: float | None = None) -> Drift | None:
    """The trend of the anchors' offsets against the time they crossed A, as a Theil-Sen slope:
    the median of the slopes between pairs of anchors at least DRIFT_MIN_DT_MIN apart, so a few
    wrong anchors cannot tilt it. An epoch's drift is taken over the anchors its offsets rest on
    (each car takes the median of those near it in time), not from its trend line. The line is
    fitted to the enclosed anchors alone and only fills in where kept anchors are few; over that
    shorter span it read bias_A - bias_B 0.0101 off the realised value in DriftLawTest (156
    anchors), against 0.0060 from all 206 kept anchors (review, 2026-10-26).

    If A counts a share bias_A too many and B bias_B, an anchor's offset grows by
    (bias_B - bias_A) x (cars crossing A per hour). Divided by the exit rate, that is minutes of
    transit error per hour for a fixed offset. Divided by 60 x the entry rate (the exit rate when
    none is given), it is bias_A - bias_B."""
    if len(found) < 2:
        return None
    t0 = min(x.entered_at for x in found)
    slope = _theil_sen([_minutes(x.entered_at - t0) for x in found], [x.offset for x in found])
    if slope is None:
        return None
    cars_per_hour = slope * 60
    entry_rate = exit_rate if entry_rate is None else entry_rate
    minutes_per_hour = bias = None
    if exit_rate is not None and exit_rate >= MIN_EXIT_RATE:
        minutes_per_hour = -cars_per_hour / exit_rate
    if entry_rate is not None and entry_rate >= MIN_EXIT_RATE:
        bias = -cars_per_hour / (60 * entry_rate)
    return Drift(cars_per_hour, minutes_per_hour, bias, len(found))


def _theil_sen(xs: Sequence[float], ys: Sequence[float]) -> float | None:
    """Median slope over the pairs at least DRIFT_MIN_DT_MIN apart in x (minutes), thinned
    evenly to DRIFT_MAX_ANCHORS points first.

    None unless DRIFT_MIN_ENDS points lie DRIFT_MIN_DT_MIN before the last one and as many that
    long after the first. Otherwise the pairs far enough apart all share a few points, and one
    look-alike among those sets the slope: in a review run five anchors within 10 min and one
    look-alike 10 min on gave 1.3 cars/min, and the offset 47 min back came out 61 cars low."""
    pts = sorted(zip(xs, ys))
    if len(pts) > DRIFT_MAX_ANCHORS:
        pts = pts[::math.ceil(len(pts) / DRIFT_MAX_ANCHORS)]
    if not pts or min(sum(1 for x, _ in pts if x <= pts[-1][0] - DRIFT_MIN_DT_MIN),
                      sum(1 for x, _ in pts if x >= pts[0][0] + DRIFT_MIN_DT_MIN)) < DRIFT_MIN_ENDS:
        return None
    slopes = sorted((y2 - y1) / (x2 - x1)
                    for i, (x1, y1) in enumerate(pts) for x2, y2 in pts[i + 1:]
                    if x2 - x1 >= DRIFT_MIN_DT_MIN)
    return _quantile(slopes, 0.5)


def _fit(found: Sequence[Anchor], start: datetime, extra: Sequence[Anchor] = (),
         trend: bool = True) -> tuple[tuple[float, float], tuple[Anchor, ...]]:
    """The trend line of `found` (offset at `start`, cars per minute; flat without `trend`) and
    the anchors near it: those of `found`, then those of `extra` that share no car with one
    already kept. In A order."""
    xs = [_minutes(x.entered_at - start) for x in found]
    ys = [x.offset for x in found]
    slope = _theil_sen(xs, ys) if trend and len(found) >= REANCHOR_MIN_ANCHORS else None
    slope = 0.0 if slope is None else slope
    line = (_quantile(sorted(y - slope * x for x, y in zip(xs, ys)), 0.5), slope)

    def off(x: Anchor) -> float:
        return abs(x.offset - _on(line, start, x.entered_at))

    keep = max(INLIER_MIN_CARS, INLIER_MADS * _quantile(sorted(off(x) for x in found), 0.5))
    near = [x for x in found if off(x) <= keep]
    used_a, used_b = {x.a_index for x in near}, {x.b_index for x in near}
    near += [x for x in extra
             if off(x) <= keep and x.a_index not in used_a and x.b_index not in used_b]
    near.sort(key=lambda x: (x.a_index, x.b_index))
    return line, tuple(near)


def _on(line: tuple[float, float], start: datetime, at: datetime) -> float:
    """The line's offset for a car crossing A at `at`."""
    at_start, slope = line
    return at_start + slope * _minutes(at - start)


# --- a whole run: epochs, gaps, windows -----------------------------------------------------

@dataclass(frozen=True)
class _Incoming:
    """An offset carried into an epoch, across a gap or from a prior."""
    offset: int
    uncertainty: float | None    # cars, about one sd; None when not known
    continued: bool              # a sure count with no gap in between: stays sure unchecked ...
    checked_at: datetime | None  # ... until CHECK_MAX_MIN after anchors or a given count set it


def measure(a: Sequence[Crossing], b: Sequence[Crossing], lo_min: float, hi_min: float, *,
            gaps_a: Sequence[Gap] = (), gaps_b: Sequence[Gap] = (),
            start: datetime | None = None, end: datetime | None = None,
            offset: int | None = None, prior: Prior | None = None) -> Measurement:
    """Transits for one counter's crossings at A and B in [start, end), cut into epochs at every
    gap. Crossings outside that span are left out.

    `offset`, if given, is the cars between the lines at `start` (say from a hand count), and the
    first epoch uses it as it is. `prior` (`prior_at` on an earlier measurement) is that count
    carried from before; `start` defaults to prior.at. The first epoch takes it like an offset
    carried across a gap, except that a sure prior its anchors cannot check stays sure until
    CHECK_MAX_MIN after its last check. Either needs `start` seen: a gap over it raises
    ValueError.

    Every other epoch's offset comes from the first of these that applies:
    - "anchors": at least MIN_ANCHORS enclosed anchors that agree with their line (module
      docstring) and do not dispute the offset carried in. Short of that, at least MIN_ANCHORS
      anchors whose offsets lie within OUTLIER_CARS + AGREE_SDS sd of the carried one, where
      the line of all the anchors agrees with it at the epoch's start too. The line is then
      fitted to those of them that also lie near their own median, and the others are kept
      only near it. Each car then takes the median of the kept anchors within
      REANCHOR_HALF_MIN of it, or their line where there are too few.
    - "disputed": enclosed anchors that disagree with the carried offset. Used, flagged.
    - "continued": a sure prior its anchors cannot check, last checked (by anchors' crossings at
      B, or a given count) no more than CHECK_MAX_MIN before the epoch's end. Used as it is.
    - "carried": the offset carried across the gap, or a sure prior checked longer ago. Used,
      flagged.
    - "few-anchors": anchors that cannot stand on their own, with nothing carried in to check:
      used, flagged.
    - "unknown": no offset and no transits.

    Whatever the source, a car that crossed A more than CHECK_MAX_MIN after the epoch's offset
    was last checked (Epoch.checked_at) has its transit flagged, and `now_from` at such a time is
    flagged too.

    A Gap with no `after` came before the first frame: counting starts after it, so it cuts
    nothing."""
    if offset is not None and prior is not None:
        raise ValueError("give an offset or a prior, not both")
    if prior is not None:
        if start is not None and start != prior.at:
            raise ValueError("a prior is the count at prior.at: start there")
        start = prior.at
    if offset is not None and start is None:
        raise ValueError("a given offset is the cars between the lines at `start`: give it")
    method = _method(a, b)
    oa, ob = ordered(a), ordered(b)
    if start is None:
        firsts = [o[0].at for o in (oa, ob) if o]
        if not firsts:
            raise ValueError("no crossings and no start")
        start = min(firsts)
    if end is None:
        lasts = [o[-1].at for o in (oa, ob) if o]
        end = max(lasts) + _TICK if lasts else start
    oa, ob = ordered(oa, start, end), ordered(ob, start, end)
    ta, tb = [c.at for c in oa], [c.at for c in ob]
    ga, gb = _merge(_intervals(gaps_a)), _merge(_intervals(gaps_b))
    blackouts = [(max(s, start), min(e, end)) for s, e in _merge(ga + gb) if e > start and s < end]
    bounds, s = [], start
    for g0, g1 in blackouts:
        bounds.append((s, g0))
        s = g1
    bounds.append((s, end))

    epochs: list[Epoch] = []
    out: list[Transit] = []
    for s, e in bounds:
        if s >= e:
            continue
        if not epochs and (offset is not None or prior is not None) and s != start:
            raise ValueError("a gap covers `start`, so a count given there cannot be continued")
        incoming = None
        if epochs:
            incoming = _carry(epochs[-1], ta, tb, epochs[-1].end, s, ga, gb, start)
        elif prior is not None:
            incoming = _Incoming(prior.between, prior.uncertainty, continued=prior.sure,
                                 checked_at=prior.at if prior.checked_at is None
                                 else prior.checked_at)
        ea, eb = ordered(oa, s, e), ordered(ob, s, e)
        epoch = _epoch(ea, eb, s, e, lo_min, hi_min, offset if not epochs else None, incoming)
        epochs.append(epoch)
        if epoch.offset is not None:
            eta, etb = [c.at for c in ea], [c.at for c in eb]
            out += _pair(eta, etb, _offsets(epoch, eta), [_unsure(epoch, t) for t in eta])
    return Measurement(method, start, end, tuple(epochs), tuple(out), tuple(ta), tuple(tb),
                       tuple(ga), tuple(gb), hi_min, _why(gaps_a, ga), _why(gaps_b, gb))


def _epoch(ea: Sequence[Crossing], eb: Sequence[Crossing], s: datetime, e: datetime,
           lo_min: float, hi_min: float, given: int | None,
           incoming: _Incoming | None) -> Epoch:
    """One epoch's offset and where it came from (see `measure`)."""
    found = _chain(_scored(ea, eb, lo_min, hi_min), ea, eb)
    enclosed = _enclosed(found, s, e, hi_min)
    # Without much reordering between A and B (ASSUMPTION, module docstring), true anchors
    # agree with their own line to a car or two. A chain of look-alikes scatters by tens of
    # cars, and INLIER_MADS of its own deviations would still take it all in.
    own, own_near = _fit(enclosed, s, extra=found) if enclosed else (None, ())
    agree = [x for x in enclosed if abs(x.offset - _on(own, s, x.entered_at)) <= OUTLIER_CARS]
    if 2 * len(agree) < len(enclosed):
        agree = []
    line: tuple[float, float] | None = None
    inliers: tuple[Anchor, ...] = ()
    off: int | None = None
    uncertainty: float | None = None
    checked_at: datetime | None = None
    if given is None and len(agree) >= MIN_ANCHORS:
        line, inliers = own, own_near
        off = _line_offsets(line, inliers, s, [s])[0]
        if (incoming is not None and incoming.uncertainty is not None
                and abs(off - incoming.offset) > _tolerance(incoming.uncertainty)):
            source = "disputed"
        else:
            source, uncertainty = "anchors", ANCHORED_SD
    else:
        fit_line, fit_near = _fit(found, s) if found else (None, ())
        fit_off = _line_offsets(fit_line, fit_near, s, [s])[0] if fit_line is not None else None
        if given is not None:
            source, off, uncertainty, checked_at = "given", given, 0.0, s
            inliers = fit_near                   # shown against the given offset
        elif incoming is not None:
            # These anchors can only confirm the count carried in. Near the epoch's edges they
            # pair cars with look-alikes, and fitted with the rest those tilted the line and set
            # the offsets there: 241 cars short at an epoch's end, carried across the next gap,
            # unflagged (EdgeLookAlikeTest). So the offsets rest on the anchors that follow on
            # from the carried count (`_following`) and agree with each other, and on the others
            # only where they lie near those anchors' line. With each other, near their median:
            # a look-alike within the tolerance can be the only anchor DRIFT_MIN_DT_MIN from the
            # rest, and then its slope is the line's (0.7 cars/min in a review run, 47 cars out
            # a trip on). Held flat instead, the line lost the drift in LiveTest's night:
            # windows 3.6 min out.
            agreeing = ([] if incoming.uncertainty is None else
                        _following(found, incoming.offset, _tolerance(incoming.uncertainty)))
            if agreeing:
                agreeing = list(_fit(agreeing, s, trend=False)[1])
            if (len(agreeing) >= MIN_ANCHORS and len(fit_near) >= MIN_ANCHORS
                    and abs(fit_off - incoming.offset) <= _tolerance(incoming.uncertainty)):
                line, inliers = _fit(agreeing, s, extra=found)
                source, uncertainty = "anchors", ANCHORED_SD
                off = _line_offsets(line, inliers, s, [s])[0]
            else:
                checked_at = incoming.checked_at
                fresh = (checked_at is not None
                         and e - checked_at <= timedelta(minutes=CHECK_MAX_MIN))
                source = "continued" if incoming.continued and fresh else "carried"
                off, uncertainty = incoming.offset, incoming.uncertainty
        elif found:
            source, line, inliers, off = "few-anchors", fit_line, fit_near, fit_off
        else:
            source = "unknown"
    if source == "anchors":       # checked as each anchor crossed B: at A it is a trip earlier
        checked_at = max(x.exited_at for x in inliers)
    minutes = _minutes(e - s)
    exit_rate = len(eb) / minutes if minutes > 0 else None
    entry_rate = len(ea) / minutes if minutes > 0 else None
    return Epoch(start=s, end=e, offset=off, source=source,
                 estimate=offset_from_anchors(found),
                 drift=drift(inliers or found, exit_rate, entry_rate),
                 anchors=tuple(found), inliers=inliers, line=line,
                 enclosed=len(agree),
                 incoming=incoming.offset if incoming is not None else None,
                 uncertainty=uncertainty,
                 checked_at=checked_at if source in _SURE else None)


def _tolerance(uncertainty: float) -> float:
    return OUTLIER_CARS + AGREE_SDS * uncertainty


def _following(found: Sequence[Anchor], offset: int, tolerance: float) -> list[Anchor]:
    """The anchors, in A order, that each lie within `tolerance` of the median of the last
    REANCHOR_MIN_ANCHORS kept before them (of `offset`, the count carried in, before any). The
    band follows the counters' drift through the epoch: held at the carried count, it kept 8 of
    45 true anchors at about 20 cars an hour of drift, and their flat line put sure transits up
    to 6 min out (DriftConfirmTest). A look-alike tens of cars off is still left out."""
    kept: list[Anchor] = []
    for x in found:
        recent = sorted(k.offset for k in kept[-REANCHOR_MIN_ANCHORS:])
        if abs(x.offset - (_quantile(recent, 0.5) if recent else offset)) <= tolerance:
            kept.append(x)
    return kept


def _unsure(epoch: Epoch, at: datetime) -> bool:
    """Whether what `epoch` gives at `at` (a car crossing A then, or the count then) is not sure:
    its offset is not, or anchors or a given count last set it more than CHECK_MAX_MIN before.
    Anchors are found afresh while they stay in a measurement (up to the signatures' TTL), so
    without this they kept every car after them sure: 16 cars out at night in LiveTest."""
    return (epoch.checked_at is None             # set just when the offset is sure
            or at - epoch.checked_at > timedelta(minutes=CHECK_MAX_MIN))


def offset_at(epoch: Epoch, at: datetime) -> int | None:
    """The offset a car crossing A at `at` takes. Where the epoch's offsets come from its
    anchors: the median of the kept anchors within REANCHOR_HALF_MIN of it, or the anchors'
    trend line where they are too few. Otherwise the epoch's own offset (given or carried)."""
    return _offsets(epoch, [at])[0]


def _offsets(epoch: Epoch, times: Sequence[datetime]) -> list[int | None]:
    if epoch.line is None:
        return [epoch.offset] * len(times)
    return _line_offsets(epoch.line, epoch.inliers, epoch.start, times)


def _line_offsets(line: tuple[float, float], inliers: Sequence[Anchor], start: datetime,
                  times: Sequence[datetime]) -> list[int]:
    marks = [x.entered_at for x in inliers]              # in A order
    half = timedelta(minutes=REANCHOR_HALF_MIN)
    out = []
    for t in times:
        lo, hi = bisect_left(marks, t - half), bisect_right(marks, t + half)
        if hi - lo < REANCHOR_MIN_ANCHORS:
            out.append(_round(_on(line, start, t)))
        else:
            out.append(_round(_quantile(sorted(x.offset for x in inliers[lo:hi]), 0.5)))
    return out


def now_from(m: Measurement, at: datetime) -> Now:
    """`now` for a measured run: inside the epoch holding `at`, with that epoch's offset."""
    epoch = _epoch_at(m, at)
    if epoch is None or epoch.offset is None:
        return Now(at, None, None, None, flagged=True)
    ta = m.a_times[bisect_left(m.a_times, epoch.start):]
    tb = m.b_times[bisect_left(m.b_times, epoch.start):]
    return _now(ta, tb, offset_at(epoch, at), at, epoch.start, flagged=_unsure(epoch, at))


def prior_at(m: Measurement, at: datetime) -> Prior | None:
    """The cars between the lines at `at` (crossings at `at` itself not yet counted), for
    `measure(prior=...)` to start from. If `at` falls in a gap, the prior is taken where the next
    epoch starts, and its `at` says so. None when no epoch from `at` on has an offset, and past
    m.end: nothing after it was measured."""
    epoch = _epoch_at(m, at)
    if epoch is None:
        later = [e for e in m.epochs if e.start > at]
        if not later:
            return None
        epoch, at = later[0], later[0].start
    if epoch.offset is None:
        return None
    return Prior(at, _between(m, at), epoch.source in _SURE, epoch.uncertainty,
                 epoch.checked_at)


def windows(m: Measurement, width_min: float = WINDOW_MIN) -> list[Window]:
    """Per window of `width_min` (aligned to the clock), the transits by exit and by entry time
    and whether each is complete, the counts, the exit rate, the cars between at the window's
    end, and the drift residual."""
    width, hi = timedelta(minutes=width_min), timedelta(minutes=m.hi_min)
    origin = _floor(m.start, width)
    count = max(1, math.ceil((m.end - origin) / width))
    by_exit: list[list[Transit]] = [[] for _ in range(count)]
    by_entry: list[list[Transit]] = [[] for _ in range(count)]
    for t in m.transits:
        for slots, at in ((by_exit, t.exited_at), (by_entry, t.entered_at)):
            k = (at - origin) // width
            if 0 <= k < count:
                slots[k].append(t)
    residuals: list[list[int]] = [[] for _ in range(count)]
    for epoch in m.epochs:
        if epoch.offset is None:
            continue
        for x in epoch.inliers:
            k = (x.entered_at - origin) // width
            if 0 <= k < count:
                residuals[k].append(x.offset - epoch.offset)
    out = []
    for k in range(count):
        w0 = origin + k * width
        w1 = w0 + width
        seen0, seen1 = max(w0, m.start), min(w1, m.end)
        seen_b = _minutes(seen1 - seen0) - _overlap_s(m.gaps_b, seen0, seen1) / 60
        exited = _count(m.b_times, w0, w1)
        gap = _overlap_s(m.gaps_a, w0, w1) > 0 or _overlap_s(m.gaps_b, w0, w1) > 0
        uncertain = any(e.start < w1 and e.end > w0 and _unsure(e, min(w1, e.end))
                        for e in m.epochs)
        sx, sn = _stats(by_exit[k]), _stats(by_entry[k])
        res = sorted(residuals[k])
        out.append(Window(
            start=w0, end=w1, by_exit=sx, by_entry=sn,
            by_exit_complete=w0 - hi >= m.start and w1 <= m.end,
            by_entry_complete=w0 >= m.start and w1 + hi <= m.end,
            entered=_count(m.a_times, w0, w1), exited=exited,
            exit_rate=exited / seen_b if seen_b >= MIN_RATE_SPAN_MIN else None,
            between=_between(m, min(w1, m.end)),
            gap=gap, flagged=gap or uncertain or sx.flagged > 0 or sn.flagged > 0,
            anchors=len(res), anchor_residual=_quantile(res, 0.5)))
    return out


def record(m: Measurement, wins: Sequence[Window], current: Now | None = None, *,
           lane_type: str | None = None) -> dict:
    """The derived numbers for the JSONL log, counts and statistics per window and per epoch.
    No field names a vehicle: no signature, index or crossing time. Statistics over few transits
    or anchors can give their values back, never which car had which.

    `gaps_a` and `gaps_b` are each line's unseen stretches inside the measured span, as the
    epochs were cut at them (padded by GAP_PAD_S, merged, clipped to the span): the minute each
    began, its length in seconds and why, so that a run's records name the line and the cause
    of every cut. A stretch starts just after a frame, and a car can have been counted in that
    frame, so its start is written to the minute as the other times are; its length alone gives
    no time back."""
    return {
        "kind": "transit",
        "method": m.method,
        "lane_type": lane_type,
        "start": _iso(_floor(m.start, timedelta(minutes=WINDOW_MIN))),
        "end": _iso(wins[-1].end) if wins else None,
        "transits": len(m.transits),
        "transits_flagged": sum(1 for t in m.transits if t.flagged),
        "epochs": [_epoch_record(e) for e in m.epochs],
        "gaps_a": _gap_records(m.gaps_a, m.why_a, m.start, m.end),
        "gaps_b": _gap_records(m.gaps_b, m.why_b, m.start, m.end),
        "windows": [_window_record(w) for w in wins],
        "now": None if current is None else {   # to the minute: `at` may be a crossing's time
            "at": _iso(_floor(current.at, timedelta(minutes=1))), "between": current.between,
            "exit_rate": _r(current.exit_rate, 3), "minutes": _r(current.minutes),
            "flagged": current.flagged},
    }


def _epoch_record(e: Epoch) -> dict:
    est, d = e.estimate, e.drift
    minute = timedelta(minutes=1)
    return {   # to the minute: an epoch's edges are a car's or a frame's capture time
        "start": _iso(_floor(e.start, minute)), "end": _iso(_ceil(e.end - _TICK, minute)),
        "source": e.source, "offset": e.offset, "offset_sd": _r(e.uncertainty),
        "carried_offset": e.incoming,
        "anchors": len(e.anchors), "anchors_kept": len(e.inliers),
        "anchors_enclosed": e.enclosed,
        "anchor_offset": est.offset if est else None,
        "anchor_spread": _r(est.spread) if est else None,
        "anchor_outliers": est.outliers if est else None,
        "anchor_p10": _r(est.low) if est else None,
        "anchor_p90": _r(est.high) if est else None,
        "drift_cars_per_hour": _r(d.cars_per_hour) if d else None,
        "drift_minutes_per_hour": _r(d.minutes_per_hour) if d else None,
        "bias_difference": _r(d.bias_difference, 4) if d else None,
    }


MIN_STATS_N = 3              # fewer transits than this in a window: no median or percentiles.
                             # This is not anonymity: at 3 they give all three times back, and a
                             # window rewritten each minute as it fills can be differenced. No
                             # written time is tied to a car (no signature, index or crossing
                             # time of its own)


def _window_record(w: Window) -> dict:
    def stats(s: Stats) -> dict:
        if s.n < MIN_STATS_N:
            return {"n": s.n, "flagged": s.flagged, "median": None, "p10": None, "p90": None}
        return {"n": s.n, "flagged": s.flagged, "median": _r(s.median), "p10": _r(s.p10),
                "p90": _r(s.p90)}
    return {
        "start": _iso(w.start), "by_exit": stats(w.by_exit), "by_entry": stats(w.by_entry),
        "by_exit_complete": w.by_exit_complete, "by_entry_complete": w.by_entry_complete,
        "entered": w.entered, "exited": w.exited, "exit_rate": _r(w.exit_rate, 3),
        "between": w.between, "gap": w.gap, "flagged": w.flagged, "anchors": w.anchors,
        "anchor_residual": _r(w.anchor_residual),
    }


# --- anchoring progress ---------------------------------------------------------------------

def anchoring(m: Measurement) -> dict:
    """How far the latest epoch has come towards anchoring its count, as each record carries it
    (`anchoring`) and the owner's progress bars show it (asked 2026-10-28, for future tests).
    Counts and minutes only: no anchor's time.

    An anchor of a trip of T min is enclosed only in an unbroken stretch (no gap at either line:
    an epoch) longer than 2 x hi_min - T (`_enclosed`), and the count is anchored once
    MIN_ANCHORS enclosed anchors agree, or once as many that follow a count carried in (across a
    gap, or the runner's prior) confirm it, enclosed or not (`_epoch`). So `state` is:
    - "broken": a gap at either line runs to the end of what is measured (the loop learning,
      YOLOX stopped): nothing is unbroken now, and the stretch starts again once both lines are
      seen - never after YOLOX stopped, whose gap stays open for the rest of the run
      (runner._gaps_for). A stream down and a frozen or still picture are not shown so while
      they last: the runner holds the end at that line's last frame that moved (seen_until),
      which no gap covers yet, so the state stays as it was there (the page flags the record
      stale after LAG_ALERT_S); once frames move again the gap is recorded and a new epoch
      starts after it;
    - "anchored": the latest epoch's count is set by its anchors ("anchors"), or given, and
      was checked no more than CHECK_MAX_MIN before the end (`_unsure`, the Now's own flag);
    - "waiting": anything else, "continued", "carried", "disputed", "few-anchors" and anchors
      last checked more than CHECK_MAX_MIN before the end (at night) included.
    `percent` is the share of `needed_min` the epoch has run, rounded down and never above 99
    while waiting: a stretch long enough still holds no anchor until distinctive cars cross
    both lines, and only an anchored count is 100. T (`transit_min`) is the median of the
    latest WINDOW_MIN window, by exit, holding at least MIN_STATS_N unflagged transits, taken at
    most hi_min (a longer trip cannot anchor); with none, 0 (`transit_assumed`), which asks for
    the longest stretch, 2 x hi_min. None of this says the offset is right: only `measure`
    sets "anchors", and a transit's flag and the display rule (combine.display) are as before."""
    broken = any(g0 < m.end <= g1 for g0, g1 in (*m.gaps_a, *m.gaps_b))
    last = None if broken or not m.epochs else m.epochs[-1]
    t = _latest_transit(m)
    needed = 2 * m.hi_min - min(t or 0.0, m.hi_min)
    unbroken = 0.0 if last is None else _minutes(last.end - last.start)
    if broken:
        state, percent = "broken", 0
    elif (last is not None and last.source in ("anchors", "given")
          and not _unsure(last, m.end)):
        state, percent = "anchored", 100
    else:
        state, percent = "waiting", min(99, math.floor(100 * unbroken / needed))
    return {"state": state, "unbroken_min": round(unbroken, 1), "needed_min": round(needed, 1),
            "transit_min": _r(t, 1), "transit_assumed": t is None, "percent": percent,
            "enclosed": 0 if last is None else last.enclosed, "min_anchors": MIN_ANCHORS}


def _latest_transit(m: Measurement) -> float | None:
    """The median minutes of the unflagged transits that crossed B in the latest WINDOW_MIN
    window (aligned to the clock) holding at least MIN_STATS_N of them; None with none."""
    done = sorted((t for t in m.transits if not t.flagged), key=lambda t: t.exited_at)
    width = timedelta(minutes=WINDOW_MIN)
    while len(done) >= MIN_STATS_N:
        k = bisect_left([t.exited_at for t in done], _floor(done[-1].exited_at, width))
        if len(done) - k >= MIN_STATS_N:
            return _quantile(sorted(t.minutes for t in done[k:]), 0.5)
        done = done[:k]
    return None


def anchoring_text(method: str, group: str, a: Mapping | None, *,
                   stopped: bool = False) -> str:
    """A record's `anchoring` as one line of the terminal status, in ASCII, e.g.
    "anchoring loop  normal [#####.....] 58% 101/174 min, anchors 1/3". Minutes are rounded
    down and the needed ones up, and the bar fills by whole tenths, so it never shows more than
    there is. A count anchored by the anchors that follow a count carried in (fewer enclosed
    than MIN_ANCHORS, `_epoch`) says so, not a stretch and anchors it never needed. `stopped`:
    YOLOX stopped at a camera this run (runner.yolox_stopped), so a broken bar of this method
    stays broken to the end of the run."""
    head = f"anchoring {method!s:5} {group!s:6}"
    if not a:
        return f"{head} [{'?' * ANCHOR_BAR}] not recorded"
    filled = min(ANCHOR_BAR, a["percent"] * ANCHOR_BAR // 100)
    bar = "#" * filled + "." * (ANCHOR_BAR - filled)
    if a["state"] == "broken" and stopped:
        return (f"{head} [{bar}] broken for the rest of this run: YOLOX stopped, its line unseen "
                "(YOLOX STOPPED)")
    if a["state"] == "broken":
        return (f"{head} [{bar}] broken: a line is unseen now (a gap at A or B); the stretch "
                "starts again once both are seen")
    if a["state"] == "anchored" and a["enclosed"] < a["min_anchors"]:
        return (f"{head} [{bar}] 100%: anchored, the count carried in confirmed by distinctive "
                "cars that follow it")
    text = (f"{head} [{bar}] {a['percent']}% {math.floor(a['unbroken_min'])}/"
            f"{math.ceil(a['needed_min'])} min, anchors {a['enclosed']}/{a['min_anchors']}")
    if a["state"] == "anchored":
        text += ": anchored"
    elif a["unbroken_min"] >= a["needed_min"]:
        text += ": long enough, waiting for distinctive cars at both lines"
    if a["transit_assumed"]:
        text += " (no trip measured yet: taken as 0 min, the longest stretch)"
    return text


# --- helpers --------------------------------------------------------------------------------

def _method(a: Sequence[Crossing], b: Sequence[Crossing]) -> str:
    methods = sorted({c.method for c in a} | {c.method for c in b})
    if len(methods) > 1:
        raise ValueError(f"one counter at a time, got {', '.join(methods)}")
    return methods[0] if methods else "unknown"


def _intervals(gaps: Sequence[Gap]) -> list[tuple[datetime, datetime]]:
    """The unseen stretches, padded. Gap.after is the last frame before the gap, and that frame
    was seen: the stretch starts just after it, so a car counted in it keeps its transit."""
    return [(g.after + _TICK, g.after + timedelta(seconds=g.seconds + GAP_PAD_S))
            for g in gaps if g.after is not None]


def _why(gaps: Sequence[Gap], merged: Sequence[tuple[datetime, datetime]]
         ) -> tuple[tuple[str, ...], ...]:
    """For each stretch of `merged` (_merge of _intervals(gaps)), the reasons of the gaps in it,
    each once, in the order the gaps began (ties by reason, so the order gaps come in does not
    matter)."""
    out: list[list[str]] = [[] for _ in merged]
    starts = [s for s, _ in merged]
    for g in sorted((g for g in gaps if g.after is not None), key=lambda g: (g.after, g.reason)):
        k = bisect_right(starts, g.after + _TICK) - 1
        if g.reason not in out[k]:
            out[k].append(g.reason)
    return tuple(tuple(reasons) for reasons in out)


def _gap_records(spans: Sequence[tuple[datetime, datetime]], why: Sequence[tuple[str, ...]],
                 start: datetime, end: datetime) -> list[dict]:
    """One line's unseen stretches inside [start, end), for `record`."""
    minute = timedelta(minutes=1)
    out = []
    for i, (g0, g1) in enumerate(spans):
        s, e = max(g0, start), min(g1, end)
        if e > s:
            out.append({"start": _iso(_floor(s, minute)), "seconds": _r((e - s).total_seconds(), 1),
                        "reasons": list(why[i]) if i < len(why) else []})
    return out


def _merge(intervals: Sequence[tuple[datetime, datetime]]) -> list[tuple[datetime, datetime]]:
    out: list[tuple[datetime, datetime]] = []
    for s, e in sorted(intervals):
        if out and s <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], e))
        else:
            out.append((s, e))
    return out


def _overlap_s(intervals: Sequence[tuple[datetime, datetime]], t0: datetime,
               t1: datetime) -> float:
    return sum(max(0.0, (min(e, t1) - max(s, t0)).total_seconds()) for s, e in intervals)


def _inside(t: datetime, intervals: Sequence[tuple[datetime, datetime]]) -> bool:
    return any(s <= t < e for s, e in intervals)


def _count(times: Sequence[datetime], t0: datetime, t1: datetime) -> int:
    """Crossings in [t0, t1)."""
    return bisect_left(times, t1) - bisect_left(times, t0)


def _carry(prev: Epoch, ta: Sequence[datetime], tb: Sequence[datetime], g0: datetime,
           g1: datetime, ga: Sequence[tuple[datetime, datetime]],
           gb: Sequence[tuple[datetime, datetime]], since: datetime) -> _Incoming | None:
    """The cars between the lines at g1, carried from the epoch before the blackout [g0, g1),
    with its uncertainty: the previous epoch's, plus sqrt(n) for the n cars estimated at the two
    lines. None when the previous epoch has no offset or a line's rate is unknown."""
    if prev.offset is None:
        return None
    between = offset_at(prev, g0) + _count(ta, prev.start, g0) - _count(tb, prev.start, g0)
    through_a = _through(ta, ga, g0, g1, since)
    through_b = _through(tb, gb, g0, g1, since)
    if through_a is None or through_b is None:
        return None
    uncertainty = None
    if prev.uncertainty is not None:
        uncertainty = math.sqrt(prev.uncertainty ** 2 + through_a[1] + through_b[1])
    return _Incoming(_round(between + through_a[0] - through_b[0]), uncertainty,
                     continued=False, checked_at=None)


def _through(times: Sequence[datetime], gaps: Sequence[tuple[datetime, datetime]],
             g0: datetime, g1: datetime, since: datetime) -> tuple[float, float] | None:
    """Cars that crossed one line in [g0, g1), and how many of those are estimated: the ones
    seen, plus the line's rate times the time it did not see. The rate is counted over the seen
    time in the RATE_WINDOW_MIN before g0, across earlier gaps and epochs (two gaps a minute
    apart leave an epoch too short for a rate of its own, and the count was lost for good).
    Only `since`, the measurement's start, bounds it. None when less than MIN_RATE_SPAN_MIN of
    it was seen."""
    seen = sum(1 for t in times[bisect_left(times, g0):bisect_left(times, g1)]
               if not _inside(t, gaps))
    unseen_s = _overlap_s(gaps, g0, g1)
    if unseen_s == 0:
        return float(seen), 0.0
    t0 = max(since, g0 - timedelta(minutes=RATE_WINDOW_MIN))
    seen_s = (g0 - t0).total_seconds() - _overlap_s(gaps, t0, g0)
    if seen_s < MIN_RATE_SPAN_MIN * 60:
        return None
    before = sum(1 for t in times[bisect_left(times, t0):bisect_left(times, g0)]
                 if not _inside(t, gaps))
    estimated = before / seen_s * unseen_s
    return seen + estimated, estimated


def _epoch_at(m: Measurement, at: datetime) -> Epoch | None:
    """The epoch holding `at`; the last one also holds m.end itself, but nothing past it."""
    for e in m.epochs:
        if e.start <= at and (at < e.end or at == e.end == m.end):
            return e
    return None


def _between(m: Measurement, at: datetime) -> int | None:
    epoch = _epoch_at(m, at)
    if epoch is None or epoch.offset is None:
        return None
    return (offset_at(epoch, at) + _count(m.a_times, epoch.start, at)
            - _count(m.b_times, epoch.start, at))


def _stats(ts: Sequence[Transit]) -> Stats:
    vals = sorted(t.minutes for t in ts)
    return Stats(len(vals), sum(1 for t in ts if t.flagged), _quantile(vals, 0.5),
                 _quantile(vals, 0.1), _quantile(vals, 0.9))


def _quantile(sorted_vals: Sequence[float], q: float) -> float | None:
    """Linear interpolation between order statistics (numpy's default)."""
    if not sorted_vals:
        return None
    pos = q * (len(sorted_vals) - 1)
    lo = math.floor(pos)
    hi = min(lo + 1, len(sorted_vals) - 1)
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (pos - lo)


def _round(x: float) -> int:
    return math.floor(x + 0.5)


def _minutes(td: timedelta) -> float:
    return td.total_seconds() / 60


def _floor(t: datetime, width: timedelta) -> datetime:
    epoch = datetime(1970, 1, 1, tzinfo=t.tzinfo)
    return t - (t - epoch) % width


def _ceil(t: datetime, width: timedelta) -> datetime:
    down = _floor(t, width)
    return down if down == t else down + width


def _iso(t: datetime) -> str:
    return t.astimezone(UTC).isoformat(timespec="seconds")


def _r(x: float | None, digits: int = 2) -> float | None:
    return None if x is None else round(x, digits)
