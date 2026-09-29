"""Record a reading. Cron this so history accrues whether or not anyone asks.

    python3 -m border.poll                  # one reading, written to Supabase
    python3 -m border.poll --loop 300       # keep going, every 5 minutes
    python3 -m border.poll --dry-run        # read and report, write nothing
    python3 -m border.poll --prune          # one reading, then apply retention (cron daily)

In --loop mode retention runs at start and then once a day, so nothing else needs to
schedule it. Readings are kept 60 days by default: "normal for this hour" looks back 8
weeks, so a shorter window quietly starves it.

Without it, border_readings only fills when the pipeline happens to pull, so deltas and
drop alerts are blind after any quiet spell. With it, every restart of the API already
has yesterday's context to compare against.

Exit codes: 0 read and stored, 1 the read failed, 2 stored nothing (storage disabled).
"""
from __future__ import annotations

import argparse
import json
import logging
import signal
import threading
import time
from datetime import datetime, timezone

from .core.storage import Storage
from .service import BorderFeed, FeedError

log = logging.getLogger("border.poll")
_stop = threading.Event()
PRUNE_EVERY_SECONDS = 24 * 3600
KEEP_READINGS_DAYS = 60


def _handle_signal(signum, frame):  # pragma: no cover - signal path
    _stop.set()
    log.info("signal %s received, finishing the current reading", signum)


def once(feed: BorderFeed, dry_run: bool = False) -> dict:
    started = time.monotonic()
    # A last-good reading served after a failure is not a reading at all for a poller,
    # and treating it as one would skip the backoff.
    snapshot = feed.snapshot(force=True, allow_stale=False)
    lanes = [lane for port in snapshot.ports for lane in port.lanes.values()]
    open_lanes = [lane for lane in lanes if lane.state == "open"]

    result = {
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "ms": round((time.monotonic() - started) * 1000),
        "ports": len(snapshot.ports),
        "lanes": len(lanes),
        "open_lanes": len(open_lanes),
        "disputed": feed.lanes_disputed,
        "sources": {name: str(status) for name, status in feed.source_status.items()},
        "stored": not dry_run and feed.storage.enabled,
    }
    if dry_run:
        result["would_write"] = len(lanes)
    return result


def prune(storage: Storage, keep_days: int) -> dict | None:
    removed = storage.prune(keep_readings_days=keep_days)
    if removed is not None:
        log.info("retention: removed %s", removed)
    return removed


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--loop", type=int, metavar="SECONDS",
                    help="keep polling on this interval instead of exiting")
    ap.add_argument("--dry-run", action="store_true", help="read and report, write nothing")
    ap.add_argument("--quiet", action="store_true", help="only print problems")
    ap.add_argument("--prune", action="store_true",
                    help="apply retention after the reading (implied daily with --loop)")
    ap.add_argument("--keep-days", type=int, default=KEEP_READINGS_DAYS, metavar="DAYS",
                    help=f"days of readings to keep (default {KEEP_READINGS_DAYS})")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.WARNING if args.quiet else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    storage = Storage()
    if not storage.enabled and not args.dry_run:
        log.warning("SUPABASE_URL / SUPABASE_SERVICE_KEY unset — nothing will be stored")

    # cache_seconds=0: a poller exists to take a genuinely new reading each time.
    feed = BorderFeed(storage=storage, cache_seconds=0, enrich=False)
    failures = 0
    pruned_at: float | None = None

    while True:
        try:
            result = once(feed, args.dry_run)
            failures = 0
            if not args.quiet:
                print(json.dumps(result, ensure_ascii=False))
        except FeedError as exc:
            failures += 1
            log.error("read failed (%s in a row): %s", failures, exc)
            if not args.loop:
                return 1

        # Retention does not depend on CBP answering, so it runs even after a failed read.
        due = pruned_at is None or time.monotonic() - pruned_at >= PRUNE_EVERY_SECONDS
        if storage.enabled and not args.dry_run and (args.prune or args.loop) and due:
            prune(storage, args.keep_days)
            pruned_at = time.monotonic()

        if not args.loop or _stop.is_set():
            break

        # Back off while the feed is failing rather than hammering it.
        delay = args.loop * min(2 ** failures, 8) if failures else args.loop
        if _stop.wait(delay):
            break

    if not storage.enabled and not args.dry_run:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
