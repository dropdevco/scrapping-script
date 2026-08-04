"""Content hashing + dedupe.

Assigns a stable ``content_hash`` to each item (used as the Supabase upsert key) and
collapses near-duplicates that arrive from different sources (e.g. the same concert
from Ticketmaster and a venue page) into ONE event carrying every source's ticket
link, instead of one row per ticketing site. This is the in-batch (same
orchestrator run) half of that merge — see ``storage.py`` for the cross-run half,
which catches the same real event showing up in a *later* scrape.
"""

from __future__ import annotations

import hashlib
import re
from difflib import SequenceMatcher

from .models import Document, Event, TicketLink, Trend
from .ticket_labels import ticket_label

_WS = re.compile(r"\s+")

# Confidence tiers for "these two records are the same real-world event":
# a near-identical title on the same day is enough on its own; a merely
# similar title needs a similar venue too, since two ticketing sites often
# word the same event's title quite differently ("Machetes - World Tour
# 2026" vs "Machetes Live in El Paso") but rarely disagree on the venue.
_TITLE_ONLY_THRESHOLD = 0.9
_TOKEN_OVERLAP_THRESHOLD = 0.8
_VENUE_THRESHOLD = 0.6

# Generic words that appear in event titles regardless of what the event
# actually is ("Live", "Tour", "Night") — stripping them before comparing
# titles is what makes the venue-assisted tier reliable. Character-level
# similarity on the raw strings does NOT work for this: "Salsa Night" vs
# "Bachata Night" scores *higher* (0.67) than genuine same-event pairs like
# "Machetes - World Tour 2026" vs "Machetes Live in Concierto" (0.54), because
# short titles sharing common filler words dominate the ratio. Comparing the
# remaining distinctive words instead cleanly separates the two.
_TITLE_STOPWORDS = {
    "the", "a", "an", "in", "on", "at", "of", "for", "with", "and", "or", "to",
    "el", "la", "los", "las", "de", "en", "con", "y", "o", "del", "al",
    "live", "tour", "gira", "concert", "concierto", "show", "event", "evento",
    "presents", "presenta", "world", "mundial", "night", "noche", "tickets", "boletos",
    "vs", "featuring", "feat", "ft", "special", "edition", "edicion", "edición",
}
_TOKEN_RE = re.compile(r"[a-z0-9áéíóúñü]+")


def _norm(text: str | None) -> str:
    if not text:
        return ""
    return _WS.sub(" ", text.strip().lower())


def _hash(key: str) -> str:
    return hashlib.sha1(key.encode("utf-8")).hexdigest()


def _event_key(e: Event) -> str:
    if e.url:
        return _norm(e.url)
    day = e.start_time.date().isoformat() if e.start_time else ""
    return f"{_norm(e.title)}|{day}|{_norm(e.venue or e.location)}"


def _trend_key(t: Trend) -> str:
    if t.url:
        return f"{t.platform}|{_norm(t.url)}"
    return f"{t.platform}|{_norm(t.title)}"


def assign_hashes_events(events: list[Event]) -> list[Event]:
    for e in events:
        e.content_hash = _hash(_event_key(e))
    return events


def assign_hashes_trends(trends: list[Trend]) -> list[Trend]:
    for t in trends:
        t.content_hash = _hash(_trend_key(t))
    return trends


def _similar(a: str, b: str) -> float:
    return SequenceMatcher(None, a, b).ratio()


def _meaningful_tokens(title: str) -> set[str]:
    tokens = _TOKEN_RE.findall(title.lower())
    return {t for t in tokens if len(t) > 2 and t not in _TITLE_STOPWORDS and not t.isdigit()}


def _title_token_overlap(a: str, b: str) -> float:
    """What fraction of the SHORTER title's distinctive words also appear in
    the other title. 1.0 means one title's real content is fully contained in
    the other's (typical of the same event with an added marketing suffix);
    0.0 means they share no distinctive word at all. 0.0 (not merge-eligible)
    when either title reduces to nothing but filler words."""
    ta, tb = _meaningful_tokens(a), _meaningful_tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / min(len(ta), len(tb))


def seed_ticket_link(e: Event) -> None:
    """Give an event its own single ticket link (derived from its URL) before
    any merging happens, so a same-event merge always has something to union.
    A no-op if the source already set ticket_links explicitly."""
    if not e.ticket_links and e.url:
        e.ticket_links = [TicketLink(source=e.source, label=ticket_label(e.url), url=e.url)]


def merge_ticket_links(a: list[TicketLink], b: list[TicketLink]) -> list[TicketLink]:
    """Union two ticket-link lists, deduped by URL, order preserved."""
    seen: dict[str, TicketLink] = {}
    for link in (*a, *b):
        seen.setdefault(link.url, link)
    return list(seen.values())


def _merge_categories(a: list[str], b: list[str]) -> list[str]:
    seen: dict[str, None] = {}
    for c in (*a, *b):
        if c:
            seen.setdefault(c, None)
    return list(seen)


def _same_real_event(a: Event, b: Event, title_only_threshold: float) -> bool:
    """True when a and b are almost certainly the SAME real-world event
    scraped from two different sources, not merely two different events that
    happen to share a day."""
    a_day = a.start_time.date() if a.start_time else None
    b_day = b.start_time.date() if b.start_time else None
    if a_day is None or a_day != b_day:
        return False

    title_sim = _similar(_norm(a.title), _norm(b.title))
    if title_sim >= title_only_threshold:
        return True

    # Moderate-confidence tier: the two titles must share essentially all of
    # their distinctive words (not just generic filler), AND the venue must
    # match too — either signal alone is too weak, but together they're a
    # reliable "same event, different ticketing site" fingerprint.
    if _title_token_overlap(a.title, b.title) < _TOKEN_OVERLAP_THRESHOLD:
        return False
    venue_sim = _similar(_norm(a.venue or a.location), _norm(b.venue or b.location))
    return venue_sim >= _VENUE_THRESHOLD


def _merge_into(kept: Event, dup: Event) -> Event:
    """Combine two records of the same real event into one. The richer
    record's own fields win (title, description, image, ...); ticket_links
    and categories are unioned rather than dropped, so merging never loses a
    source's ticket link or its category guess."""
    richer, other = (kept, dup) if _fields_filled(kept) >= _fields_filled(dup) else (dup, kept)
    richer.ticket_links = merge_ticket_links(richer.ticket_links, other.ticket_links)
    richer.categories = _merge_categories(richer.categories, other.categories)
    return richer


def dedupe_events(events: list[Event], fuzzy_threshold: float = _TITLE_ONLY_THRESHOLD) -> list[Event]:
    """Drop exact hash duplicates, then merge same-day near-identical events
    (by title, or by title+venue) into one record — unioning ticket links and
    categories rather than picking a single "winner" and discarding the rest.
    """
    for e in events:
        seed_ticket_link(e)

    seen: dict[str, Event] = {}
    for e in events:
        key = e.content_hash or _hash(_event_key(e))
        seen[key] = _merge_into(seen[key], e) if key in seen else e

    merged: list[Event] = []
    for e in seen.values():
        dup_index = next(
            (i for i, kept in enumerate(merged) if _same_real_event(e, kept, fuzzy_threshold)), None
        )
        if dup_index is None:
            merged.append(e)
        else:
            merged[dup_index] = _merge_into(merged[dup_index], e)
    return merged


def dedupe_trends(trends: list[Trend]) -> list[Trend]:
    seen: dict[str, Trend] = {}
    for t in trends:
        seen.setdefault(t.content_hash or _hash(_trend_key(t)), t)
    return list(seen.values())


def dedupe_documents(docs: list[Document]) -> list[Document]:
    seen: dict[str, Document] = {}
    for d in docs:
        seen.setdefault(_norm(d.url), d)
    return list(seen.values())


def _fields_filled(e: Event) -> int:
    return sum(
        1
        for v in (e.description, e.start_time, e.end_time, e.venue, e.location, e.url, e.image_url)
        if v
    )
