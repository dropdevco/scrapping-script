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
import unicodedata
from difflib import SequenceMatcher
from typing import Any, Optional

from .eventtime import local_day
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


def fold(text: str | None) -> str:
    """Accent- and punctuation-insensitive form, for comparing titles.

    _norm only lowercases and collapses whitespace, which is why
    "SIN BANDERA - ESCENAS US TOUR" and "Sin Bandera – ESCENAS US TOUR" — the
    same concert, stored twice on 2026-09-18 — compared at 0.97 rather than
    1.0: a hyphen and an en-dash are different characters. Collapsing every
    non-alphanumeric run to a single space removes that whole class of
    near-miss, and NFKD plus combining-mark stripping does the same for
    "MÁGICO" against "MAGICO".
    """
    decomposed = unicodedata.normalize("NFKD", text or "")
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    return " ".join(re.sub(r"[^a-z0-9]+", " ", stripped.lower()).split())


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


# ── stored-row identity (shared by storage's live merge and the backfill) ──────

# Cross-venue matching has no venue confirmation, so the title has to carry the
# whole claim: a much harder similarity bar than the venue-confirmed lane's 0.90.
_CROSS_VENUE_TITLE_THRESHOLD = 0.95
# ...and the title must actually say something. "Karaoke Night" reduces to one
# distinctive word and would match every karaoke night in the city; "Yoga in the
# Park" reduces to two. Three is the floor for treating a title as an identity.
_CROSS_VENUE_MIN_TOKENS = 3

_CITY_PATTERNS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("ciudad juarez", ("ciudad juarez", "cd juarez", "juarez", "chihuahua")),
    ("el paso", ("el paso", "fort bliss", "socorro", "canutillo", "horizon city")),
    ("las cruces", ("las cruces", "mesilla", "new mexico")),
)


def city_of(row: dict[str, Any]) -> Optional[str]:
    """Coarse city for a stored row, or None when it cannot be determined.

    None BLOCKS a cross-venue merge rather than permitting one: a touring act
    playing Juárez on Friday and El Paso on Saturday is two real events, and
    guessing here would erase one of them.
    """
    haystack = fold(f"{row.get('location') or ''} {row.get('venue') or ''}")
    for city, needles in _CITY_PATTERNS:
        if any(n in haystack for n in needles):
            return city
    return None


def _day_of(row: dict[str, Any]) -> Optional[Any]:
    """Local calendar day. Both sides go through local_day because the incoming
    row carries a local offset while the stored one comes back from Postgres in
    UTC, and comparing those .date() values directly files the same evening
    event under two different days."""
    raw = row.get("start_time")
    if not raw:
        return None
    resolved = local_day(raw)
    return resolved.date() if resolved else None


def is_same_stored_event(
    a: dict[str, Any], b: dict[str, Any], *, allow_cross_venue: bool = False
) -> bool:
    """Are these two STORED rows the same real happening?

    One definition, used by both storage._merge_with_existing and
    backfill_merge_duplicates, which previously carried divergent copies.

    ``allow_cross_venue`` is the harder lane, for the case the venue-keyed lane
    structurally cannot see: the same show listed by an aggregator under its own
    brand ("El Paso Live") and by the building ("Plaza Theatre"). It carries no
    venue confirmation, so every gate below has to do that work instead. The
    standing bias is that a false merge silently hides a real event, which is
    worse than an unmerged duplicate — so each gate fails closed.
    """
    day = _day_of(a)
    if day is None or day != _day_of(b):
        return False

    title_a = a.get("title") or ""
    title_b = b.get("title") or ""

    if not allow_cross_venue:
        # The caller has already proven venue + day, so the title alone decides.
        if _similar(_norm(title_a), _norm(title_b)) >= _TITLE_ONLY_THRESHOLD:
            return True
        return _title_token_overlap(title_a, title_b) >= _TOKEN_OVERLAP_THRESHOLD

    if len(_meaningful_tokens(title_a)) < _CROSS_VENUE_MIN_TOKENS:
        return False
    if len(_meaningful_tokens(title_b)) < _CROSS_VENUE_MIN_TOKENS:
        return False

    city = city_of(a)
    if city is None or city != city_of(b):
        return False

    return _similar(fold(title_a), fold(title_b)) >= _CROSS_VENUE_TITLE_THRESHOLD


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
    # Both copies are the same event, so both were classified from the same
    # facts; whichever actually placed it wins. Unioning would let a
    # low-confidence guess from one source dilute the other's.
    richer.content_tags = richer.content_tags or other.content_tags

    # Identity must NOT depend on which copy happened to be richer this run.
    # It used to: the survivor kept its own hash, so the day a second source
    # started carrying one extra field — or the day the first source simply
    # failed and only one copy existed — the cluster changed hash, missed the
    # upsert conflict, and INSERTed a second row for an event already stored.
    # Three such pairs were live on 2026-09-17. min() is commutative and
    # associative, so a cluster resolves to the same hash under any arrival
    # order, any richness, and any subset of sources being reachable.
    hashes = [h for h in (kept.content_hash, dup.content_hash) if h]
    if hashes:
        richer.content_hash = min(hashes)

    # Two sources disagreeing on the start time is real and common (an
    # aggregator listing 13:30 for a 19:30 show). Record it rather than
    # resolving it: picking a winner here would be inventing a fact, and this
    # repo omits rather than guesses times. _apply_merge never overwrites a
    # stored start_time, so the disagreement stays visible instead of flipping.
    if other.start_time and richer.start_time and other.start_time != richer.start_time:
        alts = richer.raw.setdefault("_merged_alt_start_times", [])
        if isinstance(alts, list):
            alts.append({"source": other.source, "start_time": other.start_time.isoformat()})

    return richer


def dedupe_events(
    events: list[Event],
    fuzzy_threshold: float = _TITLE_ONLY_THRESHOLD,
    stats: Optional[dict[str, int]] = None,
) -> list[Event]:
    """Drop exact hash duplicates, then merge same-day near-identical events
    (by title, or by title+venue) into one record — unioning ticket links and
    categories rather than picking a single "winner" and discarding the rest.

    Pass ``stats`` to record how much each stage collapsed; omitted, this
    behaves exactly as before. Nothing in here used to log at all, so an
    in-batch merge was completely invisible after the fact.
    """
    for e in events:
        seed_ticket_link(e)

    seen: dict[str, Event] = {}
    for e in events:
        key = e.content_hash or _hash(_event_key(e))
        seen[key] = _merge_into(seen[key], e) if key in seen else e

    merged: list[Event] = []
    fuzzy_merges = 0
    for e in seen.values():
        dup_index = next(
            (i for i, kept in enumerate(merged) if _same_real_event(e, kept, fuzzy_threshold)), None
        )
        if dup_index is None:
            merged.append(e)
        else:
            merged[dup_index] = _merge_into(merged[dup_index], e)
            fuzzy_merges += 1

    if stats is not None:
        stats.update(
            raw=len(events),
            after_exact=len(seen),
            after_fuzzy=len(merged),
            fuzzy_merges=fuzzy_merges,
        )
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
