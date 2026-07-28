"""Address -> (lat, lng) geocoding for venues.

Most event sources (notably the HTML calendar scrapers in ``events_web``) give a
venue name and a human-typed address but no coordinates. Without coordinates a
venue can never appear on the map, so the site ends up showing far fewer events
on /map than in the list. This module fills that gap.

Backend is Nominatim (OpenStreetMap): keyless and free, but it demands a real
identifying User-Agent and at most 1 request/second. Both are honoured here.

Two safeguards keep bad data out of the map:

* Scraped addresses are messy ("The Hoppy Monk', 4141 N Mesa St, El Paso, TX
  79902, El Paso, TX, us"), so each address is cleaned and then tried as a
  series of progressively simpler queries until one resolves.
* Every hit is checked against a border-region bounding box. Sources carry the
  occasional out-of-area listing ("Boston Career Fair"), and a confident wrong
  pin is worse than no pin.
"""

from __future__ import annotations

import logging
import re
import time
from typing import Iterable, Optional

import httpx

from .config import settings

log = logging.getLogger("scraper.geocode")

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"

# El Paso / Ciudad Juárez / Las Cruces catchment. Anything outside is rejected.
BBOX = {"min_lat": 30.9, "max_lat": 32.6, "min_lng": -107.5, "max_lng": -105.6}

_MIN_INTERVAL = 1.1  # seconds between Nominatim calls (their policy is 1 req/s)
_last_call = 0.0

# Process-local memo so repeated venues in one run cost a single call.
_cache: dict[str, Optional[tuple[float, float]]] = {}


def in_region(lat: float, lng: float) -> bool:
    return (
        BBOX["min_lat"] <= lat <= BBOX["max_lat"]
        and BBOX["min_lng"] <= lng <= BBOX["max_lng"]
    )


# Online/virtual events carry a nominal city ("Virtual via Zoom, El Paso, TX")
# but no physical place. They must never get a pin.
VIRTUAL_RE = re.compile(
    r"\b(virtual|online|zoom|google\s*meet|ms\s*teams|teams\s*meeting|webinar|"
    r"livestream|live\s*stream|web\s*conference|link\s+(will\s+be\s+)?sent|"
    r"link\s+in\s+the\s+description)\b",
    re.I,
)

# Tokens that carry no street-level information on their own.
_PLACE_TOKENS = {
    "el paso", "elpaso", "tx", "tx.", "texas", "us", "usa", "u.s.", "united states",
    "juarez", "juárez", "ciudad juarez", "ciudad juárez", "cd juarez", "cd juárez",
    "chihuahua", "chih", "mx", "mex", "mexico", "méxico",
    "nm", "n.m.", "new mexico", "las cruces", "sunland park", "canutillo",
    "socorro", "san elizario", "horizon city", "anthony", "nr",
}

_ZIP_RE = re.compile(r"\b\d{5}(-\d{4})?\b")


def is_virtual(*values: Optional[str]) -> bool:
    return any(VIRTUAL_RE.search(v) for v in values if v)


def is_city_only(addr: str) -> bool:
    """True when an address carries nothing more specific than a city/state/zip.

    Geocoding "El Paso, TX" succeeds — it returns the city centroid — which would
    scatter unrelated venues onto one bogus downtown pin. Such addresses must be
    rejected so the venue name is used instead (or nothing at all).
    """
    meaningful = []
    for part in addr.split(","):
        p = _ZIP_RE.sub("", part).strip().strip(".").lower()
        p = re.sub(r"\s+", " ", p)
        if not p or p in _PLACE_TOKENS:
            continue
        meaningful.append(p)
    return not meaningful


def clean_address(raw: str) -> str:
    """Normalize a scraped address into something a geocoder can parse."""
    s = raw.strip()
    s = s.replace("’", "'").replace("\xa0", " ")
    # "Venue Name - 240 W Castellano, ..." / "Venue Name', 4141 N Mesa St, ..."
    s = re.sub(r"^[^,]{0,60}?'\s*,\s*", "", s)
    s = re.sub(r"^[^,]{0,60}?\s+-\s+(?=\d)", "", s)
    s = re.sub(r"\s*,\s*", ", ", s)
    s = re.sub(r"\s+", " ", s)
    s = re.sub(r",\s*(us|usa|mx|mex)\s*$", "", s, flags=re.I)
    s = re.sub(r"[,\s]+$", "", s)
    return s.strip()


def _candidates(address: Optional[str], venue: Optional[str]) -> list[str]:
    """Progressively simpler queries, most specific first.

    Returns [] for anything that shouldn't be on a map at all (online events) or
    that could only resolve to a city centroid.
    """
    if is_virtual(address, venue):
        return []

    out: list[str] = []

    def add(q: Optional[str]) -> None:
        if not q:
            return
        q = re.sub(r"\s+", " ", q).strip(" ,")
        if len(q) > 5 and q.lower() not in {c.lower() for c in out}:
            out.append(q)

    addr = clean_address(address) if address else ""

    # A city-only address ("El Paso, TX") geocodes happily to the city centroid,
    # so it is dropped entirely — the venue name is the only usable signal.
    if addr and not is_city_only(addr):
        add(addr)
        # Drop suite/room noise that often defeats a match.
        add(re.sub(r"\b(suite|ste\.?|bldg\.?|building|room|rm\.?|#)\s*[\w-]+,?", "", addr, flags=re.I))
        # "Landmark Name / 10780 Pebble Hills, El Paso, TX" — keep the segment
        # that actually starts with a street number.
        if "/" in addr:
            head, _, tail = addr.partition("/")
            numbered = tail if re.match(r"\s*\d", tail) else head if re.match(r"\s*\d", head) else ""
            if numbered.strip():
                rest = [p.strip() for p in addr.split(",")[1:] if p.strip()]
                add(", ".join([numbered.strip().split(",")[0]] + rest))
        parts = [p.strip() for p in addr.split(",") if p.strip()]
        # street + city + state (skip trailing country fragments)
        if len(parts) >= 3:
            add(", ".join(parts[:3]))
        if len(parts) >= 2:
            add(", ".join(parts[:2]))

    # The venue's own name, anchored to the region. This is the primary signal
    # when the address is city-only, and a fallback otherwise.
    if venue:
        v = venue.strip()
        # Some "venue" values are sentences ("All tours meet at ..."), and some are
        # just a city name — neither identifies a place.
        if 3 < len(v) <= 60 and not is_city_only(v):
            city = "Ciudad Juárez" if re.search(r"ju[aá]rez|chih", addr, re.I) else "El Paso, TX"
            add(f"{v}, {city}")

    return out


def _throttle() -> None:
    global _last_call
    wait = _MIN_INTERVAL - (time.monotonic() - _last_call)
    if wait > 0:
        time.sleep(wait)
    _last_call = time.monotonic()


def _query(client: httpx.Client, q: str) -> Optional[tuple[float, float]]:
    _throttle()
    try:
        r = client.get(
            NOMINATIM_URL,
            params={
                "q": q,
                "format": "json",
                "limit": 3,
                # Bias/limit results to the border region.
                "viewbox": f"{BBOX['min_lng']},{BBOX['max_lat']},{BBOX['max_lng']},{BBOX['min_lat']}",
                "bounded": 1,
                "countrycodes": "us,mx",
            },
        )
        r.raise_for_status()
        for hit in r.json() or []:
            try:
                lat, lng = float(hit["lat"]), float(hit["lon"])
            except (KeyError, TypeError, ValueError):
                continue
            if in_region(lat, lng):
                return lat, lng
    except Exception as exc:  # noqa: BLE001 - geocoding must never break a scrape
        log.warning("geocode request failed for %r: %s", q, exc)
    return None


def geocode(
    address: Optional[str],
    venue: Optional[str] = None,
    client: Optional[httpx.Client] = None,
) -> Optional[tuple[float, float]]:
    """Best-effort (lat, lng) for a venue. Returns None when nothing resolves
    inside the border region."""
    queries = _candidates(address, venue)
    if not queries:
        return None

    # Keyed on the full (address, venue) pair: a hit can come from any candidate
    # in the chain, including the name-anchored one, so keying on the address
    # alone could hand one venue's coordinates to a different venue.
    key = f"{(address or '').strip().lower()}|{(venue or '').strip().lower()}"
    if key in _cache:
        return _cache[key]

    owns_client = client is None
    client = client or httpx.Client(
        timeout=settings.http_timeout_seconds,
        headers={"User-Agent": settings.user_agent, "Accept-Language": "en"},
    )
    try:
        for q in queries:
            hit = _query(client, q)
            if hit:
                _cache[key] = hit
                log.info("geocoded %r -> %s", q, hit)
                return hit
    finally:
        if owns_client:
            client.close()

    log.info("no geocode match for %r / %r", venue, address)
    _cache[key] = None
    return None


def geocode_many(
    rows: Iterable[tuple[Optional[str], Optional[str]]],
) -> list[Optional[tuple[float, float]]]:
    """Geocode a batch over one shared, rate-limited connection."""
    with httpx.Client(
        timeout=settings.http_timeout_seconds,
        headers={"User-Agent": settings.user_agent, "Accept-Language": "en"},
    ) as client:
        return [geocode(addr, venue, client=client) for addr, venue in rows]
