"""Human-friendly labels for ticket/RSVP links, derived from the URL's domain.

The domain is a far better signal than the internal scraper source name: a
single source module (``events_web``) fetches Eventbrite, Meetup, Visit El
Paso, and La Nube alike, so labeling by source would show "events_web" for
four different real ticketing platforms.
"""

from __future__ import annotations

import re
from typing import Optional
from urllib.parse import urlparse

# host substring -> display label, longest/most-specific keys first so a host
# like "ventas.donboleton.com" matches its own entry before a generic one.
_DOMAIN_LABELS: tuple[tuple[str, str], ...] = (
    ("ticketmaster.com.mx", "Ticketmaster México"),
    ("ticketmaster.com", "Ticketmaster"),
    ("eventbrite.com", "Eventbrite"),
    ("meetup.com", "Meetup"),
    ("axs.com", "AXS"),
    ("donboleton.com", "Don Boletón"),
    ("boletia.com", "Boletia"),
    ("visitelpaso.com", "Visit El Paso"),
    ("elpasolive.com", "El Paso Live"),
    ("elpasotexas.gov", "City of El Paso"),
    ("epcounty.com", "El Paso County"),
    ("southwestuniversitypark.com", "Southwest University Park"),
    ("utep.edu", "UTEP"),
    ("lowbrowpalace.com", "Lowbrow Palace"),
    ("elpasocoliseum.com", "El Paso County Coliseum"),
    ("rockhousebarandgrill.com", "RockHouse"),
    ("visitajuarez.mx", "Visita Juárez"),
    ("cultura.chihuahua.gob.mx", "Cultura Chihuahua"),
    ("juarez.gob.mx", "Gobierno de Juárez"),
    ("uacj.mx", "UACJ"),
    ("la-nube.org", "La Nube"),
)


def _prettify_host(host: str) -> str:
    """Fallback for an unmapped domain: 'example-site.com' -> 'Example Site'."""
    name = host.removeprefix("www.").split(".")[0]
    words = re.split(r"[-_]+", name)
    return " ".join(w.capitalize() for w in words if w) or host


def ticket_label(url: Optional[str]) -> str:
    """Best-effort display label for a ticket/event URL's platform."""
    if not url:
        return "Tickets"
    try:
        host = (urlparse(url).netloc or "").lower().removeprefix("www.")
    except ValueError:
        return "Tickets"
    if not host:
        return "Tickets"
    for needle, label in _DOMAIN_LABELS:
        if needle in host:
            return label
    return _prettify_host(host)
