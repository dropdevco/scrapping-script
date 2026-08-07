"""Events discovered on the open web (keyless).

Strategy: fetch city listing pages on platforms that were empirically verified to serve
crawlable ``schema.org/Event`` JSON-LD to a plain (non-JS) scraper AND allow it in
robots.txt — Eventbrite, Ticketmaster's public pages, and Meetup — then extract the
structured events. A ``site:``-scoped DuckDuckGo search adds extra detail pages, including AXS
pages that are indexed but often bot-protected on direct fetch. Sites that
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
from ..core.categorize import guess_categories
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


def _is_link_only_paragraph(p: Any) -> bool:
    """True for a <p> that's just a wrapped CTA button ("View Website", "Purchase
    Your Admission Tickets"), not real description prose — its entire text is a
    single contained link's text, with nothing else in the paragraph. Both
    templates wrap these buttons in a plain <p> alongside the real paragraphs,
    and we already render the actual ticket link as its own button from
    event.url, so keeping these would just duplicate boilerplate into the copy.
    """
    text = p.get_text(strip=True)
    if not text:
        return True
    links = p.find_all("a")
    return len(links) == 1 and links[0].get_text(strip=True) == text


def _full_description_and_link(
    soup: Any, container_selector: str
) -> tuple[Optional[str], Optional[str]]:
    """Full "About this event" body text, AND the real outbound link a "View
    Website"/"Purchase Tickets" CTA in that same copy points at, from a detail
    page's content container. Both pulled in one pass since they come from the
    exact same paragraphs — one is what's left after excluding CTAs, the other
    is what the first excluded CTA points to.

    Both Visit El Paso and La Nube run the same white-label "event-card"
    calendar widget (see module docstring), but neither source's own summary
    is the real thing: Visit El Paso's schema.org JSON-LD `description` is an
    SEO meta-description auto-truncated to ~200 chars (ends in a literal "…"),
    and the listing-card blurb used for La Nube is a short teaser, not the
    full copy. The complete text is always in this template's own <p> tags
    on the detail page — this pulls that instead of trusting either summary.

    Neither source's `url` is useful either: both are pure calendar
    aggregators, not the actual venue/ticket seller, and their own JSON-LD
    `url` field just points back at the same aggregator page — a dead end for
    anyone trying to actually buy a ticket or find the business. The real
    destination (flixbrewhouse.com, koronaevent.com, whatever it is) is
    always the one link inside a CTA-only <p> in this same container — the
    thing _is_link_only_paragraph excludes from the description text is
    exactly the thing worth keeping as the URL.

    `container_selector` is a Bootstrap utility class (`.mb-5`, `.mt-3`) —
    both templates reuse the same utility classes elsewhere on the page (nav
    menus, badge wrappers, spacing on unrelated sections), so it matches
    several elements and only ONE of them is the real content block. Every
    match is tried in document order and the first one that actually
    contains <p> tags wins — badge/date/nav wrappers never have paragraph
    children, only real body copy does.
    """
    for container in soup.select(container_selector):
        paragraphs: list[str] = []
        link: Optional[str] = None
        for p in container.select("p"):
            if _is_link_only_paragraph(p):
                if link is None:
                    a = p.find("a")
                    href = a.get("href") if a else None
                    if href:
                        link = href
                continue
            text = p.get_text(separator=" ", strip=True)
            if text:
                paragraphs.append(text)
        if paragraphs:
            return "\n\n".join(paragraphs), link
    return None, None


def _find_key(node: Any, key: str) -> Any:
    """Depth-first search for the first occurrence of `key` anywhere in a
    nested dict/list — used to fish one value out of a large, unstable
    third-party page-state blob (Next.js __NEXT_DATA__ etc.) without hardcoding
    its exact nesting, which shifts between a site's deploys."""
    if isinstance(node, dict):
        if key in node:
            return node[key]
        for v in node.values():
            found = _find_key(v, key)
            if found is not None:
                return found
    elif isinstance(node, list):
        for item in node:
            found = _find_key(item, key)
            if found is not None:
                return found
    return None


def _next_data(html: str) -> Any:
    """Parsed __NEXT_DATA__ JSON from a Next.js-rendered page, or None."""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    tag = soup.find("script", id="__NEXT_DATA__")
    if not tag or not tag.string:
        return None
    try:
        return json.loads(tag.string)
    except (json.JSONDecodeError, ValueError):
        return None


_EVENTBRITE_DETAIL_RE = re.compile(r"/e/[^/?#]+-tickets-\d+")
_MEETUP_DETAIL_RE = re.compile(r"/events/\d+")


def _eventbrite_full_description(html: str) -> Optional[str]:
    """Eventbrite's own schema.org/OG/Twitter `description` is a short SEO
    summary (~140 chars on a sampled live event) — nowhere near the real
    "About this event" copy, which the page instead renders from a
    structuredContent.modules[] block (rich-text HTML fragments) buried in
    its __NEXT_DATA__ state. Verified on a live event: 137 chars from
    schema.org vs 2,408 from this. Every module's HTML fragment is stripped
    to plain text and joined; a page with no structuredContent (a listing
    page, not a single event) yields None."""
    data = _next_data(html)
    if data is None:
        return None
    structured_content = _find_key(data, "structuredContent")
    modules = structured_content.get("modules") if isinstance(structured_content, dict) else None
    if not isinstance(modules, list):
        return None

    from bs4 import BeautifulSoup

    parts: list[str] = []
    for m in modules:
        if not isinstance(m, dict):
            continue
        frag = m.get("text")
        if not isinstance(frag, str) or not frag.strip():
            continue
        text = BeautifulSoup(frag, "html.parser").get_text(separator=" ", strip=True)
        if text:
            parts.append(text)
    return "\n\n".join(parts) if parts else None


def _meetup_full_description(html: str) -> Optional[str]:
    """Meetup's schema.org/OG/Twitter `description` is inconsistently
    truncated — sometimes complete, sometimes cut off mid-word with no
    ellipsis (observed both on the same live event on different requests).
    The page's __NEXT_DATA__ state always carries the real, complete text at
    props.pageProps.event.description (865 chars vs 155 truncated, on the
    event where this was caught), so that's used directly."""
    data = _next_data(html)
    if not isinstance(data, dict):
        return None
    props = data.get("props")
    page_props = props.get("pageProps") if isinstance(props, dict) else None
    event = page_props.get("event") if isinstance(page_props, dict) else None
    desc = event.get("description") if isinstance(event, dict) else None
    return desc.strip() if isinstance(desc, str) and desc.strip() else None


def _individual_page_full_description(url: str, html: str) -> Optional[str]:
    """Only meaningful on a SINGLE event's own detail page. `_direct_urls`
    and the DDG site-search both also produce multi-event LISTING pages
    (eventbrite.com/d/…/all-events/, meetup.com/find/…) whose JSON-LD holds
    several distinct events at once — this must never run there, since the
    page-level state it reads corresponds to at most one event, not every
    JSON-LD node found on the page. The regexes require the URL shape only a
    genuine individual event page has (Eventbrite: /e/…-tickets-<id>; Meetup:
    /events/<id>) so a listing page's URL simply never matches."""
    if "eventbrite." in url and _EVENTBRITE_DETAIL_RE.search(url):
        return _eventbrite_full_description(html)
    if "meetup.com" in url and _MEETUP_DETAIL_RE.search(url):
        return _meetup_full_description(html)
    return None


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


def _page_event_from_jsonld(node: Any, url: str, source: str) -> Optional[Event]:
    if not isinstance(node, dict):
        return None
    venue, location = _as_location(node.get("location"))
    lat, lng = _as_geo(node.get("location"))
    image = node.get("image")
    if isinstance(image, list):
        image = image[0] if image else None
    if isinstance(image, dict):
        image = image.get("url")
    title = str(node.get("name") or "Untitled event")
    return Event(
        source=source,
        source_id=node.get("@id") if isinstance(node.get("@id"), str) else None,
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
        categories=guess_categories(title),
        raw=node,
    )


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
# Sole content container on a Visit El Paso detail page, verified to hold every
# <p> of the full "About this event" copy — see _full_description.
_VISITELPASO_DESC_SELECTOR = "div.mb-5"


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
# Sole content container on a La Nube detail page holding the full "About
# this event" copy — same shared widget as Visit El Paso, different wrapper.
_LANUBE_DESC_SELECTOR = "section.event-detail .mt-3"


async def _lanube_full_description_and_link(
    href: str, http: HttpClient
) -> tuple[Optional[str], Optional[str]]:
    try:
        if not await http.can_fetch(href):
            return None, None
        html = await http.get_text(href, headers={"User-Agent": _BROWSER_UA})
    except Exception as exc:  # noqa: BLE001
        log.debug("fetch %s failed: %s", href, exc)
        return None, None

    from bs4 import BeautifulSoup

    return _full_description_and_link(BeautifulSoup(html, "html.parser"), _LANUBE_DESC_SELECTOR)


async def _lanube_events(http: HttpClient) -> list[Event]:
    """Events from La Nube's calendar (same widget as Visit El Paso). Its
    detail pages carry no JSON-LD, so the listing card supplies every other
    field — but NOT the description: that's a short teaser blurb, not the
    full copy, so each event's detail page is fetched separately (concurrent,
    bounded by HttpClient's shared semaphore — same pattern Visit El Paso
    already uses) for the real text, same fix as Visit El Paso above. The
    same fetch also recovers the real ticket/info link (see
    _full_description_and_link) — La Nube is a calendar aggregator, so its
    own page is never where a visitor actually buys a ticket.
    """
    soup = await _fetch_calendar_listing(_LANUBE_LISTING, http)
    if soup is None:
        return []

    cards: list[dict[str, Any]] = []
    seen: set[str] = set()
    for card in soup.select(".event-card"):
        fields = _parse_calendar_card(card, _LANUBE_LISTING)
        href = fields["href"]
        if not href or href in seen or not fields["title"]:
            continue
        seen.add(href)
        cards.append(fields)
        if len(cards) >= _LANUBE_MAX_EVENTS:
            break

    details = await asyncio.gather(
        *(_lanube_full_description_and_link(f["href"], http) for f in cards)
    )

    events: list[Event] = []
    for fields, (full_description, real_url) in zip(cards, details):
        href = fields["href"]
        start, end = _parse_card_datetime(fields["date_text"], fields["time_text"])
        events.append(
            Event(
                source="events_web",
                title=fields["title"],
                description=full_description or fields["description"],
                start_time=start,
                end_time=end,
                venue=fields["venue"],
                location=fields["address"],
                url=real_url or href,
                image_url=clean_image_url(fields["image"]),
                categories=fields["categories"] or guess_categories(fields["title"]),
                raw=fields,
            )
        )
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
        search_q = f"{topic} {loc} (site:eventbrite.com OR site:meetup.com OR site:axs.com)".strip()
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
        """Visit El Paso detail pages only. Fetches once so the full-body
        description AND the real destination link (see
        _full_description_and_link) can both be pulled from the same HTML
        already fetched for the JSON-LD event data, instead of trusting the
        JSON-LD's own truncated `description` and self-referential `url`.
        """
        try:
            if not await http.can_fetch(url):
                return []
            html = await http.get_text(url, headers={"User-Agent": _BROWSER_UA})
        except Exception as exc:  # noqa: BLE001
            log.debug("fetch %s failed: %s", url, exc)
            return []

        from bs4 import BeautifulSoup

        soup = BeautifulSoup(html, "html.parser")
        full_description, real_url = _full_description_and_link(soup, _VISITELPASO_DESC_SELECTOR)

        page: list[Event] = []
        for node in _iter_jsonld_events(html):
            event = _page_event_from_jsonld(node, url, source=self.name)
            if event is not None:
                page.append(event)

        for e in page:
            if full_description:
                e.description = full_description
            if real_url:
                e.url = real_url
            if categories:
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
            event = _page_event_from_jsonld(node, url, source=self.name)
            if event is not None:
                out.append(event)

        full_description = _individual_page_full_description(url, html)
        if full_description:
            for e in out:
                e.description = full_description
        return out


SOURCE = EventsWebSource()
