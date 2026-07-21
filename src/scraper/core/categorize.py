"""Keyword-based category guessing for sources with no classification signal.

Ticketmaster ships its own segment/genre/subGenre classification (left as-is).
Meetup/Eventbrite's schema.org/Event markup carries no category field at all,
so this fills the gap with a free, deterministic title match. Buckets match
the ones the submission form offers, so scraped and user-submitted events
share one filterable taxonomy.

Title only, deliberately - event descriptions are marketing boilerplate
("Lunch and Learn", "grab a coffee and network") whose incidental words
produce false positives; titles describe what the event actually is.
"""

from __future__ import annotations

import re
from typing import Optional

# Ordered: first matching bucket wins.
_RULES: list[tuple[str, re.Pattern[str]]] = [
    (
        "Food & Drink",
        re.compile(
            r"\b(dinner|brunch|happy hour|wine tasting|beer fest|brewery|tasting|"
            r"food truck|restaurant week|supper club|cocktail hour|bbq|taco tuesday)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "Music",
        re.compile(
            r"\b(concert|live music|open mic|karaoke|dj set|band night|jazz night|"
            r"acoustic|music festival|album release)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "Arts & Theatre",
        re.compile(
            r"\b(theatre|theater|art exhibit|art walk|gallery|film|screening|comedy|"
            r"stand-?up|poetry|dance performance|musical)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "Sports",
        re.compile(
            r"\b(5k|10k|marathon|run club|pickup basketball|soccer league|baseball|"
            r"basketball|tournament|golf|tennis|volleyball|road ride|cycling)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "Family",
        re.compile(
            r"\b(kids|children|toddler|family friendly|story time|playdate)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "Tech",
        re.compile(
            r"\b(hackathon|developer|coding|startup|mcp server|ai|artificial intelligence|"
            r"machine learning|web3|cybersecurity|tech meetup|software|data science|"
            r"python|javascript|aws|scrum|agile)\b",
            re.IGNORECASE,
        ),
    ),
]

DEFAULT_CATEGORY = "Community"


def guess_category(title: Optional[str]) -> str:
    """Best-effort category from an event title. Falls back to 'Community'."""
    text = title or ""
    for category, pattern in _RULES:
        if pattern.search(text):
            return category
    return DEFAULT_CATEGORY
