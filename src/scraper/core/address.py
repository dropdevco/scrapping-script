"""Shared full-address formatting and venue identity for event sources.

Providers hand us address parts assembled differently — Ticketmaster keeps street/city/
state/postal/country cleanly separate, while some Eventbrite/Meetup listings repeat the
city and state *inside* the street line itself (e.g. streetAddress="2430 N. Mesa, El
Paso, TX"). A naive join would then duplicate that tail. This collapses everything into
one clean address string instead of each source reinventing that logic.

The second half of this module is venue IDENTITY: deciding when two differently-spelled
addresses are the same building. Measured 2026-09-17, 85 of 439 venue rows were redundant
— the same place under several ids because ``address_hash`` was sha1 over the raw strings,
so every difference in punctuation forked a new venue. That fragmentation then defeated
the cross-run event merge, which matches on exact ``venue_id``, and shipped duplicate
events to the carousel.

Keep ``normalize_address`` portable: web/src/lib/hash.ts carries a line-for-line port
because the public submission form resolves a venue by this same hash, and the two
implementations drifting apart would re-fork every user-submitted venue.
"""

from __future__ import annotations

import hashlib
import re
from typing import Optional

from .dedupe import fold


def format_address(
    street: Optional[str],
    city: Optional[str] = None,
    region: Optional[str] = None,
    postal: Optional[str] = None,
    country: Optional[str] = None,
) -> Optional[str]:
    parts: list[str] = []
    if street:
        parts.append(street.strip())

    tail = ", ".join(p for p in (city, region) if p)
    if postal:
        tail = f"{tail} {postal}".strip()

    street_lower = (street or "").lower()
    if tail and tail.lower() not in street_lower:
        parts.append(tail)

    if country and not any(country.lower() in p.lower() for p in parts):
        parts.append(country)

    return ", ".join(parts) or None


# ── venue identity ─────────────────────────────────────────────────────────────

# Trailing country tokens. Ticketmaster appends "US" where the venue's own site
# does not, which alone forked several El Paso venues.
_COUNTRY_TAIL = ("us", "usa", "united states", "mx", "mex", "mexico")

# Only applied to the FIRST token: "One Civic Center Plaza" and "1 Civic Center
# Plaza" are the Abraham Chavez Theatre, which held three venue ids. Mid-address
# number words ("Two Bridges Road") are left alone.
_NUMBER_WORDS = {
    "one": "1", "two": "2", "three": "3", "four": "4", "five": "5",
    "six": "6", "seven": "7", "eight": "8", "nine": "9", "ten": "10",
}

# Street types and directionals, folded to one spelling each.
_ABBREVIATIONS = {
    "street": "st", "str": "st",
    "avenue": "ave", "av": "ave",
    "boulevard": "blvd",
    "drive": "dr",
    "road": "rd",
    "place": "pl",
    "court": "ct",
    "lane": "ln",
    "parkway": "pkwy",
    "highway": "hwy",
    "circle": "cir",
    "north": "n", "south": "s", "east": "e", "west": "w",
    "northeast": "ne", "northwest": "nw", "southeast": "se", "southwest": "sw",
    # Suite markers are CANONICALIZED, never dropped: two tenants in one building
    # must stay two venues. Over-merging here would conflate them.
    "suite": "ste", "unit": "ste", "apt": "ste", "apartment": "ste",
}

_ZIP_PLUS_FOUR = re.compile(r"\b(\d{5})-\d{4}\b")


def _fold(text: Optional[str]) -> str:
    """Address-specific folding on top of the shared text fold.

    Both steps here have to happen BEFORE the generic fold strips punctuation:
    "#12" is a suite number and loses its meaning once the sigil is gone, and
    the ZIP+4 pattern needs its hyphen to be recognisable as one.
    """
    pre = (text or "").replace("#", " ste ")
    return fold(_ZIP_PLUS_FOUR.sub(r"\1", pre))


def normalize_address(address: Optional[str]) -> str:
    """One spelling per building, for use as an identity key."""
    tokens = _fold(address).split()
    if not tokens:
        return ""

    # Drop a trailing country, including the two-word "united states".
    if len(tokens) >= 2 and " ".join(tokens[-2:]) in _COUNTRY_TAIL:
        tokens = tokens[:-2]
    elif tokens and tokens[-1] in _COUNTRY_TAIL:
        tokens = tokens[:-1]

    if tokens and tokens[0] in _NUMBER_WORDS:
        tokens[0] = _NUMBER_WORDS[tokens[0]]

    return " ".join(_ABBREVIATIONS.get(t, t) for t in tokens)


def normalize_venue_name(name: Optional[str]) -> str:
    """Venue name folded for identity. A leading article is dropped so
    "The Plaza Theatre" and "Plaza Theatre" agree; nothing else is removed,
    because "Plaza Theatre" and "Plaza" are not obviously the same room."""
    folded = _fold(name)
    return folded[4:] if folded.startswith("the ") else folded


def venue_identity(address: Optional[str], venue_name: Optional[str]) -> str:
    """The venues.address_hash natural key."""
    key = f"{normalize_address(address)}|{normalize_venue_name(venue_name)}"
    return hashlib.sha1(key.encode("utf-8")).hexdigest()
