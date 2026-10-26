"""The Chisme estimate: every signal about one lane, in one direction, fused into one wait.

Today's number follows the freshest reading (consensus.py rule 2) and stays as the first
of two numbers (Q-1, ADR-0013). This module computes the second, as SPEC.md §4 sets out:

- **Weight** (§4.1) is trust x freshness x kind factor. Trust is earned against timed
  crossings (Roadmap 2.3's trust.py) and is a hand-set prior until then. Freshness halves
  every half-life of the signal's kind. A crowd report's kind factor is 0.3, on top of
  its lower prior, so a fresh one weighs 0.09 against another site's 0.4.
- **Minutes** (§4.4) are the weighted median, never the mean. One broken scraper saying 58
  against signals near 20 drags a mean to about 28, a wait nobody claimed; the median
  stays on a value some signal did claim.
- **Range** (§4.4) is the weighted 10th to 90th percentile, taken over each signal's own
  low and high where it gives them, so a camera's uncertainty widens the range.
- **Confidence** (§4.5) is one word from the total weight, how many kinds voted, and how
  wide the range is.
- **Crowd guards** (§4.3) decide which reports from people vote at all, and cap what
  they can weigh together. A position report is a person's report too (§4.1).
- **Closures follow CBP** (§4.4, consensus rule 1). A measured signal (a camera or Maps)
  that saw traffic moving after CBP first said closed raises closure_conflict: the lane
  shows both facts, and neither overrides the other (Q-2, ADR-0013 decision 4).

Rules the SPEC implies and this module makes explicit:

- **A source votes once, with its newest claim, newest by when we read it.**
  pasosfronterizos refreshes about every 8 minutes and CBP about hourly (SPEC §5.2), so
  an hour of stored signals holds seven of one and one of the other. Counting every row
  would let the faster site outvote CBP by refreshing, not by being right. Newest is by
  read, not by observed_at, because the two clocks differ: signals.py dates a closure
  when it is read and a number by CBP's hourly stamp, so by observed_at a closure read at
  10:03 outranks the 10:00 reopening read at 10:13 until the next hour's stamp. People
  are the exception: each report is a person, and the rate limit (§4.3) already decides
  how often one person counts.
- **Only a CBP closure closes the lane.** "Update Pending" is CBP saying nothing, not
  saying closed, so it neither closes the lane nor replaces what CBP said last: that
  claim keeps voting and ages as it would. A lane nothing votes on has no number,
  whatever any one site's state says: SPEC gives no closure rule but CBP's.
- **An old claim is stale and changes nothing.** Past LIVE_HALF_LIVES half-lives of its
  kind it does not vote, does not keep a lane from showing its crowd alone (§4.3's "no
  other live signal"), does not set the cap people are held to, does not count as a
  kind for confidence, and does not close a lane.

Everything is a pure function of the signals, the clock and the trust table. Signals are
put in time order first, so the same inputs give the same estimate whatever order they
arrive in, and every vote carries each factor of its weight so a person can redo the
arithmetic (SPEC §1). Each number marked [A] is SPEC's starting value, to be tuned by the
replay harness (Roadmap 2.5), not by inspection.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta

from . import consensus
from .crossings import MAX_PLAUSIBLE, MIN_PLAUSIBLE
from .signals import FROM_PEOPLE, Signal

METHOD = "fusion_v1"   # border_estimates.method: a row is graded only against its own fusion

# §4.1 [A]: minutes after which a claim counts half. A camera count goes stale fastest,
# CBP republishes about hourly (windows.py:3-4), and a person's report describes the line
# they just left.
HALF_LIFE = {"measured": 10, "published": 30, "crowd": 20,
             # ASSUMPTION: SPEC gives a position report no half-life. Its wait is our own
             # queue speed applied to where the person stands (§5.4), so it ages like one.
             "position": 10}
# §4.1 [A]: "taken less seriously" (Q-13). A position report's wait comes from our own
# measurement rather than the person's guess, so it sits between crowd and measured.
KIND_FACTOR = {"measured": 1.0, "published": 1.0, "crowd": 0.3, "position": 0.6}
# ASSUMPTION [A]: past this many half-lives of its kind a claim no longer describes the
# lane now, and is stale. Six is the smallest whole number that keeps a Google Maps
# reading live until the next one on the free tier's cadence of about 52 minutes (SPEC
# §5.1, Q-8, at measured's 10-minute half-life). A claim that old counts 1/64 of new.
LIVE_HALF_LIVES = 6
# How far back the caller passes signals: the longest any kind stays live (published, 3
# hours). A signal observed before that is stale, so leaving it out changes nothing.
HISTORY = timedelta(minutes=LIVE_HALF_LIVES * max(HALF_LIFE.values()))

# §4.2 [A]: trust before any timed crossing grades a source. It fades as crossings arrive
# (trust.py), and until then it is the only trust there is.
AUTHORITY_PRIOR = 0.6    # CBP, the operator's own feed
PRIOR = {"published": 0.4, "measured": 0.5, "crowd": 0.3,
         # ASSUMPTION: SPEC gives a position report no prior; measured's, as for its half-life.
         "position": 0.5}

# §4.3 crowd guards [A]. Plausibility reuses crossings.py's bounds for a real crossing.
CORROBORATION_WINDOW = timedelta(minutes=20)   # a second reporter agrees "in the same 20 minutes"
RATE_LIMIT = timedelta(minutes=30)             # one counted report per reporter per lane
CROWD_SHARE_CAP = 0.25                         # people's total weight, against everyone else's

# §4.4: the estimate and its range, as shares of the total weight.
MEDIAN, LOW_PERCENTILE, HIGH_PERCENTILE = 0.5, 0.1, 0.9

# §4.5 [A]
HIGH_WEIGHT = 1.0       # total weight for "high"
HIGH_KINDS = 2          # ... from at least this many kinds of signal
TIGHT_RANGE = 15        # ... with a range no wider than this, in minutes
MEDIUM_WEIGHT = 0.5     # total weight for "medium"
# "A single high-trust fresh signal" is also "medium". ASSUMPTION: high trust is what a
# source earns at the go-live error (Q-6: 5 minutes), by §4.2's 1 / (1 + MAE / 10); fresh
# is within one half-life of its kind, where it still counts at least half.
HIGH_TRUST = 1 / (1 + 5 / 10)


@dataclass(frozen=True)
class Vote:
    """One signal's part in an estimate: what happened to it, and every factor of its
    weight. outcome is one of:

    - counted: it voted. On a lane CBP closes, CBP's closure is the one counted claim.
    - conflict: a measured signal saw traffic after CBP first said closed (Q-2).
    - lane_closed: any other open claim on a lane CBP calls closed.
    - mirror: it republishes CBP, so it never votes (consensus rule 3).
    - stale: older than LIVE_HALF_LIVES half-lives of its kind.
    - superseded: the same source has said something newer, by when we read it.
    - no_minutes: nothing to vote with: closed, no data, or open with no number.
    - implausible, rate_limited, uncorroborated: a person's report the crowd guards
      stopped (§4.3); the words border_reports.outcome uses.
    """
    signal: Signal
    outcome: str
    age_minutes: float      # negative for a claim stamped ahead of the clock
    trust: float
    freshness: float
    kind_factor: float
    cap: float = 1.0        # the crowd share cap's scale, below 1 only where it bit

    @property
    def weight(self) -> float:
        if self.outcome != "counted":
            return 0.0
        return self.trust * self.freshness * self.kind_factor * self.cap

    def to_input(self) -> dict:
        """This vote as one entry of border_estimates.inputs. It cites the signal by its
        content_hash, border_signals' unique key, and never by its evidence or sender:
        Q-14 keeps those 30 days, and estimates are kept 60 (0021). A person's report is
        cited by nothing, because its content_hash is a hash over its reporter_hash and
        the rest of this entry: anyone holding a candidate sender's hash could confirm
        from the row, after the 30 days, who reported what."""
        s = self.signal
        cited = None if s.kind in FROM_PEOPLE else s.content_hash
        return {"signal": cited, "source": s.source, "kind": s.kind,
                "state": s.state, "minutes": s.minutes, "low": s.low, "high": s.high,
                "outcome": self.outcome, "age_minutes": self.age_minutes, "trust": self.trust,
                "freshness": self.freshness, "kind_factor": self.kind_factor, "cap": self.cap,
                "weight": self.weight}


@dataclass(frozen=True)
class Estimate:
    """The Chisme estimate for one port, lane and direction (SPEC §1). minutes, low, high
    and confidence are None when there is no number: a closed lane, or nothing voted."""
    port_number: str
    lane: str
    direction: str
    state: str                  # open | closed | no_data
    minutes: int | None
    low: int | None
    high: int | None
    confidence: str | None      # high | medium | low
    closure_conflict: bool
    crowd_only: bool            # only people voted: shown "labelled as such" (§4.3)
    computed_at: datetime
    votes: tuple[Vote, ...]     # every signal it was given, oldest first

    @property
    def counted(self) -> tuple[Vote, ...]:
        return tuple(v for v in self.votes if v.outcome == "counted")

    @property
    def total_weight(self) -> float:
        """The weight behind the estimate: every counted vote's, after the crowd cap."""
        return sum(v.weight for v in self.votes)

    def to_row(self) -> dict:
        """A border_estimates row (SPEC §6). shown is left to the table's default, false,
        until the bridge meets its target (§4.6). inputs holds the signals that decided
        the row: the ones counted, and the ones behind a closure conflict."""
        return {
            "port_number": self.port_number,
            "lane": self.lane,
            "direction": self.direction,
            "state": self.state,
            "closure_conflict": self.closure_conflict,
            "minutes": self.minutes,
            "low": self.low,
            "high": self.high,
            "confidence": self.confidence,
            "method": METHOD,
            "inputs": [v.to_input() for v in self.votes if v.outcome in ("counted", "conflict")],
            "computed_at": self.computed_at.astimezone(UTC).isoformat(),
        }


def prior(source: str, kind: str) -> float:
    """§4.2's starting trust for a source that timed crossings have not graded."""
    return AUTHORITY_PRIOR if source == consensus.AUTHORITY else PRIOR[kind]


def freshness(age_minutes: float, kind: str) -> float:
    """Halves every half-life of the kind. A claim stamped ahead of the clock counts as
    new, never as fresher than new."""
    return 0.5 ** (max(age_minutes, 0.0) / HALF_LIFE[kind])


def weighted_percentile(claims: list[tuple[int, float]], share: float) -> int:
    """The smallest claimed value whose cumulative weight passes `share` of the total.

    It always lands on a value some signal claimed. On an exact tie at the median, two
    equal weights at 20 and 30, it takes the higher: a wait promised short and served
    long costs the follower more (the same reasoning as scoring.FALLING_WEIGHT)."""
    ordered = sorted(claims)
    total = sum(weight for _, weight in ordered)
    if not ordered or total <= 0:
        raise ValueError("a weighted percentile needs a positive total weight")
    running = 0.0
    for value, weight in ordered:
        running += weight
        if running > share * total:
            return value
    return ordered[-1][0]    # reached only if rounding leaves the sum an ulp short


def fuse(signals: Iterable[Signal], now: datetime,
         trust: Mapping[tuple[str, str], float] | None = None,
         read_at: Mapping[str, datetime] | None = None) -> Estimate:
    """One lane's estimate in one direction, from every signal about it.

    The caller passes every signal observed in the last HISTORY. An older one is stale
    and changes nothing; a newer one left out can change which claim is a source's
    newest, when CBP's closure began, and which reports the crowd guards let through.

    trust maps (source, direction) to the trust trust.py earned for it; a source missing
    from it is at its prior. read_at maps a signal's content_hash to when we first read
    it (border_signals.captured_at), which decides a source's newest claim. A signal
    missing from it counts as read when it was observed: true of a claim dated at its
    read, such as a closure, and too early for one with a stamp (module docstring).

    Raises ValueError for no signals, signals about more than one lane, a naive clock or
    read time, or a trust outside (0, 1]."""
    if not isinstance(now, datetime) or now.utcoffset() is None:
        raise ValueError(f"now must be a timezone-aware datetime, not {now!r}")
    ordered = sorted(signals, key=lambda s: (s.observed_at, s.content_hash))
    if not ordered:
        raise ValueError("fuse needs at least one signal")
    lanes = {(s.port_number, s.lane, s.direction) for s in ordered}
    if len(lanes) > 1:
        raise ValueError(f"fuse takes one lane in one direction, got {sorted(lanes)}")
    port_number, lane, direction = lanes.pop()

    outcomes, closed = _outcomes(ordered, now, read_at or {})
    votes = _weigh(ordered, outcomes, now, trust or {})
    common = {"port_number": port_number, "lane": lane, "direction": direction,
              "computed_at": now, "votes": votes}
    if closed:
        conflict = any(v.outcome == "conflict" for v in votes)
        return Estimate(state="closed", minutes=None, low=None, high=None, confidence=None,
                        closure_conflict=conflict, crowd_only=False, **common)

    counted = [v for v in votes if v.outcome == "counted"]
    if sum(v.weight for v in counted) <= 0:
        return Estimate(state="no_data", minutes=None, low=None, high=None, confidence=None,
                        closure_conflict=False, crowd_only=False, **common)
    minutes = weighted_percentile([(v.signal.minutes, v.weight) for v in counted], MEDIAN)
    low = weighted_percentile([(_low(v.signal), v.weight) for v in counted], LOW_PERCENTILE)
    high = weighted_percentile([(_high(v.signal), v.weight) for v in counted], HIGH_PERCENTILE)
    crowd_only = all(v.signal.kind in FROM_PEOPLE for v in counted)
    return Estimate(state="open", minutes=minutes, low=low, high=high,
                    confidence=_confidence(counted, high - low, crowd_only),
                    closure_conflict=False, crowd_only=crowd_only, **common)


def fuse_all(signals: Iterable[Signal], now: datetime,
             trust: Mapping[tuple[str, str], float] | None = None,
             read_at: Mapping[str, datetime] | None = None,
             ) -> dict[tuple[str, str, str], Estimate]:
    """Every lane's estimate, keyed by (port_number, lane, direction)."""
    grouped: dict[tuple[str, str, str], list[Signal]] = {}
    for s in signals:
        grouped.setdefault((s.port_number, s.lane, s.direction), []).append(s)
    return {key: fuse(group, now, trust, read_at) for key, group in grouped.items()}


def _outcomes(ordered: list[Signal], now: datetime,
              read_at: Mapping[str, datetime]) -> tuple[list[str], bool]:
    """What happens to each signal (Vote lists the words), and whether CBP closed the
    lane. ordered is oldest first."""
    outcome: dict[int, str] = {}
    claims: dict[str, list[int]] = {}    # each source's claims of a state, people's aside
    for i, s in enumerate(ordered):
        if not s.independent:
            outcome[i] = "mirror"
        elif _age(s, now) > LIVE_HALF_LIVES * HALF_LIFE[s.kind]:
            outcome[i] = "stale"
        elif s.kind not in FROM_PEOPLE and s.state != "no_data":   # no_data says nothing
            claims.setdefault(s.source, []).append(i)
    for mine in claims.values():
        mine.sort(key=lambda i: (_read(ordered[i], read_at), ordered[i].observed_at,
                                 ordered[i].content_hash))
        for i in mine[:-1]:
            outcome[i] = "superseded"
    live = [i for i in range(len(ordered)) if i not in outcome]

    authority = claims.get(consensus.AUTHORITY)
    if authority and ordered[authority[-1]].state == "closed":
        since = _closed_since([ordered[i] for i in authority])
        for i in live:
            outcome[i] = ("counted" if i == authority[-1]
                          else _under_closure(ordered[i], since))
        return [outcome[i] for i in range(len(ordered))], True

    for i in live:
        if ordered[i].minutes is None:
            outcome[i] = "no_minutes"      # a person's "closed" lands here: no votes on closures
    others_vote = any(i not in outcome and ordered[i].kind not in FROM_PEOPLE for i in live)
    people = [i for i in live if i not in outcome and ordered[i].kind in FROM_PEOPLE]
    for i in people:
        if not _plausible(ordered[i]):
            outcome[i] = "implausible"
    people = [i for i in people if i not in outcome]
    for i in _rate_limited(ordered, people):
        outcome[i] = "rate_limited"
    people = [i for i in people if i not in outcome]
    if others_vote:     # a lane nothing else votes on may show its crowd alone (§4.3)
        for i in people:
            if not any(_agree(ordered[i], ordered[j]) for j in people):
                outcome[i] = "uncorroborated"
    return [outcome.get(i, "counted") for i in range(len(ordered))], False


def _closed_since(cbp: list[Signal]) -> datetime:
    """When CBP's current closure began, as far as its claims show: the first closure
    read since it last said anything else. cbp is oldest read first, and ends closed."""
    first = len(cbp) - 1
    while first > 0 and cbp[first - 1].state == "closed":
        first -= 1
    return cbp[first].observed_at


def _under_closure(s: Signal, since: datetime) -> str:
    """A claim on a lane CBP calls closed. Only a measured signal (a camera or Maps, as
    ADR-0013 decision 4 names them) that saw traffic moving after CBP first said closed
    contradicts it. A site showing minutes is usually a stale page (consensus rule 1), a
    person never opens a lane (§4.3), and a count from before the closure is consistent
    with it. This holds whatever the measured source's cadence: Maps' reading from 40
    minutes into a closure conflicts as a camera's from 4 minutes in does."""
    if s.state != "open":
        return "no_minutes"
    if s.kind == "measured" and s.observed_at >= since:
        return "conflict"
    return "lane_closed"


def _plausible(s: Signal) -> bool:
    """§4.3 guard 4: every number a person gives lies within a believable crossing."""
    return all(MIN_PLAUSIBLE <= value <= MAX_PLAUSIBLE
               for value in (s.minutes, s.low, s.high) if value is not None)


def _rate_limited(ordered: list[Signal], people: list[int]) -> list[int]:
    """§4.3 guard 3: a person's first report in any 30 minutes is their only one there.
    people is oldest first, so the first of a burst is kept and a correction minutes later
    is not. ASSUMPTION: this runs before corroboration, so the kept report holds the slot
    even if no one agrees with it. The other order would let one person send 40, 50, 60
    and 70 and have whichever another person matches counted. Only live reports hold a
    slot, so the outcome depends on the clock but never on how far back the caller read:
    for someone who reported every 30 minutes or faster, which of their reports count
    shifts as the oldest goes stale, and never more than one counts in any 30 minutes."""
    last_counted: dict[str, datetime] = {}
    limited = []
    for i in people:
        s = ordered[i]
        if s.reporter in last_counted and s.observed_at - last_counted[s.reporter] < RATE_LIMIT:
            limited.append(i)
        else:
            last_counted[s.reporter] = s.observed_at
    return limited


def _agree(a: Signal, b: Signal) -> bool:
    """§4.3 guard 1: two different people, within 20 minutes of each other, within
    consensus' tolerance of each other."""
    return (a.reporter != b.reporter
            and abs(a.observed_at - b.observed_at) <= CORROBORATION_WINDOW
            and abs(a.minutes - b.minutes) <= consensus.tolerance_for([a.minutes, b.minutes]))


def _weigh(ordered: list[Signal], outcomes: list[str], now: datetime,
           trust: Mapping[tuple[str, str], float]) -> tuple[Vote, ...]:
    """Each signal's vote. People's counted weight is scaled down together until it is at
    most CROWD_SHARE_CAP of everyone else's (§4.3 guard 2); with no one else voting there
    is nothing to cap against, and the lane is crowd-only."""
    votes = []
    for s, outcome in zip(ordered, outcomes, strict=True):
        age = _age(s, now)
        votes.append(Vote(signal=s, outcome=outcome, age_minutes=age, trust=_trust(s, trust),
                          freshness=freshness(age, s.kind), kind_factor=KIND_FACTOR[s.kind]))
    counted = [v for v in votes if v.outcome == "counted"]
    people = sum(v.weight for v in counted if v.signal.kind in FROM_PEOPLE)
    others = sum(v.weight for v in counted if v.signal.kind not in FROM_PEOPLE)
    if people <= 0 or all(v.signal.kind in FROM_PEOPLE for v in counted):
        return tuple(votes)
    cap = min(1.0, CROWD_SHARE_CAP * others / people)
    return tuple(replace(v, cap=cap) if v.outcome == "counted" and v.signal.kind in FROM_PEOPLE
                 else v for v in votes)


def _trust(s: Signal, trust: Mapping[tuple[str, str], float]) -> float:
    value = trust.get((s.source, s.direction))
    if value is None:
        return prior(s.source, s.kind)
    if isinstance(value, bool) or not isinstance(value, int | float) or not 0 < value <= 1:
        raise ValueError(f"trust for {s.source} {s.direction} must be in (0, 1], not {value!r}")
    return float(value)


def _age(s: Signal, now: datetime) -> float:
    return (now - s.observed_at).total_seconds() / 60


def _read(s: Signal, read_at: Mapping[str, datetime]) -> datetime:
    when = read_at.get(s.content_hash, s.observed_at)
    if not isinstance(when, datetime) or when.utcoffset() is None:
        raise ValueError(f"read_at for {s.source} must be a timezone-aware datetime, "
                         f"not {when!r}")
    return when


def _low(s: Signal) -> int:
    return s.low if s.low is not None else s.minutes


def _high(s: Signal) -> int:
    """An open-ended "at least" (high None, SPEC §5.3) adds nothing above its own claim."""
    return s.high if s.high is not None else s.minutes


def _confidence(counted: list[Vote], spread: int, crowd_only: bool) -> str:
    """§4.5. A crowd-only lane is always low, however many people agree, and a person is
    never the "single high-trust fresh signal": the cap may have scaled them to nothing
    (§4.3), and their trust is the channel's, not their own."""
    if crowd_only:
        return "low"
    total = sum(v.weight for v in counted)
    kinds = {v.signal.kind for v in counted}
    if total >= HIGH_WEIGHT and len(kinds) >= HIGH_KINDS and spread <= TIGHT_RANGE:
        return "high"
    if total >= MEDIUM_WEIGHT or any(v.signal.kind not in FROM_PEOPLE
                                     and v.trust >= HIGH_TRUST
                                     and v.age_minutes <= HALF_LIFE[v.signal.kind]
                                     for v in counted):
        return "medium"
    return "low"
