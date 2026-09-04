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



@dataclass(frozen=True)
class ScoreProfile:
    """Editorial weights for one post format.

    A daily digest and a six-months-out roundup are looking for different
    things: "what's on tonight" wants variety and evening starts, while "get
    tickets now" wants the handful of events big enough to have gone on sale
    already. Same scorer, different weights.

    The defaults are EXACTLY the values score_event/choose used before this
    existed, so the digest profile is a behavioural no-op.
    """

    category_multiplier: float = 3.0
    ticket_bonus: float = 1.5
    evening_bonus: float = 1.0
    recurrence_penalty: float = 2.0
    recently_posted_penalty: float = 2.5
    # Horizon only: "a big event six months out" is, in this data, simply "an
    # event with a ticket link". Nothing else in the schema separates a
    # stadium show from a weekly open mic that happens to be scheduled far
    # ahead, and without this the six-month post fills with the latter.
    require_ticket_links: bool = False
    max_per_venue: int = 2
    max_per_category: int = 3


PROFILES: dict[str, ScoreProfile] = {
    # Byte-for-byte today's behaviour.
    "digest": ScoreProfile(),
    "breaking": ScoreProfile(),
    # A weekend is browsed, not scanned: spread it across more venues and lean
    # harder into evening things.
    "weekend": ScoreProfile(evening_bonus=1.5, max_per_venue=1),
    # A month of events is mostly recurring noise; only the ticketed ones are
    # worth planning around.
    "monthly": ScoreProfile(
        ticket_bonus=3.0, recurrence_penalty=4.0, max_per_venue=1, max_per_category=2
    ),
    # Far-future: ticketed or nothing, and recurrence is fatal.
    #
    # Diversity caps are deliberately NOT tightened here, unlike weekend and
    # monthly. Six months out, the only events on sale are at the handful of
    # venues big enough to sell that far ahead: measured 2026-09-03, all 30
    # events in the window were ticketed but spread across just four venues,
    # so max_per_venue=1 capped the post at four slides — the bare minimum,
    # one bad day away from skipping entirely. require_ticket_links is already
    # doing the quality filtering; a venue cap on top only starves it.
    "horizon": ScoreProfile(
        ticket_bonus=4.0,
        recurrence_penalty=5.0,
        recently_posted_penalty=0.0,
        require_ticket_links=True,
    ),
}

DEFAULT_PROFILE = PROFILES["digest"]


def candidates_from_rows(rows: list[dict[str, Any]], tz_name: str) -> list[Candidate]:
    """Wrap already-chosen rows as Candidates, preserving the given order.

    `choose` ranks and filters; a rebuild must do neither. The post's slide
    order was settled when it was first built and a human has since reviewed
    it, so re-scoring here would silently reshuffle slides they already
    approved. Score is left at 0.0 — nothing downstream of a rebuild reads it.
    """
    return [
        Candidate(
            row=row,
            key=dedupe_key(row),
            score=0.0,
            start_local=parse_start(row, tz_name),
        )
        for row in rows
    ]


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



def _local_midnight(day: date, tz_name: str) -> datetime:
    return datetime.combine(day, time(0, 0), tzinfo=ZoneInfo(tz_name))


def _iso_pair(start: datetime, end: datetime) -> tuple[str, str]:
    return start.isoformat(), end.isoformat()


def weekend_bounds(day: date, tz_name: str) -> tuple[str, str]:
    """Friday 00:00 -> Monday 00:00 local, for the weekend on or after `day`.

    "On or after" so a Thursday build covers the weekend about to start, which
    is when the post is useful; running it on the Saturday would cover the
    weekend already half over.
    """
    ahead = (4 - day.weekday()) % 7  # 4 = Friday
    friday = day + timedelta(days=ahead)
    start = _local_midnight(friday, tz_name)
    end = _local_midnight(friday + timedelta(days=3), tz_name)
    return _iso_pair(start, end)


def month_bounds(day: date, tz_name: str) -> tuple[str, str]:
    """`day`'s own local calendar month, half-open."""
    first = day.replace(day=1)
    nxt = (first + timedelta(days=32)).replace(day=1)
    return _iso_pair(_local_midnight(first, tz_name), _local_midnight(nxt, tz_name))


def horizon_bounds(
    day: date, tz_name: str, *, months_out: int = 6, span_days: int = 60
) -> tuple[str, str]:
    """A window centred `months_out` ahead — the "book this now" post.

    Approximated as 30-day months on purpose: the exact boundary carries no
    editorial meaning (nobody cares whether a show falls on the 5th or the 8th
    of the target month), and calendar-exact arithmetic here would only add a
    dependency and an edge case at year end.
    """
    start_day = day + timedelta(days=30 * months_out)
    return _iso_pair(
        _local_midnight(start_day, tz_name),
        _local_midnight(start_day + timedelta(days=span_days), tz_name),
    )


# Every entry returns the same (start_iso, end_iso) shape, so the builder can
# look one up by kind and stay ignorant of which format it is rendering.
BOUNDS_FOR_KIND = {
    "digest": day_bounds,
    "breaking": day_bounds,
    "weekend": weekend_bounds,
    "monthly": month_bounds,
    "horizon": horizon_bounds,
}


def period_key(kind: str, day: date, tz_name: str) -> Optional[str]:
    """The bucket a non-daily post occupies, enforcing one-per-period.

    Derived from the WINDOW, not from post_date: a weekend digest built on
    Thursday belongs to that weekend's bucket, not to Thursday's.
    """
    if kind in ("digest", "breaking"):
        return None  # the daily post has its own (post_date, slot) index
    start_iso, _ = BOUNDS_FOR_KIND[kind](day, tz_name)
    start = datetime.fromisoformat(start_iso).date()
    if kind == "weekend":
        year, week, _ = start.isocalendar()
        return f"{year}-W{week:02d}"
    return f"{start.year}-{start.month:02d}"


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
    profile: Optional[ScoreProfile] = None,
    category_bias: Optional[dict[str, float]] = None,
) -> float:
    """Higher is more feed-worthy. Weights are tuned so a ticketed evening
    concert comfortably outranks a weekday-morning library storytime.

    `category_bias` is the seam for feeding engagement data back into
    selection. Deliberately unused for now: a carousel is one media object, so
    Instagram reports no per-slide performance, and crediting a post's reach to
    one of nine categories is a guess. Revisit once there are >=60 published
    posts with t24 metrics — before that, any weighting fits noise.
    """
    p = profile or DEFAULT_PROFILE
    weight = _category_weight(row)
    if category_bias:
        weight *= max(category_bias.get(c, 1.0) for c in _categories(row)) if _categories(row) else 1.0
    score = p.category_multiplier * weight

    if row.get("ticket_links"):
        score += p.ticket_bonus
    if start_local is not None and 17 <= start_local.hour <= 23:
        score += p.evening_bonus

    if image_short_side is not None:
        score += 1.0 if image_short_side >= 1080 else 0.4
    if len(str(row.get("description") or "")) >= 120:
        score += 0.8
    if row.get("venue_id"):
        score += 0.6

    # A thing that happens every weekday is background noise, not news.
    score -= p.recurrence_penalty * min(1.0, recurrence_count / 10.0)
    if recently_posted:
        score -= p.recently_posted_penalty
    return score


def choose(
    rows: list[dict[str, Any]],
    *,
    tz_name: str,
    recent_keys: Optional[set[str]] = None,
    max_slides: int = 9,
    max_per_venue: Optional[int] = None,
    max_per_category: Optional[int] = None,
    image_sizes: Optional[dict[str, int]] = None,
    profile: Optional[ScoreProfile] = None,
) -> list[Candidate]:
    """Rank, collapse repeats, apply diversity caps, return in TIME order.

    Chronological output is deliberate: a carousel that reads 9am -> 10pm is a
    schedule someone can act on, whereas score order is a ranked list nobody
    asked for.
    """
    recent_keys = recent_keys or set()
    image_sizes = image_sizes or {}
    p = profile or DEFAULT_PROFILE
    # Explicit arguments still win, so existing callers are unaffected; the
    # profile only supplies what they leave unset.
    max_per_venue = p.max_per_venue if max_per_venue is None else max_per_venue
    max_per_category = p.max_per_category if max_per_category is None else max_per_category

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
        if p.require_ticket_links and not row.get("ticket_links"):
            # Horizon only. Six months out, an event without a ticket link is
            # almost always a recurring fixture someone scheduled far ahead,
            # not something worth planning around.
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
                    profile=p,
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
