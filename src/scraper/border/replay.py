"""Replay the Chisme estimate against timed crossings: the go-live gate (Roadmap 2.5).

    python -m scraper.border replay      # both numbers, per bridge and direction, last 30 days

SPEC §4.6 hides the estimate until it meets the Q-6 target for a bridge and direction,
and ADR-0013 says a weight change is justified by this harness, not by inspection. So it
re-runs fusion over the stored border_signals as they stood when each timed crossing
began, and grades both numbers against what the person actually waited:

- **Today's answer** is the crossing's shown_minutes: what the follower was told when
  they joined the line, frozen there by crossings.py (accuracy() calls it "chisme").
- **The Chisme estimate** is fusion.fuse at the crossing's started_at, over only the
  signals read by then (border_signals.captured_at). A claim observed before the start
  but read after it is one we could not have known yet, so it is left out, just as
  crossings.py freezes each source's opinion at the first message rather than at a
  later, luckier reading.

CBP is graded like every other number and is never the answer key: the only truth is a
timed crossing. The verdict compares sums of whole minutes, never a rounded average, so
an average error of 5.03 minutes prints as 5.0 and still fails.

Assumptions, each also stated where it is applied:

- Every crossing is northbound (trust.CROSSINGS_DIRECTION): border_crossings has no
  direction column, and every crossing starts from CBP's northbound feed.
- Trust is the newest row from before the crossing's own El Paso day, rebuilt by today's
  code from that row's n and mae, so a changed prior, k or error scale shows here; the
  grading behind n and mae (crossings.accuracy) is held as stored. See trust_at.
- A claim read again by a later poll is ranked by its first read, the only one
  border_signals keeps, so in that case alone replay and the stored estimate can differ
  (poll.merged says the same from the other side).
- Crowd and position signals are pruned after 30 days (0021), so a crossing at the
  oldest edge of the window can be replayed without reports the poll had.
- Each number is graded only on the crossings it gave a number for, as accuracy() does.
  An estimate with no number gives the follower nothing to be misled by, and SPEC §8
  measures coverage separately. But an estimate that is silent on the hard crossings
  scores better than one that answers them (trust.py's first open limit), so the count
  graded is printed beside the count of crossings, for a reader to judge.

Exit codes: 0 the report was printed, whatever the verdict; 1 a read failed, so there is
no verdict, because a partial history would change it; 2 storage is off, so there is
nothing to replay.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

from . import fusion, trust
from .cbp import LOCAL_TZ, PORTS
from .crossings import CLOSE_ENOUGH
from .poll import latest_trust
from .signals import Signal
from .storage import PAGE_ROWS, BorderStore

log = logging.getLogger("scraper.border.replay")

# The go-live target for one bridge and direction (Q-6, SPEC §8, ADR-0013).
TARGET_ERROR = 5            # average error at most this many minutes
TARGET_WITHIN = 80          # at least this percent of crossings within CLOSE_ENOUGH minutes
TARGET_CROSSINGS = 30       # over at least this many graded crossings
GRADING_DAYS = 30           # SPEC §8 grades over a rolling 30 days

# The border_source_trust columns trust_at rebuilds trust from. storage.source_trust reads
# the stored trust alone, which is all the poller needs.
TRUST_COLUMNS = "source,direction,day,n,mae,trust"


@dataclass(frozen=True)
class Graded:
    """One timed crossing, and both numbers as they stood when it began."""
    port_number: str
    lane: str
    direction: str
    started_at: datetime
    actual: int                 # the minutes the person actually waited
    today: int | None           # shown_minutes; None when the answer had no number
    estimate: int | None        # replayed; None when fusion had no number (closed, no data)


@dataclass(frozen=True)
class Score:
    """One number's record over a set of crossings, as sums of whole minutes so the
    verdict is exact. error = claimed - actual, as in crossings.accuracy(): a positive
    bias over-states the wait."""
    n: int = 0
    abs_error: int = 0
    error: int = 0
    within: int = 0             # crossings within CLOSE_ENOUGH minutes

    def add(self, claimed: int, actual: int) -> Score:
        error = claimed - actual
        return Score(n=self.n + 1, abs_error=self.abs_error + abs(error),
                     error=self.error + error,
                     within=self.within + int(abs(error) <= CLOSE_ENOUGH))

    @property
    def mae(self) -> float | None:
        return self.abs_error / self.n if self.n else None

    @property
    def bias(self) -> float | None:
        return self.error / self.n if self.n else None

    @property
    def within_10(self) -> float | None:
        return self.within / self.n if self.n else None

    def shortfalls(self) -> list[str]:
        """Why this record misses the go-live target, in words; empty when it meets it."""
        missing = []
        if self.n < TARGET_CROSSINGS:
            missing.append(f"{self.n} of {TARGET_CROSSINGS} crossings graded")
        if self.n and self.abs_error > TARGET_ERROR * self.n:
            missing.append(f"average error {self.mae:.2f} min, over {TARGET_ERROR}")
        if self.n and self.within * 100 < TARGET_WITHIN * self.n:
            missing.append(f"{self.within_10:.1%} within {CLOSE_ENOUGH} min, "
                           f"under {TARGET_WITHIN}%")
        return missing


@dataclass(frozen=True)
class Bridge:
    """Both numbers' records at one bridge in one direction, every lane together: the
    estimate is switched on per bridge and direction, not per lane (SPEC §4.6)."""
    port_number: str
    direction: str
    crossings: int              # every finished crossing, graded by either number or not
    today: Score
    estimate: Score

    @property
    def passes(self) -> bool:
        """The go-live verdict. It is the estimate's alone: today's answer is shown either
        way, and is graded beside it only for comparison (SPEC §8)."""
        return not self.estimate.shortfalls()


def trust_at(rows: list[dict], day: date) -> dict[tuple[str, str], float]:
    """The trust to replay a crossing on `day` with: each source's newest
    border_source_trust row from the TRUST_WINDOW_DAYS before that day, the window the
    poller reads (poll.estimate), with its trust rebuilt by today's code.

    Rebuilt, because the stored trust holds the prior, k and error scale in force the day
    it was written, and while no crossing has graded a source (n = 0) it is that prior.
    Taken as stored, a changed prior would replay as no change at all, and this harness
    is what justifies a weight change (ADR-0013). Rebuilt from the row's own n and mae, it
    equals the stored trust until one of those three changes.

    ASSUMPTION: never the day's own row. A row carries no time of computation, and a
    rerun of the daily job overwrites the day, so the day's row may already count the
    crossing being graded; weights earned from a crossing's own outcome would flatter
    the estimate of it. A row from an earlier day was computed before the crossing
    began, so it cannot. The cost: a crossing after the daily run (10:20 UTC,
    border_poll.yml) is replayed with trust one day older than the poll used."""
    since = (day - timedelta(days=trust.TRUST_WINDOW_DAYS)).isoformat()
    roster = trust.published_roster()       # the sources trust.recompute writes rows for
    return latest_trust([_rebuilt(row, roster) for row in rows
                         if since <= row["day"] < day.isoformat()])


def _rebuilt(row: dict, roster: Mapping[tuple[str, str], str]) -> dict:
    """The row with its trust recomputed as trust.trust_rows would write it today. A
    source the roster no longer names has no kind to take a prior from, so it keeps the
    trust it was stored with."""
    kind = roster.get((row["source"], row["direction"]))
    if kind is None:
        return row
    value = trust.trust_from(row["n"], row["mae"], fusion.prior(row["source"], kind))
    return {**row, "trust": round(value, 3)}    # rounded as trust_rows stores it


def replay(crossings: Iterable[dict], signal_rows: Iterable[dict],
           trust_rows: list[dict]) -> list[Graded]:
    """Every finished crossing with both numbers as they stood at its start, oldest first.

    signal_rows are border_signals rows with captured_at (storage.SIGNAL_COLUMNS), and
    trust_rows are border_source_trust rows. A crossing with no actual_minutes has not
    finished and is left out. Raises ValueError for a time with no zone, and whatever
    Signal.from_row raises for a malformed row: a bad stored row is named, not skipped."""
    # Each lane's claims with their first read, paired once here: content_hash is a sha1
    # that is not cached, and every crossing filters its whole lane.
    signals: dict[tuple[str, str, str], list[tuple[Signal, datetime]]] = {}
    read_at: dict[str, datetime] = {}
    for row in signal_rows:
        signal = Signal.from_row(row)
        claim = signal.content_hash
        if claim in read_at:                    # a page boundary can repeat a row
            continue
        read = read_at[claim] = _moment(row["captured_at"], "a signal's captured_at")
        signals.setdefault((signal.port_number, signal.lane, signal.direction),
                           []).append((signal, read))

    weights: dict[date, dict[tuple[str, str], float]] = {}
    graded = []
    for row in crossings:
        if row.get("actual_minutes") is None:
            continue
        started = _moment(row["started_at"], f"crossing {row.get('id')}'s started_at")
        # ASSUMPTION: northbound, the direction every logged crossing has (trust.py).
        direction = trust.CROSSINGS_DIRECTION
        known = [s for s, read in signals.get((row["port_number"], row["lane"], direction), ())
                 if s.observed_at >= started - fusion.HISTORY and read <= started]
        estimate = None
        if known:
            day = trust.el_paso_day(started)
            if day not in weights:
                weights[day] = trust_at(trust_rows, day)
            estimate = fusion.fuse(known, started, weights[day], read_at).minutes
        graded.append(Graded(port_number=row["port_number"], lane=row["lane"],
                             direction=direction, started_at=started,
                             actual=row["actual_minutes"], today=row.get("shown_minutes"),
                             estimate=estimate))
    return sorted(graded, key=lambda g: g.started_at)


def grade(graded: Iterable[Graded]) -> list[Bridge]:
    """Each bridge and direction's records, by port number then direction."""
    tallies: dict[tuple[str, str], tuple[int, Score, Score]] = {}
    for g in graded:
        crossings, today, estimate = tallies.get((g.port_number, g.direction),
                                                 (0, Score(), Score()))
        if g.today is not None:
            today = today.add(g.today, g.actual)
        if g.estimate is not None:
            estimate = estimate.add(g.estimate, g.actual)
        tallies[(g.port_number, g.direction)] = (crossings + 1, today, estimate)
    return [Bridge(port_number=port, direction=direction, crossings=crossings,
                   today=today, estimate=estimate)
            for (port, direction), (crossings, today, estimate) in sorted(tallies.items())]


def render(bridges: list[Bridge], now: datetime) -> str:
    """The report a person reads, and the one Roadmap 7.1 posts in a PR."""
    window = (f"{fusion.METHOD} replayed against timed crossings finished in the "
              f"{GRADING_DAYS} days to {now.astimezone(LOCAL_TZ):%Y-%m-%d %H:%M} El Paso time")
    target = (f"Target per bridge and direction: average error <= {TARGET_ERROR} min, "
              f">= {TARGET_WITHIN}% within {CLOSE_ENOUGH} min, >= {TARGET_CROSSINGS} crossings")
    lines = [window, target, ""]
    if not bridges:
        lines.append("No finished crossings to grade, so no bridge passes.")
    for b in bridges:
        name = PORTS.get(b.port_number, {}).get("name", "unknown port")
        lines.append(f"{name} ({b.port_number}), {b.direction}: {b.crossings} crossings")
        lines.append(f"  {_record('today', b.today)}")
        lines.append(f"  {_record('estimate', b.estimate)}")
        missing = b.estimate.shortfalls()
        lines.append("  pass" if not missing else f"  FAIL: {'; '.join(missing)}")
    return "\n".join(lines)


def _record(label: str, score: Score) -> str:
    if not score.n:
        return f"{label:<9} 0 graded"
    return (f"{label:<9} {score.n} graded, error {score.mae:.1f} min, "
            f"bias {score.bias:+.1f} min, within {CLOSE_ENOUGH} min {score.within_10:.0%}")


def _moment(text: str, what: str) -> datetime:
    moment = datetime.fromisoformat(text)
    if moment.utcoffset() is None:
        raise ValueError(f"{what} has no time zone: {text!r}")
    return moment


async def collect(store: BorderStore, now: datetime) -> list[Graded] | None:
    """Read what replay needs, and replay it. None when storage is off or a read failed:
    a short history would quietly change the verdict, so no verdict beats a wrong one."""
    if not store.enabled:
        return None
    failures = store.failures
    crossings = await store.crossings_since((now - timedelta(days=GRADING_DAYS)).isoformat())
    if store.failures > failures:       # crossings_since answers a failed read with []
        return None
    if _cut(crossings, "border_crossings"):
        return None
    finished = [c for c in crossings if c.get("actual_minutes") is not None]
    if not finished:
        return []
    earliest = min(_moment(c["started_at"], f"crossing {c.get('id')}'s started_at")
                   for c in finished)
    signal_rows = await store.signals_since((earliest - fusion.HISTORY).isoformat())
    trust_since = trust.el_paso_day(earliest) - timedelta(days=trust.TRUST_WINDOW_DAYS)
    # Through the store's own call, as trust.save writes the table, so a failure is
    # logged and counted like every border call (ADR-0011).
    trust_rows = await store._call("replay_trust", lambda c: c.table(trust.TABLE)
                                   .select(TRUST_COLUMNS).gte("day", trust_since.isoformat()))
    if signal_rows is None or trust_rows is None or _cut(trust_rows, trust.TABLE):
        return None
    return replay(finished, signal_rows, trust_rows)


def _cut(rows: list[dict], table: str) -> bool:
    """True when a read that is not paged came back at PostgREST's cap, which cuts an
    answer at PAGE_ROWS without saying so: rows the verdict needs may be missing."""
    if len(rows) < PAGE_ROWS:
        return False
    log.error("%s returned %d rows, PostgREST's cap: some may be missing", table, len(rows))
    return True


async def run(store: BorderStore, now: datetime) -> int:
    if not store.enabled:
        log.error("SUPABASE_URL / SUPABASE_KEY unset: no stored signals or crossings to replay")
        return 2
    graded = await collect(store, now)
    if graded is None:
        log.error("a read failed, so there is no verdict: a partial history would change it")
        return 1
    print(render(grade(graded), now))
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    return asyncio.run(run(BorderStore(), datetime.now(UTC)))


if __name__ == "__main__":
    raise SystemExit(main())
