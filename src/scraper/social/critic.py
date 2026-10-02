"""A last look at the FINISHED carousel — final picked events plus caption —
before a human sees it in Telegram.

Runs after rendering, once the carousel is settled. A "block" verdict never
blocks the POST: it never approves, rejects or holds anything, and it gets no
button of its own — that would be a second, competing way to approve or
reject a post, which is not this pipeline's design (see ADR-0003).

It can, however, repair the carousel before the human ever sees it. A block
may carry a `fix` — "drop" (take the event off; the build backfills the slot
from the bench, the same way an editor exclusion does) or "clear_blurb" (the
event stays, its model-written blurb goes). __main__ applies the fixes that
plan_fixes() lets through, re-renders, and runs the critic ONCE more,
report-only. Reporting a duplicate and then shipping it anyway, which is what
the 2026-09-23 digest did, helps nobody.

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
    '"severity": "block"|"warn", "fix": "none"|"drop"|"clear_blurb", '
    '"duplicate_of": int|null}, ...]}\n'
    "- slide: the event's position in the list given, 1-indexed. Use 0 only "
    "for a problem with the caption as a whole, not tied to one event.\n"
    "- issue: at most 90 characters, specific and actionable for someone "
    "glancing at a phone.\n"
    "- severity=\"block\": something that would look wrong or misleading if "
    "posted exactly as-is. \"warn\": worth a second look, probably fine.\n"
    "- fix: what would repair a block without a human. \"drop\": take that "
    "event off the carousel (another event fills the slot). \"clear_blurb\": "
    "keep the event but remove its blurb — for a blurb that is wrong or "
    "invented. \"none\": anything else, including every warn.\n"
    "- Two entries that are the same happening: report it ONCE, on either "
    "slide, with fix \"drop\" and duplicate_of set to the other slide.\n"
    '- An event shown with no time is a deliberate all-day/unknown-time '
    'listing, not an error.\n'
    '- Return {"issues": []} if nothing is wrong. Most carousels are fine — do '
    "not invent a problem in order to have something to say."
)

_MAX_ISSUES = 6
_MAX_CAPTION_CHARS = 2500
# At most this many events leave a carousel on the critic's say-so per build;
# anything beyond it stays a flag for the human. Same reasoning as the editor's
# exclude cap: a runaway model must not be able to hollow out a post.
_MAX_DROPS = 2
_FIXES = ("none", "drop", "clear_blurb")


@dataclasses.dataclass(frozen=True)
class CriticIssue:
    slide: int
    issue: str
    severity: str
    fix: str = "none"
    duplicate_of: int | None = None


@dataclasses.dataclass(frozen=True)
class FixPlan:
    """What the build should actually do, in 0-based indexes into `picked`."""

    drop: frozenset[int] = frozenset()
    clear_blurb: frozenset[int] = frozenset()
    notes: tuple[str, ...] = ()
    # The issues this plan resolves, so they are not ALSO shown as open blocks.
    resolved: tuple[CriticIssue, ...] = ()


@dataclasses.dataclass(frozen=True)
class CriticReport:
    applied: bool
    issues: tuple[CriticIssue, ...] = ()


def _payload(picked: list[selection.Candidate], caption: str) -> str:
    lines = [f"Caption:\n{caption[:_MAX_CAPTION_CHARS]}", "", "Events:"]
    for i, cand in enumerate(picked, start=1):
        row = cand.row
        # Same rule the caption and slide use: an implausible hour (a
        # date-only listing stored at midnight) is shown as no time at all.
        # Printing the raw 12:00AM here made the critic "catch" a mismatch
        # with a caption that was, correctly, silent about the time.
        when = (
            cand.start_local.strftime("%a %I:%M%p")
            if selection.has_plausible_time(cand.start_local)
            else "(no time listed)"
        )
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
        fix = item.get("fix") if item.get("fix") in _FIXES else "none"
        if severity != "block" or slide == 0:
            fix = "none"  # only a block tied to one event is ever acted on
        dup = item.get("duplicate_of")
        if not isinstance(dup, int) or not (1 <= dup <= slide_count) or dup == slide:
            dup = None
        out.append(
            CriticIssue(slide=slide, issue=issue, severity=severity, fix=fix, duplicate_of=dup)
        )
    return tuple(out)


def plan_fixes(report: CriticReport, picked: list[selection.Candidate]) -> FixPlan:
    """Turn the critic's proposals into edits, with every guarantee in Python.

    - For a duplicate pair, the LOWER-scored listing is dropped, whichever
      slide the model happened to name — and a pair reported twice (once per
      slide, as the 2026-09-23 critic did) still drops only one of them.
    - Never more than _MAX_DROPS events, applied in the order reported.
    - A clear_blurb on an event that has no blurb, or is being dropped, is moot.
    """
    drop: list[int] = []
    clear: list[int] = []
    notes: list[str] = []
    resolved: list[CriticIssue] = []
    handled_pairs: set[frozenset[int]] = set()

    def title(i: int) -> str:
        return str(picked[i].row.get("title") or "?")

    for issue in report.issues:
        i = issue.slide - 1
        if issue.fix == "drop":
            if issue.duplicate_of is not None:
                j = issue.duplicate_of - 1
                pair = frozenset((i, j))
                if pair in handled_pairs:
                    resolved.append(issue)
                    continue
                victim, keeper = (i, j) if picked[i].score <= picked[j].score else (j, i)
                if victim in drop or len(drop) >= _MAX_DROPS:
                    continue
                handled_pairs.add(pair)
                drop.append(victim)
                notes.append(f'dropped "{title(victim)}" — duplicate of "{title(keeper)}"')
            else:
                if i in drop or len(drop) >= _MAX_DROPS:
                    continue
                drop.append(i)
                notes.append(f'dropped "{title(i)}" — {issue.issue}')
            resolved.append(issue)
        elif issue.fix == "clear_blurb":
            if i in drop or i in clear or not picked[i].row.get("blurb"):
                continue
            clear.append(i)
            notes.append(f'removed the blurb on "{title(i)}" — {issue.issue}')
            resolved.append(issue)

    # A clear_blurb reported before the same event was dropped is moot.
    clear = [i for i in clear if i not in drop]
    return FixPlan(
        drop=frozenset(drop),
        clear_blurb=frozenset(clear),
        notes=tuple(notes),
        resolved=tuple(resolved),
    )


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


def to_jsonable(report: CriticReport, fixed: tuple[str, ...] = ()) -> dict[str, Any]:
    """`report` is the critic's LAST word on the carousel as it will ship;
    `fixed` is what an earlier pass already repaired on the way there."""
    return {
        "applied": report.applied,
        "issues": [dataclasses.asdict(i) for i in report.issues],
        "fixed": list(fixed),
    }
