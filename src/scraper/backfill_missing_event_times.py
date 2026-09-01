"""Recover start times for stored events a listing only gave a date for.

Sibling of `backfill_event_timezones`, deliberately kept separate because it
works differently: that one re-derives a time from the payload already saved on
the row and never touches the network, while this one has nothing to re-derive
and must go ask the event's own page.

The rows this repairs come from listing pages that publish
``"startDate": "2026-08-28"`` with no hour — Eventbrite's search results are the
main offender. Parsing that yields local midnight, which the carousel and the
knowledge-base export both (correctly) refuse to show, so a real 8pm show
reaches customers with no time at all. The event's own page carries the full
``2026-08-28T20:00:00-05:00``.

Note these rows never self-heal: storage._apply_merge only backfills fields an
existing row is MISSING, and start_time is not one of them, so re-scraping a
known event leaves its midnight in place no matter how often it is seen.

    python -m scraper.backfill_missing_event_times --dry-run
    python -m scraper.backfill_missing_event_times --dry-run --all
    python -m scraper.backfill_missing_event_times
"""

from __future__ import annotations

import argparse
import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from .core.config import settings
from .core.eventtime import local_day, to_event_local
from .core.http import HttpClient
from .sources.events_web import (
    _BROWSER_UA,
    _dt,
    _is_date_only,
    _is_detail_url,
    _iter_jsonld_events,
)

log = logging.getLogger("scraper.backfill_missing_event_times")

_PAST_WINDOW_DAYS = 30
# Public events essentially never start before this hour, so a stored time
# earlier than it is a placeholder rather than a real doors-open. Same threshold
# social/selection.py uses to decide a time is not worth showing.
_EARLIEST_PLAUSIBLE_HOUR = 6


def _needs_a_time(row: dict[str, Any]) -> bool:
    """A row whose stored time is a parse artifact rather than something learned."""
    start = local_day(row.get("start_time"))
    if start is None or start.hour >= _EARLIEST_PLAUSIBLE_HOUR:
        return False
    raw = row.get("raw")
    # Only date-only rows are safe to repair this way. A row with a real
    # timestamp that merely lands early is a different bug (see
    # backfill_event_timezones) and must not be overwritten from the web.
    return isinstance(raw, dict) and _is_date_only(raw.get("startDate"))


async def _lookup(row: dict[str, Any], http: HttpClient) -> Optional[datetime]:
    """The event's real start time, from its own page. None if unavailable."""
    url = row.get("url")
    if not url or not _is_detail_url(url):
        return None
    try:
        if not await http.can_fetch(url):
            return None
        html = await http.get_text(url, headers={"User-Agent": _BROWSER_UA})
    except Exception as exc:  # noqa: BLE001 - a failed lookup leaves the row as-is
        log.debug("lookup %s failed: %s", url, exc)
        return None

    stored = local_day(row.get("start_time"))
    for node in _iter_jsonld_events(html):
        if not isinstance(node, dict):
            continue
        raw_start = node.get("startDate")
        if _is_date_only(raw_start):
            continue  # no better than what we already have
        parsed = _dt(raw_start)
        if parsed is None:
            continue
        found = to_event_local(parsed)
        # A detail page also carries "related events" JSON-LD. Requiring the
        # same calendar day keeps us from adopting a neighbor's time.
        if stored is not None and found.date() != stored.date():
            continue
        return found
    return None


async def _run(args: argparse.Namespace) -> int:
    if not (settings.supabase_url and settings.supabase_key):
        log.error("SUPABASE_URL / SUPABASE_KEY are not set; nothing to do.")
        return 1

    from supabase import create_client

    client = create_client(settings.supabase_url, settings.supabase_key)
    now = datetime.now(timezone.utc)
    floor = (now - timedelta(days=_PAST_WINDOW_DAYS)) if args.all else now

    rows = (
        client.table("events")
        .select("id,source,title,url,start_time,raw")
        .gte("start_time", floor.isoformat())
        .order("start_time")
        .limit(2000)
        .execute()
        .data
        or []
    )
    targets = [r for r in rows if _needs_a_time(r)]
    log.info("%d event(s) in scope, %d missing a time", len(rows), len(targets))
    if not targets:
        return 0

    async with HttpClient() as http:
        found = await asyncio.gather(*(_lookup(r, http) for r in targets))

    fixed = 0
    for row, start in zip(targets, found):
        if start is None:
            log.info("MISS %s  no time on the page  %r", row["id"], row["title"][:46])
            continue
        log.info(
            "FIX  %s  %s -> %s  %r",
            row["id"],
            local_day(row["start_time"]).strftime("%Y-%m-%d %H:%M"),
            start.strftime("%Y-%m-%d %H:%M"),
            row["title"][:46],
        )
        if not args.dry_run:
            client.table("events").update({"start_time": start.isoformat()}).eq(
                "id", row["id"]
            ).execute()
        fixed += 1

    log.info(
        "%s: %d row(s) recovered, %d still without a time",
        "dry run" if args.dry_run else "done",
        fixed,
        len(targets) - fixed,
    )
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Recover times for date-only event rows.")
    ap.add_argument("--dry-run", action="store_true", help="report without writing")
    ap.add_argument("--all", action="store_true", help=f"also look back {_PAST_WINDOW_DAYS} days")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
