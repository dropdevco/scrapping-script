"""Events discovered on the open web (keyless).

Strategy: fetch city listing pages on platforms that were empirically verified to serve
crawlable ``schema.org/Event`` JSON-LD to a plain (non-JS) scraper AND allow it in
robots.txt — Eventbrite, Ticketmaster's public pages, and Meetup — then extract the
structured events. A ``site:``-scoped DuckDuckGo search adds extra detail pages. Sites that
bot-block or render events only via JavaScript (allevents.in, 10times, bandsintown, seatgeek,
dice.fm…) are intentionally skipped because a keyless fetch gets nothing from them.

For El Paso specifically, Visit El Paso's own events calendar (visitelpaso.com/events) is
fetched as a dedicated, higher-priority path: it's server-rendered (confirmed via plain GET,
no JS needed), lists events soonest-first with no pagination, robots.txt allows it, and each
event's detail page carries full schema.org/Event JSON-LD (as a @graph entry, already handled
by _iter_jsonld_events below). The listing page's own category badges are richer than our
keyword guess and are used directly, the same way Ticketmaster's own classification is trusted
over the guesser.

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
from urllib.parse import quote, urljoin

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
        if location:
            # Some sites (Visit El Paso) prefix the plain-string address with the
            # venue name again, e.g. "MACC: 201 W Franklin Ave. El Paso, TX 79901".
            # A colon this early in a real address is always this label artifact.
            location = re.sub(r"^[^:]{1,40}:\s*", "", location)
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


# Visit El Paso and La Nube both run the same white-label "event-card" calendar
# widget (same CSS classes, same S3 image bucket naming, cross-listed events) —
# they only differ in whether the detail page also carries JSON-LD.
_CARD_DATE_RE = re.compile(r"([A-Za-z]+\s+\d{1,2},\s*\d{4})")
_CARD_TIME_RE = re.compile(r"(\d{1,2}:\d{2}\s*[AP]M)", re.IGNORECASE)


def _parse_calendar_card(card: Any, base_url: str) -> dict[str, Any]:
    """Extract every field the shared card format exposes.

    Sites whose detail pages carry richer JSON-LD only need ``href``/``categories``
    from here; sites that don't (La Nube) build the whole Event from these fields.
    """
    link = card.select_one(".event-card__title a[href]")
    href = urljoin(base_url, link["href"]) if link and link.get("href") else None
    title = link.get_text(strip=True) if link else None

    img = card.select_one("img[src]")
    image = img.get("src") if img else None

    date_text = None
    time_text = None
    categories: list[str] = []
    for date_div in card.select(".event-card__date"):
        badges = date_div.select(".badge")
        if badges:
            categories = [b.get_text(strip=True) for b in badges if b.get_text(strip=True)]
            continue
        text = date_div.get_text(strip=True)
        if not text:
            continue
        if re.search(r"\d{4}", text):
            date_text = text
        elif re.search(r"(am|pm)", text, re.IGNORECASE):
            time_text = text

    venue = None
    address = None
    loc_el = card.select_one(".event-card__location")
    if loc_el:
        for icon in loc_el.select("i"):
            icon.decompose()
        lines = [ln.strip() for ln in loc_el.get_text(separator="\n").split("\n") if ln.strip()]
        if lines:
            venue = lines[0]
        if len(lines) > 1:
            address = lines[1]

    desc_el = card.select_one(".mt-2, .mt-3")
    description = desc_el.get_text(separator=" ", strip=True) if desc_el else None

    return {
        "href": href,
        "title": title,
        "image": image,
        "date_text": date_text,
        "time_text": time_text,
        "categories": categories,
        "venue": venue,
        "address": address,
        "description": description,
    }


def _parse_card_datetime(
    date_text: Optional[str], time_text: Optional[str]
) -> tuple[Optional[datetime], Optional[datetime]]:
    """Cards show a date RANGE plus a separate daily TIME range, e.g.
    "Jul 10, 2026 - Jul 31, 2026" + "12:00 PM - 3:00 PM" for a recurring daily
    session. Anchored to the range's first day (its next/soonest occurrence) —
    the fuller recurrence is preserved in the description text.
    """
    dates = _CARD_DATE_RE.findall(date_text or "")
    if not dates:
        return None, None
    times = _CARD_TIME_RE.findall(time_text or "")

    def _combine(date_str: str, time_str: Optional[str]) -> Optional[datetime]:
        fmt = "%B %d, %Y %I:%M %p" if time_str else "%B %d, %Y"
        raw = f"{date_str} {time_str}" if time_str else date_str
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            return None

    start = _combine(dates[0], times[0] if times else None)
    end = _combine(dates[0], times[1]) if len(times) > 1 else None
    return start, end


async def _fetch_calendar_listing(listing_url: str, http: HttpClient) -> Optional[Any]:
    """Fetch a calendar listing page and return its parsed soup, or None on failure."""
    try:
        if not await http.can_fetch(listing_url):
            return None
        html = await http.get_text(listing_url, headers={"User-Agent": _BROWSER_UA})
    except Exception as exc:  # noqa: BLE001
        log.debug("fetch %s failed: %s", listing_url, exc)
        return None

    from bs4 import BeautifulSoup

    return BeautifulSoup(html, "html.parser")


_VISITELPASO_LISTING = "https://visitelpaso.com/events"
_VISITELPASO_MAX_EVENTS = 30  # cards are listed soonest-first; caps detail-page fetches


async def _visitelpaso_events(http: HttpClient) -> list[tuple[str, list[str]]]:
    """(detail_url, categories) pairs from Visit El Paso's official calendar, soonest-first.

    The listing page's category badges are the site's own classification — richer
    than our keyword guesser — but don't appear in the detail page's Event JSON-LD,
    so they're captured here and applied after the JSON-LD fetch.
    """
    soup = await _fetch_calendar_listing(_VISITELPASO_LISTING, http)
    if soup is None:
        return []

    out: list[tuple[str, list[str]]] = []
    seen: set[str] = set()
    for card in soup.select(".event-card"):
        fields = _parse_calendar_card(card, _VISITELPASO_LISTING)
        href = fields["href"]
        if not href or href in seen:
            continue
        seen.add(href)
        out.append((href, fields["categories"]))
        if len(out) >= _VISITELPASO_MAX_EVENTS:
            break
    return out


_LANUBE_LISTING = "https://la-nube.org/plan-your-day/calendar"
_LANUBE_MAX_EVENTS = 60


async def _lanube_events(http: HttpClient) -> list[Event]:
    """Events from La Nube's calendar (same widget as Visit El Paso), built
    straight from the listing card's fields — its detail pages carry no JSON-LD.
    """
    soup = await _fetch_calendar_listing(_LANUBE_LISTING, http)
    if soup is None:
        return []

    events: list[Event] = []
    seen: set[str] = set()
    for card in soup.select(".event-card"):
        fields = _parse_calendar_card(card, _LANUBE_LISTING)
        href = fields["href"]
        if not href or href in seen or not fields["title"]:
            continue
        seen.add(href)
        start, end = _parse_card_datetime(fields["date_text"], fields["time_text"])
        events.append(
            Event(
                source="events_web",
                title=fields["title"],
                description=fields["description"],
                start_time=start,
                end_time=end,
                venue=fields["venue"],
                location=fields["address"],
                url=href,
                image_url=clean_image_url(fields["image"]),
                categories=fields["categories"] or [guess_category(fields["title"])],
                raw=fields,
            )
        )
        if len(events) >= _LANUBE_MAX_EVENTS:
            break
    return events


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

        # El Paso's own calendar sites: dedicated paths so they aren't squeezed
        # out by the generic _MAX_PAGES cap above.
        if city and "el paso" in city.lower():
            vep_items = await _visitelpaso_events(http)
            vep_pages = await asyncio.gather(
                *(self._page_events_with_categories(u, cats, http) for u, cats in vep_items)
            )
            events += [e for page in vep_pages for e in page]
            events += await _lanube_events(http)

        events = [e for e in events if _in_window(e, params.start_date, params.end_date)]
        return events

    async def _page_events_with_categories(
        self, url: str, categories: list[str], http: HttpClient
    ) -> list[Event]:
        page = await self._page_events(url, http)
        if categories:
            for e in page:
                e.categories = categories
        return page

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
