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

import logging
import re
from datetime import datetime, timezone
from typing import Any, Optional

from ..core.config import settings
from ..core.content_tags import PILLARS
from ..core.dedupe import fold
from ..core.http import HttpClient
from ..core.llm import LLMUnavailable, complete_json

log = logging.getLogger("scraper.social.clarify")

# A blurb has to earn its place in the caption. Models overrun an explicit
# limit in the prompt as a matter of course — measured against a stated 90:
# 88, 91, 94, 94, 100, 131, 137, 145 — so length is enforced here and never
# trusted from the response. The prompt asks for less than this ceiling on
# purpose, to leave the usual overshoot somewhere to land.
#
# Overlong blurbs are TRUNCATED at a word boundary rather than dropped. They
# are otherwise good copy, and half a useful sentence beats the bare title we
# would fall back to.
_ASK_BLURB = 100
MAX_BLURB = 120

# Below this there is nothing to summarise FROM, and a model asked to explain
# an event it knows nothing about will invent. Measured on a real row with no
# description, one candidate model produced "low-cost vaccinations and basic
# health checks for dogs and cats" -- entirely fabricated. Silence is correct.
_MIN_DESCRIPTION = 60

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
    """Should this event get a generated one-line summary?

    Two questions, both of which must be yes: is the title opaque, and do we
    have enough source text to answer honestly? An opaque title with no
    description gets nothing -- see _MIN_DESCRIPTION.
    """
    if len(str(row.get("description") or "").strip()) < _MIN_DESCRIPTION:
        return False
    if title_repeats_venue(row):
        return True
    return not is_self_explanatory(row)


_SYSTEM = (
    "You write one-line clarifications for a local events feed covering El Paso, "
    "Texas and Ciudad Juarez, Mexico.\n"
    "You are given an event whose TITLE may not say what the event actually is. "
    "Decide whether a reader seeing only the title would already understand it.\n\n"
    'Reply with STRICT JSON: {"self_explanatory": boolean, "blurb": string}\n'
    "- self_explanatory=true when the title alone is clear. Then blurb must be \"\".\n"
    f"- Otherwise blurb states what the thing IS, in at most {_ASK_BLURB} characters.\n"
    "- Use ONLY facts present in the input. If the input does not say, leave it out. "
    "Never invent prices, times, ages, sponsors or services.\n"
    "- Do not repeat the title or the venue name. Do not use marketing voice or "
    "exclamation marks. Plain, factual, lowercase-sentence style.\n"
    "- Always write the blurb in English, even when the source text is Spanish."
)


def _prompt_for(row: dict[str, Any]) -> str:
    return (
        f"TITLE: {row.get('title')}\n"
        f"VENUE: {row.get('venue') or '(unknown)'}\n"
        f"DESCRIPTION: {str(row.get('description') or '')[:900]}"
    )


def _clean_blurb(raw: Any, row: dict[str, Any]) -> Optional[str]:
    """Accept a blurb only if it is usable. Enforced, not trusted."""
    if not isinstance(raw, str):
        return None
    text = " ".join(raw.split()).strip().strip('"')
    if not text:
        return None
    # A blurb that just echoes the title has added nothing, and a two-word
    # fragment is noise rather than an explanation.
    if fold(text) == fold(row.get("title")) or len(text.split()) < 4:
        return None
    if len(text) > MAX_BLURB:
        clipped = text[: MAX_BLURB - 1]
        # Back up to a word boundary so it does not end mid-word; if there is
        # no space to back up to, the text is one long token and unusable.
        if " " not in clipped:
            return None
        text = clipped[: clipped.rindex(" ")].rstrip(" ,;:-") + "…"
    return text


async def generate_blurb(http: HttpClient, row: dict[str, Any]) -> Optional[str]:
    """A one-line explanation of an opaque event, or None.

    None on every failure path — no key, model down, malformed reply, a blurb
    that broke the rules, or the model judging the title already clear. The
    caller renders what it had before, so the worst case is today's output.
    """
    if not needs_clarifier(row):
        return None
    try:
        data = await complete_json(http, system=_SYSTEM, user=_prompt_for(row), max_tokens=400)
    except LLMUnavailable as exc:
        log.info("clarifier unavailable for %r: %s", row.get("title"), exc)
        return None

    if data.get("self_explanatory") is True:
        return None
    blurb = _clean_blurb(data.get("blurb"), row)
    if blurb is None:
        log.info("clarifier returned an unusable blurb for %r: %r", row.get("title"), data.get("blurb"))
    return blurb


_CURATOR_SYSTEM = (
    "You place local events into content pillars for an Instagram account "
    "covering El Paso, Texas and Ciudad Juarez, Mexico.\n\n"
    "Choose from EXACTLY these six, and only these:\n"
    "- Arts & Culture: theatre, galleries, museums, film, dance, history, literature\n"
    "- Live Music: concerts, DJs, orchestras, any performance where music is the point\n"
    "- Sports: SPECTATOR sport only — games and matches you watch\n"
    "- Fitness & Activities: things you turn up and DO — runs, yoga, hikes, classes\n"
    "- Family: aimed at children or at families together, whatever the activity\n"
    "- Food & Drink: where eating or drinking is the main event\n\n"
    'Reply with STRICT JSON: {"pillars": [string, ...]}\n'
    "- Usually one pillar. Two only when both are genuinely central.\n"
    "- An activity aimed at small children is Family, not Fitness: a baby yoga "
    "class is a family outing, not a workout.\n"
    "- Use [] when none of the six honestly fits. An empty list is a valid, "
    "useful answer — do not force a bad fit."
)


def _clean_pillars(raw: Any) -> list[str]:
    """Only real pillars, deduped, order fixed. A model inventing a seventh
    bucket must not be able to create one downstream."""
    if not isinstance(raw, list):
        return []
    picked = {p for p in raw if isinstance(p, str) and p in PILLARS}
    return [p for p in PILLARS if p in picked]


async def assign_pillars(http: HttpClient, row: dict[str, Any]) -> list[str]:
    """Pillars for an event the keyword pass could not place. [] on any failure."""
    user = (
        f"TITLE: {row.get('title')}\n"
        f"VENUE: {row.get('venue') or '(unknown)'}\n"
        f"SOURCE CATEGORIES: {', '.join(row.get('categories') or []) or '(none)'}\n"
        f"DESCRIPTION: {str(row.get('description') or '(none)')[:600]}"
    )
    try:
        data = await complete_json(http, system=_CURATOR_SYSTEM, user=user, max_tokens=300)
    except LLMUnavailable as exc:
        log.info("curator unavailable for %r: %s", row.get("title"), exc)
        return []
    return _clean_pillars(data.get("pillars"))


async def fill_pillars(
    storage: Any, http: HttpClient, rows: list[dict[str, Any]], *, dry_run: bool = False
) -> int:
    """Place the events the keyword pass left empty. Returns how many were placed.

    Only rows with NO pillar are considered: the keyword pass is confident by
    construction, so there is nothing for the model to second-guess. Cached on
    the row like blurbs, and a row judged to fit none of the six records that
    judgement so it is not re-asked every build.
    """
    if not settings.council_available:
        return 0

    placed = 0
    budget = settings.council_max_calls
    for row in rows:
        if budget <= 0:
            break
        if row.get("content_tags") or row.get("content_tags_source") in ("council", "manual"):
            continue

        budget -= 1
        pillars = await assign_pillars(http, row)
        row["content_tags"] = pillars  # in-memory, so this build uses it immediately
        if dry_run:
            placed += bool(pillars)
            continue
        ok = await storage.cache_event_editorial(
            str(row.get("id")),
            {"content_tags": pillars, "content_tags_source": "council"},
        )
        if ok:
            placed += bool(pillars)

    used = settings.council_max_calls - budget
    if used:
        log.info(
            "curator: placed %d of %d event(s)%s",
            placed, used, " (dry run, not cached)" if dry_run else "",
        )
    return placed


async def fill_blurbs(
    storage: Any, http: HttpClient, rows: list[dict[str, Any]], *, dry_run: bool = False
) -> int:
    """Give each opaque event a cached one-liner. Returns how many were written.

    Cached on the row, so a recurring series is paid for once and reused by
    every later occurrence and every rebuild — the cost tracks NEW events, not
    builds. `blurb_checked_at` records that we looked even when the answer was
    "no blurb needed", so a self-explanatory title is never re-asked.

    `dry_run` still calls the model, because seeing the real caption is the
    whole point of a preview, but writes nothing back — `build --dry-run`
    promises to touch nothing, and a preview quietly populating a cache would
    make the next real build behave differently for having been previewed.

    Never raises. The caption renders whatever it had, which is today's output.
    """
    if not settings.council_available:
        return 0

    written = 0
    budget = settings.council_max_calls
    for row in rows:
        if budget <= 0:
            break
        if row.get("blurb") or row.get("blurb_checked_at"):
            continue  # already judged, including "judged and left blank"
        if not needs_clarifier(row):
            continue

        budget -= 1
        blurb = await generate_blurb(http, row)
        row["blurb"] = blurb  # in-memory, so this build uses it immediately
        if dry_run:
            written += bool(blurb)
            continue
        patch = {
            "blurb": blurb,
            "blurb_source": "council",
            "blurb_checked_at": datetime.now(timezone.utc).isoformat(),
        }
        if await storage.cache_event_editorial(str(row.get("id")), patch):
            written += bool(blurb)

    used = settings.council_max_calls - budget
    if used:
        log.info(
            "clarifier: %d blurb(s) from %d call(s)%s",
            written, used, " (dry run, not cached)" if dry_run else "",
        )
    return written
