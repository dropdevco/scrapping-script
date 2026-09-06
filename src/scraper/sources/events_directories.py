"""First-party borderland event calendars and venue listings.

This source supplements the generic web/Eventbrite/Meetup fallback with the
specific El Paso and Ciudad Juarez calendars that matter locally. It stays
keyless and polite: robots.txt is checked before every page fetch, pages are
bounded, and sites that do not expose crawlable event markup simply yield no
events instead of blocking the run.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Optional
from urllib.parse import urljoin, urlparse

from ..core.categorize import guess_categories
from ..core.http import HttpClient
from ..core.media import clean_image_url
from ..core.models import Event, Kind, SearchParams
from .base import Source
from .events_web import (
    _BROWSER_UA,
    _in_window,
    _is_link_only_paragraph,
    _iter_jsonld_events,
    _page_event_from_jsonld,
)

log = logging.getLogger("scraper.events_directories")


@dataclass(frozen=True)
class Directory:
    name: str
    url: str
    city_hint: str
    max_details: int = 18


DIRECTORIES: tuple[Directory, ...] = (
    # El Paso primary calendars and venues.
    Directory("visit_el_paso", "https://visitelpaso.com/events", "el paso", 30),
    Directory("el_paso_live", "https://www.elpasolive.com/events", "el paso", 45),
    Directory("city_of_el_paso_events", "https://events.elpasotexas.gov/", "el paso", 40),
    Directory("el_paso_county_calendar", "https://www.epcounty.com/Calendar/home", "el paso", 18),
    Directory("southwest_university_park", "https://southwestuniversitypark.com/events", "el paso", 18),
    Directory("utep_special_events", "https://www.utep.edu/special-events/", "el paso", 18),
    Directory("lowbrow_palace", "https://lowbrowpalace.com/shows/", "el paso", 24),
    Directory("el_paso_coliseum", "https://www.elpasocoliseum.com/events", "el paso", 24),
    Directory("rockhouse", "https://rockhousebarandgrill.com/events/", "el paso", 18),
    Directory("axs_el_paso", "https://www.axs.com/category/cities/5520993/el-paso-tx", "el paso", 35),
    # Ciudad Juarez calendars, ticketing portals, and venues.
    Directory("don_boleton_juarez", "https://donboleton.com/", "juarez", 30),
    Directory("boletia_juarez", "https://boletia.com/eventos/ciudad-juarez", "juarez", 20),
    Directory("ticketmaster_mx_juarez", "https://www.ticketmaster.com.mx/search?q=Ciudad%20Juarez", "juarez", 20),
    Directory("visita_juarez", "https://visitajuarez.mx/", "juarez", 24),
    Directory("juarez_municipal_events", "https://www.juarez.gob.mx/", "juarez", 18),
    Directory("uacj_agenda", "https://www.uacj.mx/agenda/", "juarez", 24),
    # Chihuahua state Secretaría de Cultura's statewide calendar. Covers
    # Centro Cultural de las Fronteras, Centro Cultural Paso del Norte
    # (as "Teatro Experimental Octavio Trías" / "CCPN"), and the Juárez
    # Cineteca and public libraries — a real find: a single Wix-hosted
    # events widget mixing Chihuahua-capital and Juárez events together,
    # so _event_matches_directory_region does the real filtering.
    Directory("cultura_chihuahua_juarez", "https://www.culturachihuahua.com/agendacultural", "juarez", 5),
    # YOSIVOY: a small bilingual Juárez-El Paso border culture aggregator
    # that also picks up independent venues (cafés, studios) the
    # government/university calendars never mention.
    Directory("yosivoy_juarez", "https://yosivoy.yociudadano.com.mx/cartelera", "juarez", 5),
)

EVENT_LINK_RE = re.compile(
    r"(event|events|evento|eventos|agenda|calendar|calendario|show|shows|concert|concierto|"
    r"bolet|ticket|teatro|festival)",
    re.IGNORECASE,
)
MONTH_RE = re.compile(
    r"^(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{1,2}(?:,\s*\d{4})?",
    re.IGNORECASE,
)
TIME_RE = re.compile(r"(\d{1,2}:\d{2}\s*[AP]M)", re.IGNORECASE)
AXS_EVENT_RE = re.compile(
    r"\b(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)\s+\([^)]+\)\s+"
    r"(?P<month>Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+\([^)]+\)\s+"
    r"(?P<day>\d{1,2}),\s+(?P<year>\d{4})\s+-\s+"
    r"(?P<time>\d{1,2}:\d{2}\s*[AP]M)\s+"
    r"(?P<title>.+?)\s+"
    r"(?P<venue>[^.]+?),\s+El Paso,\s+TX,\s+United States",
    re.IGNORECASE,
)
CITY_EVENT_RE = re.compile(
    r"^(?P<date>[A-Za-z]{3}\s+\d{1,2})\s+\w+\s+schedule\s+"
    r"(?P<start>\d{1,2}:\d{2}\s*[AP]M)"
    r"(?:\s*-\s*(?P<end>\d{1,2}:\d{2}\s*[AP]M))?\s+location_on\s+"
    r"(?P<rest>.+)$",
    re.IGNORECASE,
)
_CITY_MAX_DETAILS = 60  # caps detail-page fetches per run, same order as other directories


def _city_event_full_description(soup: Any) -> Optional[str]:
    """Full description from a events.elpasotexas.gov event-detail.php page.

    The listing page's flattened anchor text (what CITY_EVENT_RE parses) never
    carries a description at all — city_of_el_paso_events events used to store
    none. Each event's own detail page does, in .eventsDetail: the shared
    template puts the venue/date block and the real description as SIBLING <p>
    children there, e.g.:

        <p><strong>Wed, 8/5/2026</strong>...<strong>Richard Burges...</strong></p>
        <p><p>Join us every Wednesday...</p><p>Ages 9-12 years.</p>...</p>
        <p><a href="...">Visit Libraries Website</a></p>

    The venue/date block always contains <strong> tags and the real
    description never does, so that's what distinguishes them — same
    _is_link_only_paragraph exclusion as every other source handles its own
    trailing CTA link with. recursive=False matters here: BeautifulSoup keeps
    this page's malformed nested <p>Ages 9-12...</p> markup as genuinely
    nested rather than flattening it the way a browser would, so a plain
    .select("p") would return both the wrapping paragraph AND its own nested
    children and double the text.
    """
    container = soup.select_one(".eventsDetail")
    if not container:
        return None
    paragraphs = []
    for p in container.find_all("p", recursive=False):
        if p.find("strong") or _is_link_only_paragraph(p):
            continue
        text = p.get_text(separator=" ", strip=True)
        if text:
            paragraphs.append(text)
    return "\n\n".join(paragraphs) if paragraphs else None


def _wanted_for_location(directory: Directory, location: str | None) -> bool:
    loc = (location or "").lower()
    if not loc:
        return True
    if "juarez" in loc or "juárez" in loc or "chihuahua" in loc:
        return directory.city_hint == "juarez"
    if "el paso" in loc:
        return directory.city_hint == "el paso"
    return True


def _same_host_or_child(base: str, href: str) -> bool:
    base_host = urlparse(base).netloc.lower().removeprefix("www.")
    href_host = urlparse(href).netloc.lower().removeprefix("www.")
    return href_host == base_host or href_host.endswith(f".{base_host}")


def _dedupe_events(events: list[Event]) -> list[Event]:
    seen: set[tuple[str, Optional[str], Optional[str]]] = set()
    out: list[Event] = []
    for event in events:
        key = (event.title.strip().lower(), event.url, event.start_time.isoformat() if event.start_time else None)
        if key in seen:
            continue
        seen.add(key)
        out.append(event)
    return out


# National touring-show listings (Ticketmaster MX, ticket resellers) mention
# Ciudad Juarez in a "also playing in: ..." blurb even when the specific event
# on the page is in a different city entirely — so a hard negative on the
# *place* fields always wins, regardless of what the free-text blurb says.
_OTHER_CITY_MARKERS = (
    "cdmx",
    "mexico-cdmx",
    "ciudad de mexico",
    "ciudad de méxico",
    "distrito federal",
    ", df,",
    "monterrey",
    "guadalajara",
    "chihuahua, chihuahua",  # the state capital, a different city from Cd. Juarez
    "chihuahua capital",
)


def _event_matches_directory_region(event: Event, directory: Directory) -> bool:
    if directory.city_hint != "juarez":
        return True

    # Place fields only — NOT description. Touring-show blurbs routinely list
    # "now playing in: Ciudad Juarez, Monterrey, CDMX..." for a show whose
    # title/venue/location are some other city; description would make that
    # incidental mention look like a positive regional match.
    place = " ".join(str(part or "") for part in (event.venue, event.location)).lower()
    if any(marker in place for marker in _OTHER_CITY_MARKERS):
        return False

    # City/venue-owned Juarez sites are already geographically scoped, once the
    # negative check above has ruled out an obviously-mislabeled event.
    if directory.name in {"visita_juarez", "juarez_municipal_events", "uacj_agenda"}:
        return True

    haystack = " ".join(
        str(part or "") for part in (event.title, event.venue, event.location, event.url)
    ).lower()
    if directory.name in {"don_boleton_juarez", "boletia_juarez", "ticketmaster_mx_juarez"}:
        return any(
            token in haystack
            for token in (
                "cd.juarez",
                "cd. juarez",
                "cd juarez",
                "ciudad juarez",
                "ciudad juárez",
                "juarez, chih",
                "juárez, chih",
                "juarez chih",
                "juárez chih",
                "juarez, chihuahua",
                "juárez, chihuahua",
            )
        )
    # No bare "juarez"/"juárez" here — "Benito Juárez" is also a street/avenue
    # name all over Chihuahua state (state-wide sources like
    # cultura_chihuahua_juarez mix Chihuahua-capital venues in freely, and one
    # sits on "Av. Benito Juárez" in the state capital, nowhere near the
    # border). Same qualified forms as the ticketing-site branch above.
    return any(
        token in haystack
        for token in (
            "cd.juarez",
            "cd. juarez",
            "cd juarez",
            "ciudad juarez",
            "ciudad juárez",
            "juarez, chih",
            "juárez, chih",
            "juarez chih",
            "juárez chih",
            "juarez, chihuahua",
            "juárez, chihuahua",
            "paso del norte",
            "chamizal",
            "mexicanidad",
            "uacj",
        )
    )


def _parse_iso(value: Optional[str]) -> Optional[datetime]:
    """Wix's warmup JSON ships real ISO-8601 UTC instants, not the loose
    text every other directory here needs regexes for."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


_SPANISH_MONTHS = {
    "enero": 1,
    "febrero": 2,
    "marzo": 3,
    "abril": 4,
    "mayo": 5,
    "junio": 6,
    "julio": 7,
    "agosto": 8,
    "septiembre": 9,
    "octubre": 10,
    "noviembre": 11,
    "diciembre": 12,
}
_SPANISH_DATE_RE = re.compile(r"(\d{1,2})\s+de\s+([a-záéíóúñ]+)", re.IGNORECASE)
_SPANISH_TIME_RE = re.compile(r"(\d{1,2}):(\d{2})")


def _parse_spanish_datetime(date_text: Optional[str], time_text: Optional[str]) -> Optional[datetime]:
    """Parses text like "domingo 13 de septiembre" + "07:00 hrs" — no year,
    unlike every other date format this module parses, since YOSIVOY's card
    only ever shows day/month. Same forward-roll as _parse_datetime once
    the year is assumed."""
    if not date_text:
        return None
    match = _SPANISH_DATE_RE.search(date_text)
    if not match:
        return None
    month = _SPANISH_MONTHS.get(match.group(2).lower())
    if not month:
        return None
    hour = minute = 0
    if time_text:
        time_match = _SPANISH_TIME_RE.search(time_text)
        if time_match:
            hour, minute = int(time_match.group(1)), int(time_match.group(2))
    try:
        parsed = datetime(date.today().year, month, int(match.group(1)), hour, minute)
    except ValueError:
        return None
    if parsed.date() < date.today():
        parsed = parsed.replace(year=parsed.year + 1)
    return parsed


def _parse_datetime(date_text: str, time_text: Optional[str] = None) -> Optional[datetime]:
    date_clean = date_text.split("–", 1)[0].split("-", 1)[0].strip()
    if not re.search(r"\d{4}", date_clean):
        date_clean = f"{date_clean}, {date.today().year}"
    raw = f"{date_clean} {time_text}" if time_text else date_clean
    fmts = ["%b %d, %Y %I:%M %p", "%B %d, %Y %I:%M %p", "%b %d, %Y", "%B %d, %Y"]
    for fmt in fmts:
        try:
            parsed = datetime.strptime(raw, fmt)
            if parsed.date() < date.today().replace(month=1, day=1):
                parsed = parsed.replace(year=parsed.year + 1)
            return parsed
        except ValueError:
            continue
    return None


class EventDirectoriesSource(Source):
    name = "events_directories"
    kind = Kind.EVENTS

    def is_configured(self) -> bool:
        return True

    async def fetch(self, params: SearchParams, http: HttpClient) -> list[Event]:
        directories = [d for d in DIRECTORIES if _wanted_for_location(d, params.location)]
        pages = await asyncio.gather(*(self._directory_events(d, params, http) for d in directories))
        events = [event for page in pages for event in page]
        events = [event for event in events if _in_window(event, params.start_date, params.end_date)]
        return _dedupe_events(events)

    async def _directory_events(
        self, directory: Directory, params: SearchParams, http: HttpClient
    ) -> list[Event]:
        html = await self._fetch(directory.url, http)
        if not html:
            return []

        events = await self._events_from_page(html, directory.url, directory.name, http)

        # city_of_el_paso_events fetches its own detail pages inside
        # _city_listing_events now (for the description JSON-LD never has
        # here — see there), so the generic detail-page crawl below would
        # just fetch every one of those same URLs a second time for nothing:
        # _events_from_page's regex-based extraction only matches the
        # LISTING page's flattened anchor text, never a detail page's own
        # markup, so running it again on each detail page always yields [].
        if directory.name != "city_of_el_paso_events":
            detail_urls = self._detail_urls(html, directory)
            detail_pages = await asyncio.gather(*(self._fetch(url, http) for url in detail_urls))
            for url, detail_html in zip(detail_urls, detail_pages):
                if detail_html:
                    events.extend(await self._events_from_page(detail_html, url, directory.name, http))

        topic = (params.query or "").strip().lower()
        if topic:
            events = [
                event
                for event in events
                if topic in event.title.lower() or topic in (event.description or "").lower()
            ]
        events = [event for event in events if _event_matches_directory_region(event, directory)]
        return events

    async def _fetch(self, url: str, http: HttpClient) -> Optional[str]:
        try:
            if not await http.can_fetch(url):
                return None
            return await http.get_text(url, headers={"User-Agent": _BROWSER_UA})
        except Exception as exc:  # noqa: BLE001
            log.debug("fetch %s failed: %s", url, exc)
            return None

    async def _events_from_page(
        self, html: str, url: str, source_name: str, http: HttpClient
    ) -> list[Event]:
        out: list[Event] = []
        for node in _iter_jsonld_events(html):
            event = _page_event_from_jsonld(node, url, source=self.name)
            if event is None:
                continue
            event.source_id = f"{source_name}:{event.source_id or event.url or event.title}"
            if not event.categories:
                event.categories = guess_categories(event.title)
            event.raw = {"directory": source_name, "jsonld": event.raw}
            out.append(event)
        if source_name == "el_paso_live":
            out.extend(self._elpasolive_listing_events(html, url))
        elif source_name == "axs_el_paso":
            out.extend(self._axs_listing_events(html, url))
        elif source_name == "city_of_el_paso_events":
            out.extend(await self._city_listing_events(html, url, http))
        elif source_name == "cultura_chihuahua_juarez":
            out.extend(self._culturachihuahua_listing_events(html))
        elif source_name == "yosivoy_juarez":
            out.extend(self._yosivoy_listing_events(html))
        return out

    def _elpasolive_listing_events(self, html: str, url: str) -> list[Event]:
        """Each event is a `.component--event-list` card with its own image,
        title/link, date, and (optionally) a showtime — unlike the other
        directories here, this markup is structured enough to read directly
        instead of flattening to text and re-parsing it with regexes. That
        also means, unlike a flattened-text parse, the card's own <img> is
        available: elpasolive.com is the only directory site that actually
        publishes a photo per event.
        """
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(html, "html.parser")
        events: list[Event] = []
        for card in soup.select("div.component--event-list"):
            info = card.select_one(".component--event-list-info")
            title_link = info.select_one("a") if info else None
            title = title_link.get_text(" ", strip=True) if title_link else None
            if not title or title.lower() in {"events", "upcoming events"}:
                continue

            date_div = info.find("div", recursive=False) if info else None
            current_date = date_div.get_text(" ", strip=True) if date_div else None
            if not current_date or not MONTH_RE.match(current_date):
                continue

            clock = card.select_one(".icon-clock")
            time_match = TIME_RE.search(clock.get_text(" ", strip=True)) if clock else None
            start = time_match.group(1) if time_match else None

            img = card.select_one(".component--event-list-image img")
            image_url = clean_image_url(urljoin(url, img["src"])) if img and img.get("src") else None

            events.append(
                Event(
                    source=self.name,
                    source_id=f"el_paso_live:{title}:{current_date}",
                    title=title,
                    start_time=_parse_datetime(current_date, start),
                    venue="El Paso Live",
                    location="One Civic Center Plaza, El Paso, TX 79901",
                    url=urljoin(url, title_link["href"]) if title_link and title_link.get("href") else url,
                    image_url=image_url,
                    categories=guess_categories(title),
                    raw={"directory": "el_paso_live", "date": current_date, "time": start},
                )
            )
        return events

    def _axs_listing_events(self, html: str, url: str) -> list[Event]:
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(html, "html.parser")
        text = soup.get_text(" ", strip=True)
        events: list[Event] = []
        for match in AXS_EVENT_RE.finditer(text):
            title = match.group("title").removesuffix("...").strip()
            venue = match.group("venue").strip()
            date_text = f"{match.group('month')} {match.group('day')}, {match.group('year')}"
            events.append(
                Event(
                    source=self.name,
                    source_id=f"axs_el_paso:{title}:{date_text}:{match.group('time')}",
                    title=title,
                    start_time=_parse_datetime(date_text, match.group("time")),
                    venue=venue,
                    location=f"{venue}, El Paso, TX, United States",
                    url=url,
                    categories=guess_categories(title),
                    raw={"directory": "axs_el_paso", "date": date_text, "time": match.group("time")},
                )
            )
        return events

    def _culturachihuahua_listing_events(self, html: str) -> list[Event]:
        """Wix ships every event as JSON in a `#wix-warmup-data` script tag
        (its own SSR hydration payload) rather than in the visible markup —
        the flattened text has no reliable field boundaries to regex apart,
        but this JSON carries real ISO timestamps, a formatted address, and
        venue coordinates directly, sparing a later geocode() call entirely.

        This is a *statewide* Chihuahua calendar, not a Juarez-only one —
        it mixes in Chihuahua-capital venues freely, so this method makes no
        region judgment itself; _event_matches_directory_region filters the
        output same as every other directory here.
        """
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(html, "html.parser")
        tag = soup.find("script", id="wix-warmup-data")
        if not tag or not tag.string:
            return []
        try:
            warmup = json.loads(tag.string)
        except ValueError:
            return []

        raw_events: list[dict[str, Any]] = []
        for app in (warmup.get("appsWarmupData") or {}).values():
            if not isinstance(app, dict):
                continue
            for widget in app.values():
                if not isinstance(widget, dict):
                    continue
                candidates = ((widget.get("events") or {}).get("events"))
                if isinstance(candidates, list):
                    raw_events.extend(candidates)

        events: list[Event] = []
        for item in raw_events:
            title = item.get("title")
            if not title:
                continue
            scheduling = (item.get("scheduling") or {}).get("config") or {}
            location = item.get("location") or {}
            full_address = location.get("fullAddress") or {}
            geocode = full_address.get("geocode") or {}
            slug = item.get("slug")
            events.append(
                Event(
                    source=self.name,
                    source_id=f"cultura_chihuahua_juarez:{item.get('id') or slug or title}",
                    title=title,
                    description=item.get("description") or item.get("about") or None,
                    start_time=_parse_iso(scheduling.get("startDate")),
                    end_time=_parse_iso(scheduling.get("endDate")),
                    venue=location.get("name"),
                    location=full_address.get("formattedAddress") or location.get("address"),
                    lat=geocode.get("latitude"),
                    lng=geocode.get("longitude"),
                    url=f"https://www.culturachihuahua.com/events-1/{slug}" if slug else None,
                    image_url=(item.get("mainImage") or {}).get("url"),
                    categories=guess_categories(title),
                    raw={
                        "directory": "cultura_chihuahua_juarez",
                        "scheduling": item.get("scheduling"),
                        "location": location,
                    },
                )
            )
        return events

    def _yosivoy_listing_events(self, html: str) -> list[Event]:
        """Each event is a `.ev-item` card. Unlike the JSON sources above,
        this markup has no machine-readable date at all — just Spanish
        prose ("domingo 13 de septiembre") with no year, so the year is
        inferred the same way _parse_datetime does: assume this year, roll
        to next if that lands in the past.
        """
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(html, "html.parser")
        events: list[Event] = []
        for card in soup.select(".ev-item"):
            title_el = card.select_one(".ev-title")
            title = title_el.get_text(" ", strip=True) if title_el else None
            if not title:
                continue

            venue = date_text = time_text = None
            for meta in card.select(".ev-meta-item"):
                icon = meta.select_one("i")
                kind = icon.get("data-feather") if icon else None
                text = meta.get_text(" ", strip=True)
                if kind == "map-pin":
                    venue = text
                elif kind == "calendar":
                    date_text = text
                elif kind == "clock":
                    time_text = text

            desc_el = card.select_one(".ev-desc")
            link_el = card.select_one("a.ev-link")
            img_el = card.select_one(".ev-img-wrap img")

            events.append(
                Event(
                    source=self.name,
                    source_id=f"yosivoy_juarez:{link_el['href'] if link_el and link_el.get('href') else title}",
                    title=title,
                    description=desc_el.get_text(" ", strip=True) if desc_el else None,
                    start_time=_parse_spanish_datetime(date_text, time_text),
                    venue=venue,
                    location=f"{venue}, Juárez, Chih., México" if venue else "Juárez, Chih., México",
                    url=link_el["href"] if link_el and link_el.get("href") else None,
                    image_url=img_el["src"] if img_el and img_el.get("src") else None,
                    categories=guess_categories(title),
                    raw={"directory": "yosivoy_juarez", "date": date_text, "time": time_text},
                )
            )
        return events

    async def _city_listing_events(self, html: str, url: str, http: HttpClient) -> list[Event]:
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(html, "html.parser")
        pending: list[tuple[Event, str]] = []
        for anchor in soup.find_all("a", href=True):
            text = anchor.get_text(" ", strip=True)
            match = CITY_EVENT_RE.match(text)
            if not match:
                continue
            rest = match.group("rest")
            parts = rest.split()
            category = parts[-1] if parts else "Community"
            body = " ".join(parts[:-1]) if len(parts) > 1 else rest
            # City listings put branch/area first, then title. Keep the full body
            # as title when we cannot confidently split it.
            title = re.sub(r"^(Central|Downtown|Eastside|Mission Valley|Northeast|Upper Valley|Westside)\s+", "", body)
            detail_url = urljoin(url, anchor["href"])
            event = Event(
                source=self.name,
                source_id=f"city_of_el_paso_events:{text}",
                title=title,
                start_time=_parse_datetime(match.group("date"), match.group("start")),
                end_time=(
                    _parse_datetime(match.group("date"), match.group("end"))
                    if match.group("end")
                    else None
                ),
                venue=None,
                location="El Paso, TX",
                url=detail_url,
                categories=[category, *guess_categories(title)],
                raw={"directory": "city_of_el_paso_events", "listing_text": text},
            )
            pending.append((event, detail_url))
            if len(pending) >= _CITY_MAX_DETAILS:
                break

        # The listing page's flattened anchor text never carries a
        # description at all — only each event's own detail page does (real
        # prose, not a summary that got cropped). Concurrency is bounded by
        # HttpClient's shared semaphore, same as every other detail-page fan
        # -out in this module.
        descriptions = await asyncio.gather(
            *(self._city_event_description(detail_url, http) for _, detail_url in pending)
        )
        for (event, _), description in zip(pending, descriptions):
            if description:
                event.description = description
        return [event for event, _ in pending]

    async def _city_event_description(self, url: str, http: HttpClient) -> Optional[str]:
        html = await self._fetch(url, http)
        if not html:
            return None
        from bs4 import BeautifulSoup

        return _city_event_full_description(BeautifulSoup(html, "html.parser"))

    def _detail_urls(self, html: str, directory: Directory) -> list[str]:
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(html, "html.parser")
        urls: list[str] = []
        seen: set[str] = set()
        for anchor in soup.find_all("a", href=True):
            href = urljoin(directory.url, anchor["href"])
            if href in seen or not _same_host_or_child(directory.url, href):
                continue
            text = anchor.get_text(" ", strip=True)
            haystack = f"{href} {text}"
            if not EVENT_LINK_RE.search(haystack):
                continue
            seen.add(href)
            urls.append(href)
            if len(urls) >= directory.max_details:
                break
        return urls


SOURCE = EventDirectoriesSource()
