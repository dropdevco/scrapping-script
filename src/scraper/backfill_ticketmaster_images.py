"""Repoint Ticketmaster events at the largest artwork the provider offered.

`events_ticketmaster` used to take `images[0]`, and Ticketmaster lists the same
artwork at a dozen sizes in no useful order. For Thee Sacred Souls that first
entry was a 305x203 ARTIST_PAGE thumbnail while a 2048x1365 SOURCE sat further
down the same list -- so the thumbnail failed imaging.py's quality gate and the
event lost its carousel slot for want of a photo that was there all along.

The source is fixed for future scrapes; this repairs rows already stored, using
the `images` array preserved in each row's `raw` payload. Re-runnable, and a no
-op on any row already pointing at its best image.

    python -m scraper.backfill_ticketmaster_images --dry-run
    python -m scraper.backfill_ticketmaster_images
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timedelta, timezone
from typing import Any

from .core.config import settings
from .core.media import clean_image_url
from .sources.events_ticketmaster import _best_image

log = logging.getLogger("scraper.backfill_ticketmaster_images")

_PAST_WINDOW_DAYS = 365


def _dims(images: Any, url: str | None) -> str:
    if not isinstance(images, list) or not url:
        return "?"
    for i in images:
        if isinstance(i, dict) and i.get("url") == url:
            return f"{i.get('width')}x{i.get('height')}"
    return "?"


def main() -> int:
    ap = argparse.ArgumentParser(description="Point Ticketmaster rows at their largest image.")
    ap.add_argument("--dry-run", action="store_true", help="report without writing")
    ap.add_argument("--all", action="store_true", help="also repair past events")
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
    page, offset = 1000, 0
    while True:
        batch = (
            client.table("events")
            .select("id,title,image_url,raw")
            .eq("source", "events_ticketmaster")
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

    log.info("%d Ticketmaster event(s) in scope", len(rows))

    upgraded = 0
    already = 0
    skipped = 0
    for row in rows:
        raw = row.get("raw")
        images = raw.get("images") if isinstance(raw, dict) else None
        best = clean_image_url(_best_image(images))
        if not best:
            skipped += 1
            continue
        if best == row.get("image_url"):
            already += 1
            continue

        log.info(
            "FIX %s  %s -> %s  %r",
            row["id"],
            _dims(images, row.get("image_url")),
            _dims(images, best),
            (row.get("title") or "")[:48],
        )
        upgraded += 1
        if not args.dry_run:
            try:
                client.table("events").update({"image_url": best}).eq("id", row["id"]).execute()
            except Exception as exc:  # noqa: BLE001 - one bad row must not end the sweep
                log.error("  failed to update %s: %s", row["id"], exc)

    log.info(
        "\n%s: %d upgraded, %d already best, %d with no usable image in raw",
        "dry run" if args.dry_run else "done",
        upgraded,
        already,
        skipped,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
