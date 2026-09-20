"""A last look at the FINISHED carousel — final picked events plus caption —
before a human sees it in Telegram.

Runs after rendering, once the carousel is settled. A "block" verdict never
actually blocks anything: it becomes one line in the Telegram message. The
human already has drop-event, edit-caption and swap-photo buttons for exactly
this situation; the critic's job is to point at a problem, not to act on it.
Giving it a button of its own would be a second, competing way to approve or
reject a post, which is not this pipeline's design (see ADR-0003).

Same failure contract as every other council role: on any problem this
returns applied=False with no issues, which is indistinguishable from a
carousel the critic looked at and found nothing wrong with. That is
deliberately the same outward behaviour as "the council is off".
"""

from __future__ import annotations

import dataclasses
import logging
from typing import Any

from ..core.config import settings
from ..core.http import HttpClient
from ..core.llm import LLMUnavailable, complete_json
from . import selection

log = logging.getLogger("scraper.social.critic")

_SYSTEM = (
    "You are proofreading a FINISHED Instagram carousel for a local events "
    "account covering El Paso, Texas and Ciudad Juarez, Mexico, right before a "
    "human approves it.\n\n"
    "You are given the final ordered list of events on the carousel and the "
    "full caption text. Look for CONCRETE problems: a caption line or blurb "
    "that contradicts that event's own listed time; two entries that read as "
    "the same real happening under different titles; a blurb that reads as "
    "invented rather than drawn from real information; anything that would "
    "read as an outright error if posted unchanged.\n\n"
    'Reply with STRICT JSON: {"issues": [{"slide": int, "issue": string, '
    '"severity": "block"|"warn"}, ...]}\n'
    "- slide: the event's position in the list given, 1-indexed. Use 0 only "
    "for a problem with the caption as a whole, not tied to one event.\n"
    "- issue: at most 90 characters, specific and actionable for someone "
    "glancing at a phone.\n"
    "- severity=\"block\": something that would look wrong or misleading if "
    "posted exactly as-is. \"warn\": worth a second look, probably fine.\n"
    '- Return {"issues": []} if nothing is wrong. Most carousels are fine — do '
    "not invent a problem in order to have something to say."
)

_MAX_ISSUES = 6
_MAX_CAPTION_CHARS = 2500


@dataclasses.dataclass(frozen=True)
class CriticIssue:
    slide: int
    issue: str
    severity: str


@dataclasses.dataclass(frozen=True)
class CriticReport:
    applied: bool
    issues: tuple[CriticIssue, ...] = ()


def _payload(picked: list[selection.Candidate], caption: str) -> str:
    lines = [f"Caption:\n{caption[:_MAX_CAPTION_CHARS]}", "", "Events:"]
    for i, cand in enumerate(picked, start=1):
        row = cand.row
        when = cand.start_local.strftime("%a %I:%M%p") if cand.start_local else "(no time)"
        blurb = row.get("blurb")
        lines.append(
            f"{i}. {row.get('title')} — {selection.venue_key(row) or '?'} — {when}"
            + (f"\n   blurb: {blurb}" if blurb else "")
        )
    return "\n".join(lines)


def _clean_issues(raw: Any, slide_count: int) -> tuple[CriticIssue, ...]:
    if not isinstance(raw, list):
        return ()
    out: list[CriticIssue] = []
    for item in raw:
        if not isinstance(item, dict) or len(out) >= _MAX_ISSUES:
            continue
        slide = item.get("slide")
        if not isinstance(slide, int) or not (0 <= slide <= slide_count):
            continue
        severity = item.get("severity")
        if severity not in ("block", "warn"):
            continue
        issue = " ".join(str(item.get("issue") or "").split())[:90]
        if not issue:
            continue
        out.append(CriticIssue(slide=slide, issue=issue, severity=severity))
    return tuple(out)


async def run_critic(
    http: HttpClient, picked: list[selection.Candidate], caption: str
) -> CriticReport:
    """Never raises. On any failure, no issues are reported — identical to a
    carousel the critic examined and approved of."""
    if not settings.council_available or not picked:
        return CriticReport(applied=False)

    try:
        data = await complete_json(
            http, system=_SYSTEM, user=_payload(picked, caption), max_tokens=500
        )
        issues = _clean_issues(data.get("issues"), len(picked))
    except LLMUnavailable as exc:
        log.info("critic unavailable: %s", exc)
        return CriticReport(applied=False)
    except Exception as exc:  # noqa: BLE001 - a parsing bug must never break a build
        log.warning("critic failed unexpectedly: %s", exc)
        return CriticReport(applied=False)

    if issues:
        log.info("critic: %d issue(s) flagged", len(issues))
    return CriticReport(applied=True, issues=issues)


def to_jsonable(report: CriticReport) -> dict[str, Any]:
    return {
        "applied": report.applied,
        "issues": [dataclasses.asdict(i) for i in report.issues],
    }
