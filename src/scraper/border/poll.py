"""Record one reading. Scheduled by GitHub Actions, like every other job in this repo.

    python -m scraper.border.poll               # one reading, written to Supabase
    python -m scraper.border.poll --prune       # ...then the daily work: retention, trust
    python -m scraper.border.poll --dry-run     # read and report, write nothing

Each run also upserts border_current_waits: every lane of every bridge as a follower
would be told it right now, which is what the knowledge-base Sheet reads.

Without it, border_readings only fills when something happens to ask, so deltas, drop
alerts, the trend and "normal for this hour" are blind after any quiet spell. There
is no --loop: GitHub Actions is the only scheduler in this system, deliberately, so a
missed run is visible in the Actions UI rather than inside a process nobody watches.

Readings are kept 60 days by default: border_typical_waits looks back 8 weeks, so a
shorter window quietly starves it.

Each run then feeds the Chisme estimate (Roadmap 1.3 and 2.4). Every published reading
is stored as a Signal in border_signals, and fusion turns the last fusion.HISTORY of
them into one estimate per bridge, lane and direction, stored in border_estimates with
shown = false. Nothing reads those rows for an answer: the estimate is graded against
timed crossings and shown only once a bridge meets the Q-6 target (SPEC §4.6, ADR-0013).
So this step runs after the answer is written and never touches it. The daily run
(--prune) also applies the estimator's retention (0021) and recomputes each source's
trust (trust.py), which the next day's estimates are weighed by.

Exit codes: 0 read (and stored, unless a dry run), 1 the read failed, 2 stored nothing
(storage off), 3 read but a database call outside the estimator failed, 4 the answer is
unaffected but the estimator reported trouble: a reading the Signal contract refused, a
failed call to its tables (0021), or an error in it or in the daily trust job. The log
line names which. Code 3 is the one that matters in production: every write swallows
its own failure, so without it a missing migration or a revoked key would leave the
workflow green while nothing was being recorded (ADR-0011). Code 4 is just as red, and
says followers were not affected. A dry run can exit 4 too: it still checks every
reading against the Signal contract.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import time
from datetime import UTC, datetime, timedelta

from . import current, fusion, trust
from .service import BorderFeed, FeedError
from .signals import Signal, from_reading
from .sources import Reading, Source, readings_from
from .storage import BorderStore

log = logging.getLogger("scraper.border.poll")
KEEP_READINGS_DAYS = 60


class _Kept(Source):
    """One of the feed's other sources, keeping what its last fetch returned.

    BorderFeed reconciles every reading into decisions and keeps only those, while a
    Signal needs the readings themselves: a stamp, a label, the lanes open. Wrapping the
    sources is the way in that leaves BorderFeed, and every answer it gives, untouched:
    the feed sees the same name, ports and readings as before. It only works on a feed
    that does not cache sources, which the poller's never does (cache_seconds=0)."""

    def __init__(self, inner: Source) -> None:
        self.inner, self.name = inner, inner.name
        self.independent, self.expected_ports = inner.independent, inner.expected_ports
        self.readings: list[Reading] = []

    def is_configured(self) -> bool:
        return self.inner.is_configured()

    async def fetch(self, http, now: datetime) -> list[Reading]:
        self.readings = await self.inner.fetch(http, now)
        return self.readings


def _keep_readings(feed: BorderFeed) -> list[_Kept]:
    """Wrap the feed's other sources (once), emptied for this poll, so a source that
    fails or is switched off now contributes nothing rather than its last poll's readings."""
    kept = [s if isinstance(s, _Kept) else _Kept(s) for s in feed.extra_sources]
    for source in kept:
        source.readings = []
    feed.extra_sources = kept
    return kept


async def once(feed: BorderFeed, dry_run: bool = False) -> dict:
    started = time.monotonic()
    kept = _keep_readings(feed)
    # A last-good reading served after a failure is not a reading at all for a poller.
    snapshot = await feed.snapshot(force=True, allow_stale=False)
    lanes = [lane for port in snapshot.ports for lane in port.lanes.values()]
    # The knowledge-base table: every lane of every bridge, as a follower would be told it.
    now_rows = current.rows(feed, snapshot, datetime.now(UTC))
    written = 0 if dry_run else await feed.storage.upsert_current(now_rows)
    result = {
        "at": datetime.now(UTC).isoformat(timespec="seconds"),
        "ms": round((time.monotonic() - started) * 1000),
        "ports": len(snapshot.ports),
        "lanes": len(lanes),
        "open_lanes": sum(1 for lane in lanes if lane.state == "open"),
        "disputed": feed.lanes_disputed,
        "sources": {name: str(status) for name, status in feed.source_status.items()},
        "stored": not dry_run and feed.storage.enabled,
        "current_rows": written,
    }
    if dry_run:
        result["would_write"] = len(lanes)
        result["would_write_current"] = len(now_rows)
    # The answer is written. Whatever happens to the hidden estimate cannot change it, so
    # an error there is logged and reported, never raised into the poll (ADR-0011).
    failures = feed.storage.failures
    try:
        readings = readings_from(snapshot) + [r for s in kept for r in s.readings if r.source]
        report = await estimate(feed.storage, snapshot.at, readings)
    except Exception as exc:   # the hidden estimate must never cost the answer
        log.exception("the estimate failed; the answer was recorded without it")
        report = {"error": f"{type(exc).__name__}: {exc}"}
    report["failed_calls"] = feed.storage.failures - failures
    result["estimate"] = report
    return result


async def estimate(store: BorderStore, read_at: datetime, readings: list[Reading]) -> dict:
    """Store this poll's published readings as signals, then fuse every signal of the last
    fusion.HISTORY into one hidden estimate per bridge, lane and direction.

    A lane gets an estimate in a direction once any signal speaks for it there. Every
    published source is northbound today (signals.PUBLISHED_DIRECTION), so southbound
    lanes get theirs when a southbound signal exists, not before.

    With storage off (a dry run) the estimate is made from this poll's signals alone and
    nothing is written, so a dry run still proves every reading passes the contract."""
    fresh, refused = published_signals(readings, read_at)
    report = {"signals": len(fresh), "refused": refused, "signal_rows": 0,
              "estimates": 0, "estimate_rows": 0}
    if not store.enabled:
        report["estimates"] = len(fusion.fuse_all(fresh, read_at))
        return report
    report["signal_rows"] = await store.save_signals([s.to_row() for s in fresh])
    stored = await store.signals_since((read_at - fusion.HISTORY).isoformat())
    since_day = trust.el_paso_day(read_at) - timedelta(days=trust.TRUST_WINDOW_DAYS)
    rows = await store.source_trust(since_day.isoformat())
    if stored is None or rows is None:
        # Without the history a source's last word and CBP's closure are lost; without
        # the trust table every source drops to its prior. An estimate made either way
        # would be stored as if it were the real one. The failed read is already counted.
        return report
    signals, first_read = merged(stored, fresh, read_at)
    estimates = fusion.fuse_all(signals, read_at, latest_trust(rows), first_read)
    report["estimates"] = len(estimates)
    # Hidden until that bridge and direction meet the go-live target (SPEC §4.6, Roadmap
    # 7.2). Written out rather than left to the column's default, so nothing here can
    # show an estimate by accident.
    report["estimate_rows"] = await store.save_estimates(
        [{**e.to_row(), "shown": False} for e in estimates.values()])
    return report


def published_signals(readings: list[Reading], read_at: datetime) -> tuple[list[Signal], int]:
    """Each reading as a Signal, each distinct claim once, and how many the contract
    refused. A refused reading is named in the log and never stored: it means a source
    and the contract disagree (a lane id, a negative wait), which someone has to fix."""
    made: dict[str, Signal] = {}
    refused = 0
    for reading in readings:
        try:
            signal = from_reading(reading, read_at)
        except ValueError as exc:
            refused += 1
            log.error("signal refused: %s %s %s: %s", reading.source, reading.port_number,
                      reading.lane, exc)
            continue
        made.setdefault(signal.content_hash, signal)
    return list(made.values()), refused


def merged(stored: list[dict], fresh: list[Signal],
           read_at: datetime) -> tuple[list[Signal], dict[str, datetime]]:
    """Every signal fusion weighs this poll, once each, and when each was last read,
    which is what decides a source's newest claim (fusion).

    What a source says this poll is its word now, even a claim it made before. CBP's
    9:00 pm 48, first read at 9:03 and shown again at 9:40 after a closure read at 9:20,
    has reopened the lane, as the answer written this poll says; dated by its first read,
    it would lose to the closure and the estimate would say closed (SPEC §4.4: closures
    follow CBP). A stored claim no source repeated this poll, such as a site that is down,
    is dated by captured_at, the read that stored it.

    border_signals keeps one row per claim, so a re-read is not stored. The replay
    harness (Roadmap 2.5), which has only captured_at, ranks a repeated claim by its first
    read: in exactly that case replay and the stored estimate can differ."""
    signals: dict[str, Signal] = {}
    read: dict[str, datetime] = {}
    for row in stored:
        signal = Signal.from_row(row)
        signals[signal.content_hash] = signal
        read[signal.content_hash] = datetime.fromisoformat(row["captured_at"])
    for signal in fresh:
        signals.setdefault(signal.content_hash, signal)
        read[signal.content_hash] = read_at
    return list(signals.values()), read


def latest_trust(rows: list[dict]) -> dict[tuple[str, str], float]:
    """Each source's newest trust in each direction, as fusion takes it. A source with no
    row is weighed at its prior, which is also what trust.py stores for a source no
    timed crossing has graded yet, so the two cannot disagree."""
    newest: dict[tuple[str, str], tuple[str, float]] = {}
    for row in rows:
        key = (row["source"], row["direction"])
        if key not in newest or row["day"] > newest[key][0]:   # ISO dates sort as dates
            newest[key] = (row["day"], row["trust"])
    return {key: value for key, (_, value) in newest.items()}


async def daily(store: BorderStore) -> dict:
    """The estimator's part of the daily run: its own retention (border_prune_estimator,
    which bounds the rows every poll adds), then each source's trust from the last 30
    days of timed crossings (trust.recompute), which the next estimates are weighed by."""
    failures = store.failures
    report: dict = {}
    try:
        report["removed"] = await store.prune_estimator()
        report["trust_rows"] = await trust.recompute(store, datetime.now(UTC))
    except Exception as exc:   # like the estimate's, never the reading's problem
        log.exception("the daily trust job failed")
        report["error"] = f"{type(exc).__name__}: {exc}"
    report["failed_calls"] = store.failures - failures
    log.info("estimator retention and trust: %s", report)
    return report


def _trouble(part: str, report: dict) -> list[str]:
    """What one of the estimator's reports says went wrong, in words for the log."""
    found = []
    if report.get("refused"):
        found.append(f"{report['refused']} reading(s) the Signal contract refused")
    if report.get("failed_calls"):
        found.append(f"{report['failed_calls']} failed database call(s)")
    if report.get("error"):
        found.append(report["error"])
    return [f"{part}: {what}" for what in found]


async def run(dry_run: bool = False, prune: bool = False, keep_days: int = KEEP_READINGS_DAYS,
              quiet: bool = False) -> int:
    storage = BorderStore()
    if not storage.enabled and not dry_run:
        log.warning("SUPABASE_URL / SUPABASE_KEY unset — nothing will be stored")
    # cache_seconds=0: a poller exists to take a genuinely new reading each time.
    # dry_run gets a storage-less feed, so "writes nothing" is true by construction.
    feed = BorderFeed(storage=storage if not dry_run else BorderStore(client=None),
                      cache_seconds=0, enrich=False)
    daily_report: dict = {}
    try:
        result = await once(feed, dry_run)
    except FeedError as exc:
        log.error("read failed: %s", exc)
        return 1
    finally:
        # Retention does not depend on CBP answering, so it runs even after a failed read.
        if prune and storage.enabled and not dry_run:
            removed = await storage.prune(keep_readings_days=keep_days)
            log.info("retention: removed %s", removed)
            daily_report = await daily(storage)
    if not quiet:
        print(json.dumps(result, ensure_ascii=False))
    if not storage.enabled and not dry_run:
        return 2    # nothing was stored, so nothing the estimator did is the news
    # The estimator's failed calls are its own; any other failure is the reading's.
    parts = {"estimate": result["estimate"], "daily job": daily_report}
    if storage.failures > sum(part.get("failed_calls", 0) for part in parts.values()):
        log.error("%d database call(s) failed; this reading may not have been recorded",
                  storage.failures)
        return 3
    trouble = [what for name, part in parts.items() for what in _trouble(name, part)]
    if trouble:
        log.error("the answer is unaffected, but the estimator had trouble: %s",
                  "; ".join(trouble))
        return 4
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true", help="read and report, write nothing")
    ap.add_argument("--quiet", action="store_true", help="only print problems")
    ap.add_argument("--prune", action="store_true",
                    help="after the reading, the daily work: retention and source trust")
    ap.add_argument("--keep-days", type=int, default=KEEP_READINGS_DAYS, metavar="DAYS",
                    help=f"days of readings to keep (default {KEEP_READINGS_DAYS})")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.WARNING if args.quiet else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    return asyncio.run(run(args.dry_run, args.prune, args.keep_days, args.quiet))


if __name__ == "__main__":
    raise SystemExit(main())
