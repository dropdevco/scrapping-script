"""Content hashing + dedupe.

Assigns a stable ``content_hash`` to each item (used as the Supabase upsert key) and
collapses near-duplicates that arrive from different sources (e.g. the same concert
from Ticketmaster and a venue page).
"""

from __future__ import annotations

import hashlib
import re
from difflib import SequenceMatcher

from .models import Document, Event, Trend

_WS = re.compile(r"\s+")


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


def dedupe_events(events: list[Event], fuzzy_threshold: float = 0.9) -> list[Event]:
    """Drop exact hash duplicates, then fuzzily merge same-day near-identical titles."""
    seen: dict[str, Event] = {}
    for e in events:
        seen.setdefault(e.content_hash or _hash(_event_key(e)), e)

    merged: list[Event] = []
    for e in seen.values():
        dup = False
        e_day = e.start_time.date() if e.start_time else None
        for kept in merged:
            k_day = kept.start_time.date() if kept.start_time else None
            if e_day == k_day and _similar(_norm(e.title), _norm(kept.title)) >= fuzzy_threshold:
                # keep the richer record (more fields populated)
                if _fields_filled(e) > _fields_filled(kept):
                    merged[merged.index(kept)] = e
                dup = True
                break
        if not dup:
            merged.append(e)
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
