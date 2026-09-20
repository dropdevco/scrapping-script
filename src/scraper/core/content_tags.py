"""The six Instagram content pillars, and a cheap baseline for assigning them.

A pillar is an ADDITIONAL, Instagram-only editorial label. It does not replace
``events.categories``, which the website filter rail, the submission form and
the knowledge-base export own and which keeps every value it has. An event
tagged "Community, Free, Nature and Outdoors" on the site keeps all three; it
just also carries a pillar when the posting pipeline needs one.

This module is the CONFIDENT half. Placement is ultimately an editorial
judgement -- "Baby Yoga and Sound Bath" is a Family event, not a fitness class,
and no keyword table reasons its way to that -- so anything this cannot answer
plainly returns ``[]`` and the council decides. An empty list is a signal, not
a failure, which is why there is no catch-all bucket: a fallback would hide the
very events that need a human-quality judgement.

Two deliberate departures from core/categorize.py, whose taxonomy this does not
touch:

1. It reads more than the title. Ticketmaster ships real classifications
   ("Baseball", "Ice Hockey", "Minor League") that beat any title keyword, and
   the venue disambiguates ("La Nube" is the children's museum).
2. SPORTS MEANS SPECTATOR. categorize.py puts "5k", "marathon" and "run club"
   under Sports; here those are Fitness & Activities, because a race you enter
   and a game you watch are different content and the split was the point of
   introducing pillars at all.
"""

from __future__ import annotations

import re
from typing import Any, Iterable, Optional

from .dedupe import fold

ARTS = "Arts & Culture"
MUSIC = "Live Music"
SPORTS = "Sports"
FITNESS = "Fitness & Activities"
FAMILY = "Family"
FOOD = "Food & Drink"

PILLARS: tuple[str, ...] = (ARTS, MUSIC, SPORTS, FITNESS, FAMILY, FOOD)

# Local franchises. "El Paso Rhinos" carries no descriptive word at all, and
# there are 24 of their games upcoming, so naming the teams is the single
# highest-yield rule here.
#
# Deliberately NOT "utep" bare: it is the whole university, not just its
# athletics program, and tagged "Voice Area Alumni - UTEP Department of Music"
# as Sports on a live production carousel. "UTEP FB vs Oregon State" still
# matches regardless, via the standalone "vs" in _SPECTATOR below; "miners"
# alone catches a non-"vs" UTEP game title.
_TEAMS = re.compile(
    r"\b(chihuahuas|locomotive|rhinos|miners|sun bowl|bravos|indios)\b"
)
_SPECTATOR = re.compile(
    # A bare "v" is the abbreviated form of "vs" ("UTEP Volleyball v. Arizona";
    # the duplicate sweep separately paired "UTEP FB vs Hawaii" with "UTEP FB v
    # Hawaii", so both spellings are live in this data) -- but on its own it is
    # also a Roman numeral, and "Devious Maids Season V" is not a sports event.
    # The lookahead is the disambiguator: a versus-marker always names an
    # opponent AFTER it, so it is never the last word of a title, while a
    # trailing numeral usually is. "v." is handled the same way, the period
    # consumed before the lookahead checks what follows it.
    r"\b(?:vs|v)\.?(?=\s+\S)"
    r"|\b(?:lucha libre|boxeo|boxing|wrestling|rodeo|playoff|"
    r"baseball|basketball|volleyball|hockey|soccer|futbol|football)\b"
)
# Things you turn up and DO. Note "run" is absent: it collides with "run of
# show", "fun run" is caught by 5k/marathon anyway, and a false Fitness tag on
# a theatre run would be visible on the slide.
#
# "barre" is deliberately qualified ("barre class"/"pure barre"/"barre
# fitness"), not bare: a ballet barre is a piece of equipment, and "Raising
# The Barre- UTEP Theatre & Dance" -- a dance-department stage production,
# categorized by its own source as Theatre -- tagged Fitness & Activities
# purely on that word appearing in its wordplay title.
_PARTICIPATORY = re.compile(
    r"\b(5k|10k|marathon|half marathon|triathlon|run club|fun run|"
    r"yoga|zumba|pilates|barre class|pure barre|barre fitness|barre workout|"
    r"spin class|aerobics|workout|bootcamp|"
    r"hike|hiking|cycling|bike ride|swim|martial arts|karate|"
    r"carrera|maraton|caminata|senderismo|ciclismo)\b"
)
# Spanish plurals need the optional suffix: fold() strips accents so "niños"
# arrives as "ninos", but "infantiles" would not match a bare "infantil".
# Venues whose whole purpose is children, the Family equivalent of naming the
# local teams. "La Nube" is El Paso's children's museum and hosts a steady
# stream of events whose titles ("Fab Lab- Code Arcade", "World Day of Play")
# say nothing about who they are for.
_FAMILY_VENUES = re.compile(r"\b(la nube|childrens museum|children s museum|discovery museum)\b")

_KIDS = re.compile(
    r"\b(kids?|children|childrens|toddlers?|bab(?:y|ies)|infants?|preschool|"
    r"story ?times?|family friendly|sensory friendly|petting zoo|"
    r"nin[oa]s?|infantil(?:es)?|familiar(?:es)?|familia|bebes?|cuentos?)\b"
)
_MUSIC = re.compile(
    r"\b(concert|concierto|live music|dj|open mic|karaoke|acoustic|"
    r"symphony|orchestra|epso|philharmonic|mariachi|banda|norteno|"
    r"tribute|unplugged|setlist|album release|jam session)\b"
)
_ARTS = re.compile(
    r"\b(theatre|theater|teatro|gallery|galeria|museum|museo|exhibit|exhibition|"
    r"exposicion|film|movie|cine|pelicula|screening|ballet|opera|dance|danza|"
    r"poetry|poesia|comedy|comedia|stand ?up|musical|recital|"
    # No bare "play": it means theatre far less often than it means "day of
    # play", "playground" or "playdate", and it tagged a Nickelodeon children's
    # event at the science museum as Arts & Culture. "stage play" is safe.
    r"stage play|art walk|artist|mural|sculpture|history|historia|cultura)\b"
)
_FOOD = re.compile(
    r"\b(dinner|brunch|happy hour|wine|vino|beer|cerveza|brewery|cerveceria|"
    r"tasting|degustacion|food truck|restaurant|taco|bbq|cocktail|coctel|"
    r"margarita|tequila|mezcal|cena|comida|gastronom|culinary|chef)\b"
)

# Source-supplied classifications, which are far better signal than a title
# guess when present. "Sports and Fitness" is deliberately ABSENT: it is the
# exact ambiguity the pillars exist to split, so it must not resolve either way.
_FROM_CATEGORY = {
    "baseball": SPORTS, "basketball": SPORTS, "hockey": SPORTS, "ice hockey": SPORTS,
    "football": SPORTS, "soccer": SPORTS, "minor league": SPORTS, "sports": SPORTS,
    "music": MUSIC, "concert": MUSIC, "concerts": MUSIC, "latin": MUSIC, "rock": MUSIC,
    "pop": MUSIC, "country": MUSIC, "jazz": MUSIC, "live music - small venue": MUSIC,
    "arts & theatre": ARTS, "arts and theatre": ARTS, "theatre": ARTS, "theater": ARTS,
    "exhibitions": ARTS, "museum and exhibits": ARTS, "history and culture": ARTS,
    "film": ARTS, "film and video": ARTS, "performing arts": ARTS, "dance": ARTS,
    "guided tours": ARTS,
    "family": FAMILY, "family-friendly": FAMILY, "early childhood": FAMILY,
    "sensory friendly hours": FAMILY, "zoo": FAMILY,
    "food and drinks": FOOD, "food & drink": FOOD,
}


def _category_pillars(categories: Iterable[str]) -> set[str]:
    out: set[str] = set()
    for raw in categories or ():
        hit = _FROM_CATEGORY.get(fold(raw))
        if hit:
            out.add(hit)
    return out


def pillars_for(
    title: Optional[str],
    categories: Iterable[str] = (),
    venue: Optional[str] = None,
) -> list[str]:
    """Confident pillars for an event, or [] when this cannot say plainly."""
    text = fold(title)
    if not text:
        return []
    haystack = f"{text} {fold(venue)}".strip()

    found: set[str] = set(_category_pillars(categories))

    if _TEAMS.search(haystack) or _SPECTATOR.search(text):
        found.add(SPORTS)
    if _PARTICIPATORY.search(text):
        found.add(FITNESS)
    if _KIDS.search(haystack) or _FAMILY_VENUES.search(fold(venue)):
        found.add(FAMILY)
    if _MUSIC.search(text):
        found.add(MUSIC)
    if _ARTS.search(text):
        found.add(ARTS)
    if _FOOD.search(text):
        found.add(FOOD)

    # A fitness activity aimed at small children is a family outing, not a
    # workout -- "Baby Yoga and Sound Bath" at the children's museum was the
    # case that made this explicit. Family wins and Fitness is dropped.
    if FAMILY in found and FITNESS in found:
        found.discard(FITNESS)

    # Spectator and participatory are opposites. Something that reads as both
    # ("5K at the ballpark") is exactly the judgement to hand upward.
    if SPORTS in found and FITNESS in found:
        return []

    return [p for p in PILLARS if p in found]
