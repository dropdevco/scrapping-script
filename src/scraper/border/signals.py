"""The Signal contract: one source's claim about one lane, in one direction, at one moment.

Every input the estimator fuses arrives as a Signal (docs/border-estimator/SPEC.md §3):
the published sites today, then cameras, Maps, crowd reports and position reports.
`Reading` stays as it is, because consensus, the poller and their tests depend on it;
`from_reading` wraps one instead.

What a Reading cannot say, a Signal must:

- **direction.** Both directions are in scope (Q-4), and southbound has no CBP number at
  all, so a claim that does not say which line it describes cannot be filed.
- **kind.** Fusion weighs a published number, a measured one and a person's report
  differently (SPEC §4.1), so the kind travels with the claim.
- **observed_at, as a moment.** Freshness decays from when the claim was true, not from
  when we read it. A Reading carries that as a string, or not at all.

When a published reading was true, in order of what it says about itself:

1. Its own stamp, or the CBP update its label names. The CBP mirror carries only the
   label: its fixture page says "At 7:00 pm MDT", 160 minutes before the fixture's
   9:40 pm read, where UNKNOWN_AGE would have said 45.
2. Otherwise its stated age, counted back from the read. This is also the path for a
   stamp ahead of the clock: the fixture feed dates Bridge of the Americas "At 10:00 pm"
   at 9:40 pm, and cbp.Lane.known_age already counts such a stamp as UNKNOWN_AGE old.
3. A reading with no minutes claims only a state, and the state a feed shows is the
   state at the read. CBP's "Lanes Closed" and "Update Pending" lanes arrive with an
   empty update_time (every fixture feed). Dating a closure back would let an older
   "open" reading outrank it.
4. Minutes with no time at all count as UNKNOWN_AGE old, so they are never fresh. That
   is the convention cbp.py uses for a stamp it cannot trust.

Validation is strict on purpose. A malformed claim raises here, where the bad input is
named, rather than becoming a row that fusion quietly files under a lane nobody has.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from . import cbp
from .sources.base import Reading

KINDS = frozenset({"published", "measured", "crowd", "position"})
STATES = frozenset({"open", "closed", "no_data"})      # the same three as Reading and CBP
DIRECTIONS = frozenset({"north", "south"})
# The lane ids every reading already carries. SPEC §3 writes `commercial`; the pipeline's
# ids for that are `truck` and `truck_fast`, so those are what a signal must use.
LANES = frozenset(cbp.LANE_LABELS)
# Kinds that come from a person, so they carry the sender's hash: the crowd guards count
# distinct reporters and rate-limit each one (SPEC §4.3), and a position report uses the
# same guards (§4.1). Everything else has no reporter.
FROM_PEOPLE = frozenset({"crowd", "position"})
# ASSUMPTION, from evidence: every published reading today is northbound. CBP covers only
# north (SPEC §3), borderswaittime republishes CBP verbatim, and pasosfronterizos' cards
# include SENTRI and Ready Lane, which exist only on the way into the US.
PUBLISHED_DIRECTION = "north"


@dataclass(frozen=True, kw_only=True)
class Signal:
    """One claim about one lane (SPEC.md §3). Keyword-only: thirteen fields, several of
    them the same type, are too many to pass in order safely."""
    port_number: str               # CBP port id, e.g. 240202
    lane: str                      # an existing lane id: car, car_sentri, walk, truck, ...
    direction: str                 # north | south
    kind: str                      # published | measured | crowd | position
    source: str                    # cbp, pasosfronterizos, cam:pdn-north, ig_dm, ...
    state: str                     # open | closed | no_data
    minutes: int | None = None     # the claim itself; only an open lane has one
    low: int | None = None         # its own range. high None means "at least low":
    high: int | None = None        # "llevo 40 min" is still in line (SPEC §5.3)
    observed_at: datetime          # aware; when the claim was true, not when we read it
    independent: bool              # False for a CBP mirror, which never votes
    reporter: str | None = None    # opaque hash of the sender, people kinds only (Q-14)
    evidence: dict = field(default_factory=dict, hash=False)   # raw inputs, for audit

    def __post_init__(self) -> None:
        # Every broken claim raises ValueError, a wrong type included (hence the TRY004
        # waivers), so a caller can count refusals with a single except.
        for name, allowed in (("lane", LANES), ("direction", DIRECTIONS), ("kind", KINDS),
                              ("state", STATES)):
            if getattr(self, name) not in allowed:
                raise ValueError(f"{name} must be one of {sorted(allowed)}, "
                                 f"not {getattr(self, name)!r}")
        for name in ("port_number", "source"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a non-empty string, not {value!r}")
        _require_aware(self.observed_at, "observed_at")
        if not isinstance(self.independent, bool):
            raise ValueError(f"independent must be a bool, not {self.independent!r}")  # noqa: TRY004
        _check_wait(self.state, self.minutes, self.low, self.high)
        if self.kind in FROM_PEOPLE:
            if not isinstance(self.reporter, str) or not self.reporter:
                raise ValueError(f"a {self.kind} signal needs its sender's hash in reporter")
        elif self.reporter is not None:
            raise ValueError(f"a {self.kind} signal has no reporter")
        if not isinstance(self.evidence, dict):
            got = type(self.evidence).__name__
            raise ValueError(f"evidence must be a dict, not {got}")  # noqa: TRY004
        # A copy, so the caller's dict changing later cannot change a recorded claim.
        object.__setattr__(self, "evidence", dict(self.evidence))

    @property
    def content_hash(self) -> str:
        """The upsert key of border_signals. The same claim read again (CBP between its
        hourly updates) hashes the same and inserts nothing. Evidence is left out because
        a stated age grows between polls while the claim itself does not change, and the
        moment is taken in UTC so a row read back from the database hashes as written."""
        key = json.dumps([self.port_number, self.lane, self.direction, self.kind, self.source,
                          self.observed_at.astimezone(UTC).isoformat(), self.state,
                          self.minutes, self.low, self.high, self.reporter])
        return hashlib.sha1(key.encode()).hexdigest()

    def to_row(self) -> dict:
        """A border_signals row (SPEC §6). The sender is stored as reporter_hash, the only
        form the table may hold it in (Q-14)."""
        return {
            "port_number": self.port_number,
            "lane": self.lane,
            "direction": self.direction,
            "kind": self.kind,
            "source": self.source,
            "state": self.state,
            "minutes": self.minutes,
            "low": self.low,
            "high": self.high,
            "observed_at": self.observed_at.astimezone(UTC).isoformat(),
            "independent": self.independent,
            "reporter_hash": self.reporter,
            "evidence": dict(self.evidence),
            "content_hash": self.content_hash,
        }

    @classmethod
    def from_row(cls, row: dict) -> Signal:
        """A border_signals row as PostgREST returns it, observed_at as ISO text. Columns
        the database fills itself (id, captured_at) are ignored. independent has no
        default: a row that lost it must not let a mirror vote."""
        return cls(port_number=row["port_number"], lane=row["lane"], direction=row["direction"],
                   kind=row["kind"], source=row["source"], state=row["state"],
                   minutes=row.get("minutes"), low=row.get("low"), high=row.get("high"),
                   observed_at=datetime.fromisoformat(row["observed_at"]),
                   independent=row["independent"], reporter=row.get("reporter_hash"),
                   evidence=row.get("evidence") or {})


def _require_aware(value, name: str) -> None:
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise ValueError(f"{name} must be a timezone-aware datetime, not {value!r}")


def _check_wait(state: str, minutes, low, high) -> None:
    """Whole, non-negative minutes, only on an open lane (the border_readings rule), and
    low <= minutes <= high wherever two of them are given. No upper bound: plausibility
    is a crowd guard in fusion (SPEC §4.3), and a published 400 is still what was said."""
    claims = {"minutes": minutes, "low": low, "high": high}
    for name, value in claims.items():
        if value is not None and (isinstance(value, bool) or not isinstance(value, int)
                                  or value < 0):
            raise ValueError(f"{name} must be a whole number of minutes >= 0, not {value!r}")
    if state != "open" and any(value is not None for value in claims.values()):
        raise ValueError(f"a {state} lane has no wait; minutes, low and high must be None")
    given = [value for value in (low, minutes, high) if value is not None]
    if given != sorted(given):
        raise ValueError(f"need low <= minutes <= high, got {low}, {minutes}, {high}")


def _observed_at(reading: Reading, read_at: datetime) -> tuple[datetime, str]:
    """When the reading was true, and how we know: stamp | age | read | assumed. The
    order and its evidence are in the module docstring."""
    stamp = (datetime.fromisoformat(reading.observed_at) if reading.observed_at
             else cbp.parse_stamp(reading.label, read_at))
    if stamp is not None:
        _require_aware(stamp, f"{reading.source}'s observed_at")
        if stamp <= read_at:
            return stamp, "stamp"
    if reading.age_minutes is not None:
        return read_at - timedelta(minutes=reading.age_minutes), "age"
    if reading.minutes is None:
        return read_at, "read"
    return read_at - timedelta(minutes=cbp.UNKNOWN_AGE), "assumed"


def from_reading(reading: Reading, read_at: datetime) -> Signal:
    """A published reading as a Signal. read_at is when the poll read it.

    Raises ValueError when the reading breaks the contract (a negative wait, an unknown
    lane, no source name), so the caller can count it rather than store it."""
    _require_aware(read_at, "read_at")
    observed_at, how = _observed_at(reading, read_at)
    stated = {"lanes_open": reading.lanes_open, "age_minutes": reading.age_minutes,
              "label": reading.label}
    evidence = {key: value for key, value in stated.items() if value is not None}
    evidence["observed_at_from"] = how
    return Signal(port_number=reading.port_number, lane=reading.lane,
                  direction=PUBLISHED_DIRECTION, kind="published", source=reading.source,
                  state=reading.state, minutes=reading.minutes, observed_at=observed_at,
                  independent=reading.independent, evidence=evidence)
