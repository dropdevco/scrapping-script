"""Turn stored event rows into knowledge-base rows.

Two rules drive everything here:

  * **One row is one retrievable chunk.** GHL embeds rows, not columns, so a row
    of sparse cells ("Music", "8:00 PM", "Don Haskins Center") retrieves badly —
    there is no sentence for a question to match against. Every row therefore
    carries a ``content`` cell written as prose that stands on its own, with the
    structured cells alongside it as metadata rather than as the payload.

  * **No relative dates, ever.** "This weekend" and "in 3 days" are true at
    export time and false by the time anyone asks. Rows carry absolute weekday +
    date + time, and a ``data_current_as_of`` cell so the bot can date itself.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from ..core.eventtime import local_day
from ..social.selection import has_plausible_time

# Column order IS the sheet's header row. Append-only: GHL's importer maps by
# position for an existing import, so reordering silently rewrites every field.
HEADERS = [
    "content",
    "title",
    "starts_at",
    "date",
    "time",
    "weekday",
    "venue",
    "address",
    "categories",
    "tickets_url",
    "more_info_url",
    "data_current_as_of",
    "event_id",
]

# Chisme is bilingual, so each row carries both languages. One row rather than
# two: an ES question still retrieves the row on the ES half, and it keeps the
# row count (and the import) half the size.
_ES_MONTHS = [
    "enero", "febrero", "marzo", "abril", "mayo", "junio",
    "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre",
]
_ES_WEEKDAYS = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]

# Titles that carry no information about an actual event. These come from
# listing pages whose own header got scraped as a row.
_BOILERPLATE = {"event", "events", "upcoming events", "untitled event", "calendar"}

_DESCRIPTION_CHARS = 400

# GHL's importer rejects a row with ANY empty cell ("Required field cannot be
# null or empty"), so a blank breaks the whole knowledge base rather than
# degrading one field. Every cell therefore falls back to this. It reads as an
# honest answer if the bot ever quotes it verbatim, which "N/A" or "-" would
# not — and it must never be confused for a real value.
NOT_LISTED = "Not listed"


def _clean(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _en_date(dt: datetime) -> str:
    # %-d / %-I are not portable to Windows, where this is often run by hand.
    return f"{dt.strftime('%A')}, {dt.strftime('%B')} {dt.day}, {dt.year}"


def _es_date(dt: datetime) -> str:
    weekday = _ES_WEEKDAYS[dt.weekday()]
    return f"{weekday}, {dt.day} de {_ES_MONTHS[dt.month - 1]} de {dt.year}"


def _clock(dt: datetime) -> str:
    hour = dt.hour % 12 or 12
    return f"{hour}:{dt.minute:02d} {'AM' if dt.hour < 12 else 'PM'}"


def _ticket_url(row: dict[str, Any]) -> Optional[str]:
    links = row.get("ticket_links") or []
    if isinstance(links, list):
        for link in links:
            if isinstance(link, dict) and _clean(link.get("url")):
                return _clean(link["url"])
    return _clean(row.get("url"))


def _place(row: dict[str, Any]) -> tuple[Optional[str], Optional[str]]:
    """Venue name and street address, preferring the joined venues row.

    events.venue/location are whatever the source published; venues.* has been
    through geocoding and address cleanup, so it wins when the join is present.
    """
    venue_row = row.get("venues")
    if isinstance(venue_row, dict):
        return (
            _clean(venue_row.get("name")) or _clean(row.get("venue")),
            _clean(venue_row.get("address")) or _clean(row.get("location")),
        )
    return _clean(row.get("venue")), _clean(row.get("location"))


def _categories(row: dict[str, Any]) -> list[str]:
    raw = row.get("categories") or []
    if not isinstance(raw, list):
        return []
    return [c for c in (_clean(x) for x in raw) if c]


def _truncate(text: str, limit: int = _DESCRIPTION_CHARS) -> str:
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    return text[: limit - 1].rsplit(" ", 1)[0] + "…"


def is_exportable(row: dict[str, Any]) -> bool:
    """Whether this stored row belongs in the knowledge base at all."""
    title = _clean(row.get("title"))
    if not title or title.lower() in _BOILERPLATE:
        return False
    return local_day(row.get("start_time")) is not None


def build_content(row: dict[str, Any], start_local: datetime, show_time: bool) -> str:
    """The prose cell — the only part of the row that gets embedded well."""
    title = _clean(row.get("title")) or "Event"
    venue, address = _place(row)
    where = ", ".join(p for p in (venue, address) if p)
    cats = _categories(row)
    tickets = _ticket_url(row)
    more = _clean(row.get("more_info_url"))

    en = f"{title} — {_en_date(start_local)}"
    es = f"{title} — {_es_date(start_local)}"
    if show_time:
        en += f" at {_clock(start_local)}"
        es += f" a las {_clock(start_local)}"
    if where:
        en += f", at {where}"
        es += f", en {where}"
    en += "."
    es += "."

    if cats:
        en += f" Category: {', '.join(cats)}."
        es += f" Categoría: {', '.join(cats)}."

    description = _clean(row.get("description"))
    if description:
        en += f" {_truncate(description)}"

    if tickets:
        en += f" Tickets: {tickets}"
        es += f" Boletos: {tickets}"
    if more:
        en += f" More info: {more}"
        es += f" Más información: {more}"

    return f"{en}\nES: {es}"


def build_row(
    row: dict[str, Any],
    *,
    site_base_url: str,
    generated_on: str,
) -> Optional[list[str]]:
    """One stored event -> one sheet row, or None if it doesn't belong."""
    if not is_exportable(row):
        return None

    start_local = local_day(row.get("start_time"))
    assert start_local is not None  # guaranteed by is_exportable

    # Public events essentially never start before 6am, so a time in that window
    # is a parse we cannot vouch for. Same call the carousel makes: an omitted
    # time is unremarkable, a wrong one gets repeated back to a customer.
    show_time = has_plausible_time(start_local)

    venue, address = _place(row)
    event_id = _clean(row.get("id"))
    more_info = f"{site_base_url.rstrip('/')}/events/{event_id}" if event_id else None
    row = {**row, "more_info_url": more_info}

    cells = [
        build_content(row, start_local, show_time),
        _clean(row.get("title")) or "",
        start_local.isoformat(),
        start_local.strftime("%Y-%m-%d"),
        _clock(start_local) if show_time else "",
        start_local.strftime("%A"),
        venue or "",
        address or "",
        ", ".join(_categories(row)),
        _ticket_url(row) or "",
        more_info or "",
        generated_on,
        event_id or "",
    ]
    # content is never empty (it always has at least a title and a date), so
    # this only ever fills the metadata columns.
    return [c if c.strip() else NOT_LISTED for c in cells]


def build_sheet(
    rows: list[dict[str, Any]],
    *,
    site_base_url: str,
    generated_on: str,
) -> list[list[str]]:
    """Header + one row per exportable event, ready to write."""
    out = [list(HEADERS)]
    for row in rows:
        built = build_row(row, site_base_url=site_base_url, generated_on=generated_on)
        if built is not None:
            out.append(built)
    return out
