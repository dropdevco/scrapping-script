"""Re-run the (now multi-category) title classifier over already-stored events.

`categorize.guess_category()` used to return a single best-guess category;
`guess_categories()` now returns every matching bucket. Events scraped before
that change only ever got one category even when their title clearly matches
more ("Beer & Live Music Festival" -> just "Food & Drink" instead of
Food & Drink + Music + Festivals). This is a one-off (re-runnable, idempotent)
pass to upgrade what's already stored.

Only touches rows that still look like the OLD single-guess output — i.e.
their current categories are exactly one bucket. Ticketmaster-classified
events (which already carry real multi-category data from the provider, not
a title guess) and anything already multi-category are left alone.

    python -m scraper.backfill_categories --dry-run
    python -m scraper.backfill_categories
"""

from __future__ import annotations

import argparse
import logging
import sys

from .core.categorize import DEFAULT_CATEGORY, guess_categories
from .core.config import settings

log = logging.getLogger("scraper.backfill_categories")


def main() -> int:
    ap = argparse.ArgumentParser(description="Upgrade single-guess categories to multi-category.")
    ap.add_argument("--dry-run", action="store_true", help="report without writing")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    if not (settings.supabase_url and settings.supabase_key):
        log.error("SUPABASE_URL / SUPABASE_KEY are not set; nothing to do.")
        return 1

    from supabase import create_client

    client = create_client(settings.supabase_url, settings.supabase_key)

    rows = (
        client.table("events")
        .select("id,title,source,categories")
        .eq("status", "approved")
        .neq("source", "events_ticketmaster")  # has its own real multi-category data
        .execute()
        .data
        or []
    )
    log.info("%d non-Ticketmaster approved event(s) to check", len(rows))

    changed = 0
    for r in rows:
        current = r.get("categories") or []
        # Only rows that still look like the single-guess output: exactly one
        # category, and it's either the generic fallback or something the
        # classifier would produce today. A row with 2+ categories already
        # (multi-category data from another path) is left untouched.
        if len(current) != 1:
            continue

        fresh = guess_categories(r.get("title") or "")
        if fresh == current:
            continue
        # Don't downgrade: only apply when the new guess is a superset (adds
        # categories) or a more specific single match than the generic default.
        if current[0] != DEFAULT_CATEGORY and current[0] not in fresh:
            continue

        changed += 1
        log.info("%-60s %s -> %s", (r["title"] or "")[:60], current, fresh)
        if not args.dry_run:
            try:
                client.table("events").update({"categories": fresh}).eq("id", r["id"]).execute()
            except Exception as exc:  # noqa: BLE001
                log.error("  update failed for %s: %s", r["id"], exc)

    log.info(
        "\n%s: %d/%d event(s) %s",
        "dry run" if args.dry_run else "done",
        changed,
        len(rows),
        "would change" if args.dry_run else "updated",
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
