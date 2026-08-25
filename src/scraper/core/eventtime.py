"""One rule for event timestamps: they are always local, always aware.

Almost every event listing on the open web publishes a *wall-clock* time with no
offset — "Aug 26, 2026 8:00 PM" on El Paso Live, ``"startDate": "2026-08-25T18:30"``
in Visit El Paso's JSON-LD. Parsing those with ``datetime.strptime`` /
``fromisoformat`` yields a NAIVE datetime, and a naive datetime written to a
``timestamptz`` column is read by Postgres as UTC. An 8:00 PM show was therefore
stored as 20:00Z and rendered back as 2:00 PM — six hours early, every time. That
is where the carousel's implausible cluster of morning slides came from: a 5 PM
event surfaced as "11:00 AM".

The other half of the problem is the opposite: Ticketmaster hands us a genuinely
absolute UTC instant, so ``start_time.date()`` on one of its evening shows is
*tomorrow's* date. Mixing the two conventions made the same concert scraped from
two sources look like two different events on two different days, which defeated
dedupe and put both on the same carousel.

So the invariant enforced here, applied centrally in ``orchestrator.run`` so no
source can quietly regress it:

    ``Event.start_time`` / ``end_time`` are timezone-aware and expressed in the
    event's local timezone.

Aware means the absolute instant is right (Postgres stores it correctly, the
offset travels with the value). Local means ``.date()`` and ``.hour`` are the
local calendar day and the local wall-clock hour — what dedupe keys on, what the
day-bounds query filters by, and what the flyer prints.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

from .config import settings


def event_tz() -> ZoneInfo:
    """Timezone the scraped region keeps its wall clocks in.

    El Paso and Ciudad Juárez are both Mountain and (since Mexico dropped
    nationwide DST in 2022, with Juárez explicitly exempted to stay aligned with
    El Paso) observe the same transitions, so a single zone covers both cities.
    """
    return ZoneInfo(settings.event_timezone)


def to_event_local(dt: Optional[datetime]) -> Optional[datetime]:
    """Normalize one timestamp to the invariant above.

    Naive input is read as local wall-clock — the right reading for a scraped
    listing, which quotes the time a person standing at the venue would see.
    Aware input keeps its instant and is merely re-expressed locally.
    """
    if dt is None:
        return None
    tz = event_tz()
    if dt.tzinfo is None:
        return dt.replace(tzinfo=tz)
    return dt.astimezone(tz)


def local_day(value: datetime | str | None) -> Optional[datetime]:
    """Parse a stored/ISO timestamp and re-express it locally, or None.

    Used wherever a persisted ``start_time`` (UTC, per Postgres) has to be
    compared against a freshly scraped one on "same calendar day" terms.
    """
    if value is None:
        return None
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return to_event_local(value)
