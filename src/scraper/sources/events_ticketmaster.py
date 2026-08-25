"""Events from the Ticketmaster Discovery API (free API key, ~5k calls/day)."""

from __future__ import annotations

from datetime import datetime, time
from typing import Any, Optional

from ..core.address import format_address
from ..core.categorize import guess_categories
from ..core.config import settings
from ..core.http import HttpClient
from ..core.media import clean_image_url
from ..core.models import Event, Kind, SearchParams
from .base import Source

_ENDPOINT = "https://app.ticketmaster.com/discovery/v2/events.json"


def _dt(value: str | None) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _start_datetime(dates: dict[str, Any]) -> Optional[datetime]:
    """The start instant, preferring the field that actually carries an offset.

    ``dates.start.dateTime`` is a real UTC instant and is used whenever present.
    When it is absent Ticketmaster still usually ships ``localDate`` +
    ``localTime`` — a wall-clock pair. Combining those and letting
    core/eventtime.py resolve them keeps a 7pm show at 7pm; taking ``localDate``
    alone (the old behavior) silently turned every such event into midnight,
    which then read as 6pm the *previous* day once stored.
    """
    start = dates.get("start") or {}
    absolute = _dt(start.get("dateTime"))
    if absolute is not None:
        return absolute

    local_date = start.get("localDate")
    if not local_date:
        return None
    # timeTBA/noSpecificTime mean the time genuinely is not announced yet, so a
    # date-only value is the honest answer — the flyer omits an unknown time
    # rather than inventing midnight for it.
    local_time = start.get("localTime")
    if start.get("timeTBA") or start.get("noSpecificTime") or not local_time:
        return _dt(local_date)
    return _dt(f"{local_date}T{local_time}")


def _best_image(images: Any) -> Optional[str]:
    """The LARGEST image Ticketmaster offers, not the first one it lists.

    Ticketmaster ships the same artwork at a dozen sizes in no useful order, and
    taking images[0] meant taking whatever happened to be first — for Thee
    Sacred Souls that was a 305x203 ARTIST_PAGE thumbnail, while a 2048x1365
    SOURCE sat further down the same list. The thumbnail then failed
    imaging.py's quality gate (MIN_ABS_SIDE / MAX_UPSCALE), so the event lost
    its carousel slot for want of a photo the provider had all along.

    Ranked by pixel area, with a documented width/height fallback: a few entries
    omit the dimensions entirely, and those should lose to any sized candidate
    rather than sorting unpredictably among them.
    """
    if not isinstance(images, list):
        return None
    sized = [i for i in images if isinstance(i, dict) and i.get("url")]
    if not sized:
        return None

    def area(i: dict[str, Any]) -> int:
        try:
            return int(i.get("width") or 0) * int(i.get("height") or 0)
        except (TypeError, ValueError):
            return 0

    return max(sized, key=area).get("url")


def _coord(value: Any) -> Optional[float]:
    """Ticketmaster sends coordinates as strings; convert defensively."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


class TicketmasterSource(Source):
    name = "events_ticketmaster"
    kind = Kind.EVENTS

    def is_configured(self) -> bool:
        return bool(settings.ticketmaster_api_key)

    async def fetch(self, params: SearchParams, http: HttpClient) -> list[Event]:
        query: dict[str, Any] = {
            "apikey": settings.ticketmaster_api_key,
            "size": min(params.limit, 200),
            "sort": "date,asc",
        }
        if params.query:
            query["keyword"] = params.query
        if params.location:
            query["city"] = params.location.split(",")[0].strip()
        if params.start_date:
            query["startDateTime"] = datetime.combine(params.start_date, time.min).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            )
        if params.end_date:
            query["endDateTime"] = datetime.combine(params.end_date, time(23, 59, 59)).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            )

        data = await http.get_json(_ENDPOINT, params=query)
        events_raw = (data or {}).get("_embedded", {}).get("events", [])
        return [self._parse(e) for e in events_raw]

    def _parse(self, e: dict[str, Any]) -> Event:
        venues = (e.get("_embedded") or {}).get("venues") or []
        venue = venues[0] if venues else {}
        full_address = format_address(
            street=(venue.get("address") or {}).get("line1"),
            city=(venue.get("city") or {}).get("name"),
            region=(venue.get("state") or {}).get("stateCode"),
            postal=venue.get("postalCode"),
            country=(venue.get("country") or {}).get("countryCode"),
        )

        coords = venue.get("location")
        if not isinstance(coords, dict):
            coords = {}

        image_url = clean_image_url(_best_image(e.get("images")))

        categories = []
        for c in e.get("classifications") or []:
            for key in ("segment", "genre", "subGenre"):
                name = (c.get(key) or {}).get("name")
                if name and name.lower() != "undefined" and name not in categories:
                    categories.append(name)

        dates = e.get("dates") or {}
        title = e.get("name", "Untitled event")

        return Event(
            source=self.name,
            source_id=e.get("id"),
            title=title,
            description=e.get("info") or e.get("pleaseNote"),
            start_time=_start_datetime(dates),
            venue=venue.get("name"),
            location=full_address,
            lat=_coord(coords.get("latitude")),
            lng=_coord(coords.get("longitude")),
            url=e.get("url"),
            image_url=image_url,
            categories=categories or guess_categories(title),
            raw=e,
        )


SOURCE = TicketmasterSource()
