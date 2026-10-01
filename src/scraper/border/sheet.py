"""Publish the crossing times to the knowledge-base Google Sheet.

    python -m scraper.border sheet                 # rewrite the crossing-times tab
    python -m scraper.border sheet --dry-run       # print a summary, write nothing
    python -m scraper.border sheet --out waits.csv # also save a local copy

The pipeline agreed with Carlos on 2026-09-29 is GitHub Actions -> Supabase -> Google
Sheet -> GoHighLevel, whose LLM answers Instagram questions from that sheet. The
engine's `python -m scraper.kb export` publishes events; nothing published crossing
times, so the bot could not know them however full border_current_waits got. This
reads that table and writes one tab of the same sheet, every run of border-poll.

It follows the kb export's contract exactly (docs/components/knowledge-base-export.md),
because the same importer reads it:

- one row is one retrievable chunk, so `content` carries a bilingual sentence that
  stands on its own; the other columns are metadata;
- no cell is ever empty — GoHighLevel rejects the whole row — so every cell falls
  back to kb.rows.NOT_LISTED;
- no relative times: every row states when it was checked, as a date;
- the header order is append-only (the importer maps by position);
- an empty result is refused before any write: publishing nothing would tell the bot
  there are no bridges;
- the write is kb.sheets.write_sheet, which grows, writes, then shrinks, and never
  clears first.

Off until BORDER_KB_TAB names the tab (a repo variable in the workflow), the way the
engine ships features inert: with it unset this does nothing and exits 0.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import logging
import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .cbp import LANES, LOCAL_TZ, PORTS
from .storage import BorderStore

log = logging.getLogger("scraper.border.sheet")

# Column order IS the header row. Append-only: add new columns at the end.
HEADERS = [
    "content",
    "bridge_es",
    "bridge_en",
    "also_known_as",
    "lane_es",
    "lane_en",
    "wait_es",
    "wait_en",
    "state",
    "minutes",
    "source",
    "checked_at",
    "data_current_as_of",
    "port_number",
    "lane_id",
]
NOT_LISTED = "Not listed"      # the kb export's own fallback, so the bot reads one idiom
# A row older than this means the poller stopped. It is still published — every row
# states when it was checked — but said loudly, because a quiet sheet looks current.
STALE_AFTER = timedelta(minutes=45)

_PORT_ORDER = {n: m["order"] for n, m in PORTS.items()}
_LANE_ORDER = {lane: i for i, lane in enumerate(l for group in LANES.values() for l in group.values())}


def _cell(value) -> str:
    text = "" if value is None else str(value).strip()
    return text or NOT_LISTED


def build_values(rows: list[dict], now: datetime) -> list[list[str]]:
    """Header plus one row per bridge x lane, bridges in their usual order."""
    as_of = now.astimezone(LOCAL_TZ)
    current_as_of = f"{as_of:%Y-%m-%d %H:%M} {as_of.tzname()}"
    ordered = sorted(rows, key=lambda r: (_PORT_ORDER.get(r["port_number"], 999),
                                          _LANE_ORDER.get(r["lane"], 999)))
    values = [list(HEADERS)]
    for r in ordered:
        content = f"{_cell(r.get('summary_en'))}\nES: {_cell(r.get('summary_es'))}"
        record = {**r, "content": content, "data_current_as_of": current_as_of, "lane_id": r.get("lane")}
        values.append([_cell(record.get(h)) for h in HEADERS])
    return values


def _write_csv(values: list[list[str]], path: Path) -> None:
    with path.open("w", encoding="utf-8", newline="") as fh:
        csv.writer(fh).writerows(values)


def _stale(rows: list[dict], now: datetime) -> bool:
    newest = max(datetime.fromisoformat(r["checked_at"]) for r in rows)
    return now - newest > STALE_AFTER


async def run(dry_run: bool = False, out: str | None = None, tab: str | None = None,
              storage: BorderStore | None = None) -> int:
    tab = tab if tab is not None else (os.getenv("BORDER_KB_TAB") or "").strip()
    if not tab and not dry_run:
        print("BORDER_KB_TAB is not set — the crossing-times sheet export is off.")
        return 0
    storage = storage or BorderStore()
    if not storage.enabled:
        print("Supabase is not configured — set SUPABASE_URL and SUPABASE_KEY.", file=sys.stderr)
        return 1

    rows = await storage.current_waits()
    now = datetime.now(UTC)
    if not rows:
        print("border_current_waits is empty — refusing to publish an empty sheet.", file=sys.stderr)
        return 1
    if _stale(rows, now):
        log.error("newest crossing time is older than %s: is border-poll still running?", STALE_AFTER)

    values = build_values(rows, now)
    if out:
        _write_csv(values, Path(out))
    if dry_run:
        print(f"[dry-run] {len(values) - 1} rows for tab '{tab or '(unset)'}'")
        for row in values[1:4]:
            print(f"  - {row[0].splitlines()[1]}")
        return 0

    from ..kb.sheets import SheetsUnavailable, write_sheet

    try:
        written = write_sheet(values, tab=tab)
    except SheetsUnavailable as exc:
        print(f"Sheets export unavailable: {exc}", file=sys.stderr)
        return 1
    print(f"Published {written} crossing times to sheet tab '{tab}'.")
    return 0


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    ap = argparse.ArgumentParser(prog="python -m scraper.border sheet", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true", help="print a summary, write nothing")
    ap.add_argument("--out", help="also write the rows to this CSV path")
    args = ap.parse_args(argv)
    return asyncio.run(run(args.dry_run, args.out))


if __name__ == "__main__":
    raise SystemExit(main())
