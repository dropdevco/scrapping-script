"""First-party borderland event calendars and venue listings.

This source supplements the generic web/Eventbrite/Meetup fallback with the
specific El Paso and Ciudad Juarez calendars that matter locally. It stays
keyless and polite: robots.txt is checked before every page fetch, pages are
bounded, and sites that do not expose crawlable event markup simply yield no
events instead of blocking the run.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Optional
from urllib.parse import urljoin, urlparse

from ..core.categorize import guess_categories
from ..core.http import HttpClient
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
    Directory("cultura_chihuahua_ccpn", "https://www.cultura.chihuahua.gob.mx/", "juarez", 24),
    Directory("juarez_municipal_events", "https://www.juarez.gob.mx/", "juarez", 18),
    Directory("uacj_agenda", "https://www.uacj.mx/agenda/", "juarez", 24),
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
NOISE_TEXT = {
    "image",
    "image: showtime",
    "image: buy tickets",
    "image: more info",
    "buy tickets",
    "more info",
    "load more events",
}

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
    return any(
        token in haystack
        for token in (
            "juarez",
            "juárez",
            "cd.juarez",
            "cd. juarez",
            "ciudad juarez",
            "ciudad juárez",
            "paso del norte",
            "chamizal",
            "mexicanidad",
            "uacj",
        )
    )


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
        return out

    def _elpasolive_listing_events(self, html: str, url: str) -> list[Event]:
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(html, "html.parser")
        link_by_title = {
            a.get_text(" ", strip=True): urljoin(url, a["href"])
            for a in soup.find_all("a", href=True)
            if a.get_text(" ", strip=True)
        }
        lines = [s.strip() for s in soup.stripped_strings if s.strip()]

        events: list[Event] = []
        current_date: Optional[str] = None
        for idx, line in enumerate(lines):
            if not MONTH_RE.match(line):
                continue
            current_date = line
            title = None
            start = None
            for lookahead in lines[idx + 1 : idx + 8]:
                normalized = lookahead.strip().lower()
                if not lookahead or normalized in NOISE_TEXT or normalized.startswith("image:"):
                    continue
                if MONTH_RE.match(lookahead):
                    break
                if TIME_RE.search(lookahead):
                    start = TIME_RE.search(lookahead).group(1)
                    continue
                if title is None and len(lookahead) > 2:
                    title = lookahead
            if not title or title.lower() in {"events", "upcoming events"}:
                continue
            events.append(
                Event(
                    source=self.name,
                    source_id=f"el_paso_live:{title}:{current_date}",
                    title=title,
                    start_time=_parse_datetime(current_date, start),
                    venue="El Paso Live",
                    location="One Civic Center Plaza, El Paso, TX 79901",
                    url=link_by_title.get(title, url),
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
