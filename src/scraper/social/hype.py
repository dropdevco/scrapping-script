"""Does this event deserve a standalone spotlight post, not just a roundup slot?

A headline touring artist, a major convention at the Convention Center, a
Comic Con, a championship-level local-team game — as opposed to the ordinary
ticketed show or community event that fills every other post's carousel. No
keyword or price-tier rule can reliably tell those apart (a $40 local show and
a $400 arena tour can share every scrapable field), so this is council-judged,
the same cache-once, model-judged, never-blocks shape social/localness.py
already established for chain-vs-local.

Judged ONCE PER EVENT and cached forever on events.is_hype (NULL = never
judged, and must behave exactly like today — no event is ever hype by
default). `hype_source = 'manual'` is the human override channel and is never
reconsidered, same convention as content_tags_source/localness_source.

A deterministic pre-pass keeps most events from ever reaching the model: only
an event with a ticket link, or at one of a handful of large-capacity venues,
is even asked. Everything else is marked not-hype for free. This is a COST
gate, not a verdict — passing it only earns an event a chance for the model to
say no, which it will for most of what passes.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

from ..core.config import settings
from ..core.dedupe import fold
from ..core.http import HttpClient
from ..core.llm import LLMUnavailable, complete_json

log = logging.getLogger("scraper.social.hype")


def _smash(text: Optional[str]) -> str:
    """Same reasoning as localness._smash: fold() turns an apostrophe into a
    space, not nothing, so a plain substring match needs whitespace stripped
    too or a possessive venue name never matches its own allowlist entry."""
    return fold(text).replace(" ", "")


# Large-capacity/marquee venues where almost anything booked is worth a look,
# even without a ticket link (a convention or a free championship game may
# carry none). Matched against the SMASHED venue name.
_MAJOR_VENUES = frozenset(
    _smash(name)
    for name in (
        "El Paso County Coliseum", "Don Haskins Center", "Sun Bowl",
        "El Paso Convention Center", "Abraham Chavez Theatre",
        "Southwest University Park", "Don Haskins",
    )
)

_BATCH_SIZE = 40

_SYSTEM = (
    "You judge whether an event deserves its OWN standalone Instagram spotlight "
    "post, for a city events account covering El Paso, Texas and Ciudad Juarez, "
    "Mexico. Most events do NOT deserve this -- they belong in an ordinary "
    "roundup post alongside several others. A spotlight is for something "
    "genuinely major: a headline touring artist or well-known act, a large "
    "convention or expo (a Comic Con, an industry convention at the convention "
    "center), or a championship/rivalry-level local sports game -- not just any "
    "ticketed concert or community event.\n\n"
    "You are given a numbered list of upcoming events (title, venue, pillar, "
    "whether it has a ticket link, and a short description). For each, decide "
    "true or false.\n\n"
    'Reply with STRICT JSON: {"judgments": [{"i": int, "is_hype": bool, '
    '"reason": string}, ...]}\n'
    "One entry per input event, in the SAME ORDER, using its \"i\" index — do "
    "not skip any. reason: at most 60 characters, plain. Be selective: when in "
    "doubt, false."
)


def hype_from_rules(row: dict[str, Any]) -> Optional[bool]:
    """False (not hype, zero cost) when nothing about this row is even
    plausible, or None to defer to the model. Never returns True — the model
    is always the one that says yes, since "major enough" is a judgment call
    no rule here can safely make."""
    if row.get("ticket_links"):
        return None
    venue = _venue_of(row)
    name = _smash(venue.get("name") if venue else None)
    if name and any(v in name for v in _MAJOR_VENUES):
        return None
    return False


def _venue_of(row: dict[str, Any]) -> Optional[dict[str, Any]]:
    """Same dict-or-list-of-one PostgREST shape render/caption/selection/
    localness all already unwrap."""
    venues = row.get("venues")
    if isinstance(venues, list):
        venues = venues[0] if venues else None
    return venues if isinstance(venues, dict) else None


def _unjudged(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [r for r in rows if r.get("id") and r.get("hype_checked_at") is None]


def _clean_judgments(raw: Any, count: int) -> dict[int, dict[str, Any]]:
    out: dict[int, dict[str, Any]] = {}
    if not isinstance(raw, list):
        return out
    for item in raw:
        if not isinstance(item, dict):
            continue
        i = item.get("i")
        if not isinstance(i, int) or not (0 <= i < count) or i in out:
            continue
        reason = " ".join(str(item.get("reason") or "").split())[:60]
        out[i] = {"is_hype": bool(item.get("is_hype")), "reason": reason}
    return out


async def _judge_batch(http: HttpClient, rows: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
    lines = []
    for i, row in enumerate(rows):
        venue = _venue_of(row)
        desc = " ".join(str(row.get("description") or "").split())[:200]
        pillars = ", ".join(row.get("content_tags") or []) or "(none)"
        lines.append(
            f"{i}. {row.get('title') or '(untitled)'} — {venue.get('name') if venue else '(no venue)'} "
            f"— pillar: {pillars} — tickets: {'yes' if row.get('ticket_links') else 'no'} — {desc or '(no description)'}"
        )
    try:
        data = await complete_json(
            http, system=_SYSTEM, user="\n".join(lines), max_tokens=60 * len(rows) + 200
        )
    except LLMUnavailable as exc:
        log.info("hype auditor unavailable for a batch of %d event(s): %s", len(rows), exc)
        return {}
    return _clean_judgments(data.get("judgments"), len(rows))


async def fill_hype(storage: Any, http: HttpClient, rows: list[dict[str, Any]]) -> int:
    """Judge every not-yet-judged event among `rows`. Returns how many were judged.

    Never raises. An unjudged event behaves exactly as it does today — NULL is
    the safe default everywhere this is read, and query_hype_candidates only
    ever selects rows where is_hype is explicitly true.
    """
    if not settings.council_available:
        return 0

    candidates = _unjudged(rows)
    if not candidates:
        return 0

    now = datetime.now(timezone.utc).isoformat()
    judged = 0
    remaining: list[dict[str, Any]] = []

    for row in candidates:
        verdict = hype_from_rules(row)
        if verdict is None:
            remaining.append(row)
            continue
        ok = await storage.cache_event_editorial(
            row["id"],
            {
                "is_hype": verdict,
                "hype_reason": "no ticket link and not a major venue",
                "hype_source": "rule",
                "hype_checked_at": now,
            },
        )
        judged += int(ok)

    for start in range(0, len(remaining), _BATCH_SIZE):
        batch = remaining[start : start + _BATCH_SIZE]
        verdicts = await _judge_batch(http, batch)
        if not verdicts:
            # The call failed or came back unreadable. Leave the rows unjudged (NULL) so
            # the next build asks again; caching a "no" here would permanently exclude
            # real headliners after a single model hiccup.
            continue
        for i, row in enumerate(batch):
            verdict = verdicts.get(i, {"is_hype": False, "reason": "no verdict returned"})
            ok = await storage.cache_event_editorial(
                row["id"],
                {
                    "is_hype": verdict["is_hype"],
                    "hype_reason": verdict["reason"],
                    "hype_source": "council",
                    "hype_checked_at": now,
                },
            )
            judged += int(ok)

    if judged:
        log.info("hype auditor: judged %d event(s)", judged)
    return judged
