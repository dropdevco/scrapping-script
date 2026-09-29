"""Reading the other sources and judging what they said.

A source that fails is recorded and ignored: a scraper breaking must never stop the
feed answering with CBP alone. A scraper whose page changed shape parses to nothing
rather than raising, which is why a healthy page's ports are declared up front.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, replace

from .sources import Reading

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class SourceStatus:
    state: str                 # ok | cached | partial | empty | failed | not configured
    detail: str = ""

    @property
    def contributed(self) -> bool:
        """Its readings went into this answer."""
        return self.state in ("ok", "cached", "partial")

    @property
    def problem(self) -> bool:
        return self.state in ("empty", "partial", "failed")

    def __str__(self) -> str:
        if not self.detail:
            return self.state
        separator = ": " if self.state in ("empty", "failed") else " "
        return f"{self.state}{separator}{self.detail}"


def judge(source, readings: list[Reading]) -> SourceStatus:
    fresh = len(readings)
    expected = getattr(source, "expected_ports", frozenset())
    missing = sorted(expected - {r.port_number for r in readings})
    if expected and not readings:
        return SourceStatus("empty", "parsed 0 readings, the page may have changed")
    if missing:
        return SourceStatus("partial", f"({fresh} readings, missing {', '.join(missing)})")
    return SourceStatus("ok", f"({fresh} readings)")


def aged(readings: list[Reading], held_seconds: float) -> list[Reading]:
    """A reading held for ten minutes is ten minutes older than the site said. Without
    this, a cached scrape keeps winning the freshness comparison."""
    elapsed = int(held_seconds // 60)
    if not elapsed:
        return list(readings)
    return [replace(r, age_minutes=r.age_minutes + elapsed) if r.age_minutes is not None else r
            for r in readings]


def signature(readings: list[Reading]) -> tuple:
    return tuple(sorted((r.port_number, r.lane, r.state, r.minutes, r.age_minutes) for r in readings))


def same_update(ours: str, theirs: str) -> bool:
    """'At 7:00 pm MDT' against the mirror's copy of the same words."""
    return " ".join(ours.lower().split()) == " ".join(theirs.lower().split())


def compare_mirrors(readings: list[Reading]) -> dict:
    """Mirrors republish CBP, so any difference points at our parsing, not the border.

    This is the check that would have caught the ahead-of-clock stamps and the
    "At Noon" format before they reached a post.
    """
    ours = {(r.port_number, r.lane): r for r in readings if r.source == "cbp"}
    mismatches = []
    compared = skipped = 0
    for reading in readings:
        if reading.independent or reading.source == "cbp":
            continue
        mine = ours.get((reading.port_number, reading.lane))
        if mine is None:
            continue
        # Only a reading of the SAME CBP update says anything about our parsing. The
        # mirror lags CBP: live at 00:12 MDT on 2026-09-29 it still read "Update pending"
        # (no stamp at all) for a lane CBP already had at 25 min, and the check reported
        # our parser as broken. So both sides must carry the stamp, and it must match;
        # anything else is counted as skipped rather than compared.
        if not (mine.label and reading.label and same_update(mine.label, reading.label)):
            skipped += 1
            continue
        compared += 1
        if mine.state != reading.state or mine.minutes != reading.minutes:
            mismatches.append({
                "port_number": reading.port_number, "lane": reading.lane,
                "ours": {"state": mine.state, "minutes": mine.minutes},
                reading.source: {"state": reading.state, "minutes": reading.minutes},
            })
    return {"compared": compared, "skipped_other_update": skipped, "mismatches": mismatches}
