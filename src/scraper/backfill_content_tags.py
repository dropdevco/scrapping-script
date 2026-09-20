"""Assign Instagram content pillars to events already stored.

The scrape assigns pillars from now on (orchestrator._assign_pillars), but
every row written before that has an empty content_tags. This fills them in
with the same confident keyword pass, so the carousel can rank and diversify by
pillar immediately instead of after a full re-scrape cycle.

Only the deterministic pass runs here. Events the keywords cannot place keep an
empty list and are left for the council's curator, which runs at build time
against the handful of events actually in contention -- classifying all 2,000
stored rows through a model would spend money on events that will never be
posted.

Never touches `categories`, and never overwrites a pillar a human or the
council already set.

    python -m scraper.backfill_content_tags --dry-run   # report only
    python -m scraper.backfill_content_tags             # apply
    python -m scraper.backfill_content_tags --all       # past events too
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections import Counter
from datetime import datetime, timezone

from .core.config import settings
from .core.content_tags import pillars_for

log = logging.getLogger("scraper.backfill_content_tags")

_PAGE = 1000


def main() -> int:
    ap = argparse.ArgumentParser(description="Assign content pillars to stored events.")
    ap.add_argument("--dry-run", action="store_true", help="report without writing")
    ap.add_argument("--all", action="store_true", help="include events already in the past")
    ap.add_argument("--limit", type=int, default=0, help="stop after N updates (0 = no cap)")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    if not (settings.supabase_url and settings.supabase_key):
        log.error("SUPABASE_URL / SUPABASE_KEY are not set; nothing to do.")
        return 1

    from supabase import create_client

    client = create_client(settings.supabase_url, settings.supabase_key)

    # Paged: PostgREST caps a select at 1000 rows and says nothing about it,
    # so an unpaged read would silently backfill only the first page.
    rows: list[dict] = []
    offset = 0
    while True:
        q = client.table("events").select(
            "id,title,venue,categories,content_tags,content_tags_source,start_time"
        )
        if not args.all:
            q = q.gte("start_time", datetime.now(timezone.utc).isoformat())
        page = q.order("start_time").range(offset, offset + _PAGE - 1).execute().data or []
        rows.extend(page)
        if len(page) < _PAGE:
            break
        offset += _PAGE

    log.info("%d event(s) to consider", len(rows))

    counts: Counter[str] = Counter()
    updated = skipped_owned = unplaced = 0

    for row in rows:
        # A council or human judgement outranks a keyword guess, always.
        if (row.get("content_tags_source") or "rule") != "rule":
            skipped_owned += 1
            continue

        tags = pillars_for(row.get("title"), row.get("categories") or [], row.get("venue"))
        existing = row.get("content_tags") or []
        if tags == existing:
            continue  # already correct — re-runs are a no-op
        if not tags and not existing:
            unplaced += 1
            continue

        # A rule-derived tag the rules no longer support has to be CLEARED, not
        # left behind. The vocabulary changes as false positives turn up (a
        # bare "play" once made "Bluey's Big Play" Arts & Culture), and a stale
        # tag would otherwise be permanent: it is not empty, so the council
        # never reconsiders it, and it is not wrong enough to notice.
        for t in tags:
            counts[t] += 1
        updated += 1
        log.info(
            "  %-24s %s%s",
            ", ".join(tags) or "(cleared)",
            (row.get("title") or "")[:60],
            f"   [was {', '.join(existing)}]" if existing else "",
        )

        if not args.dry_run:
            try:
                client.table("events").update(
                    {"content_tags": tags, "content_tags_source": "rule"}
                ).eq("id", row["id"]).execute()
            except Exception as exc:  # noqa: BLE001
                log.error("  failed to update %s: %s", row["id"], exc)

        if args.limit and updated >= args.limit:
            log.info("  (stopping at --limit %d)", args.limit)
            break

    log.info(
        "\n%s: %d updated, %d left for the council, %d already owned by council/manual",
        "dry run" if args.dry_run else "done", updated, unplaced, skipped_owned,
    )
    for pillar, n in counts.most_common():
        log.info("   %-24s %d", pillar, n)
    return 0


if __name__ == "__main__":
    sys.exit(main())
