"""Which event titles do not explain themselves?

Feedback on the published posts, 2026-09-17: the copy does not say what is
actually happening. The examples given were "Desert Bloomers" and a yoga
listing -- is that a yoga class, or is Yoga just the name of the park? Whereas
"El Paso Rhinos vs. Odessa Jackalopes" needs nothing at all; everyone can read
a matchup.

So the fix is not "print the description on every event". It is: notice the
titles that are opaque, and fill only that gap. This module is the free,
deterministic half of that -- it decides who needs help. Writing the actual
one-line summary needs a model and lives in the council.

Being wrong is cheap in one direction and not the other. A title wrongly
called opaque costs one model call and probably yields
``self_explanatory: true`` anyway. A title wrongly called obvious ships the
vague post we are trying to fix. So this leans toward asking.
"""

from __future__ import annotations

import re
from typing import Any

from ..core.dedupe import fold

# A matchup reads itself: "<someone> vs <someone>".
_MATCHUP = re.compile(r"\bvs\b")

# Local teams and franchises. "El Paso Rhinos" carries no descriptive noun at
# all, but anyone in this city knows it is a hockey game -- and there are 24 of
# them upcoming, so hard-coding the handful of teams saves the most calls for
# the least effort.
_KNOWN_TEAMS = (
    "chihuahuas", "locomotive", "rhinos", "utep miners", "sun bowl",
    "bravos", "indios", "miners",
)

# Words that state the KIND of thing an event is. One of these and a reader
# knows roughly what they would be turning up to.
_DESCRIPTIVE = re.compile(
    r"\b("
    # English
    r"concert|festival|fest|market|mercado|class|workshop|tour|exhibit|exhibition|"
    r"game|match|race|marathon|parade|screening|film|movie|comedy|karaoke|"
    r"yoga|zumba|pilates|storytime|story time|reading|lecture|talk|panel|"
    r"party|dance|ball|gala|fundraiser|benefit|auction|brunch|dinner|tasting|"
    r"happy hour|trivia|bingo|tournament|clinic|camp|fair|expo|conference|"
    r"symphony|orchestra|opera|ballet|theatre|theater|musical|play|recital|"
    r"open mic|jam|showcase|meetup|walk|run|ride|hike|cleanup|drive|"
    r"celebration|ceremony|service|mass|vigil|rodeo|circus|wrestling|boxing|"
    # Spanish
    r"concierto|feria|taller|clase|exposici[oó]n|carrera|torneo|misa|"
    r"presentaci[oó]n|funci[oó]n|obra|baile|fiesta|desfile|degustaci[oó]n|"
    r"conferencia|festival|mercadito|pel[ií]cula|cine|teatro|danza|lucha"
    r")\b"
)


# The City of El Paso calendar appends its own section name to the listing
# text, so titles arrive with a bare category glued on the end: "Marty Robbins
# Recreation Center Yoga Parks", "Richard Burges Baby Storytime Libraries".
# The descriptive word IS in there, which is why a keyword test calls these
# clear -- but "Yoga Parks" was one of the two examples singled out as
# confusing, and it is confusing: read plainly it suggests a park called Yoga.
# Machine-mangled phrasing needs a human sentence no matter what words it holds.
_MANGLED_TAIL = ("parks", "libraries", "museums", "zoo")


def has_mangled_phrasing(row: dict[str, Any]) -> bool:
    title = fold(row.get("title"))
    return bool(title) and title.split()[-1] in _MANGLED_TAIL


def is_self_explanatory(row: dict[str, Any]) -> bool:
    """True when the title alone already says what the event is."""
    title = fold(row.get("title"))
    if not title:
        return False
    if has_mangled_phrasing(row):
        return False
    if _MATCHUP.search(title):
        return True
    if any(team in title for team in _KNOWN_TEAMS):
        return True
    return bool(_DESCRIPTIVE.search(title))


def title_repeats_venue(row: dict[str, Any]) -> bool:
    """The title is just the venue's name again -- "San Pedro de Jesus
    Maldonado" at "San Pedro de Jesus Maldonado". Says nothing twice, and is a
    strong signal that a human would have to click through to learn anything."""
    title = fold(row.get("title"))
    venue = fold(row.get("venue"))
    if not title or not venue:
        return False
    return title == venue or title in venue or venue in title


def needs_clarifier(row: dict[str, Any]) -> bool:
    """Should this event get a generated one-line summary?"""
    if title_repeats_venue(row):
        return True
    return not is_self_explanatory(row)
