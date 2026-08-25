"""Repair start/end timestamps that were stored as UTC but meant local time.

Until the fix in `core/eventtime.py`, a source that parsed a naive wall-clock
time ("Aug 26, 2026 8:00 PM" on El Paso Live, `"startDate": "2026-08-25T18:30"`
in Visit El Paso's JSON-LD) handed that naive value straight to a `timestamptz`
column, where Postgres read it as UTC. Every such row sits six or seven hours
early: an 8pm concert reads as 2pm, a 5pm social reads as 11am — which is where
the carousel's implausible run of morning slides came from.

The repair does NOT guess an offset. Each row's `raw` payload still holds the
provider's original date fields, so the true value is re-derived by running the
*fixed* parser over that payload — the same code path a fresh scrape would take.
Rows whose source always shipped a real instant (Ticketmaster's
`dates.start.dateTime`) therefore come out unchanged and are left alone, and
rows whose `raw` no longer carries a usable date are reported, not touched.

    python -m scraper.backfill_event_timezones --dry-run          # report only
    python -m scraper.backfill_event_timezones --dry-run --all    # include past events
    python -m scraper.backfill_event_timezones                    # apply
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from .core.config import settings
from .core.eventtime import local_day, to_event_local
from .sources.events_directories import CITY_EVENT_RE
from .sources.events_directories import _parse_datetime as _directory_datetime
from .sources.events_ticketmaster import _start_datetime as _ticketmaster_start
from .sources.events_web import _dt as _jsonld_datetime
from .sources.events_web import _parse_card_datetime as _card_datetime

log = logging.getLogger("scraper.backfill_event_timezones")

# How far back to look when --all is passed. Older rows are dead weight: nothing
# renders them, and their `raw` payloads are the least likely to still parse.
_PAST_WINDOW_DAYS = 365


def _recompute(row: dict[str, Any]) -> tuple[Optional[datetime], Optional[datetime], str]:
    """(start, end, note) as the fixed parsers would produce them today.

    A None start with a "no date in raw" note means "cannot verify", which is
    reported and skipped — never rewritten on a guess.
    """
    source = row.get("source") or ""
    raw = row.get("raw")
    if not isinstance(raw, dict) or not raw:
        return None, None, "no raw payload"

    if source == "events_ticketmaster":
        dates = raw.get("dates")
        if not isinstance(dates, dict):
            return None, None, "no dates block in raw"
        return to_event_local(_ticketmaster_start(dates)), None, "ticketmaster"

    if source == "events_directories":
        # Three shapes, one per path in the connector: a listing card
        # (date/time), a JSON-LD node lifted off a detail page (jsonld), and the
        # City of El Paso calendar, which keeps only the anchor text it matched
        # and is re-parsed here with the same regex that produced the row.
        date_text = raw.get("date")
        if isinstance(date_text, str) and date_text.strip():
            return to_event_local(_directory_datetime(date_text, raw.get("time"))), None, "directory card"

        jsonld = raw.get("jsonld")
        if isinstance(jsonld, dict):
            return (
                to_event_local(_jsonld_datetime(jsonld.get("startDate"))),
                to_event_local(_jsonld_datetime(jsonld.get("endDate"))),
                "directory json-ld",
            )

        listing_text = raw.get("listing_text")
        if isinstance(listing_text, str):
            match = CITY_EVENT_RE.match(listing_text)
            if match is None:
                return None, None, "listing text no longer parses"
            start = _directory_datetime(match.group("date"), match.group("start"))
            end = (
                _directory_datetime(match.group("date"), match.group("end"))
                if match.group("end")
                else None
            )
            return to_event_local(start), to_event_local(end), "city listing"

        return None, None, "no date text in raw"

    if source == "events_web":
        # Two shapes: a JSON-LD node (startDate/endDate) and a calendar card
        # scraped off the La Nube / Visit El Paso listing (date_text/time_text).
        if "startDate" in raw:
            return (
                to_event_local(_jsonld_datetime(raw.get("startDate"))),
                to_event_local(_jsonld_datetime(raw.get("endDate"))),
                "json-ld",
            )
        if "date_text" in raw:
            start, end = _card_datetime(raw.get("date_text"), raw.get("time_text"))
            return to_event_local(start), to_event_local(end), "calendar card"
        return None, None, "unrecognized raw shape"

    # Anything else — notably user-submitted events, which the browser already
    # sent as a real instant — is out of scope for this repair.
    return None, None, f"source {source!r} not handled"


def _changed(stored: Optional[str], fixed: Optional[datetime]) -> bool:
    if fixed is None:
        return False
    current = local_day(stored)
    return current is None or current != fixed


def main() -> int:
    ap = argparse.ArgumentParser(description="Repair locally-parsed event times stored as UTC.")
    ap.add_argument("--dry-run", action="store_true", help="report without writing")
    ap.add_argument(
        "--all",
        action="store_true",
        help=f"also repair events that already happened, back {_PAST_WINDOW_DAYS} days",
    )
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    if not (settings.supabase_url and settings.supabase_key):
        log.error("SUPABASE_URL / SUPABASE_KEY are not set; nothing to do.")
        return 1

    from supabase import create_client

    client = create_client(settings.supabase_url, settings.supabase_key)

    now = datetime.now(timezone.utc)
    floor = (now - timedelta(days=_PAST_WINDOW_DAYS)) if args.all else now

    rows: list[dict[str, Any]] = []
    page = 1000
    offset = 0
    while True:
        batch = (
            client.table("events")
            .select("id,source,title,start_time,end_time,raw")
            .gte("start_time", floor.isoformat())
            .order("start_time")
            .range(offset, offset + page - 1)
            .execute()
            .data
            or []
        )
        rows.extend(batch)
        if len(batch) < page:
            break
        offset += page

    log.info("%d event(s) in scope", len(rows))

    fixed_count = 0
    already_ok = 0
    skipped: dict[str, int] = {}

    for row in rows:
        start, end, note = _recompute(row)
        if start is None:
            skipped[note] = skipped.get(note, 0) + 1
            continue

        # The bug being repaired shifts a timestamp by one UTC offset — at most
        # six or seven hours, so at most one calendar day. A re-parse landing
        # further away than that is disagreeing with the stored row for some
        # other reason (a date text with no year that now rolls to a different
        # one, a parser that has since changed), and rewriting on that would be
        # a guess rather than a repair.
        stored = local_day(row.get("start_time"))
        if stored is not None and abs(start - stored) > timedelta(days=1):
            skipped["re-parse disagrees by more than a day"] = (
                skipped.get("re-parse disagrees by more than a day", 0) + 1
            )
            continue

        patch: dict[str, Any] = {}
        if _changed(row.get("start_time"), start):
            patch["start_time"] = start.isoformat()
        if end is not None and _changed(row.get("end_time"), end):
            patch["end_time"] = end.isoformat()

        if not patch:
            already_ok += 1
            continue

        before = local_day(row.get("start_time"))
        log.info(
            "FIX %s  %s -> %s  [%s] %r",
            row["id"],
            before.strftime("%Y-%m-%d %H:%M") if before else "?",
            start.strftime("%Y-%m-%d %H:%M"),
            note,
            (row.get("title") or "")[:48],
        )
        fixed_count += 1

        if not args.dry_run:
            try:
                client.table("events").update(patch).eq("id", row["id"]).execute()
            except Exception as exc:  # noqa: BLE001 - one bad row must not end the sweep
                log.error("  failed to update %s: %s", row["id"], exc)

    log.info(
        "\n%s: %d row(s) corrected, %d already correct, %d skipped",
        "dry run" if args.dry_run else "done",
        fixed_count,
        already_ok,
        sum(skipped.values()),
    )
    for note, count in sorted(skipped.items(), key=lambda kv: -kv[1]):
        log.info("  skipped (%s): %d", note, count)
    return 0


if __name__ == "__main__":
    sys.exit(main())
