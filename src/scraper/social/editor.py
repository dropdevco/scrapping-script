"""One per-build model call that nudges rank and drops within an already
diversity-capped pool.

Runs between selection.choose() and the photo-fetch loop in __main__.py, which
is what makes this safe to run at all: choose() is called with
max_slides=len(rows), so `ranked` is the WHOLE diversity-capped pool — 30-60
events deep on a normal El Paso day, versus the 9 that actually make the
carousel. An exclusion here costs nothing; the next-ranked event fills the
slot. The editor can reorder and drop within that pool; it can never shrink
the post, because every guarantee below is enforced in pure Python, not by
asking the model nicely.

`temperature` is removed on current models, so determinism cannot be bought
that way. Bounded, deterministic post-processing is the only real lever:

  1. excludes capped at max(2, len(ranked) // 4), applied in RANK order
  2. never reduce the pool below floor (ig_max_slides + 2)
  3. more than half the pool excluded -> the WHOLE response is discarded
  4. score_delta clamped to +/-2.0 (deterministic scores span roughly 0-8,
     so this can reshuffle neighbours; it cannot invert the ranking)
  5. a verdict for an id outside the pool is dropped silently

On ANY failure this returns the input list unchanged and applied=False, so
"model down" is provably identical to "council never ran" — see
test_editor.py's outage test.
"""

from __future__ import annotations

import dataclasses
import logging
from datetime import datetime
from typing import Any, Optional
from zoneinfo import ZoneInfo

from ..core.config import settings
from ..core.http import HttpClient
from ..core.llm import LLMUnavailable, complete_json
from . import selection

log = logging.getLogger("scraper.social.editor")

_SYSTEM = (
    "You are the daily editor for a local events Instagram account covering El "
    "Paso, Texas and Ciudad Juarez, Mexico. You are given a RANKED list of "
    "candidate events for one carousel post — already deduplicated and "
    "diversity-capped by an automated scorer. Only the top few will actually be "
    "posted; the rest is a deep bench.\n\n"
    "Add the judgment a keyword scorer cannot: is an event actually interesting "
    "or is it routine filler (a recurring meeting, a generic class); does it "
    "duplicate something else already in the list in substance even if the "
    "title differs; is a promising-looking title actually thin once you read "
    "its details.\n\n"
    'Reply with STRICT JSON: {"verdicts": [{"i": int, "verdict": "include"|'
    '"exclude", "score_delta": number, "reason": string}, ...]}\n'
    "- Only include an entry for an event you have an actual opinion on; omit "
    "one entirely to leave it untouched.\n"
    "- score_delta: a SMALL nudge, roughly -2 to +2. Most events you do include "
    "an entry for should get 0 and just be there for the reason.\n"
    "- verdict=\"exclude\": use sparingly, only for events that are genuinely "
    "low-value or redundant with something else in the list. Do not exclude "
    "things merely to have something to say, and do not exclude more than a "
    "handful — most of the list should be left alone.\n"
    "- reason: at most 60 characters, plain, for a human glancing at your work."
)

_MAX_DESCRIPTION_CHARS = 200


@dataclasses.dataclass(frozen=True)
class EditorReport:
    applied: bool
    ranked: list[selection.Candidate]
    notes: list[str]
    error: Optional[str] = None


def _sort_key(cand: selection.Candidate, tz_name: str):
    # Mirrors selection.choose's own sort key exactly, so re-sorting after an
    # edit produces the same tie-breaking behaviour choose() already has.
    return (-cand.score, cand.start_local or datetime.max.replace(tzinfo=ZoneInfo(tz_name)))


def _payload(ranked: list[selection.Candidate]) -> str:
    lines = []
    for i, cand in enumerate(ranked):
        row = cand.row
        when = cand.start_local.strftime("%a %I:%M%p") if cand.start_local else "(no time)"
        pillar = ", ".join(row.get("content_tags") or row.get("categories") or []) or "(none)"
        venue = selection.venue_key(row) or "?"
        desc = " ".join(str(row.get("description") or "").split())[:_MAX_DESCRIPTION_CHARS]
        lines.append(
            f"{i}. {row.get('title')} — {venue} — {when} — pillar: {pillar}"
            + (f"\n   {desc}" if desc else "")
        )
    return "\n".join(lines)


def _clean_verdicts(raw: Any, count: int) -> dict[int, dict[str, Any]]:
    out: dict[int, dict[str, Any]] = {}
    if not isinstance(raw, list):
        return out
    for item in raw:
        if not isinstance(item, dict):
            continue
        i = item.get("i")
        if not isinstance(i, int) or not (0 <= i < count) or i in out:
            continue
        verdict = item.get("verdict")
        if verdict not in ("include", "exclude"):
            continue
        delta = item.get("score_delta")
        delta = max(-2.0, min(2.0, float(delta))) if isinstance(delta, (int, float)) else 0.0
        reason = " ".join(str(item.get("reason") or "").split())[:60]
        out[i] = {"verdict": verdict, "score_delta": delta, "reason": reason}
    return out


async def run_editor(
    http: HttpClient, ranked: list[selection.Candidate], *, tz_name: str
) -> EditorReport:
    """Never raises. On any failure, `ranked` comes back byte-identical."""
    noop = EditorReport(applied=False, ranked=ranked, notes=[])
    if not settings.council_available or len(ranked) < 2:
        return noop

    try:
        data = await complete_json(
            http, system=_SYSTEM, user=_payload(ranked), max_tokens=60 * len(ranked) + 300
        )
        verdicts = _clean_verdicts(data.get("verdicts"), len(ranked))
    except LLMUnavailable as exc:
        log.info("editor unavailable: %s", exc)
        return noop
    except Exception as exc:  # noqa: BLE001 - a parsing bug must never break a build
        log.warning("editor failed unexpectedly: %s", exc)
        return noop

    exclude_order = [i for i in range(len(ranked)) if verdicts.get(i, {}).get("verdict") == "exclude"]
    if len(exclude_order) > len(ranked) // 2:
        msg = f"editor excluded {len(exclude_order)}/{len(ranked)} — discarding the whole response"
        log.error(msg)
        return EditorReport(applied=False, ranked=ranked, notes=[], error=msg)

    max_excludes = max(2, len(ranked) // 4)
    floor = settings.ig_max_slides + 2
    allowed = max(0, min(max_excludes, len(ranked) - floor))
    exclude_set = set(exclude_order[:allowed])

    notes: list[str] = []
    kept: list[selection.Candidate] = []
    for i, cand in enumerate(ranked):
        v = verdicts.get(i)
        if i in exclude_set:
            notes.append(f'dropped "{cand.row.get("title")}" — {v["reason"] or "editor judgment"}')
            continue
        if v and v["score_delta"]:
            cand = dataclasses.replace(cand, score=cand.score + v["score_delta"])
            notes.append(f'nudged "{cand.row.get("title")}" ({v["score_delta"]:+.1f}) — {v["reason"]}')
        kept.append(cand)

    kept.sort(key=lambda c: _sort_key(c, tz_name))
    if notes:
        log.info("editor: %d note(s) — %s", len(notes), "; ".join(notes)[:400])
    return EditorReport(applied=True, ranked=kept, notes=notes)


def to_jsonable(report: EditorReport) -> dict[str, Any]:
    return {"applied": report.applied, "notes": report.notes, "error": report.error}
