"""The camera wait for one pair of counting lines: each counting method's wait from line A to a
CBP primary booth, side by side and never averaged (ADR-0015, proposed).

For one lane-type group and one method m (`loop` or `yolox`):

    W_m = tail_to_A + N_AB,m / mu_B,m + q_B x K_eff,m / mu_B,m

- tail_to_A: from the back of the line to line A, when the line reaches back over A. It comes
  from the queue reader on camera A's strips and on the street camera, in metres and m/s.
- N_AB,m / mu_B,m: the cars between the lines now over the rate they cross B
  (`transit.now_from`, as the runner writes it in each record's `now`).
- K_eff,m / mu_B,m: from B to the booths. K_eff = t_Bb x mu_B,m is one timed car's trip from B
  to the canopy front times the method's counted rate at B at that minute (`pairs.json`), and
  q_B is 1 when B sits inside the line.

Every constant is in the method's own counted units. A counter that misses half the cars at B
halves N_AB and mu_B together, so N/mu holds, and K_eff timed with the same zones halves too. A
count of real cars over a counted rate would read 1/p_B too high (the design's thinning runs).
So B to the booths uses K_eff, never a count of real cars, and the methods are never mixed: each
is recorded on its own, with `agree` saying whether they agree, and no mean or envelope.

ASSUMPTION (ADR-0014): first in, first out in aggregate from A to the booths within one lane-type
group, with no entries or exits. Each group is measured apart by the runner (QUESTIONS.md Q-22:
at PDN "sentri" is A lanes 4-5 and B col-e, "normal" the other lanes); "mixed" pools every lane of a
pair whose lines share no lane type.

A segment that cannot be measured is not known, and then the total is a floor: the sum of the
lows that are known, with no minutes and no high. Today B to the booths is never known. No
K_eff has been timed, and q_B is not measured: the runner does not yet record how long B's
zones stand occupied, which is what would say B is inside the line. `combine` takes q_B as an
input (`q_b`) for when it does; until then every total is a floor.

`display` is the one place that decides what the owner is shown (ADR-0015 decision 2, accepted
2026-10-26: on this PC only, never in public). Minutes appear only while the count between the
lines is sure and every segment is known; a floor is shown as "≥ N" with its reasons, and an
unsure count as "—, count not anchored". The vote stays undecided (ADR-0015 decisions 3 and 4,
proposed), so every group's `would_vote` is false, with its reasons; nothing here builds a
Signal.

Everything here is pure: no file, network or clock is read, and nothing from CBP, fusion or the
poller is imported, so the same records give the same output. CBP is never an input to a camera
number. The output holds derived numbers only, and its one time is cut to the minute.
"""
from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from .crossing import METHODS
from .measure import (
    BEYOND_VIEW_M,
    MIN_SPEED_M_S,
    ROLLING_SECONDS,
    LaneEstimate,
    rolling,
    wait_bounds,
)
from .transit import _SURE, MIN_EXIT_RATE, OUTLIER_CARS, RATE_WINDOW_MIN, WINDOW_MIN

Part = tuple[float | None, float | None, float | None, str | None]  # one lane's (minutes, low,
                                                                    # high, why it is not known)

MIXED = "mixed"              # the group of a pair whose lines share no lane type (runner.MIXED)
CBP_LANE = {"sentri": "car_sentri"}   # a group's CBP lane id. "normal" pools CBP's standard and
                                      # Ready lanes (Q-22, ASSUMPTION), so it has none, and nor
                                      # has "mixed"; other types get one once the owner names it
RANGE_SDS = 2.0              # ranges are 2 sigma. ASSUMPTION until self_check has
                             # SELF_CHECK_MIN_WINDOWS windows and sets them to cover 80-90 %
LAG_MIN = 15.0               # ASSUMPTION: a count now rests on anchors that crossed A at least one
                             # trip earlier, and never less than 15 min earlier
MIN_EXITS = 4                # n_B, the exits the rate rests on: this few or fewer bound no high
AGREE_MIN = 3.0              # ASSUMPTION: two methods agree within max(3 min, 20 % of the larger)
AGREE_SHARE = 0.20
QUEUE_FRESH_S = 300          # ASSUMPTION: a queue-reader lane read longer ago than this (the worker
                             # reads every 120 s) is down, so its tail is not known
K_EFF_MIN_TIMINGS = 3        # a K_eff from fewer timed cars can be used, but never votes
HAND_COUNT_TOLERANCE = 0.05  # ASSUMPTION: a method's capture is checked when a hand count puts it
                             # within 5 % at both lines
SELF_CHECK_MIN_WINDOWS = 30  # windows before the A->B range counts as calibrated
NOT_SHOWN = "recorded, not shown (show_minutes is false in pairs.json)"
NO_COUNT = "no count of the cars between the lines"   # transit's Now has none: a line is down at
                                                      # the live edge, or the epoch has no offset
STREET_UNTYPED = "street columns not typed: counted for every lane type"
NO_DEVICE = "device not logged"   # a yolox record from before records named their device


@dataclass(frozen=True)
class Segment:
    name: str                    # "tail_to_a" | "a_to_b" | "b_to_booth"
    minutes: float | None        # None: not known, and the total is a floor
    low: float | None            # a lower bound, kept even when not known where there is one
    high: float | None
    known: bool                  # minutes, low and high all set
    sensor: str
    why: str | None = None       # why it is not known
    info: tuple[str, ...] = ()   # what it rests on that is not measured (MethodWait.info)

    def to_dict(self) -> dict:
        return {"name": self.name, "minutes": _r(self.minutes), "low": _r(self.low),
                "high": _r(self.high), "known": self.known, "sensor": self.sensor,
                "why": self.why}


@dataclass(frozen=True)
class MethodWait:
    method: str
    lane_type: str
    segments: tuple[Segment, ...]
    minutes: float | None        # the sum, when every segment is known
    low: float | None            # the sum of the lows; for a floor, of the lows that are known
    high: float | None
    floor: bool
    source: str | None           # the source of the A->B count's epoch at t0 (transit.Epoch)
    offset_sd: float | None
    count_flagged: bool          # transit's Now.flagged: the count at t0 is not sure
    validation: tuple[str, ...]  # why no Signal at all
    transient: tuple[str, ...]   # why a validated pair would send no minutes this tick
    info: tuple[str, ...]        # shown, never widen anything

    @property
    def sure(self) -> bool:
        """The count between the lines is sure at t0: anchored, given or continued from such a
        count, with its sd known, and checked by anchors within transit.CHECK_MAX_MIN."""
        return self.source in _SURE and self.offset_sd is not None and not self.count_flagged

    def to_dict(self) -> dict:
        return {"method": self.method, "lane_type": self.lane_type,
                "minutes": _r(self.minutes), "low": _r(self.low), "high": _r(self.high),
                "floor": self.floor,
                "count": {"source": self.source, "offset_sd": self.offset_sd,
                          "flagged": self.count_flagged, "sure": self.sure},
                "segments": [s.to_dict() for s in self.segments],
                "validation": list(self.validation), "transient": list(self.transient),
                "info": list(self.info)}


# --- A to B ---------------------------------------------------------------------------------

def a_to_b(record: Mapping) -> Segment:
    """N / mu from the runner's record, with its range:

        sigma^2 = offset_sd^2 + (OUTLIER_CARS + |drift| x max(T, LAG_MIN) / 60)^2
        low  = max(0, N - 2 sigma) / (mu (1 + 2 / sqrt(n_B)))
        high = (N + 2 sigma) / (mu (1 - 2 / sqrt(n_B)))

    with T = N / mu and n_B = mu x min(RATE_WINDOW_MIN, minutes since the epoch began), the
    exits the rate rests on. A count whose sd is not known (few-anchors, disputed, unknown) gives
    no minutes and no range; a stalled line B gives no number; n_B <= MIN_EXITS gives no high."""
    sensor = f"{record.get('method')} counts at lines A and B"
    now = record.get("now") or {}
    if now.get("between") is None:
        return _unknown("a_to_b", sensor, NO_COUNT)
    epoch = _epoch_at_now(record)
    sd = None if epoch is None else epoch.get("offset_sd")
    if sd is None:
        source = None if epoch is None else epoch.get("source")
        return _unknown("a_to_b", sensor, f"count not anchored ({source})")
    rate = _rate(record)
    if rate is None:
        return _unknown("a_to_b", sensor, "no exit rate at line B yet")
    mu, n_b = rate
    if mu < MIN_EXIT_RATE:
        return _unknown("a_to_b", sensor, "line B stalled")
    n = now["between"]
    if n < 0:
        return _unknown("a_to_b", sensor, "a negative count: the offset is wrong")
    t = n / mu
    drift = abs(epoch.get("drift_cars_per_hour") or 0.0)
    sigma = math.sqrt(sd ** 2 + (OUTLIER_CARS + drift * max(t, LAG_MIN) / 60) ** 2)
    k = _rate_term(n_b)
    low = max(0.0, n - RANGE_SDS * sigma) / (mu * (1 + k))
    if n_b <= MIN_EXITS:
        return Segment("a_to_b", None, low, None, False, sensor,
                       f"too few exits counted at B to bound the rate (n_B {n_b:.0f})")
    return Segment("a_to_b", t, low, (n + RANGE_SDS * sigma) / (mu * (1 - k)), True, sensor)


# --- B to the booths ------------------------------------------------------------------------

def b_to_booth(record: Mapping, pair: Mapping, at: datetime, q_b: bool | None = None) -> Segment:
    """q_B x K_eff / mu, with the range [K_lo / mu_hi, K_hi / mu_lo] plus the cars under the
    canopy that no camera sees, [lo, hi] x primary_booths / mu (a counted mu is never above the
    real rate, so this bound is safe).

    Not known, so a floor of 0, unless pairs.json has a K_eff for this method, lane type and
    hour, and q_b is True. Nothing sets q_b today (module docstring)."""
    method, group = record.get("method"), record.get("lane_type") or MIXED
    sensor = f"{method} count at line B and timed trips from B to the booths"
    entry = _k_eff(pair, method, group, at)
    if entry is None:
        return _unknown("b_to_booth", sensor,
                        "B to the booths not timed (no K_eff for this method, type and hour)",
                        low=0.0)
    if q_b is not True:
        return _unknown("b_to_booth", sensor,
                        "B inside the line not established (B's occupancy is not measured)",
                        low=0.0)
    rate = _rate(record)
    if rate is None or rate[0] < MIN_EXIT_RATE:
        return _unknown("b_to_booth", sensor, "no exit rate at line B", low=0.0)
    mu, n_b = rate
    k_lo, k_hi = float(entry["low"]), float(entry["high"])
    k = _rate_term(n_b)
    low = k_lo / (mu * (1 + k))
    if n_b <= MIN_EXITS:
        return Segment("b_to_booth", None, low, None, False, sensor,
                       f"too few exits counted at B to bound the rate (n_B {n_b:.0f})")
    minutes = (k_lo + k_hi) / 2 / mu
    booths = pair.get("primary_booths")
    if booths is None:
        return Segment("b_to_booth", None, low, None, False, sensor,
                       "primary booth count not given: no bound for the cars under the canopy")
    per_lo, per_hi = pair.get("under_canopy_cars_per_booth") or (0, 2)
    return Segment("b_to_booth", minutes, min(low + per_lo * booths / mu, minutes),
                   max(k_hi / (mu * (1 - k)) + per_hi * booths / mu, minutes), True, sensor)


# --- the tail before A ----------------------------------------------------------------------

def tail_to_a(queue_windows: Iterable[Mapping], pair: Mapping, group: str,
              at: datetime) -> Segment:
    """From the back of the line to line A, from the queue reader's lane signals (the dicts the
    worker emits; error lines are skipped).

    Per strip of camera A in the group: 0 while its line does not reach A (queue_m < a_m and not
    past the view's near edge); (queue_m - a_m) / v15 while it reaches A in view; and past the
    near edge, r_m / v15 + the gap to the street / its speed + the street's line / its v15. v15
    is the lane's 15-min rolled speed (`measure.rolling`) over the windows in `queue_windows`,
    and each range is `measure.wait_bounds` on the windows' quarter speeds, so in view the
    length and speed share their scale error and it cancels. The gap's length is the map's
    value +- its uncertainty (clamped at 0), crossed at between the neighbours' fastest and
    slowest speeds, and its minutes at their mean speed (ASSUMPTION).

    Which of the group's lanes a car joining now takes is not known, so the minutes are the
    median lane's and the range runs from the lowest lane's low to the highest lane's high.
    The street's columns are not typed: they count for every group (ASSUMPTION).

    With no lane of the group reaching A, a line on the street is a line before A with free road
    to it (something metering traffic before A), and is timed alone, unless another type's strip
    of camera A runs past its near edge (or the head gap dropped a run that reached it): the
    street's line is then that type's, and whether this type's cars queue in it is not known (an
    open owner question), so a floor of 0. A street that cannot be read then counts as no line,
    since a line before A would show at A as well, in this group's strips or another type's.

    Not known, so a floor: a strip down, stale or unread; no recent speed; a line stalled; a
    line past the near edge whose street cannot be read, ends between the views, or runs past
    the street's view."""
    t_at = at.timestamp()
    a_cam, street_cam = pair.get("a"), pair.get("street")
    sensor = f"queue reader on {a_cam}"
    lanes = pair.get("a_lanes") or {}
    own = sorted(lane for lane, kind in lanes.items() if group == MIXED or kind == group)
    north = _readings(queue_windows, a_cam, t_at)
    street = _readings(queue_windows, street_cam, t_at) if street_cam else {}
    if not own:
        return _unknown("tail_to_a", sensor, f"no strip of {a_cam} is of lane type {group}",
                        low=0.0)
    parts: list[Part] = []
    used_street = False
    for lane in own:
        hist = north.get(lane)
        latest = _latest(hist, t_at)
        a_m, r_m = (pair.get("a_m") or {}).get(lane), (pair.get("r_m") or {}).get(lane)
        if latest is None:
            why = f"{a_cam} {lane} not read in the last {QUEUE_FRESH_S // 60} min"
            parts.append((None, 0.0, None, why))
            continue
        far = _dropped(latest)
        if latest.state == "no_queue" and (far is None or a_m is not None and far[1] < a_m):
            parts.append((0.0, 0.0, 0.0, None))
            continue
        if far is not None:
            why = f"{a_cam} {lane}: a run {far[0]:g}-{far[1]:g} m in was dropped by the head gap"
            parts.append((None, 0.0, None, why))
            continue
        if latest.state == "insufficient" and not latest.queue_m:
            parts.append((None, 0.0, None, f"{a_cam} {lane}: too few frames to read"))
            continue
        if a_m is None or r_m is None:
            parts.append((None, 0.0, None, f"line A's place on {a_cam} {lane} not known"))
            continue
        if latest.queue_m < a_m and not latest.beyond_view:
            parts.append((0.0, 0.0, 0.0, None))
            continue
        if not latest.beyond_view:
            parts.append(_timed(hist, latest.queue_m - a_m, t_at, f"{a_cam} {lane}"))
            continue
        used_street = True
        parts.append(_past_view(hist, r_m, street, pair, t_at, f"{a_cam} {lane}"))
    if all(p[0] == 0.0 for p in parts) and street:
        on_street = _street(street, t_at, from_head=True)
        # A line on the street runs on from another lane type's lanes past camera A's near
        # edge, so it is that line, not one metered before A. Whether this type's cars queue
        # in it before their own lane splits off is the owner's to say: not known until then.
        # A run the head gap dropped that reached the near edge may be that line as well.
        others = [lane for lane in sorted(lanes) if lane not in own
                  and (e := _latest(north.get(lane), t_at)) is not None
                  and (e.beyond_view or _dropped_to_edge(e, pair, lane))]
        if on_street is not None and on_street[0] != 0.0 and others:
            return _unknown("tail_to_a", f"{sensor} and {street_cam}",
                            f"the street's line runs on from {', '.join(others)} past "
                            f"{a_cam}'s near edge; whether {group} cars queue in it is not known",
                            low=0.0)
        # No strip of camera A reaches past its near edge: a line on the street is metered
        # before A, with free road between.
        # When every column the street holds is a run the head gap dropped, no line is known
        # to be there: the floor stands, but the card must not say there is one.
        if on_street is not None and on_street[0] != 0.0:
            lined = any((e := _latest(hist, t_at)) is not None and e.queue_m
                        and e.state in ("queue", "stalled", "insufficient")
                        for hist in street.values())
            held = ("a line on the street with free road to A: metered before A" if lined else
                    "a run on the street the head gap dropped: if it is a line, it is metered "
                    "before A")
            return _segment("tail_to_a", f"{sensor} and {street_cam}", [on_street],
                            (held, STREET_UNTYPED))
    if not used_street:
        return _segment("tail_to_a", sensor, parts)
    gap = pair.get("gap_street_a_m") or {}
    gap_info = (f"street-to-A gap {gap.get('value')} +- {gap.get('uncertainty')} m "
                f"({gap.get('source')}), crossed at between its neighbours' speeds")
    return _segment("tail_to_a", f"{sensor} and {street_cam}", parts,
                    (gap_info, STREET_UNTYPED))


# --- one method, one group ------------------------------------------------------------------

def method_wait(record: Mapping, tail: Segment, pair: Mapping, at: datetime,
                q_b: bool | None = None) -> MethodWait:
    """One method's wait for one group: the sum of the three segments, or a floor (module
    docstring), with its flags split by what they mean (validation, transient, info)."""
    method, group = record.get("method"), record.get("lane_type") or MIXED
    ab = a_to_b(record)
    bb = b_to_booth(record, pair, at, q_b)
    segments = (tail, ab, bb)
    floor = not all(s.known for s in segments)
    lows = [s.low for s in segments if s.low is not None]
    if floor:
        minutes, low, high = None, (sum(lows) if lows else None), None
    else:
        minutes = sum(s.minutes for s in segments)
        low, high = sum(lows), sum(s.high for s in segments)
    epoch = _epoch_at_now(record) or {}
    now = record.get("now") or {}
    entry = _k_eff(pair, method, group, at)

    validation = _pair_reasons(pair, group)
    if not _hand_counted(pair, method, group, at):
        validation.append(f"no hand count within {HAND_COUNT_TOLERANCE:.0%} at A and B "
                          f"for {method} at this hour")
    validation.append(f"A->B range not calibrated (fewer than {SELF_CHECK_MIN_WINDOWS} "
                      "self-check windows)")
    if bb.known and entry is not None and (entry.get("n") or 0) < K_EFF_MIN_TIMINGS:
        validation.append(f"K_eff from fewer than {K_EFF_MIN_TIMINGS} timings")
    if (tail.minutes or 0) > 0 or (tail.low or 0) > 0:
        validation.append("tail from the queue reader, unchecked by a timed crossing")

    transient = []
    if now.get("flagged"):
        # With no count at all the flag says so: the epoch before a gap at the live edge can be
        # anchored, so "not checked by anchors" would name the wrong cause.
        transient.append(f"count flagged: {NO_COUNT}" if now.get("between") is None
                         else "count flagged: carried, or not checked by anchors within 60 min")
    rate = _rate(record)
    if rate is not None and _since_min(record) < RATE_WINDOW_MIN:
        transient.append(f"exit rate over less than {RATE_WINDOW_MIN} min (a gap or a start)")
    if rate is not None and rate[1] <= MIN_EXITS:
        transient.append("too few exits counted at B")
    if rate is not None and rate[0] < MIN_EXIT_RATE:
        transient.append("stalled at line B")
    unknown = [s.name for s in segments if not s.known]
    if unknown:
        transient.append(f"floor: {', '.join(unknown)} not known")

    info = [i for s in segments for i in s.info]
    if ab.known:
        info.append("steady discharge assumed: the exit rate of the last 10 min holds")
    return MethodWait(method, group, segments, minutes, low, high, floor,
                      epoch.get("source"), epoch.get("offset_sd"), bool(now.get("flagged")),
                      tuple(validation), tuple(transient), tuple(info))


# --- the camera record ----------------------------------------------------------------------

def combine(records: Sequence[Mapping], queue_windows: Iterable[Mapping], pair: Mapping,
            at: datetime, q_b: Mapping[tuple[str, str], bool] | None = None,
            not_measured: Mapping[tuple[str, str], str] | None = None) -> dict:
    """The camera record for one tick: per lane-type group, each method's wait side by side,
    whether they agree, the group's CBP lane id (`lane`), and `would_vote` (always false while
    ADR-0015's vote is undecided) with its reasons. `records` are the runner's, one per method
    and group (TransitRunner.records); `queue_windows` the queue reader's recent lane signals;
    `pair` an entry of pairs.json plus a_lanes, a_m and r_m (computed at load); `at` the
    runner's seen_until(). `q_b` is (method, group) -> B inside the line, when measured. A
    method's entry keeps its record's `anchoring` and `devices` (runner.records) as they are.
    `not_measured` is (method, group) -> why the runner does not measure it
    (TransitRunner.not_measured): such a method is listed with its reason, never as a wait.
    It wins over a record of that method and group, which then feeds neither `agree` nor the
    vote's reasons: the live runner writes none, so only a replay that rebuilt not_measured
    from cameras.json as it is now can hold one (estimate.replay)."""
    windows = list(queue_windows)
    nm = not_measured or {}
    by_group: dict[str, dict[str, Mapping]] = defaultdict(dict)
    for rec in records:
        m, g = rec.get("method"), rec.get("lane_type") or MIXED
        if (m, g) not in nm:
            by_group[g][m] = rec
    skipped: dict[str, dict[str, str]] = defaultdict(dict)
    for (m, g), why in nm.items():
        skipped[g][m] = why
        by_group[g]                     # a group no method measures still appears
    groups = {}
    for group in sorted(by_group):
        tail = tail_to_a(windows, pair, group, at)
        waits = {m: method_wait(rec, tail, pair, at, (q_b or {}).get((m, group)))
                 for m, rec in by_group[group].items()}
        reasons: list[str] = []
        for w in waits.values():
            reasons += [r for r in w.validation if r not in reasons]
        methods = {m: w.to_dict() for m, w in waits.items()}
        for m, rec in by_group[group].items():
            # carried as the runner wrote them: how far the count has come towards anchors,
            # and where YOLOX ran (shown, never an input to a number)
            methods[m].update({k: rec[k] for k in ("anchoring", "devices")
                               if rec.get(k) is not None})
        for m, why in skipped[group].items():
            methods[m] = {"method": m, "lane_type": group, "not_measured": why}
            reasons.append(f"{m} not measured: {why}")
        order = {m: i for i, m in enumerate(METHODS)}
        groups[group] = {"lane_type": group, "lane": CBP_LANE.get(group),
                         "methods": dict(sorted(methods.items(),
                                                key=lambda kv: order.get(kv[0], len(order)))),
                         "agree": agree(waits.values()),
                         "would_vote": not reasons, "would_vote_reasons": reasons}
    first = records[0] if records else {}
    return {"kind": "camera_wait", "pair": pair.get("a"),
            "port_number": pair.get("port_number"), "direction": pair.get("direction"),
            "line_a": first.get("line_a"), "line_b": first.get("line_b"),
            "at": _iso(_floor(at, timedelta(minutes=1))), "groups": groups}


def agree(waits: Iterable[MethodWait]) -> bool | None:
    """Whether every method's minutes lie within max(AGREE_MIN, AGREE_SHARE x the largest) of
    each other; None while fewer than two methods have minutes. Agreement is necessary, never
    sufficient: both counters see the same frames and zones, and the same cars hide both."""
    vals = [w.minutes for w in waits]
    if len(vals) < 2 or any(v is None for v in vals):
        return None
    return max(vals) - min(vals) <= max(AGREE_MIN, AGREE_SHARE * max(vals))


# --- what the owner is shown ----------------------------------------------------------------

def display(wait: Mapping, show_minutes: bool = True) -> dict:
    """What the owner's page and terminal show for one method's wait (a `MethodWait.to_dict()`):
    `value` and `detail`, and whether minutes are shown. The rule lives here alone.

    - show_minutes false (pairs.json): nothing but that it is recorded.
    - A method the runner does not measure for this group: "—", not measured, and why.
    - The count between the lines not sure (not anchored, given or continued from one, sd not
      known, or not checked by anchors within 60 min): "—", count not anchored. An anchored
      epoch with no count now (a line down at the live edge) says there is no count.
    - A floor: "≥ N" with N the known lows' sum rounded down, and why each unknown segment is.
    - Otherwise the minutes and their range, with any transient flag."""
    if not show_minutes:
        return {"value": "", "detail": NOT_SHOWN, "minutes_shown": False}
    if wait.get("not_measured"):
        return {"value": "—", "detail": f"not measured: {wait['not_measured']}",
                "minutes_shown": False}
    count = wait.get("count") or {}
    if not count.get("sure"):
        anchored = count.get("source") in _SURE and count.get("offset_sd") is not None
        counted = all(s.get("why") != NO_COUNT for s in wait.get("segments") or []
                      if s.get("name") == "a_to_b")
        why = (f"count not anchored ({count.get('source')})" if not anchored
               else "count not checked by anchors in the last 60 min" if counted
               else NO_COUNT)
        return {"value": "—", "detail": why, "minutes_shown": False}
    if wait.get("floor"):
        reasons = [s["why"] for s in wait.get("segments", []) if not s["known"] and s["why"]]
        low = wait.get("low")
        value = "—" if low is None or math.floor(low) <= 0 else f"≥ {math.floor(low)}"
        return {"value": value, "detail": "; ".join(reasons), "minutes_shown": False}
    detail = [f"range {math.floor(wait['low'])}–{math.ceil(wait['high'])} min"]
    detail += list(wait.get("transient") or [])
    return {"value": f"{round(wait['minutes'])}", "detail": "; ".join(detail),
            "minutes_shown": True}


def display_text(wait: Mapping, show_minutes: bool = True) -> str:
    """`display` as one line of text."""
    shown = display(wait, show_minutes)
    if not shown["value"]:
        return shown["detail"]
    unit = "" if shown["value"] == "—" else " min"
    return f"{shown['value']}{unit}" + (f" ({shown['detail']})" if shown["detail"] else "")


def status_text(record: Mapping, show_minutes: bool = True) -> str:
    """The terminal's camera-wait lines for one camera record: one per group, each method's
    wait side by side, never averaged. In ASCII: a Windows console in cp1252 cannot print "≥" or
    "—" (UnicodeEncodeError on this PC, 2026-10-26), and the status prints every tick."""
    return _status(record, show_minutes).translate(_ASCII)


_ASCII = str.maketrans({"≥": ">=", "—": "-", "–": "-"})


def _status(record: Mapping, show_minutes: bool) -> str:
    if not show_minutes:
        return f"  camera wait: {NOT_SHOWN}"
    groups = record.get("groups") or {}
    if not groups:
        return "  camera wait: nothing measured yet"
    lines = []
    for group, g in groups.items():
        lane = g.get("lane") or "no CBP lane"
        methods = "  |  ".join(f"{m}: {display_text(w)}" for m, w in g["methods"].items())
        agreed = {True: "methods agree", False: "methods DISAGREE", None: "agreement —"}
        lines.append(f"  camera wait {group} ({lane}): {methods}  [{agreed[g.get('agree')]}; "
                     f"votes: {'yes' if g.get('would_vote') else 'no'}]")
    return "\n".join(lines)


# --- the self-check -------------------------------------------------------------------------

def self_check(records: Iterable[Mapping]) -> dict[str, dict]:
    """The logged Now against what a later horizon measured for the same cars.

    A record's unflagged, sure Now at t0 says how long a car crossing A then will take to B. The
    by-entry window holding t0, once a later record marks it complete and unflagged, is what
    those cars took, re-measured with anchors around t0. Per window and per (method, group),
    the error is the median Now logged in the window minus that by-entry median, and the window
    is covered when the median of their ranges holds it. This tests the live-edge offset and
    the steady discharge, needs no GPS, and is what calibrates the A->B range. Records are the
    runner's, in the order written. Yolox records are graded per device (`record_device`), one
    named "yolox/<group> on <device>": the same frames give the same counts per path, not
    across paths, so a CPU window never grades a GPU Now. One that names no device keeps the
    plain name and is never pooled with one that does."""
    width = timedelta(minutes=WINDOW_MIN)
    predicted: dict[tuple, list[Segment]] = defaultdict(list)
    truth: dict[tuple, float] = {}
    for rec in records:
        if rec.get("kind") != "transit":
            continue
        key = (rec.get("method"), rec.get("lane_type") or MIXED, record_device(rec))
        now = rec.get("now") or {}
        epoch = _epoch_at_now(rec) or {}
        seg = a_to_b(rec)
        if seg.known and not now.get("flagged") and epoch.get("source") in _SURE:
            predicted[(*key, _floor(_parse(now["at"]), width))].append(seg)
        for w in rec.get("windows") or []:
            entry = w.get("by_entry") or {}
            if (w.get("by_entry_complete") and not w.get("flagged")
                    and entry.get("median") is not None):
                truth[(*key, _parse(w["start"]))] = entry["median"]
    errors: dict[str, list[float]] = defaultdict(list)
    covered: dict[str, list[bool]] = defaultdict(list)
    for key in sorted(set(predicted) & set(truth), key=lambda k: (*k[:2], k[2] or "", k[3])):
        segs, real = predicted[key], truth[key]
        name = f"{key[0]}/{key[1]}" + ("" if key[2] in (None, NO_DEVICE) else f" on {key[2]}")
        errors[name].append(_median([s.minutes for s in segs]) - real)
        covered[name].append(_median([s.low for s in segs]) <= real
                             <= _median([s.high for s in segs]))
    out = {}
    for name in sorted(errors):
        errs = sorted(errors[name])
        out[name] = {"windows": len(errs), "error_median": _r(_quantile(errs, 0.5)),
                     "error_p10": _r(_quantile(errs, 0.1)), "error_p90": _r(_quantile(errs, 0.9)),
                     "abs_error_p90": _r(_quantile(sorted(abs(e) for e in errs), 0.9)),
                     "covered": _r(sum(covered[name]) / len(covered[name]), 3),
                     "calibrated": len(errs) >= SELF_CHECK_MIN_WINDOWS}
    return out


def record_device(rec: Mapping) -> str | None:
    """Where a transit record's YOLOX counts were made: its `devices` (runner.records, camera ->
    detect.DetectorCounter.device_record) as one name, "cpu" or "gpu:<adapter>", or the
    cameras' names joined with " + " when they differ; NO_DEVICE for a yolox record that names
    none. None for any other method's record: no YOLOX path touches a loop count."""
    if rec.get("method") != "yolox":
        return None
    devices = rec.get("devices")
    if not isinstance(devices, Mapping) or not devices:
        return NO_DEVICE
    names = sorted({str((d.get("device") if isinstance(d, Mapping) else None) or NO_DEVICE)
                    for d in devices.values()})
    return " + ".join(names)


# --- helpers --------------------------------------------------------------------------------

def _unknown(name: str, sensor: str, why: str, low: float | None = None) -> Segment:
    return Segment(name, None, low, None, False, sensor, why)


def _segment(name: str, sensor: str, parts: Sequence[Part], info: tuple[str, ...] = (),
             ) -> Segment:
    """One segment from several lanes' parts: the median lane's minutes and every lane's range;
    a floor at the lowest low if any lane is not known (a car may join that lane)."""
    if any(m is None for m, _, _, _ in parts):
        whys = [why for m, _, _, why in parts if m is None and why]
        lows = [low if low is not None else 0.0 for _, low, _, _ in parts]
        return Segment(name, None, min(lows), None, False, sensor,
                       "; ".join(whys) or "not known", info)
    return Segment(name, _median([m for m, _, _, _ in parts]), min(p[1] for p in parts),
                   max(p[2] for p in parts), True, sensor, None, info)


def _readings(windows: Iterable[Mapping], camera: str | None,
              t_at: float) -> dict[str, list[tuple[float, LaneEstimate]]]:
    """camera's lane windows in the ROLLING_SECONDS up to `t_at`, never after it (the window
    estimate.within hands over, so a replay gives what the live tick gave), as
    `measure.rolling`'s history, oldest first."""
    out: dict[str, list[tuple[float, LaneEstimate]]] = defaultdict(list)
    for w in windows:
        ev = w.get("evidence")
        if not isinstance(ev, Mapping) or ev.get("camera") != camera or not w.get("observed_at"):
            continue                             # an error line, or another camera
        t = _parse(w["observed_at"]).timestamp()
        if not 0 <= t_at - t <= ROLLING_SECONDS:
            continue
        out[ev["lane"]].append((t, LaneEstimate(
            camera=ev["camera"], lane=ev["lane"], port_number=str(ev.get("port_number", "")),
            direction=str(ev.get("direction", "")), state=ev.get("state", "insufficient"),
            frames=ev.get("frames", 0), seconds=float(ev.get("seconds") or 0.0),
            queue_m=float(ev.get("queue_m") or 0.0), beyond_view=bool(ev.get("beyond_view")),
            speed_m_s=ev.get("speed_m_s"), detail=dict(ev.get("detail") or {}))))
    for hist in out.values():
        hist.sort(key=lambda item: item[0])
    return out


def _latest(hist: list[tuple[float, LaneEstimate]] | None, t_at: float) -> LaneEstimate | None:
    if not hist or t_at - hist[-1][0] > QUEUE_FRESH_S:
        return None
    return hist[-1][1]


def _speeds(hist: list[tuple[float, LaneEstimate]],
            t_at: float) -> tuple[float, float, float] | None:
    """(15-min speed, fastest quarter, slowest quarter) of one lane, or None with no recent
    queue speed. The quarters are clamped at MIN_SPEED_M_S, as the worker clamps them."""
    speed = rolling(hist, t_at, 0.0)["speed_m_s"]
    quarters = [q for t, e in hist if t_at - t <= ROLLING_SECONDS
                and e.state in ("queue", "stalled") and e.speed_m_s is not None
                for q in (e.detail.get("quarter_speeds") or [e.speed_m_s])]
    if speed is None or not quarters:
        return None
    return speed, max(max(quarters), MIN_SPEED_M_S), max(min(quarters), MIN_SPEED_M_S)


def _timed(hist: list[tuple[float, LaneEstimate]], length_m: float, t_at: float,
           label: str) -> Part:
    """`length_m` of one lane's line at its 15-min speed, with the range on the quarter speeds
    (`measure.wait_bounds`, as the worker times a lane). A stalled line is a floor."""
    speeds = _speeds(hist, t_at)
    if speeds is None:
        return None, 0.0, None, f"{label}: no recent speed"
    speed, fastest, slowest = speeds
    low, high = wait_bounds(0, 0, length_m, fastest, slowest, True)
    if speed < MIN_SPEED_M_S:
        return None, low, None, f"{label}: line stalled"
    minutes = length_m / speed / 60
    return minutes, min(low, minutes), max(high, minutes), None


def _dropped(latest: LaneEstimate) -> list[float] | None:
    """[first, last] metres of a run the reader's head gap (cameras.json max_head_gap_m) dropped
    from a no_queue reading, else None. Such a reading is not known to be free road: it may be a
    line held inside the view (measure._queue_run), so it is a floor, never a known 0."""
    far = latest.detail.get("far_run_m") if latest.state == "no_queue" else None
    return [float(far[0]), float(far[1])] if far else None


def _dropped_to_edge(latest: LaneEstimate, pair: Mapping, lane: str) -> bool:
    """A run the head gap dropped that reached the strip's near edge, so it may run on past the
    view as a kept run would (measure: beyond_view). The edge is line A's place plus the strip's
    view past it (estimate.line_a_place); a strip with neither known counts as reaching it."""
    far = _dropped(latest)
    if far is None:
        return False
    a_m, r_m = (pair.get("a_m") or {}).get(lane), (pair.get("r_m") or {}).get(lane)
    return a_m is None or r_m is None or far[1] > a_m + r_m - BEYOND_VIEW_M


def _street(street: Mapping[str, list[tuple[float, LaneEstimate]]], t_at: float,
            from_head: bool) -> Part | None:
    """The street's line, timed per queued column and taken as `_segment` takes lanes. None when
    no column could be read; (0, 0, 0) when none is queued. From the street's booth end, or with
    `from_head` from the line's own head (a line metered before A starts where it is held)."""
    parts, read = [], False
    for lane in sorted(street):
        latest = _latest(street[lane], t_at)
        if latest is None or (latest.state == "insufficient" and not latest.queue_m):
            continue
        read = True
        if (far := _dropped(latest)) is not None:
            why = f"street {lane}: a run {far[0]:g}-{far[1]:g} m in was dropped by the head gap"
            parts.append((None, 0.0, None, why))
            continue
        if latest.state not in ("queue", "stalled", "insufficient") or not latest.queue_m:
            continue
        head = float(latest.detail.get("queue_head_m") or 0.0) if from_head else 0.0
        minutes, low, high, why = _timed(street[lane], latest.queue_m - head, t_at,
                                         f"street {lane}")
        if latest.beyond_view:
            minutes, high, why = None, None, f"street {lane}: the line runs past the street's view"
        parts.append((minutes, low, high, why))
    if not read:
        return None
    if not parts:
        return 0.0, 0.0, 0.0, None
    seg = _segment("street", "", parts)
    return seg.minutes, seg.low, seg.high, seg.why


def _past_view(hist: list[tuple[float, LaneEstimate]], r_m: float,
               street: Mapping[str, list[tuple[float, LaneEstimate]]], pair: Mapping,
               t_at: float, label: str) -> Part:
    """A line past camera A's near edge: its run-up, the gap to the street, and the street's."""
    run_up = _timed(hist, r_m, t_at, label)
    if run_up[0] is None:
        return run_up
    on_street = _street(street, t_at, from_head=False)
    if on_street is None:
        return None, run_up[1], None, f"{label}: the line runs past the view, street not read"
    if on_street[0] == 0.0:
        return None, run_up[1], None, f"{label}: the line ends between the views"
    gap = pair.get("gap_street_a_m") or {}
    value, plus = float(gap.get("value") or 0.0), float(gap.get("uncertainty") or 0.0)
    near = _speeds(hist, t_at)
    far = [s for lane in sorted(street) if (s := _speeds(street[lane], t_at)) is not None]
    fastest = max(s[1] for s in (near, *far))
    slowest = min(s[2] for s in (near, *far))
    gap_low = max(0.0, value - plus) / fastest / 60
    if on_street[0] is None:                 # the street's line runs past its view: a floor
        return None, run_up[1] + gap_low + on_street[1], None, on_street[3]
    middle = (near[0] + _median([s[0] for s in far])) / 2    # far is not empty: street timed
    gap_min = value / max(middle, MIN_SPEED_M_S) / 60
    return (run_up[0] + gap_min + on_street[0], run_up[1] + gap_low + on_street[1],
            run_up[2] + max((value + plus) / slowest / 60, gap_min) + on_street[2], None)


def _pair_reasons(pair: Mapping, group: str) -> list[str]:
    reasons = []
    if not pair.get("camera_votes"):
        reasons.append("camera votes not decided (ADR-0015 decision 3 proposed)")
    if CBP_LANE.get(group) is None:
        reasons.append(f"lane type {group} has no CBP lane id"
                       + (": every lane type pooled" if group == MIXED else ""))
    if not pair.get("line_a_verified"):
        reasons.append("line A not verified")
    if not pair.get("line_b_verified"):
        reasons.append("line B not verified")
    return reasons


def _k_eff(pair: Mapping, method: str | None, group: str, at: datetime) -> Mapping | None:
    """The newest K_eff entry for this method and lane type that holds at this hour."""
    entries = (pair.get("k_eff") or {}).get(method) or []
    fits = [e for e in entries if (e.get("lane_type") or MIXED) == group and _in_hours(e, at)]
    return max(fits, key=lambda e: str(e.get("date") or "")) if fits else None


def _hand_counted(pair: Mapping, method: str | None, group: str, at: datetime) -> bool:
    """A hand count puts this method's capture within HAND_COUNT_TOLERANCE at both lines."""
    lines = {e.get("line") for e in pair.get("hand_counts") or []
             if e.get("method") == method and (e.get("lane_type") or MIXED) == group
             and _in_hours(e, at) and e.get("capture") is not None
             and abs(float(e["capture"]) - 1) <= HAND_COUNT_TOLERANCE}
    return {"A", "B"} <= lines


def _in_hours(entry: Mapping, at: datetime) -> bool:
    """Whether `at` falls in the entry's hours_utc, [from, to), wrapping past midnight; an entry
    with none holds at every hour."""
    hours = entry.get("hours_utc")
    if not hours:
        return True
    h0, h1 = hours
    h = at.astimezone(UTC).hour
    return h0 <= h < h1 if h0 <= h1 else h >= h0 or h < h1


def _epoch_at_now(record: Mapping) -> Mapping | None:
    """The record's epoch holding its `now` (the last one starting at or before it)."""
    now = record.get("now") or {}
    if not now.get("at"):
        return None
    at = _parse(now["at"])
    held = [e for e in record.get("epochs") or [] if _parse(e["start"]) <= at]
    return held[-1] if held else None


def _since_min(record: Mapping) -> float:
    """Minutes from the start of the epoch holding `now` to `now` (both cut to the minute)."""
    epoch = _epoch_at_now(record)
    if epoch is None:
        return 0.0
    return (_parse(record["now"]["at"]) - _parse(epoch["start"])).total_seconds() / 60


def _rate(record: Mapping) -> tuple[float, float] | None:
    """(mu_B, n_B): the exit rate behind `now`, and the exits it rests on."""
    mu = (record.get("now") or {}).get("exit_rate")
    if mu is None:
        return None
    return float(mu), float(mu) * min(RATE_WINDOW_MIN, _since_min(record))


def _rate_term(n_b: float) -> float:
    """2 / sqrt(n_B): the counted rate's relative 2-sigma (Poisson). It is 1 or more at
    n_B <= MIN_EXITS, where the rate has no upper bound and so the minutes no high."""
    return RANGE_SDS / math.sqrt(n_b) if n_b > 0 else math.inf


def _parse(text: str) -> datetime:
    t = datetime.fromisoformat(text)
    return t if t.tzinfo is not None else t.replace(tzinfo=UTC)


def _floor(t: datetime, width: timedelta) -> datetime:
    epoch = datetime(1970, 1, 1, tzinfo=UTC)
    return t - (t - epoch) % width


def _iso(t: datetime) -> str:
    return t.astimezone(UTC).isoformat(timespec="seconds")


def _median(vals: Sequence[float]) -> float:
    return _quantile(sorted(vals), 0.5)


def _quantile(sorted_vals: Sequence[float], q: float) -> float | None:
    if not sorted_vals:
        return None
    pos = q * (len(sorted_vals) - 1)
    lo = math.floor(pos)
    hi = min(lo + 1, len(sorted_vals) - 1)
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (pos - lo)


def _r(x: float | None, digits: int = 2) -> float | None:
    return None if x is None else round(x, digits)
