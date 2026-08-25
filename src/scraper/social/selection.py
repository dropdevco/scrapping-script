"""Pick which of today's events make the carousel.

On a normal day El Paso has far more qualifying events than the 9 slots
Instagram leaves after the cover — and the bulk of them are routine library
programming (storytimes, teen hangouts) that would make a dull post. This module
is the editorial layer: hard filters, then a score, then diversity caps.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any, Optional
from zoneinfo import ZoneInfo

from ..core.dedupe import _norm

# Category buckets come from core/categorize.py::_RULES, plus whatever a source
# supplies verbatim (Ticketmaster ships its own segment/genre names; the City of
# El Paso calendar prepends things like "Libraries"). Unknown strings get
# _DEFAULT_WEIGHT so a new source never silently scores zero.
#
# Note "Community" is also categorize.DEFAULT_CATEGORY — the no-match fallback —
# so weighting it low also weights *unclassified* events low. That's deliberate:
# an event whose title matched no rule at all is usually low-signal.
_CATEGORY_WEIGHTS: dict[str, float] = {
    "Music": 1.0,
    "Festivals": 1.0,
    "Food & Drink": 0.9,
    "Arts & Theatre": 0.85,
    "Sports": 0.7,
    "Tech": 0.6,
    "Family": 0.5,
    "Community": 0.25,
    "Libraries": 0.15,
}
_DEFAULT_WEIGHT = 0.5

# Trailing occurrence markers that differ between repeats of the same event:
# "Desert Bloomers 2026-08-05", "Yoga — Aug 5", "Workshop (Session 3)", "Quiz #4".
# No \b wrappers: a leading \b cannot match between a space and "#", which
# silently left "Quiz #4" unstripped. Each alternative is anchored enough on its
# own (an explicit "#", an ISO date shape, or a keyword) that a title merely
# ending in a number — "Route 66", "Boxfest XIX" — is never touched.
_DATE_TAIL = re.compile(
    r"\s*[-–—(]?\s*(?:"
    r"#\d+"
    r"|\d{4}-\d{2}-\d{2}"
    r"|(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+\d{1,2}(?:,?\s*\d{4})?"
    r"|(?:session|part|day|week|no\.?)\s*\d+"
    r")\)?\s*$",
    re.IGNORECASE,
)

_BOILERPLATE_TITLES = {"event", "events", "upcoming events", "untitled event", "calendar"}

# Public events essentially never start between midnight and 6am, so a slide
# whose time lands in that window is showing a timestamp we cannot vouch for and
# is rendered WITHOUT one — honest and unremarkable, versus "TODDLER STORYTIME ·
# 2:00 AM" on a public feed.
#
# This used to be the norm rather than the exception: sources handed us a naive
# local time that was persisted as if it were UTC, so a 10am storytime landed at
# 04:00 and a 5pm social at 11:00. That is fixed at the source now (see
# core/eventtime.py) and backfilled out of the stored rows, which leaves this
# guard doing what it was always meant to do — catching the occasional genuinely
# broken parse, not masking a systematic six-hour shift. It stays deliberately
# conservative: a wrong time on a public slide is worse than no time, and the
# events this region actually runs before 6am round to none.
_EARLIEST_PLAUSIBLE_HOUR = 6


def has_plausible_time(start_local: Optional[datetime]) -> bool:
    return start_local is not None and start_local.hour >= _EARLIEST_PLAUSIBLE_HOUR


@dataclass(frozen=True)
class Candidate:
    row: dict[str, Any]
    key: str
    score: float
    start_local: Optional[datetime]


def day_bounds(day: date, tz_name: str) -> tuple[str, str]:
    """Half-open [start, end) ISO bounds for one local calendar day.

    Returned as ISO strings because that's what PostgREST compares against.
    Using local midnight rather than UTC is the whole point: a 7pm El Paso show
    is stored as 01:00 UTC the *next* day, so a UTC-day query would file it
    under tomorrow and the carousel would be wrong every single evening.
    """
    tz = ZoneInfo(tz_name)
    start = datetime.combine(day, time(0, 0), tzinfo=tz)
    # Arithmetic on the aware datetime lands on the next local midnight even
    # across a DST boundary (El Paso transitions at 02:00, never at midnight),
    # so this window is 23h or 25h on those two days a year — which is correct.
    end = datetime.combine(day + timedelta(days=1), time(0, 0), tzinfo=tz)
    return start.isoformat(), end.isoformat()


def _strip_occurrence(title: str) -> str:
    prev = None
    out = title.strip()
    # Repeat: "Storytime — Aug 5, 2026 (Session 2)" has two stackable tails.
    while out and out != prev:
        prev = out
        out = _DATE_TAIL.sub("", out).strip(" -–—(#")
    return out or title.strip()


def venue_key(row: dict[str, Any]) -> str:
    """Which venue this row is at, by NAME rather than by venue_id.

    venue_id looks like the authoritative answer and is not one: venues are
    keyed on sha1(address | name), so the same building resolves to a different
    id for every source that punctuates its address differently. The Abraham
    Chavez Theatre currently holds three ids — "1 Civic Center Plaza, El Paso,
    TX 79901, US", "One Civic Center Plaza, El Paso, TX 79901" and "1 Civic
    Center Plaza, El Paso, TX 79901" — so keying on the id put the same concert
    on the carousel three times and defeated the per-venue diversity cap at the
    same time. The venue NAMES agree where the addresses do not.

    (The duplicate venue rows themselves are a storage-side problem and want a
    real address normalizer; this is the read-side defense so the carousel does
    not repeat itself while that stands.)
    """
    return _norm(row.get("venue") or "") or str(row.get("venue_id") or "") or _norm(
        str(row.get("location") or "")
    )


def dedupe_key(row: dict[str, Any]) -> str:
    """Stable identity for "the same recurring event", across occurrences.

    Recurring events are stored as one row per date with a fresh uuid each time,
    so event ids cannot answer "did we post this last week?". Keying on the
    occurrence-stripped title plus the venue can.
    """
    title = _strip_occurrence(str(row.get("title") or ""))
    return hashlib.sha1(f"{_norm(title)}|{venue_key(row)}".encode()).hexdigest()


def _categories(row: dict[str, Any]) -> list[str]:
    return [c for c in (row.get("categories") or []) if c]


def _category_weight(row: dict[str, Any]) -> float:
    cats = _categories(row)
    if not cats:
        return _DEFAULT_WEIGHT
    return max(_CATEGORY_WEIGHTS.get(c, _DEFAULT_WEIGHT) for c in cats)


def parse_start(row: dict[str, Any], tz_name: str) -> Optional[datetime]:
    raw = row.get("start_time")
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZoneInfo(tz_name))
    return dt.astimezone(ZoneInfo(tz_name))


def is_postable(row: dict[str, Any]) -> bool:
    """Cheap pre-filters, before any network work. The photo gate lives in the
    build step, since it costs a download."""
    title = str(row.get("title") or "").strip()
    if not title or _norm(title) in _BOILERPLATE_TITLES:
        return False
    return bool(row.get("start_time"))


def score_event(
    row: dict[str, Any],
    *,
    start_local: Optional[datetime],
    recurrence_count: int = 0,
    recently_posted: bool = False,
    image_short_side: Optional[int] = None,
) -> float:
    """Higher is more feed-worthy. Weights are tuned so a ticketed evening
    concert comfortably outranks a weekday-morning library storytime."""
    score = 3.0 * _category_weight(row)

    if row.get("ticket_links"):
        score += 1.5
    if start_local is not None and 17 <= start_local.hour <= 23:
        score += 1.0

    if image_short_side is not None:
        score += 1.0 if image_short_side >= 1080 else 0.4
    if len(str(row.get("description") or "")) >= 120:
        score += 0.8
    if row.get("venue_id"):
        score += 0.6

    # A thing that happens every weekday is background noise, not news.
    score -= 2.0 * min(1.0, recurrence_count / 10.0)
    if recently_posted:
        score -= 2.5
    return score


def choose(
    rows: list[dict[str, Any]],
    *,
    tz_name: str,
    recent_keys: Optional[set[str]] = None,
    max_slides: int = 9,
    max_per_venue: int = 2,
    max_per_category: int = 3,
    image_sizes: Optional[dict[str, int]] = None,
) -> list[Candidate]:
    """Rank, collapse repeats, apply diversity caps, return in TIME order.

    Chronological output is deliberate: a carousel that reads 9am -> 10pm is a
    schedule someone can act on, whereas score order is a ranked list nobody
    asked for.
    """
    recent_keys = recent_keys or set()
    image_sizes = image_sizes or {}

    # How often each recurring thing shows up in the pool tells us whether it's
    # a one-off or wallpaper, without another database round trip.
    recurrence: dict[str, int] = {}
    for row in rows:
        if is_postable(row):
            recurrence[dedupe_key(row)] = recurrence.get(dedupe_key(row), 0) + 1

    scored: list[Candidate] = []
    seen_keys: set[str] = set()
    for row in rows:
        if not is_postable(row):
            continue
        key = dedupe_key(row)
        if key in seen_keys:
            continue  # keep only the first occurrence of a repeat within the day
        seen_keys.add(key)
        start_local = parse_start(row, tz_name)
        scored.append(
            Candidate(
                row=row,
                key=key,
                score=score_event(
                    row,
                    start_local=start_local,
                    recurrence_count=recurrence.get(key, 0),
                    recently_posted=key in recent_keys,
                    image_short_side=image_sizes.get(str(row.get("id"))),
                ),
                start_local=start_local,
            )
        )

    scored.sort(key=lambda c: (-c.score, c.start_local or datetime.max.replace(tzinfo=None)))

    picked: list[Candidate] = []
    venue_counts: dict[str, int] = {}
    category_counts: dict[str, int] = {}
    for cand in scored:
        if len(picked) >= max_slides:
            break
        venue = venue_key(cand.row) or "?"
        if venue != "?" and venue_counts.get(venue, 0) >= max_per_venue:
            continue
        cats = _categories(cand.row)
        if cats and all(category_counts.get(c, 0) >= max_per_category for c in cats):
            continue
        picked.append(cand)
        venue_counts[venue] = venue_counts.get(venue, 0) + 1
        for c in cats:
            category_counts[c] = category_counts.get(c, 0) + 1

    picked.sort(key=lambda c: c.start_local or datetime.max.replace(tzinfo=ZoneInfo(tz_name)))
    return picked
