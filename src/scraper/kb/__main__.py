"""CLI for the GHL knowledge-base export.

    python -m scraper.kb export                    # write the sheet
    python -m scraper.kb export --dry-run          # print a summary, write nothing
    python -m scraper.kb export --out events.csv   # also save a local copy
    python -m scraper.kb export --days 90
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import logging
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from ..core.config import settings
from ..core.storage import Storage
from . import rows as rows_mod
from .sheets import SheetsUnavailable, write_sheet

log = logging.getLogger("scraper.kb")


async def _collect(days: int, location: str) -> tuple[list[list[str]], str]:
    tz = ZoneInfo(settings.event_timezone)
    now = datetime.now(tz)
    # From *now*, not from local midnight: an event that started three hours ago
    # is over, and leaving it in is the exact staleness the crawler was guilty of.
    horizon = now + timedelta(days=days)

    storage = Storage()
    if not storage.enabled:
        raise SystemExit("Supabase is not configured — set SUPABASE_URL and SUPABASE_KEY.")

    stored = await storage.query_upcoming_events(
        location, now.isoformat(), horizon.isoformat()
    )
    generated_on = now.strftime("%Y-%m-%d")
    values = rows_mod.build_sheet(
        stored,
        site_base_url=settings.site_base_url,
        generated_on=generated_on,
    )
    log.info("%d stored events -> %d exportable rows", len(stored), len(values) - 1)
    return values, generated_on


def _write_csv(values: list[list[str]], path: Path) -> None:
    with path.open("w", encoding="utf-8", newline="") as fh:
        csv.writer(fh).writerows(values)
    log.info("wrote %s", path)


async def _export(args: argparse.Namespace) -> int:
    values, generated_on = await _collect(args.days, args.location)
    written = len(values) - 1

    if written == 0:
        # Publishing an empty sheet would leave the bot with no events at all,
        # which reads to a customer as "there is nothing happening in El Paso".
        # An export that finds nothing is a scrape problem, not a signal to
        # wipe the knowledge base.
        print("No upcoming events found — refusing to publish an empty sheet.", file=sys.stderr)
        return 1

    if args.out:
        _write_csv(values, Path(args.out))

    if args.dry_run:
        print(f"[dry-run] {written} rows, current as of {generated_on}")
        for row in values[1:4]:
            print(f"  - {row[0].splitlines()[0]}")
        if written > 3:
            print(f"  … and {written - 3} more")
        return 0

    try:
        write_sheet(values)
    except SheetsUnavailable as exc:
        print(f"Sheets export unavailable: {exc}", file=sys.stderr)
        return 1
    print(f"Published {written} rows to sheet tab '{settings.kb_sheet_tab}'.")
    return 0


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(prog="python -m scraper.kb")
    sub = parser.add_subparsers(dest="cmd", required=True)

    export = sub.add_parser("export", help="rewrite the knowledge-base sheet")
    export.add_argument("--days", type=int, default=settings.kb_horizon_days)
    export.add_argument("--location", default=settings.kb_location)
    export.add_argument("--dry-run", action="store_true")
    export.add_argument("--out", help="also write the rows to this CSV path")

    args = parser.parse_args()
    return asyncio.run(_export(args))


if __name__ == "__main__":
    raise SystemExit(main())
