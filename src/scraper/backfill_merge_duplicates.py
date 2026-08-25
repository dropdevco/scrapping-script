"""Merge pre-existing duplicate events already in the DB.

`storage.py`'s cross-run merge only catches a duplicate arriving from now on —
it compares each newly-scraped event against what's already stored. It has no
opinion on two rows that were BOTH already stored, from before this feature
existed. This is a one-off (re-runnable) sweep for exactly that: same venue,
same calendar day, near-identical title -> merge into one row with every
source's ticket link, delete the loser.

Only touches approved, still-upcoming events (past events are dead weight,
not worth reconciling) with a resolved venue_id (a venue-less title-only
match would be unreliable — same caution as the live merge path).

    python -m scraper.backfill_merge_duplicates --dry-run   # report only
    python -m scraper.backfill_merge_duplicates             # apply
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections import defaultdict
from datetime import datetime, timezone

from .core.config import settings
from .core.dedupe import _TITLE_ONLY_THRESHOLD, _TOKEN_OVERLAP_THRESHOLD, _norm, _similar, _title_token_overlap
from .core.dedupe import merge_ticket_links
from .core.eventtime import local_day
from .core.models import TicketLink

log = logging.getLogger("scraper.backfill_merge_duplicates")


def _is_same_event(a_title: str, b_title: str) -> bool:
    if _similar(_norm(a_title), _norm(b_title)) >= _TITLE_ONLY_THRESHOLD:
        return True
    return _title_token_overlap(a_title, b_title) >= _TOKEN_OVERLAP_THRESHOLD


def main() -> int:
    ap = argparse.ArgumentParser(description="Merge duplicate events already stored.")
    ap.add_argument("--dry-run", action="store_true", help="report without writing")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    if not (settings.supabase_url and settings.supabase_key):
        log.error("SUPABASE_URL / SUPABASE_KEY are not set; nothing to do.")
        return 1

    from supabase import create_client

    client = create_client(settings.supabase_url, settings.supabase_key)

    now = datetime.now(timezone.utc).isoformat()
    rows = (
        client.table("events")
        .select("id,title,venue_id,start_time,description,image_url,end_time,categories,ticket_links")
        .eq("status", "approved")
        .not_.is_("venue_id", "null")
        .gte("start_time", now)
        .order("start_time")
        .execute()
        .data
        or []
    )
    log.info("%d upcoming approved event(s) with a venue to check", len(rows))

    by_venue_day: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for r in rows:
        if not r.get("start_time"):
            continue
        # LOCAL calendar day, not UTC. Stored times come back from Postgres in
        # UTC, where a 7pm show is 01:00 the next day — so grouping on the UTC
        # date filed an evening event and its own 5pm duplicate at the same
        # venue under two different days and never compared them. Same fix as
        # core/storage.py's cross-run merge.
        local = local_day(r["start_time"])
        if local is None:
            continue
        day = local.date().isoformat()
        by_venue_day[(r["venue_id"], day)].append(r)

    groups = 0
    merges = 0
    for (_venue_id, _day), group in by_venue_day.items():
        if len(group) < 2:
            continue
        # Union-find-lite: greedily cluster by title match within this venue+day bucket.
        clusters: list[list[dict]] = []
        for row in group:
            placed = False
            for cluster in clusters:
                if _is_same_event(row["title"] or "", cluster[0]["title"] or ""):
                    cluster.append(row)
                    placed = True
                    break
            if not placed:
                clusters.append([row])

        for cluster in clusters:
            if len(cluster) < 2:
                continue
            groups += 1
            # Keep the one with the richest set of fields already; fold the rest into it.
            def _richness(r: dict) -> int:
                return sum(1 for f in ("description", "image_url", "end_time") if r.get(f))

            cluster.sort(key=_richness, reverse=True)
            keeper, losers = cluster[0], cluster[1:]

            links = [TicketLink(**tl) for tl in (keeper.get("ticket_links") or [])]
            categories = list(keeper.get("categories") or [])
            patch: dict = {}
            for loser in losers:
                links = merge_ticket_links(links, [TicketLink(**tl) for tl in (loser.get("ticket_links") or [])])
                categories = list(dict.fromkeys([*categories, *(loser.get("categories") or [])]))
                for field in ("description", "image_url", "end_time"):
                    if not keeper.get(field) and loser.get(field):
                        keeper[field] = loser[field]
                        patch[field] = loser[field]

            patch["ticket_links"] = [tl.model_dump() for tl in links]
            if categories != (keeper.get("categories") or []):
                patch["categories"] = categories

            loser_titles = ", ".join(f"{l['title']!r} ({l['id']})" for l in losers)
            log.info(
                "MERGE %d source(s) -> keep %r (%s): %s",
                len(losers),
                keeper["title"],
                keeper["id"],
                loser_titles,
            )
            merges += len(losers)

            if not args.dry_run:
                try:
                    client.table("events").update(patch).eq("id", keeper["id"]).execute()
                    client.table("events").delete().in_("id", [l["id"] for l in losers]).execute()
                except Exception as exc:  # noqa: BLE001
                    log.error("  failed to merge group for %r: %s", keeper["title"], exc)

    log.info(
        "\n%s: %d duplicate group(s), %d row(s) merged away",
        "dry run" if args.dry_run else "done",
        groups,
        merges,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
