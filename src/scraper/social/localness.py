"""Is a venue a local independent business, or a chain?

The council needs this to eventually let a small local venue outrank a
national chain running the exact same kind of event — the boss's example was
Flix Brewhouse. It is judged ONCE PER VENUE and cached forever on
`venues.chain_scope`/`is_local`: a building's chain-ness does not change build
to build, so this must never cost a repeat call for a venue already seen.

A deterministic pre-pass covers named national chains with ZERO API calls —
the boss's own example is covered without any model involvement. Everything
else is judged in ONE batched call per build, keyed by list position rather
than by echoing a UUID back, because a model transcribing a 36-character id is
one more way for a response to come back unusable.

`is_local` is tri-state on purpose: NULL means "never judged" and must behave
exactly like today (no penalty anywhere downstream). `localness_source =
'manual'` is the human override channel — edit the row directly in Supabase —
and is never reconsidered, the same convention as content_tags_source.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

from ..core.config import settings
from ..core.dedupe import fold
from ..core.http import HttpClient
from ..core.llm import LLMUnavailable, complete_json

log = logging.getLogger("scraper.social.localness")

_SCOPES = ("local", "regional", "national", "unknown")

def _smash(text: Optional[str]) -> str:
    """fold() turns an apostrophe into a SPACE, not nothing — "Lowe's" becomes
    "lowe s", splitting the possessive "s" into its own token. Matching against
    a hand-written chain list would otherwise mean guessing fold's exact
    tokenization for every possessive brand name ("Dave & Buster's", "Denny's",
    "Sam's Club", ...). Stripping whitespace too sidesteps that entirely."""
    return fold(text).replace(" ", "")


# Named national/large-regional chains actually seen in this dataset, plus the
# obvious brands one of these would sit next to. Matched against the SMASHED
# venue name, so case, accents, punctuation and spacing all stop mattering.
_KNOWN_CHAINS = frozenset(
    _smash(name)
    for name in (
        "Flix Brewhouse", "Topgolf", "Dave & Buster's", "Cinemark", "Alamo Drafthouse",
        "Starbucks", "Chili's", "Applebee's", "Buffalo Wild Wings", "Chuck E Cheese",
        "Main Event", "Round One", "Golden Corral", "Olive Garden", "Red Lobster",
        "IHOP", "Denny's", "Chick-fil-A", "Panda Express", "Wingstop", "Five Guys",
        "Chipotle", "Hooters", "Texas Roadhouse", "Walmart", "Target",
        "Lowe's Home Improvement", "The Home Depot", "Sam's Club", "Costco",
        "Regal Cinemas", "AMC Theatres",
    )
)

# A batch this size keeps the request comfortably inside one call regardless
# of how many new venues a scrape introduces in a day, while staying small
# enough that one bad response doesn't strand a huge number of venues.
_BATCH_SIZE = 40

_SYSTEM = (
    "You judge whether local business VENUES are independent local businesses, "
    "regional chains, or national chains, for a city events Instagram account "
    "covering El Paso, Texas and Ciudad Juarez, Mexico.\n\n"
    "You are given a numbered list of venues (name and address). For each, decide "
    "one of:\n"
    "- \"local\": an independent, single-location or small local business — this "
    "also covers public/civic venues (a stadium, theater, museum, library, park, "
    "convention center, church) even though they are not small; judge FRANCHISE-"
    "NESS, not size.\n"
    "- \"regional\": a chain within Texas/the borderland region but not nationwide\n"
    "- \"national\": a nationwide or well-known franchise/chain brand\n"
    "- \"unknown\": you genuinely cannot tell from the name/address alone\n\n"
    'Reply with STRICT JSON: {"judgments": [{"i": int, "chain_scope": string, '
    '"reason": string}, ...]}\n'
    "One entry per input venue, in the SAME ORDER, using its \"i\" index — do not "
    "skip any. reason: at most 60 characters, plain."
)


def chain_scope_from_rules(venue_name: Optional[str]) -> Optional[str]:
    """A confident, zero-cost verdict, or None to defer to the model."""
    name = _smash(venue_name)
    if not name:
        return None
    return "national" if any(chain in name for chain in _KNOWN_CHAINS) else None


def _venue_of(row: dict[str, Any]) -> Optional[dict[str, Any]]:
    """The joined venues(*) row, handling PostgREST's dict-or-list-of-one shape
    the same way render._venue_label and caption._venue_label already do."""
    venues = row.get("venues")
    if isinstance(venues, list):
        venues = venues[0] if venues else None
    return venues if isinstance(venues, dict) else None


def _unjudged_venues(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: dict[str, dict[str, Any]] = {}
    for row in rows:
        v = _venue_of(row)
        if v and v.get("id") and v.get("localness_checked_at") is None:
            seen.setdefault(v["id"], v)
    return list(seen.values())


def _clean_judgments(raw: Any, count: int) -> dict[int, dict[str, str]]:
    """{index: {"chain_scope": ..., "reason": ...}}, one entry per input at most.

    Anything malformed for a given index is simply absent — that venue falls
    through to "unknown", not to an exception.
    """
    out: dict[int, dict[str, str]] = {}
    if not isinstance(raw, list):
        return out
    for item in raw:
        if not isinstance(item, dict):
            continue
        i = item.get("i")
        if not isinstance(i, int) or not (0 <= i < count) or i in out:
            continue
        scope = item.get("chain_scope")
        scope = scope if scope in _SCOPES else "unknown"
        reason = " ".join(str(item.get("reason") or "").split())[:60]
        out[i] = {"chain_scope": scope, "reason": reason}
    return out


async def _judge_batch(http: HttpClient, venues: list[dict[str, Any]]) -> dict[int, dict[str, str]]:
    lines = [f"{i}. {v.get('name') or '(unnamed)'} — {v.get('address') or '(no address)'}"
             for i, v in enumerate(venues)]
    try:
        data = await complete_json(http, system=_SYSTEM, user="\n".join(lines), max_tokens=60 * len(venues) + 200)
    except LLMUnavailable as exc:
        log.info("localness auditor unavailable for a batch of %d venue(s): %s", len(venues), exc)
        return {}
    return _clean_judgments(data.get("judgments"), len(venues))


async def fill_localness(storage: Any, http: HttpClient, rows: list[dict[str, Any]]) -> int:
    """Judge every not-yet-judged venue among `rows`. Returns how many were judged.

    Never raises. A venue left unjudged behaves exactly as it does today —
    NULL is the safe default everywhere this is read.
    """
    if not settings.council_available:
        return 0

    venues = _unjudged_venues(rows)
    if not venues:
        return 0

    now = datetime.now(timezone.utc).isoformat()
    judged = 0
    remaining: list[dict[str, Any]] = []

    for v in venues:
        scope = chain_scope_from_rules(v.get("name"))
        if scope is None:
            remaining.append(v)
            continue
        ok = await storage.cache_venue_editorial(
            v["id"],
            {
                "chain_scope": scope,
                "is_local": False,
                "localness_reason": "matches a known national chain",
                "localness_source": "rule",
                "localness_checked_at": now,
            },
        )
        judged += int(ok)

    for start in range(0, len(remaining), _BATCH_SIZE):
        batch = remaining[start : start + _BATCH_SIZE]
        verdicts = await _judge_batch(http, batch)
        for i, v in enumerate(batch):
            verdict = verdicts.get(i, {"chain_scope": "unknown", "reason": "no verdict returned"})
            scope = verdict["chain_scope"]
            ok = await storage.cache_venue_editorial(
                v["id"],
                {
                    "chain_scope": scope,
                    "is_local": True if scope == "local" else (False if scope in ("regional", "national") else None),
                    "localness_reason": verdict["reason"],
                    "localness_source": "council",
                    "localness_checked_at": now,
                },
            )
            judged += int(ok)

    if judged:
        log.info("localness auditor: judged %d venue(s)", judged)
    return judged
