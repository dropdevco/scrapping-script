"""Re-fetch full descriptions (and, for Visit El Paso / La Nube, the real
outbound link) for events already stored from any source known to have
cropped this information at scrape time.

Every one of these sources renders the full text somewhere on its own page,
but our earlier extraction only ever read a short summary field sitting next
to it:

  - Visit El Paso: schema.org JSON-LD `description` is an SEO meta-description
    auto-cut to ~200 chars (ends in a literal "…"). Its JSON-LD `url` is also
    just the aggregator's own page, not the real venue/business — the actual
    destination is a "View Website" link elsewhere in the same content block.
  - La Nube: detail pages carry no JSON-LD at all, so only the short
    listing-card teaser was ever stored. Same self-referential `url` problem
    as Visit El Paso — it's a calendar aggregator, not the ticket seller.
  - Eventbrite: schema.org/OG/Twitter `description` is a short tagline; the
    real "About this event" copy lives in a structuredContent.modules[] block
    inside the page's __NEXT_DATA__ state (verified: 137 chars vs 2,408).
  - Meetup: schema.org/OG/Twitter `description` is inconsistently truncated —
    sometimes complete, sometimes cut off mid-word. __NEXT_DATA__'s
    props.pageProps.event.description is always the real, complete text
    (865 chars vs a 155-char truncation seen on the same event).
  - events.elpasotexas.gov (city_of_el_paso_events): the listing page's own
    flattened anchor text never carried a description AT ALL — only each
    event's own detail page (never previously fetched for this directory)
    has one, in a specific sibling-<p> inside .eventsDetail.

See events_web.py / events_directories.py for the extraction logic itself —
this is only the one-off pass to fix rows already live before those fixes
existed. Cross-run merge (storage.py:_apply_merge) only ever backfills a
MISSING description, never overwrites a present-but-truncated one, so
nothing fixes these rows on its own; they need this script.

Targeting: only rows whose `url` (unchanged by merge after insert — see
storage.py:_apply_merge, `url` is never in its patch) points at one of these
five domains, so a row that merely acquired one of them as a secondary
ticket link from some other primary source is correctly left alone.

    python -m scraper.backfill_full_descriptions --dry-run
    python -m scraper.backfill_full_descriptions
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Optional

from .core.config import settings
from .core.http import HttpClient
from .sources.events_directories import _city_event_full_description
from .sources.events_web import (
    _BROWSER_UA,
    _LANUBE_DESC_SELECTOR,
    _VISITELPASO_DESC_SELECTOR,
    _full_description_and_link,
    _individual_page_full_description,
)

log = logging.getLogger("scraper.backfill_full_descriptions")

Handler = Callable[[str, HttpClient], Awaitable[tuple[Optional[str], Optional[str]]]]


async def _fetch(url: str, http: HttpClient) -> Optional[str]:
    try:
        if not await http.can_fetch(url):
            return None
        return await http.get_text(url, headers={"User-Agent": _BROWSER_UA})
    except Exception as exc:  # noqa: BLE001
        log.warning("  fetch failed for %s: %s", url, exc)
        return None


async def _widget_handler(url: str, http: HttpClient, selector: str) -> tuple[Optional[str], Optional[str]]:
    from bs4 import BeautifulSoup

    html = await _fetch(url, http)
    if not html:
        return None, None
    return _full_description_and_link(BeautifulSoup(html, "html.parser"), selector)


async def _visitelpaso_handler(url: str, http: HttpClient) -> tuple[Optional[str], Optional[str]]:
    return await _widget_handler(url, http, _VISITELPASO_DESC_SELECTOR)


async def _lanube_handler(url: str, http: HttpClient) -> tuple[Optional[str], Optional[str]]:
    return await _widget_handler(url, http, _LANUBE_DESC_SELECTOR)


async def _generic_page_handler(url: str, http: HttpClient) -> tuple[Optional[str], Optional[str]]:
    """Eventbrite / Meetup — description only, `url` is already correct (the
    ticket seller's own page)."""
    html = await _fetch(url, http)
    if not html:
        return None, None
    return _individual_page_full_description(url, html), None


async def _city_handler(url: str, http: HttpClient) -> tuple[Optional[str], Optional[str]]:
    from bs4 import BeautifulSoup

    html = await _fetch(url, http)
    if not html:
        return None, None
    return _city_event_full_description(BeautifulSoup(html, "html.parser")), None


# Order matters only in that each domain must appear once; first substring
# match wins.
_HANDLERS: list[tuple[str, Handler]] = [
    ("visitelpaso.com", _visitelpaso_handler),
    ("la-nube.org", _lanube_handler),
    ("eventbrite.com", _generic_page_handler),
    ("eventbrite.ca", _generic_page_handler),
    ("meetup.com", _generic_page_handler),
    ("events.elpasotexas.gov", _city_handler),
]


def _handler_for(url: str) -> Optional[Handler]:
    for domain, handler in _HANDLERS:
        if domain in url:
            return handler
    return None


async def _run(dry_run: bool) -> int:
    if not (settings.supabase_url and settings.supabase_key):
        log.error("SUPABASE_URL / SUPABASE_KEY are not set; nothing to do.")
        return 1

    from supabase import create_client

    client = create_client(settings.supabase_url, settings.supabase_key)

    # Same scope as what the site actually serves (events.ts: fetchEvents /
    # fetchCrawlerEvents) — approved and still upcoming. A past event's detail
    # page is frequently gone from the source site entirely (redirects to
    # its homepage), so including expired rows only wastes requests without
    # fixing anything a visitor would ever see.
    now_iso = datetime.now(timezone.utc).isoformat()
    rows = (
        client.table("events")
        .select("id,title,url,description")
        .in_("source", ["events_web", "events_directories"])
        .eq("status", "approved")
        .gte("start_time", now_iso)
        .execute()
        .data
        or []
    )
    targets = [(r, _handler_for(r.get("url") or "")) for r in rows]
    targets = [(r, h) for r, h in targets if h is not None]
    log.info(
        "%d upcoming approved events_web/events_directories event(s) total, %d from a known-cropped source",
        len(rows),
        len(targets),
    )

    http = HttpClient()
    changed = 0
    unchanged = 0
    failed = 0

    async def handle(row: dict[str, Any], handler: Handler) -> None:
        nonlocal changed, unchanged, failed
        url = row.get("url") or ""
        full, real_url = await handler(url, http)
        current = row.get("description") or ""
        url_changed = bool(real_url and real_url != url)
        desc_changed = bool(full and full != current)
        if not desc_changed and not url_changed:
            unchanged += 1
            return

        changed += 1
        patch: dict[str, Any] = {}
        if desc_changed:
            patch["description"] = full
            log.info("%-60s desc %d -> %d chars", (row["title"] or "")[:60], len(current), len(full))
        if url_changed:
            patch["url"] = real_url
            log.info("%-60s url  %s -> %s", (row["title"] or "")[:60], url, real_url)
        if not dry_run:
            try:
                client.table("events").update(patch).eq("id", row["id"]).execute()
            except Exception as exc:  # noqa: BLE001
                failed += 1
                log.error("  update failed for %s: %s", row["id"], exc)

    # Concurrency is bounded by HttpClient's own shared semaphore, same as a
    # live scrape — safe to fan every row out at once rather than serialize.
    await asyncio.gather(*(handle(row, handler) for row, handler in targets))

    log.info(
        "\n%s: %d/%d event(s) %s (%d already correct or unfetchable, %d update failure(s))",
        "dry run" if dry_run else "done",
        changed,
        len(targets),
        "would update" if dry_run else "updated",
        unchanged,
        failed,
    )
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Re-fetch full descriptions/real links for already-stored events from known-cropped sources."
    )
    ap.add_argument("--dry-run", action="store_true", help="report without writing")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    return asyncio.run(_run(args.dry_run))


if __name__ == "__main__":
    sys.exit(main())
