"""Events from the Ticketmaster Discovery API (free API key, ~5k calls/day)."""

from __future__ import annotations

from datetime import datetime, time
from typing import Any, Optional

from ..core.address import format_address
from ..core.config import settings
from ..core.http import HttpClient
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

        images = e.get("images") or []
        image_url = images[0].get("url") if images else None

        categories = []
        for c in e.get("classifications") or []:
            for key in ("segment", "genre", "subGenre"):
                name = (c.get(key) or {}).get("name")
                if name and name.lower() != "undefined" and name not in categories:
                    categories.append(name)

        dates = e.get("dates") or {}
        start = (dates.get("start") or {}).get("dateTime") or (dates.get("start") or {}).get("localDate")

        return Event(
            source=self.name,
            source_id=e.get("id"),
            title=e.get("name", "Untitled event"),
            description=e.get("info") or e.get("pleaseNote"),
            start_time=_dt(start),
            venue=venue.get("name"),
            location=full_address,
            lat=_coord(coords.get("latitude")),
            lng=_coord(coords.get("longitude")),
            url=e.get("url"),
            image_url=image_url,
            categories=categories,
            raw=e,
        )


SOURCE = TicketmasterSource()
