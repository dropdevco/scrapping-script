"""Keyword-based category guessing for sources with no classification signal.

Ticketmaster ships its own segment/genre/subGenre classification (left as-is).
Meetup/Eventbrite's schema.org/Event markup carries no category field at all,
so this fills the gap with a free, deterministic title match. Buckets match
the ones the submission form offers, so scraped and user-submitted events
share one filterable taxonomy.

Title only, deliberately - event descriptions are marketing boilerplate
("Lunch and Learn", "grab a coffee and network") whose incidental words
produce false positives; titles describe what the event actually is.

An event can genuinely belong to more than one bucket ("Beer & Live Music
Festival" is Food & Drink, Music, AND Festivals), and the storage layer keeps
``categories`` as an array end to end, so ``guess_categories`` returns every
matching bucket rather than stopping at the first hit.
"""

from __future__ import annotations

import re
from typing import Optional

# Every bucket whose pattern matches is included (order only affects the
# fallback single-category path below).
_RULES: list[tuple[str, re.Pattern[str]]] = [
    (
        "Food & Drink",
        re.compile(
            r"\b(dinner|brunch|happy hour|wine tasting|beer fest|brewery|tasting|"
            r"food truck|restaurant week|supper club|cocktail hour|bbq|taco tuesday|"
            r"cena|brunch|vino|cerveza|gastronom|comida|bebida|taco)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "Music",
        re.compile(
            r"\b(concert|live music|open mic|karaoke|dj set|band night|jazz night|"
            r"acoustic|music festival|album release|concierto|m[uú]sica|banda|"
            r"festival musical|regional mexicano|norte[nñ]o|mariachi)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "Arts & Theatre",
        re.compile(
            r"\b(theatre|theater|art exhibit|art walk|gallery|film|screening|comedy|"
            r"stand-?up|poetry|dance performance|musical|teatro|danza|ballet|"
            r"opera|[óo]pera|cine|pel[ií]cula|exposici[oó]n|museo|arte|comedia)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "Sports",
        re.compile(
            r"\b(5k|10k|marathon|run club|pickup basketball|soccer league|baseball|"
            r"basketball|tournament|golf|tennis|volleyball|road ride|cycling|"
            r"f[uú]tbol|beisbol|b[eé]isbol|lucha libre|carrera|marat[oó]n|torneo)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "Family",
        re.compile(
            r"\b(kids|children|toddler|family friendly|story time|playdate|"
            r"ni[nñ]os|infantil|familiar|familia)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "Festivals",
        re.compile(r"\b(festival|feria|fiesta|celebraci[oó]n)\b", re.IGNORECASE),
    ),
    (
        "Tech",
        re.compile(
            r"\b(hackathon|developer|coding|startup|mcp server|ai|artificial intelligence|"
            r"machine learning|web3|cybersecurity|tech meetup|software|data science|"
            r"python|javascript|aws|scrum|agile|tecnolog[ií]a|programaci[oó]n|"
            # "universidad"/"conferencia" removed: too generic (any subject can
            # have a university event or a conference) — was mistagging things
            # like a wellness conference as Tech purely for saying "conferencia".
            r"emprendimiento)\b",
            re.IGNORECASE,
        ),
    ),
]

DEFAULT_CATEGORY = "Community"


def guess_categories(title: Optional[str]) -> list[str]:
    """Every matching category bucket for an event title. Falls back to
    ['Community'] when nothing matches, so an event is never uncategorized."""
    text = title or ""
    matches = [category for category, pattern in _RULES if pattern.search(text)]
    return matches or [DEFAULT_CATEGORY]


def guess_category(title: Optional[str]) -> str:
    """Single best-effort category — kept for callers that only want one
    label (e.g. a UI badge slot). Prefer guess_categories() when storing."""
    return guess_categories(title)[0]
