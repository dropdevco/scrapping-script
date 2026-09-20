"""Recover start AND end times for stored events a listing only gave a date for.

Sibling of `backfill_event_timezones`, deliberately kept separate because it
works differently: that one re-derives a time from the payload already saved on
the row and never touches the network, while this one has nothing to re-derive
and must go ask the event's own page.

The rows this repairs come from listing pages that publish
``"startDate": "2026-08-28"`` with no hour — Eventbrite's search results are the
main offender, and ``endDate`` is routinely just as bare. Parsing a bare date
yields local midnight, which the carousel and the knowledge-base export both
(correctly) refuse to show, so a real 8pm show reaches customers with no time
at all — and a real 12 PM-6 PM festival can end up stored as if it ran
midnight to midnight. The event's own page carries the full
``2026-08-28T20:00:00-05:00``, for both fields.

Note these rows never self-heal: storage._apply_merge only backfills fields an
existing row is MISSING, and start_time/end_time are not among them, so
re-scraping a known event leaves the midnight in place no matter how often it
is seen.

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


def _needs_an_end_time(row: dict[str, Any]) -> bool:
    """Same placeholder problem, for end_time.

    Deliberately no hour heuristic here the way _needs_a_time has one: a real
    end time can legitimately BE midnight or later (a show ending at 11:59 PM,
    or one that runs past midnight), so the stored hour proves nothing either
    way. The ORIGINAL raw endDate having been date-only is what's authoritative
    -- if the listing never gave a real end time, whatever is stored now is
    _dt()'s midnight reading of a bare date, not something learned.
    """
    raw = row.get("raw")
    return (
        row.get("end_time") is not None
        and isinstance(raw, dict)
        and _is_date_only(raw.get("endDate"))
    )


async def _lookup(row: dict[str, Any], http: HttpClient) -> tuple[Optional[datetime], Optional[datetime]]:
    """The event's real (start, end) from its own page. Either is None when
    unavailable, or when the detail page is ITSELF still date-only for that
    field -- no better than what is already stored, so not worth adopting."""
    url = row.get("url")
    if not url or not _is_detail_url(url):
        return None, None
    try:
        if not await http.can_fetch(url):
            return None, None
        html = await http.get_text(url, headers={"User-Agent": _BROWSER_UA})
    except Exception as exc:  # noqa: BLE001 - a failed lookup leaves the row as-is
        log.debug("lookup %s failed: %s", url, exc)
        return None, None

    stored_start = local_day(row.get("start_time"))
    for node in _iter_jsonld_events(html):
        if not isinstance(node, dict):
            continue
        raw_start = node.get("startDate")
        if _is_date_only(raw_start):
            continue  # no better than what we already have
        parsed_start = _dt(raw_start)
        if parsed_start is None:
            continue
        found_start = to_event_local(parsed_start)
        # A detail page also carries "related events" JSON-LD. Requiring the
        # same calendar day keeps us from adopting a neighbor's time.
        if stored_start is not None and found_start.date() != stored_start.date():
            continue

        found_end = None
        raw_end = node.get("endDate")
        if raw_end and not _is_date_only(raw_end):
            parsed_end = _dt(raw_end)
            if parsed_end is not None:
                found_end = to_event_local(parsed_end)
        return found_start, found_end
    return None, None


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
        .select("id,source,title,url,start_time,end_time,raw")
        .gte("start_time", floor.isoformat())
        .order("start_time")
        .limit(2000)
        .execute()
        .data
        or []
    )
    targets = [r for r in rows if _needs_a_time(r) or _needs_an_end_time(r)]
    log.info("%d event(s) in scope, %d missing a time", len(rows), len(targets))
    if not targets:
        return 0

    async with HttpClient() as http:
        found = await asyncio.gather(*(_lookup(r, http) for r in targets))

    fixed_start = fixed_end = misses = 0
    for row, (start, end) in zip(targets, found):
        patch: dict[str, Any] = {}
        parts: list[str] = []

        if _needs_a_time(row):
            if start is not None:
                patch["start_time"] = start.isoformat()
                parts.append(f"start {local_day(row['start_time']).strftime('%Y-%m-%d %H:%M')} -> {start.strftime('%Y-%m-%d %H:%M')}")
                fixed_start += 1

        if _needs_an_end_time(row):
            if end is not None:
                patch["end_time"] = end.isoformat()
                parts.append(f"end -> {end.strftime('%Y-%m-%d %H:%M')}")
                fixed_end += 1

        if patch:
            log.info("FIX  %s  %s  %r", row["id"], "; ".join(parts), row["title"][:46])
            if not args.dry_run:
                client.table("events").update(patch).eq("id", row["id"]).execute()
        else:
            log.info("MISS %s  nothing better on the page  %r", row["id"], row["title"][:46])
            misses += 1

    log.info(
        "%s: %d start time(s) and %d end time(s) recovered, %d row(s) still unresolved",
        "dry run" if args.dry_run else "done",
        fixed_start,
        fixed_end,
        misses,
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
