"""Shared full-address formatting for event sources.

Providers hand us address parts assembled differently — Ticketmaster keeps street/city/
state/postal/country cleanly separate, while some Eventbrite/Meetup listings repeat the
city and state *inside* the street line itself (e.g. streetAddress="2430 N. Mesa, El
Paso, TX"). A naive join would then duplicate that tail. This collapses everything into
one clean address string instead of each source reinventing that logic.
"""

from __future__ import annotations

from typing import Optional


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
