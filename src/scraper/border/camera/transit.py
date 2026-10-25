"""Crossing time from the running counts at two lines (ADR-0014).

Every car that crosses line A (FPFCH `pdn-north`, the Mexican half of Paso del Norte) later
crosses line B (City `bridgepdn1`, the US half). Draw each line's running count against time: the
horizontal gap between the two curves is the transit, and the vertical gap is the cars between
the lines. Put as indices: the k-th car to cross A after the start is the (offset + k)-th to cross
B, where `offset` is the cars already between the lines at the start. No car has to be recognised
for this, which matters here. Cars are 52-69 px wide at line A, about 3 in 4 are white, grey,
black or silver, and published re-identification across viewpoints manages 34-68 % even at full
resolution (ADR-0014, Context).

ASSUMPTION: the stretch from A to B is first-in-first-out with no entries or exits. Nobody joins
or leaves between the cameras, and nobody moves between lane types (pair one lane type at a time,
`of_lane_type`). Overtaking breaks "the k-th car" but not the curves: the area between them is
still the sum of every car's time between the lines, so the mean transit holds, and the median
holds while the order only changes locally.

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
  look-alike for a place in the chain. At least MIN_ANCHORS enclosed anchors near their own
  trend line make the epoch's offset, and the other anchors near that line are kept too. Without
  them, an epoch's anchors can only confirm an offset carried into it, never set one unflagged.
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
`prior_at(previous, at)` gives the cars between the lines at `at`, a count with no per-vehicle
data, and `measure(..., prior=...)` continues from it, its own anchors checking it. A window's
by-exit numbers are complete in a horizon once every car that left in it entered after the
horizon's start (window start - hi_min >= start), its by-entry numbers once every car that
entered in it has left (window end + hi_min <= end). Right after a gap they rest on the carried
offset, flagged, until enough of the new epoch is seen for its anchors to confirm it (about one
transit plus hi_min): a caller that writes a window once must either wait that long after a gap
or accept a flag a later horizon would clear. LiveTest runs this.

Signatures are compared with `distance` (Euclidean by default) against MATCH_DISTANCE. That
assumes a vector of components scaled to about 0-1 in which neutral colours sit together, such as
(saturation x cos hue, saturation x sin hue, value, width share). On 2026-10-24 neither counter
emits that: loop.py gives (hue in degrees, saturation 0-255, value 0-255, width in metres,
seconds in the zone) and detect.py (COCO class, hue / 180, saturation, value, width share). Raw
hue wraps at red and is noise for a grey car, so under plain Euclidean distance a grey car would
look rare and a red one would not match itself; seconds in the zone depend on speed, not looks.
So with the default distance a component beyond SIGNATURE_LIMIT is refused (ValueError) rather
than turned into silent look-alike anchors. Until the counters share a form, pass a `distance`
that handles theirs.

Everything here is a fixed computation: the same crossings give the same output, whatever order
they come in, with deterministic tie-breaks and no randomness. Per-vehicle data (signatures,
anchors, single transits) stays in memory; `record` writes window and epoch numbers only
(ADR-0014 decision 4).
"""
from __future__ import annotations

import math
from bisect import bisect_left, bisect_right
from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
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
MATCH_DISTANCE = 0.15        # ASSUMPTION: signature components are scaled to about 0-1 (see
                             # above), so two readings of one car, or two cars of one shade,
                             # fall within this
SIGNATURE_LIMIT = 1.5        # with the default distance, a component beyond +-1.5 is not on that
                             # scale (hue in degrees, 0-255 channels, a class id): refused
RARITY_HALF_MIN = 15         # a car's look is judged against its line's cars within 15 min of it
MIN_POPULATION = 20          # fewer neighbours than this cannot say that a look is rare
RARE_SHARE = 0.05            # distinctive: at most 1 in 20 neighbours look alike. ASSUMPTION
                             # (ADR-0014): about 3 in 4 cars are white, grey, black or silver,
                             # roughly 19 % per colour; a vivid colour is 1-2 % of cars
REANCHOR_HALF_MIN = 30       # a car's offset is the median of the kept anchors within 30 min
REANCHOR_MIN_ANCHORS = 5     # with fewer kept anchors near it, the trend line gives the offset;
                             # with fewer in the epoch, the line is flat at their median
OUTLIER_CARS = 3             # an anchor further than this from the median offset disagrees
MIN_ANCHORS = 3              # fewer enclosed anchors than this do not set an offset, and fewer
                             # anchors than this do not confirm one: a single look-alike would
INLIER_MIN_CARS = 3          # an anchor this close to the anchors' trend line is always kept,
INLIER_MADS = 4              # and one further than 4 median deviations from it never is
ANCHORED_SD = 2.0            # ASSUMPTION: an anchored offset is good to about 2 cars (AnchorTest
                             # and GapTest find it within 2 of the truth)
AGREE_SDS = 3.0              # anchors agree with a carried offset within OUTLIER_CARS + 3 of its
                             # standard deviations
DRIFT_MIN_DT_MIN = 10        # Theil-Sen pairs closer in time than this say nothing about drift
DRIFT_MIN_ENDS = 5           # and a slope needs this many anchors at each end of those pairs
DRIFT_MAX_ANCHORS = 400      # Theil-Sen looks at every pair: thin evenly to this many anchors
_TICK = timedelta(microseconds=1)
_SURE = ("given", "anchors", "continued")   # offset sources whose transits are not flagged


@dataclass(frozen=True)
class Transit:
    entered_at: datetime         # capture time of the crossing at A
    exited_at: datetime          # capture time of the crossing at B it pairs with
    minutes: float
    flagged: bool = False        # its offset was not anchored after the last gap


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
    enclosed: int = 0            # enclosed anchors near their own line (see the module docstring)
    incoming: int | None = None  # the offset carried into it (across a gap, or a prior)
    uncertainty: float | None = None   # of `offset`, cars, about one sd; None when not known


@dataclass(frozen=True)
class Measurement:
    method: str
    start: datetime
    end: datetime
    epochs: tuple[Epoch, ...]
    transits: tuple[Transit, ...]
    a_times: tuple[datetime, ...]                    # every crossing at A, in order
    b_times: tuple[datetime, ...]
    gaps_a: tuple[tuple[datetime, datetime], ...]    # unseen stretches at A, padded
    gaps_b: tuple[tuple[datetime, datetime], ...]


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
    entered: int                 # crossings at A in the window
    exited: int                  # crossings at B
    exit_rate: float | None      # cars/min at B over the window's seen time
    between: int | None          # cars between the lines at the window's end
    gap: bool                    # a reading gap at either line overlaps the window
    flagged: bool                # a gap, or an offset not anchored after the last gap
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
    return _pair(ta, tb, [offset] * len(ta), flagged=False)


def now(a: Sequence[Crossing], b: Sequence[Crossing], offset: int, at: datetime,
        start: datetime | None = None) -> Now:
    """Cars between the lines at `at` = offset + N_A(at) - N_B(at), the exit rate at B over the
    last RATE_WINDOW_MIN, and between / rate (None when the rate is about zero)."""
    return _now(cumulative(a, start), cumulative(b, start), offset, at, start, flagged=False)


def _pair(ta: Sequence[datetime], tb: Sequence[datetime], offsets: Sequence[int],
          flagged: bool) -> list[Transit]:
    out = []
    for k, (entered, off) in enumerate(zip(ta, offsets), start=1):
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

def look_alike_shares(crossings: Sequence[Crossing],
                      distance: Callable[[Sequence[float], Sequence[float]], float] = math.dist,
                      match_distance: float = MATCH_DISTANCE) -> list[float | None]:
    """For each crossing (in capture order), the share of its line's cars within RARITY_HALF_MIN
    that look like it. None when it has no signature or too few neighbours to judge."""
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
                    if m != n and distance(c.signature, signed[m][1].signature) <= match_distance)
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
            start: datetime | None = None, *,
            distance: Callable[[Sequence[float], Sequence[float]], float] = math.dist,
            match_distance: float = MATCH_DISTANCE,
            within: tuple[datetime, datetime] | None = None) -> list[Anchor]:
    """Distinctive cars matched between A and B by signature.

    A pair is a candidate when both cars are distinctive on their own line (look-alike share at
    most RARE_SHARE), it reached B lo_min to hi_min minutes after A, and the two signatures are
    within match_distance. Of the candidates, the largest set in which no two pairs cross is
    kept: first-in-first-out allows no other. Ties go to the higher total confidence, then to the
    earlier pairs. With `within` = (s, e), only the anchors of that set which the stretch
    encloses are returned: A more than hi_min before e, B at least hi_min after s. (Filtering
    the candidates instead would drop the true partner of an enclosed car whenever that partner
    is not enclosed itself, and leave the car only look-alikes to pair with.) Each anchor's
    confidence is the product of
    - how alike the two readings are (1 at the same reading, 0 at match_distance);
    - how rare the look is (1 for a look nobody shares, 0 at RARE_SHARE);
    - 1 / (1 + the other candidates of either car that look at least as alike);
    - the lower of the two crossings' own confidences."""
    oa, ob = ordered(a, start), ordered(b, start)
    found = _chain(_scored(oa, ob, lo_min, hi_min, distance, match_distance), oa, ob)
    if within is not None:
        found = _enclosed(found, *within, hi_min)
    return found


def _scored(oa: Sequence[Crossing], ob: Sequence[Crossing], lo_min: float, hi_min: float,
            distance: Callable[[Sequence[float], Sequence[float]], float],
            match_distance: float) -> list[tuple[_Candidate, float]]:
    """Every candidate pair with its confidence, sorted by (ia, ib). See `anchors`."""
    if not 0 <= lo_min < hi_min:
        raise ValueError("anchors need 0 <= lo_min < hi_min")
    if distance is math.dist:
        _check_scale(oa)
        _check_scale(ob)
    sa = look_alike_shares(oa, distance, match_distance)
    sb = look_alike_shares(ob, distance, match_distance)
    rare_b = [(j, c) for j, c in enumerate(ob) if _rare(sb[j])]
    tb = [c.at for _, c in rare_b]
    lo, hi = timedelta(minutes=lo_min), timedelta(minutes=hi_min)
    cands: list[_Candidate] = []
    for i, ca in enumerate(oa):
        if not _rare(sa[i]):
            continue
        for m in range(bisect_left(tb, ca.at + lo), bisect_right(tb, ca.at + hi)):
            j, cb = rare_b[m]
            d = distance(ca.signature, cb.signature)
            if d <= match_distance:
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
        confidence = ((1 - c.distance / match_distance) * (1 - c.share / RARE_SHARE)
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
    """The default distance needs components on a 0-1 scale. Nothing of the signature is put in
    the message: it is per-vehicle data."""
    for c in crossings:
        if c.signature is not None and not all(-SIGNATURE_LIMIT <= v <= SIGNATURE_LIMIT
                                               for v in c.signature):
            raise ValueError(
                f"{c.camera} ({c.method}): a signature component lies beyond "
                f"+-{SIGNATURE_LIMIT}. The default distance needs components on a 0-1 scale "
                "(see the transit module docstring); pass a `distance` for this form")


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
    wrong anchors cannot tilt it.

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


def _fit(found: Sequence[Anchor], start: datetime,
         extra: Sequence[Anchor] = ()) -> tuple[tuple[float, float], tuple[Anchor, ...]]:
    """The trend line of `found` (offset at `start`, cars per minute) and the anchors near it:
    those of `found`, then those of `extra` that share no car with one already kept. In A
    order."""
    xs = [_minutes(x.entered_at - start) for x in found]
    ys = [x.offset for x in found]
    slope = _theil_sen(xs, ys) if len(found) >= REANCHOR_MIN_ANCHORS else None
    slope = 0.0 if slope is None else slope
    at_start = _quantile(sorted(y - slope * x for x, y in zip(xs, ys)), 0.5)

    def off(x: Anchor) -> float:
        return abs(x.offset - at_start - slope * _minutes(x.entered_at - start))

    keep = max(INLIER_MIN_CARS, INLIER_MADS * _quantile(sorted(off(x) for x in found), 0.5))
    near = [x for x in found if off(x) <= keep]
    used_a, used_b = {x.a_index for x in near}, {x.b_index for x in near}
    near += [x for x in extra
             if off(x) <= keep and x.a_index not in used_a and x.b_index not in used_b]
    near.sort(key=lambda x: (x.a_index, x.b_index))
    return (at_start, slope), tuple(near)


# --- a whole run: epochs, gaps, windows -----------------------------------------------------

@dataclass(frozen=True)
class _Incoming:
    """An offset carried into an epoch, across a gap or from a prior."""
    offset: int
    uncertainty: float | None    # cars, about one sd; None when not known
    continued: bool              # a sure count with no gap in between: stays sure unchecked


def measure(a: Sequence[Crossing], b: Sequence[Crossing], lo_min: float, hi_min: float, *,
            gaps_a: Sequence[Gap] = (), gaps_b: Sequence[Gap] = (),
            start: datetime | None = None, end: datetime | None = None,
            offset: int | None = None, prior: Prior | None = None,
            distance: Callable[[Sequence[float], Sequence[float]], float] = math.dist,
            match_distance: float = MATCH_DISTANCE) -> Measurement:
    """Transits for one counter's crossings at A and B, cut into epochs at every gap.

    `offset`, if given, is the cars between the lines at `start` (say from a hand count), and the
    first epoch uses it as it is. `prior` (`prior_at` on an earlier measurement) is that count
    carried from before; `start` defaults to prior.at. The first epoch takes it like an offset
    carried across a gap, except that a sure prior its anchors cannot check stays sure. Either
    needs `start` seen: a gap over it raises ValueError.

    Every other epoch's offset comes from the first of these that applies:
    - "anchors": at least MIN_ANCHORS enclosed anchors near their line that do not dispute the
      offset carried in. Short of that, at least MIN_ANCHORS anchors near their line whose
      offset at the epoch's start lies within OUTLIER_CARS + AGREE_SDS sd of the carried one.
      Each car then takes the median of the kept anchors within REANCHOR_HALF_MIN of it, or
      their line where there are too few.
    - "disputed": enclosed anchors that disagree with the carried offset. Used, flagged.
    - "continued": a sure prior its anchors cannot check. Used as it is.
    - "carried": the offset carried across the gap. Used, flagged.
    - "few-anchors": anchors that cannot stand on their own, with nothing carried in to check:
      used, flagged.
    - "unknown": no offset and no transits.

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
    ta, tb = [c.at for c in oa], [c.at for c in ob]
    if start is None:
        firsts = [t[0] for t in (ta, tb) if t]
        if not firsts:
            raise ValueError("no crossings and no start")
        start = min(firsts)
    if end is None:
        lasts = [t[-1] for t in (ta, tb) if t]
        end = max(lasts) + _TICK if lasts else start
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
            incoming = _Incoming(prior.between, prior.uncertainty, continued=prior.sure)
        ea, eb = ordered(oa, s, e), ordered(ob, s, e)
        epoch = _epoch(ea, eb, s, e, lo_min, hi_min, distance, match_distance,
                       offset if not epochs else None, incoming)
        epochs.append(epoch)
        if epoch.offset is not None:
            eta, etb = [c.at for c in ea], [c.at for c in eb]
            out += _pair(eta, etb, _offsets(epoch, eta), flagged=epoch.source not in _SURE)
    return Measurement(method, start, end, tuple(epochs), tuple(out), tuple(ta), tuple(tb),
                       tuple(ga), tuple(gb))


def _epoch(ea: Sequence[Crossing], eb: Sequence[Crossing], s: datetime, e: datetime,
           lo_min: float, hi_min: float,
           distance: Callable[[Sequence[float], Sequence[float]], float],
           match_distance: float, given: int | None, incoming: _Incoming | None) -> Epoch:
    """One epoch's offset and where it came from (see `measure`)."""
    found = _chain(_scored(ea, eb, lo_min, hi_min, distance, match_distance), ea, eb)
    enclosed = _enclosed(found, s, e, hi_min)
    enclosed_near = _fit(enclosed, s)[1] if enclosed else ()
    line: tuple[float, float] | None = None
    inliers: tuple[Anchor, ...] = ()
    off: int | None = None
    uncertainty: float | None = None
    if given is not None:
        source, off, uncertainty = "given", given, 0.0
        inliers = _fit(found, s)[1] if found else ()     # shown against the given offset
    elif len(enclosed_near) >= MIN_ANCHORS:
        line, inliers = _fit(enclosed, s, extra=found)
        off = _line_offsets(line, inliers, s, [s])[0]
        if (incoming is not None and incoming.uncertainty is not None
                and abs(off - incoming.offset) > _tolerance(incoming.uncertainty)):
            source = "disputed"
        else:
            source, uncertainty = "anchors", ANCHORED_SD
    else:
        fit_line, fit_near = _fit(found, s) if found else (None, ())
        fit_off = _line_offsets(fit_line, fit_near, s, [s])[0] if fit_line is not None else None
        if incoming is not None:
            if (len(fit_near) >= MIN_ANCHORS and incoming.uncertainty is not None
                    and abs(fit_off - incoming.offset) <= _tolerance(incoming.uncertainty)):
                source, line, inliers = "anchors", fit_line, fit_near
                off, uncertainty = fit_off, ANCHORED_SD
            else:
                source = "continued" if incoming.continued else "carried"
                off, uncertainty = incoming.offset, incoming.uncertainty
        elif found:
            source, line, inliers, off = "few-anchors", fit_line, fit_near, fit_off
        else:
            source = "unknown"
    minutes = _minutes(e - s)
    exit_rate = len(eb) / minutes if minutes > 0 else None
    entry_rate = len(ea) / minutes if minutes > 0 else None
    return Epoch(start=s, end=e, offset=off, source=source,
                 estimate=offset_from_anchors(found),
                 drift=drift(inliers or found, exit_rate, entry_rate),
                 anchors=tuple(found), inliers=inliers, line=line,
                 enclosed=len(enclosed_near),
                 incoming=incoming.offset if incoming is not None else None,
                 uncertainty=uncertainty)


def _tolerance(uncertainty: float) -> float:
    return OUTLIER_CARS + AGREE_SDS * uncertainty


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
    at_start, slope = line
    out = []
    for t in times:
        lo, hi = bisect_left(marks, t - half), bisect_right(marks, t + half)
        if hi - lo < REANCHOR_MIN_ANCHORS:
            out.append(_round(at_start + slope * _minutes(t - start)))
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
    return _now(ta, tb, offset_at(epoch, at), at, epoch.start,
                flagged=epoch.source not in _SURE)


def prior_at(m: Measurement, at: datetime) -> Prior | None:
    """The cars between the lines at `at` (crossings at `at` itself not yet counted), for
    `measure(prior=...)` to start from. If `at` falls in a gap, the prior is taken where the next
    epoch starts, and its `at` says so. None when no epoch from `at` on has an offset."""
    epoch = _epoch_at(m, at)
    if epoch is None:
        later = [e for e in m.epochs if e.start > at]
        if not later:
            return None
        epoch, at = later[0], later[0].start
    if epoch.offset is None:
        return None
    return Prior(at, _between(m, at), epoch.source in _SURE, epoch.uncertainty)


def windows(m: Measurement, width_min: float = WINDOW_MIN) -> list[Window]:
    """Per window of `width_min` (aligned to the clock), the transits by exit and by entry time,
    the counts, the exit rate, the cars between at the window's end, and the drift residual."""
    width = timedelta(minutes=width_min)
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
        uncertain = any(e.start < w1 and e.end > w0 and e.source not in _SURE
                        for e in m.epochs)
        sx, sn = _stats(by_exit[k]), _stats(by_entry[k])
        res = sorted(residuals[k])
        out.append(Window(
            start=w0, end=w1, by_exit=sx, by_entry=sn,
            entered=_count(m.a_times, w0, w1), exited=exited,
            exit_rate=exited / seen_b if seen_b >= MIN_RATE_SPAN_MIN else None,
            between=_between(m, min(w1, m.end)),
            gap=gap, flagged=gap or uncertain or sx.flagged > 0 or sn.flagged > 0,
            anchors=len(res), anchor_residual=_quantile(res, 0.5)))
    return out


def record(m: Measurement, wins: Sequence[Window], current: Now | None = None, *,
           lane_type: str | None = None) -> dict:
    """The derived numbers for the JSONL log. No per-vehicle field: no signature, no single
    crossing, anchor or transit, only counts and statistics per window and per epoch."""
    return {
        "kind": "transit",
        "method": m.method,
        "lane_type": lane_type,
        "start": _iso(_floor(m.start, timedelta(minutes=WINDOW_MIN))),
        "end": _iso(wins[-1].end) if wins else None,
        "transits": len(m.transits),
        "transits_flagged": sum(1 for t in m.transits if t.flagged),
        "epochs": [_epoch_record(e) for e in m.epochs],
        "windows": [_window_record(w) for w in wins],
        "now": None if current is None else {
            "at": _iso(current.at), "between": current.between,
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


MIN_STATS_N = 3              # fewer transits than this in a window: no statistics are written,
                             # so a record never carries what is in effect one car's trip


def _window_record(w: Window) -> dict:
    def stats(s: Stats) -> dict:
        if s.n < MIN_STATS_N:
            return {"n": s.n, "flagged": s.flagged, "median": None, "p10": None, "p90": None}
        return {"n": s.n, "flagged": s.flagged, "median": _r(s.median), "p10": _r(s.p10),
                "p90": _r(s.p90)}
    return {
        "start": _iso(w.start), "by_exit": stats(w.by_exit), "by_entry": stats(w.by_entry),
        "entered": w.entered, "exited": w.exited, "exit_rate": _r(w.exit_rate, 3),
        "between": w.between, "gap": w.gap, "flagged": w.flagged, "anchors": w.anchors,
        "anchor_residual": _r(w.anchor_residual),
    }


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
                     continued=False)


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
    for i, e in enumerate(m.epochs):
        last = i == len(m.epochs) - 1
        if e.start <= at and (at < e.end or (last and e.end >= m.end)):
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
