"""Reconcile several sources into one number, and say how sure we are.

Three rules, in order:

1. **Closures follow CBP.** It is the operator's own feed, so it knows when a lane is
   shut. A scraped site showing minutes for a lane CBP calls closed is usually reading
   a stale page, not discovering something.

2. **Minutes follow freshness.** Among sources that agree a lane is open, the newest
   reading wins. CBP republishes roughly hourly; a site refreshed eight minutes ago is
   the better description of the line right now.

3. **Correlated sources do not corroborate.** Most border sites re-publish CBP
   verbatim. Three of them agreeing is one source agreeing with itself, so only
   sources marked independent count toward agreement.

Disagreement is never hidden. When sources differ by more than the tolerance, the
spread travels with the answer so a caption can say so and confidence drops.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .sources.base import Reading

TOLERANCE_MINUTES = 10      # below this, two sources are describing the same line
TOLERANCE_FRACTION = 0.25   # long waits deserve proportionally more room
AUTHORITY = "cbp"


@dataclass
class Decision:
    port_number: str
    lane: str
    state: str
    minutes: int | None
    agreement: str                       # single | agree | conflict | closed
    chosen_source: str
    age_minutes: int | None = None
    spread_minutes: int | None = None
    opinions: list[dict] = field(default_factory=list)

    @property
    def disputed(self) -> bool:
        return self.agreement == "conflict"

    def to_dict(self) -> dict:
        return {
            "state": self.state, "minutes": self.minutes, "agreement": self.agreement,
            "chosen_source": self.chosen_source, "age_minutes": self.age_minutes,
            "spread_minutes": self.spread_minutes, "opinions": self.opinions,
        }


def tolerance_for(values: list[int]) -> float:
    return max(TOLERANCE_MINUTES, min(values) * TOLERANCE_FRACTION)


def _age(reading: Reading) -> int:
    return reading.age_minutes if reading.age_minutes is not None else 999


def reconcile(readings: list[Reading]) -> Decision:
    """One lane's readings from every source that had an opinion."""
    if not readings:
        raise ValueError("reconcile needs at least one reading")

    port, lane = readings[0].port_number, readings[0].lane
    opinions = [{"source": r.source, "state": r.state, "minutes": r.minutes,
                 "age_minutes": r.age_minutes, "independent": r.independent}
                for r in readings]
    authority = next((r for r in readings if r.source == AUTHORITY), None)

    # 1. Closures and no-data follow the operator's feed.
    if authority and authority.state != "open":
        return Decision(port, lane, authority.state, None,
                        agreement="closed" if authority.state == "closed" else "single",
                        chosen_source=AUTHORITY, age_minutes=authority.age_minutes,
                        opinions=opinions)

    usable = [r for r in readings if r.state == "open" and r.minutes is not None]
    if not usable:
        state = authority.state if authority else readings[0].state
        return Decision(port, lane, state, None, agreement="single",
                        chosen_source=authority.source if authority else readings[0].source,
                        opinions=opinions)

    # 2. Only independent sources get a vote on how far apart the world is.
    voters = [r for r in usable if r.independent] or usable
    values = [r.minutes for r in voters]
    spread = max(values) - min(values)
    freshest = min(voters, key=_age)

    if len(voters) == 1:
        agreement = "single"
    elif spread <= tolerance_for(values):
        agreement = "agree"
    else:
        agreement = "conflict"

    return Decision(port, lane, "open", freshest.minutes, agreement=agreement,
                    chosen_source=freshest.source, age_minutes=freshest.age_minutes,
                    spread_minutes=spread if len(voters) > 1 else None,
                    opinions=opinions)


def reconcile_all(readings: list[Reading]) -> dict[tuple[str, str], Decision]:
    grouped: dict[tuple[str, str], list[Reading]] = {}
    for reading in readings:
        grouped.setdefault((reading.port_number, reading.lane), []).append(reading)
    return {key: reconcile(group) for key, group in grouped.items()}
