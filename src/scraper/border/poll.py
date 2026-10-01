"""Record one reading. Scheduled by GitHub Actions, like every other job in this repo.

    python -m scraper.border.poll               # one reading, written to Supabase
    python -m scraper.border.poll --prune       # ...then apply retention (run it daily)
    python -m scraper.border.poll --dry-run     # read and report, write nothing

Each run also upserts border_current_waits: every lane of every bridge as a follower
would be told it right now, which is what the knowledge-base Sheet reads.

Without it, border_readings only fills when something happens to ask, so deltas, drop
alerts, the trend and "normal for this hour" are blind after any quiet spell. There
is no --loop: GitHub Actions is the only scheduler in this system, deliberately, so a
missed run is visible in the Actions UI rather than inside a process nobody watches.

Readings are kept 60 days by default: border_typical_waits looks back 8 weeks, so a
shorter window quietly starves it.

Exit codes: 0 read (and stored), 1 the read failed, 2 stored nothing (storage off),
3 read but a database call failed. Code 3 is the one that matters in production: every
write swallows its own failure, so without it a missing migration or a revoked key
would leave the workflow green while nothing was being recorded (ADR-0011).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import time
from datetime import UTC, datetime

from . import current
from .service import BorderFeed, FeedError
from .storage import BorderStore

log = logging.getLogger("scraper.border.poll")
KEEP_READINGS_DAYS = 60


async def once(feed: BorderFeed, dry_run: bool = False) -> dict:
    started = time.monotonic()
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
    return result


async def run(dry_run: bool = False, prune: bool = False, keep_days: int = KEEP_READINGS_DAYS,
              quiet: bool = False) -> int:
    storage = BorderStore()
    if not storage.enabled and not dry_run:
        log.warning("SUPABASE_URL / SUPABASE_KEY unset — nothing will be stored")
    # cache_seconds=0: a poller exists to take a genuinely new reading each time.
    # dry_run gets a storage-less feed, so "writes nothing" is true by construction.
    feed = BorderFeed(storage=storage if not dry_run else BorderStore(client=None),
                      cache_seconds=0, enrich=False)
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
    if not quiet:
        print(json.dumps(result, ensure_ascii=False))
    if storage.failures:
        log.error("%d database call(s) failed; this reading may not have been recorded",
                  storage.failures)
        return 3
    return 0 if (storage.enabled or dry_run) else 2


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true", help="read and report, write nothing")
    ap.add_argument("--quiet", action="store_true", help="only print problems")
    ap.add_argument("--prune", action="store_true", help="apply retention after the reading")
    ap.add_argument("--keep-days", type=int, default=KEEP_READINGS_DAYS, metavar="DAYS",
                    help=f"days of readings to keep (default {KEEP_READINGS_DAYS})")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.WARNING if args.quiet else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    return asyncio.run(run(args.dry_run, args.prune, args.keep_days, args.quiet))


if __name__ == "__main__":
    raise SystemExit(main())
