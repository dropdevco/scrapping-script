"""Events discovered on the open web (keyless).

Strategy: fetch city listing pages on platforms that were empirically verified to serve
crawlable ``schema.org/Event`` JSON-LD to a plain (non-JS) scraper AND allow it in
robots.txt — Eventbrite, Ticketmaster's public pages, and Meetup — then extract the
structured events. A ``site:``-scoped DuckDuckGo search adds extra detail pages. Sites that
bot-block or render events only via JavaScript (allevents.in, 10times, bandsintown, seatgeek,
dice.fm…) are intentionally skipped because a keyless fetch gets nothing from them.

For full, reliable coverage set ``TICKETMASTER_API_KEY`` — the Discovery API source is the
primary events provider; this one is the free fallback / supplement.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import date, datetime
from typing import Any, Optional
from urllib.parse import quote

from ..core.address import format_address
from ..core.categorize import guess_category
from ..core.http import HttpClient
from ..core.media import clean_image_url
from ..core.models import Event, Kind, SearchParams
from .base import Source

log = logging.getLogger("scraper.events_web")

# Several of these sites return 200 to a browser UA but block unknown agents.
_BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)
_MAX_PAGES = 14

# 2-letter US state codes, to detect a "City, ST" location string.
_STATE = re.compile(r"^[A-Za-z]{2}$")


def _dt(value: Any) -> Optional[datetime]:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.strip().lower()).strip("-")


def _parse_location(loc: str) -> tuple[Optional[str], Optional[str]]:
    """"El Paso, TX" -> ("El Paso", "TX"); "El Paso" -> ("El Paso", None)."""
    parts = [p.strip() for p in loc.split(",") if p.strip()]
    if not parts:
        return None, None
    city = parts[0]
    state = None
    for p in parts[1:]:
        token = p.split()[0]
        if _STATE.match(token):
            state = token.upper()
            break
    return city, state


def _direct_urls(city: Optional[str], state: Optional[str]) -> list[str]:
    """City listing pages proven to serve JSON-LD events to a keyless fetch."""
    urls: list[str] = []
    if not city:
        return urls
    city_slug = _slug(city)
    if state:
        urls.append(f"https://www.eventbrite.com/d/{state.lower()}--{city_slug}/all-events/")
        urls.append(f"https://www.meetup.com/find/?location=us--{state.lower()}--{quote(city)}&source=EVENTS")
    urls.append(f"https://www.ticketmaster.com/discover/concerts/{city_slug}")
    return urls


def _as_location(loc: Any) -> tuple[Optional[str], Optional[str]]:
    """Return (venue_name, location_string) from a schema.org location value."""
    if isinstance(loc, list):
        loc = loc[0] if loc else None
    if isinstance(loc, str):
        return None, loc
    if not isinstance(loc, dict):
        return None, None
    name = loc.get("name")
    addr = loc.get("address")
    if isinstance(addr, dict):
        country = addr.get("addressCountry")
        if isinstance(country, dict):
            country = country.get("name") or country.get("@id")
        location = format_address(
            street=addr.get("streetAddress"),
            city=addr.get("addressLocality"),
            region=addr.get("addressRegion"),
            postal=addr.get("postalCode"),
            country=country if isinstance(country, str) else None,
        )
    else:
        location = addr if isinstance(addr, str) else None
    return name, location


def _coord(value: Any) -> Optional[float]:
    """schema.org geo values arrive as strings or numbers; convert defensively."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_geo(loc: Any) -> tuple[Optional[float], Optional[float]]:
    """Return (lat, lng) from a schema.org location value's ``geo``, when present."""
    if isinstance(loc, list):
        loc = loc[0] if loc else None
    if not isinstance(loc, dict):
        return None, None
    geo = loc.get("geo")
    if isinstance(geo, list):
        geo = geo[0] if geo else None
    if not isinstance(geo, dict):
        return None, None
    return _coord(geo.get("latitude")), _coord(geo.get("longitude"))


def _walk_for_events(node: Any):
    """Yield schema.org Event dicts, descending through @graph / ItemList / ListItem."""
    if isinstance(node, list):
        for item in node:
            yield from _walk_for_events(item)
    elif isinstance(node, dict):
        if "@graph" in node:
            yield from _walk_for_events(node["@graph"])
        if "itemListElement" in node:
            yield from _walk_for_events(node["itemListElement"])
        if isinstance(node.get("item"), dict):
            yield from _walk_for_events(node["item"])
        t = node.get("@type", "")
        types = t if isinstance(t, list) else [t]
        if any(isinstance(x, str) and "event" in x.lower() for x in types):
            yield node


def _iter_jsonld_events(html: str):
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    for tag in soup.find_all("script", type="application/ld+json"):
        text = tag.string or tag.get_text() or ""
        if not text.strip():
            continue
        try:
            data = json.loads(text)
        except (json.JSONDecodeError, ValueError):
            continue
        yield from _walk_for_events(data)


def _in_window(event: Event, start: Optional[date], end: Optional[date]) -> bool:
    """Keep events inside [start, end]. Events without a date are kept (can't judge)."""
    if event.start_time is None:
        return True
    d = event.start_time.date()
    if start and d < start:
        return False
    if end and d > end:
        return False
    return True


def _ddg_site_search(query: str, max_results: int) -> list[str]:
    try:
        from ddgs import DDGS

        with DDGS() as ddg:
            return [r["href"] for r in ddg.text(query, max_results=max_results) if r.get("href")]
    except Exception as exc:  # noqa: BLE001
        log.warning("ddg search failed: %s", exc)
        return []


class EventsWebSource(Source):
    name = "events_web"
    kind = Kind.EVENTS

    def is_configured(self) -> bool:
        return True  # keyless

    async def fetch(self, params: SearchParams, http: HttpClient) -> list[Event]:
        city, state = _parse_location(params.location or "")
        urls = _direct_urls(city, state)

        # Supplement with a site:-scoped search for extra detail pages on the good domains.
        topic = params.query or "events"
        loc = params.location or ""
        search_q = f"{topic} {loc} (site:eventbrite.com OR site:meetup.com)".strip()
        urls += await asyncio.to_thread(_ddg_site_search, search_q, 6)

        urls = list(dict.fromkeys(u for u in urls if u))[:_MAX_PAGES]
        pages = await asyncio.gather(*(self._page_events(u, http) for u in urls))
        events = [e for page in pages for e in page]
        events = [e for e in events if _in_window(e, params.start_date, params.end_date)]
        return events

    async def _page_events(self, url: str, http: HttpClient) -> list[Event]:
        try:
            if not await http.can_fetch(url):
                return []
            html = await http.get_text(url, headers={"User-Agent": _BROWSER_UA})
        except Exception as exc:  # noqa: BLE001
            log.debug("fetch %s failed: %s", url, exc)
            return []

        out: list[Event] = []
        for node in _iter_jsonld_events(html):
            if not isinstance(node, dict):
                continue
            venue, location = _as_location(node.get("location"))
            lat, lng = _as_geo(node.get("location"))
            image = node.get("image")
            if isinstance(image, list):
                image = image[0] if image else None
            if isinstance(image, dict):
                image = image.get("url")
            title = str(node.get("name") or "Untitled event")
            out.append(
                Event(
                    source=self.name,
                    title=title,
                    description=node.get("description"),
                    start_time=_dt(node.get("startDate")),
                    end_time=_dt(node.get("endDate")),
                    venue=venue,
                    location=location,
                    lat=lat,
                    lng=lng,
                    url=(node.get("url") if isinstance(node.get("url"), str) else None) or url,
                    image_url=clean_image_url(image),
                    categories=[guess_category(title)],
                    raw=node,
                )
            )
        return out


SOURCE = EventsWebSource()
