"""Merge venue rows that are the same building under different ids.

`venues.address_hash` used to be sha1 over the raw address and name, so every
difference in punctuation, spacing, accent or country suffix forked a new row.
Measured 2026-09-17: 439 rows held only 412 distinct buildings. The El Paso
Convention Center had four ids; the Abraham Chavez Theatre and the El Paso
County Coliseum two each.

That is not a cosmetic problem. The cross-run event merge in storage.py matches
on EXACT venue_id, so a forked venue means the merge can never fire and the
same event gets inserted twice -- which is what put three duplicate pairs on the
2026-09-18 carousel. core/address.py now normalizes before hashing; this sweep
converges the rows that were written under the old rule.

Re-runnable: rows already stored under the normalized hash are left alone.

    python -m scraper.backfill_merge_venues --dry-run   # report only
    python -m scraper.backfill_merge_venues             # apply
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections import defaultdict

from .core.address import venue_identity
from .core.config import settings

log = logging.getLogger("scraper.backfill_merge_venues")


def _keeper_of(group: list[dict], event_counts: dict[str, int]) -> dict:
    """The row the others fold into: most events first, then one that already
    has map coordinates, then the oldest. Choosing the busiest row minimises
    how many events have to be repointed, and keeping coordinates avoids
    dropping a venue off the map."""
    return max(
        group,
        key=lambda v: (
            event_counts.get(v["id"], 0),
            1 if (v.get("lat") is not None and v.get("lng") is not None) else 0,
            v.get("created_at") or "",
        ),
    )


def main() -> int:
    ap = argparse.ArgumentParser(description="Merge duplicate venue rows.")
    ap.add_argument("--dry-run", action="store_true", help="report without writing")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    if not (settings.supabase_url and settings.supabase_key):
        log.error("SUPABASE_URL / SUPABASE_KEY are not set; nothing to do.")
        return 1

    from supabase import create_client

    client = create_client(settings.supabase_url, settings.supabase_key)

    venues = (
        client.table("venues").select("id,name,address,lat,lng,address_hash,created_at").execute().data
        or []
    )
    log.info("%d venue row(s) to check", len(venues))

    # One pass over events so keeper choice can prefer the busiest row. Cheaper
    # than a count query per venue. PAGED: PostgREST caps a select at 1000 rows,
    # and silently -- an unpaged count said the ballpark holding every Chihuahuas
    # game had zero events, which picked the wrong keeper.
    event_counts: dict[str, int] = defaultdict(int)
    page_size, offset = 1000, 0
    while True:
        page = (
            client.table("events")
            .select("venue_id")
            .range(offset, offset + page_size - 1)
            .execute()
            .data
            or []
        )
        for e in page:
            if e.get("venue_id"):
                event_counts[e["venue_id"]] += 1
        if len(page) < page_size:
            break
        offset += page_size
    log.info("counted %d event(s) across %d venue(s)", sum(event_counts.values()), len(event_counts))

    groups: dict[str, list[dict]] = defaultdict(list)
    for v in venues:
        groups[venue_identity(v.get("address"), v.get("name"))].append(v)

    merged_groups = 0
    merged_rows = 0
    rehashed = 0
    repointed = 0

    for new_hash, group in groups.items():
        if len(group) > 1:
            keeper = _keeper_of(group, event_counts)
            losers = [v for v in group if v["id"] != keeper["id"]]
            moving = sum(event_counts.get(v["id"], 0) for v in losers)
            merged_groups += 1
            merged_rows += len(losers)
            repointed += moving

            log.info(
                "MERGE %d row(s) -> keep %r (%s, %d event(s)); moving %d event(s)",
                len(losers), keeper["name"], keeper["id"], event_counts.get(keeper["id"], 0), moving,
            )
            for v in losers:
                log.info("        %r  %r  (%s)", v["name"], v.get("address"), v["id"])

            patch: dict = {"address_hash": new_hash}
            if keeper.get("lat") is None or keeper.get("lng") is None:
                donor = next(
                    (v for v in losers if v.get("lat") is not None and v.get("lng") is not None), None
                )
                if donor:
                    patch["lat"], patch["lng"] = donor["lat"], donor["lng"]
                    log.info("        (taking coordinates from %s)", donor["id"])

            if not args.dry_run:
                loser_ids = [v["id"] for v in losers]
                try:
                    # ORDER MATTERS. events.venue_id references venues(id) with
                    # no ON DELETE, so the repoint has to land before the delete
                    # or the delete fails. And the keeper's address_hash can only
                    # be rewritten once the losers are gone, because that column
                    # is unique and the whole group resolves to the same value.
                    client.table("events").update({"venue_id": keeper["id"]}).in_(
                        "venue_id", loser_ids
                    ).execute()
                    client.table("venues").delete().in_("id", loser_ids).execute()
                    client.table("venues").update(patch).eq("id", keeper["id"]).execute()
                except Exception as exc:  # noqa: BLE001
                    log.error("  failed to merge %r: %s", keeper["name"], exc)

        elif group[0].get("address_hash") != new_hash:
            # Not a duplicate, but stored under the old hashing rule. Rewriting
            # it now is what stops the next scrape inserting a second row for it.
            rehashed += 1
            if not args.dry_run:
                try:
                    client.table("venues").update({"address_hash": new_hash}).eq(
                        "id", group[0]["id"]
                    ).execute()
                except Exception as exc:  # noqa: BLE001
                    log.error("  failed to rehash %r: %s", group[0]["name"], exc)

    log.info(
        "\n%s: %d group(s) merged, %d row(s) removed, %d event(s) repointed, "
        "%d untouched row(s) rehashed -> %d venues remain",
        "dry run" if args.dry_run else "done",
        merged_groups, merged_rows, repointed, rehashed, len(groups),
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
