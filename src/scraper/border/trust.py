"""Trust per source and direction, earned against timed crossings (SPEC §4.2).

Fusion weighs each signal by trust x freshness x kind factor. Trust is the one factor
that is earned rather than declared, and the only thing it is earned against is a timed
crossing: when CBP says 20 and pasosfronterizos says 58, only someone who crossed can
settle it (crossings.py). CBP is graded like every other source and is never the answer
key. It is not the truth either: its feed marks every El Paso car lane "Manual", an
officer's estimate (ADR-0014). So a site that agrees with CBP earns nothing for agreeing.

    empirical = 1 / (1 + MAE / 10)                  MAE 0 -> 1.0, 10 -> 0.5, 30 -> 0.25
    trust     = (n * empirical + k * prior) / (n + k)

The prior is the hand-set starting trust, and k is crossings.ENOUGH_TO_JUDGE. Below that
many graded crossings the prior outweighs the evidence, so one lucky crossing cannot
crown a site; at exactly k the two count equally; with none, trust is the prior. The
prior is fusion.prior, the one table of them: fusion falls back on it for a source with
no trust row, so writing a row for a source no crossing has graded (n = 0) leaves that
source's weight exactly where it was.

What gets a row, and what does not:

- Rows come from a roster of the sources fusion weighs. A source with no crossings yet
  still gets a row at its prior (n = 0, which 0021 expects), and a retired source that
  only survives in old claims gets none.
- The "chisme" claim that accuracy() also grades is today's consensus answer, made from
  the sources themselves. Ranking it among them would grade them against themselves, so
  the roster never names it (the rule migration 0021 records for border_source_trust).
- Direction. border_crossings has no direction column, and every crossing starts from
  BorderFeed.bridge(), which is CBP's northbound feed (service.start_crossing). So
  today's accuracy() report is northbound, and a southbound row stays at its prior until
  southbound crossings are logged with their direction (Q-20).

Recomputed daily over the last 30 days and stored one row per source, direction and El
Paso day, so each estimate can cite the trust it used. The ranking of sources is that
table ordered by trust.

Two limits the owner has not ruled on, and should before fusion weighs by these rows
(Roadmap 2.4):

- Each source is graded only on the crossings it gave a number for, as accuracy() does.
  One that is silent on the hard crossings (a lane it does not map, a rush it did not
  publish) scores a lower error than one that answered them, so the ranking can put the
  worse source first.
- A claim is graded as it stood when the crossing began, however old it was, and fusion
  then discounts the same signal again for its age (§4.1). A source that refreshes
  slowly pays for its staleness twice.
"""
from __future__ import annotations

from collections.abc import Mapping
from datetime import date, datetime
from typing import TYPE_CHECKING

from . import fusion
from .cbp import LOCAL_TZ
from .crossings import ENOUGH_TO_JUDGE, CrossingLog
from .signals import DIRECTIONS, PUBLISHED_DIRECTION
from .sources import SOURCES
from .sources.cbp_source import CbpSource

if TYPE_CHECKING:
    from .storage import BorderStore

TABLE = "border_source_trust"
TRUST_WINDOW_DAYS = 30            # SPEC §4.2 grades each source over the last 30 days
PRIOR_CROSSINGS = ENOUGH_TO_JUDGE  # k: the prior counts as this many crossings (SPEC §4.2)
ERROR_SCALE = 10                  # minutes of average error that halve empirical trust

# ASSUMPTION, from evidence: every logged crossing is northbound (see the docstring).
CROSSINGS_DIRECTION = "north"

# A source accuracy() did not grade: no crossings, so nothing but the prior.
_NOT_GRADED = {"crossings": 0, "mean_abs_error": None, "bias": None, "within_10": None}


def empirical(mae: float) -> float:
    """Trust from the average error alone: 1 when exact, halving at ERROR_SCALE minutes."""
    if mae < 0:
        raise ValueError(f"a mean absolute error cannot be negative, got {mae}")
    return 1 / (1 + mae / ERROR_SCALE)


def trust_from(n: int, mae: float | None, prior: float) -> float:
    """The prior and the evidence, weighted by how much evidence there is."""
    if n < 0:
        raise ValueError(f"a count of crossings cannot be negative, got {n}")
    if n == 0:
        return prior
    if mae is None:
        raise ValueError(f"{n} graded crossings but no error")
    return (n * empirical(mae) + PRIOR_CROSSINGS * prior) / (n + PRIOR_CROSSINGS)


def el_paso_day(now: datetime) -> date:
    """The day a row belongs to: El Paso's date, never UTC's (invariants §1)."""
    if now.tzinfo is None:
        raise ValueError("naive time: which day it is depends on the zone")
    return now.astimezone(LOCAL_TZ).date()


def published_roster() -> dict[tuple[str, str], str]:
    """Every published source we read today, in the one direction they publish:
    {(source, direction): kind}. Other kinds join as their signals are built."""
    names = (CbpSource.name, *(source.name for source in SOURCES))
    return {(name, PUBLISHED_DIRECTION): "published" for name in names}


def trust_rows(reports: Mapping[str, dict], roster: Mapping[tuple[str, str], str],
               day: date) -> list[dict]:
    """One border_source_trust row per roster entry, best first within each direction.

    reports maps a direction to the crossings.accuracy() report graded in it. A
    direction with no report has no crossings, so its rows are the prior alone.
    """
    for direction in (*reports, *(d for _, d in roster)):
        if direction not in DIRECTIONS:
            raise ValueError(f"unknown direction {direction!r}; expected one of {sorted(DIRECTIONS)}")
    rows = []
    for (source, direction), kind in roster.items():
        graded = (reports[direction]["sources"].get(source, _NOT_GRADED)
                  if direction in reports else _NOT_GRADED)
        start = fusion.prior(source, kind)
        rows.append({
            "source": source,
            "direction": direction,
            "day": day.isoformat(),
            "n": graded["crossings"],
            "mae": graded["mean_abs_error"],
            "bias": graded["bias"],
            "within_10": graded["within_10"],
            "prior": start,
            "trust": round(trust_from(graded["crossings"], graded["mean_abs_error"], start), 3),
        })
    return sorted(rows, key=lambda r: (r["direction"], -r["trust"], r["source"]))


async def save(store: BorderStore, rows: list[dict]) -> int:
    """Upsert one day's rows, so a rerun of the daily job replaces that day instead of
    failing on the key. It goes through the store's own call, so a failed write is logged
    and counted in store.failures like every border write (ADR-0011), never raised.
    Returns the rows written: 0 when storage is off or the write failed."""
    if not rows:
        return 0
    written = await store._call("save_trust", lambda c: c.table(TABLE).upsert(
        rows, on_conflict="source,direction,day"))
    return len(written) if isinstance(written, list) else 0


async def recompute(store: BorderStore, now: datetime) -> int:
    """The daily job: grade every published source over the window and store the rows.
    Returns the rows written, which is 0 when storage is off or a call failed, so a
    caller never reports rows that are not in the table.

    A failed read of border_crossings comes back as [], exactly like no crossings, and
    storing that would reset every source to its prior for the day. So when the read
    fails nothing is written; the failure is already counted in store.failures for the
    run to report (ADR-0011).
    """
    day = el_paso_day(now)
    failures = store.failures
    report = await CrossingLog(store, lambda: now).accuracy(TRUST_WINDOW_DAYS)
    if store.failures > failures:
        return 0
    return await save(store, trust_rows({CROSSINGS_DIRECTION: report}, published_roster(), day))
