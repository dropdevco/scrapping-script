"""Backfill venues.lat / venues.lng for venues that were stored without coordinates.

The map only renders events whose venue carries coordinates, so venues scraped
without them are invisible on /map even though they show up in the list view.
This one-off (re-runnable) job geocodes them.

Only rows where lat or lng IS NULL are touched — existing coordinates are never
overwritten. Rows that don't resolve inside the border region are left NULL, so
re-running later picks them up again after their address improves.

    python -m scraper.backfill_geocode --dry-run   # report only, no writes
    python -m scraper.backfill_geocode             # apply
    python -m scraper.backfill_geocode --limit 20  # work through a slice

Nominatim allows ~1 request/second, so expect roughly one second per venue.
"""

from __future__ import annotations

import argparse
import logging
import sys

import httpx

from .core.config import settings
from .core.geocode import clean_address, geocode, is_city_only, is_virtual

log = logging.getLogger("scraper.backfill_geocode")


def _repair(client, *, dry_run: bool) -> None:
    """Null out coordinates that today's rules would never produce.

    Two classes, both of which put a misleading pin on the map:

    * Online/virtual "venues" ("Virtual via Zoom, El Paso, TX") — they have a
      nominal city but no physical location, so they must not appear at all.
    * Venues whose address is city-only ("El Paso, TX"). Geocoding that string
      succeeds and returns the *city centroid*, which silently collapses many
      unrelated venues onto one wrong downtown pin. Clearing them lets the fill
      pass re-derive a real position from the venue name instead.

    Coordinates that a source supplied directly (Ticketmaster et al.) carry real
    street addresses, so they never match either rule.
    """
    rows = (
        client.table("venues").select("id,name,address,lat,lng").not_.is_("lat", "null").execute()
    ).data or []

    doomed: list[tuple[str, str, str]] = []
    for r in rows:
        addr, name = r.get("address"), r.get("name")
        if is_virtual(addr, name):
            doomed.append((r["id"], name or "?", "virtual/online"))
        elif addr and is_city_only(clean_address(addr)):
            doomed.append((r["id"], name or "?", "city-only address"))

    if not doomed:
        log.info("repair: nothing to clear")
        return

    log.info("repair: clearing coordinates on %d venue(s)", len(doomed))
    for vid, name, why in doomed:
        log.info("  - %-58s (%s)", name[:58], why)
        if not dry_run:
            try:
                client.table("venues").update({"lat": None, "lng": None}).eq("id", vid).execute()
            except Exception as exc:  # noqa: BLE001
                log.error("    clear failed for %s: %s", vid, exc)
    log.info("")


def main() -> int:
    ap = argparse.ArgumentParser(description="Geocode venues missing coordinates.")
    ap.add_argument("--dry-run", action="store_true", help="report without writing")
    ap.add_argument("--limit", type=int, default=0, help="max venues to process (0 = all)")
    ap.add_argument(
        "--repair",
        action="store_true",
        help="first clear coordinates that today's rules say are untrustworthy "
        "(online/virtual venues, and city-only addresses that can only have "
        "produced a city centroid), so the fill pass can re-derive them",
    )
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    if not (settings.supabase_url and settings.supabase_key):
        log.error("SUPABASE_URL / SUPABASE_KEY are not set; nothing to do.")
        return 1

    from supabase import create_client

    client = create_client(settings.supabase_url, settings.supabase_key)

    if args.repair:
        _repair(client, dry_run=args.dry_run)

    q = (
        client.table("venues")
        .select("id,name,address,lat,lng")
        .or_("lat.is.null,lng.is.null")
        .order("id")
    )
    if args.limit:
        q = q.limit(args.limit)
    rows = q.execute().data or []

    todo = [r for r in rows if (r.get("address") or r.get("name"))]
    log.info("%d venue(s) missing coordinates, %d with something to geocode", len(rows), len(todo))
    if not todo:
        return 0

    hits = misses = 0
    with httpx.Client(
        timeout=settings.http_timeout_seconds,
        headers={"User-Agent": settings.user_agent, "Accept-Language": "en"},
    ) as http:
        for i, row in enumerate(todo, 1):
            found = geocode(row.get("address"), row.get("name"), client=http)
            label = (row.get("name") or row.get("address") or "?")[:60]
            if not found:
                misses += 1
                log.info("[%d/%d] MISS %s", i, len(todo), label)
                continue

            lat, lng = found
            hits += 1
            log.info("[%d/%d] OK   %s -> %.5f, %.5f", i, len(todo), label, lat, lng)
            if not args.dry_run:
                try:
                    client.table("venues").update({"lat": lat, "lng": lng}).eq(
                        "id", row["id"]
                    ).execute()
                except Exception as exc:  # noqa: BLE001
                    log.error("       update failed for %s: %s", row["id"], exc)

    log.info(
        "\n%s: %d geocoded, %d unresolved",
        "dry run" if args.dry_run else "done",
        hits,
        misses,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
