"""The critic: a last look at the FINISHED carousel.

A "block" verdict never holds the post. It may carry a fix, which the build
applies before a human sees the carousel; plan_fixes() is where every bound on
that is enforced, so it is tested directly. The rest is the failure contract:
on any problem, the critic comes back with no issues, indistinguishable from a
carousel it looked at and approved of.
"""

from __future__ import annotations

from datetime import datetime
from unittest import mock
from zoneinfo import ZoneInfo

from scraper.core.llm import LLMUnavailable
from scraper.social import critic as critic_mod
from scraper.social import selection

TZ = "America/Denver"


def _cand(title="Some Event", blurb=None) -> selection.Candidate:
    return selection.Candidate(
        row={"id": "1", "title": title, "venue": "A Venue", "blurb": blurb},
        key="key1",
        score=5.0,
        start_local=datetime(2026, 9, 26, 19, tzinfo=ZoneInfo(TZ)),
    )


async def test_council_off_reports_nothing():
    with mock.patch.object(critic_mod.settings, "council_available", False):
        report = await critic_mod.run_critic(None, [_cand()], "some caption")
    assert report.applied is False
    assert report.issues == ()


async def test_an_empty_carousel_is_never_sent_to_the_model():
    calls = []

    async def counting(*_a, **_kw):
        calls.append(1)
        return {"issues": []}

    with mock.patch.object(critic_mod, "complete_json", counting), \
         mock.patch.object(critic_mod.settings, "council_available", True):
        report = await critic_mod.run_critic(None, [], "caption")
    assert calls == []
    assert report.applied is False


async def test_a_model_outage_reports_nothing():
    async def boom(*_a, **_kw):
        raise LLMUnavailable("down")

    with mock.patch.object(critic_mod, "complete_json", boom), \
         mock.patch.object(critic_mod.settings, "council_available", True):
        report = await critic_mod.run_critic(None, [_cand()], "caption")
    assert report.applied is False
    assert report.issues == ()


async def test_a_malformed_response_is_a_no_op_not_a_crash():
    async def garbage(*_a, **_kw):
        return {"issues": "not a list"}

    with mock.patch.object(critic_mod, "complete_json", garbage), \
         mock.patch.object(critic_mod.settings, "council_available", True):
        report = await critic_mod.run_critic(None, [_cand()], "caption")
    assert report.applied is True  # a response WAS parsed, just with no usable issues
    assert report.issues == ()


async def test_valid_issues_are_kept_and_shaped():
    async def real_issue(*_a, **_kw):
        return {
            "issues": [
                {"slide": 1, "issue": "blurb says 7pm but listed start is 6pm", "severity": "warn"},
                {"slide": 0, "issue": "caption date does not match cover", "severity": "block"},
            ]
        }

    with mock.patch.object(critic_mod, "complete_json", real_issue), \
         mock.patch.object(critic_mod.settings, "council_available", True):
        report = await critic_mod.run_critic(None, [_cand()], "caption")

    assert report.applied is True
    assert len(report.issues) == 2
    assert report.issues[0].severity == "warn"
    assert report.issues[1].slide == 0


def test_a_block_severity_never_raises_or_stops_anything():
    """The entire point of the design: nothing in this module can prevent a
    post. There is no code path here that returns anything but a data
    structure for notify.py to print."""
    issue = critic_mod.CriticIssue(slide=1, issue="looks wrong", severity="block")
    report = critic_mod.CriticReport(applied=True, issues=(issue,))
    # Constructing and reading a "block" issue is inert — no exception,
    # no side effect, just data.
    assert report.issues[0].severity == "block"


async def test_an_out_of_range_slide_index_is_dropped():
    async def bad_index(*_a, **_kw):
        return {"issues": [{"slide": 99, "issue": "x", "severity": "warn"}]}

    with mock.patch.object(critic_mod, "complete_json", bad_index), \
         mock.patch.object(critic_mod.settings, "council_available", True):
        report = await critic_mod.run_critic(None, [_cand()], "caption")
    assert report.issues == ()


async def test_an_invalid_severity_is_dropped():
    async def bad_severity(*_a, **_kw):
        return {"issues": [{"slide": 1, "issue": "x", "severity": "critical"}]}

    with mock.patch.object(critic_mod, "complete_json", bad_severity), \
         mock.patch.object(critic_mod.settings, "council_available", True):
        report = await critic_mod.run_critic(None, [_cand()], "caption")
    assert report.issues == ()


async def test_issues_are_capped_at_the_module_maximum():
    async def flood(*_a, **_kw):
        return {
            "issues": [
                {"slide": 1, "issue": f"issue {i}", "severity": "warn"} for i in range(20)
            ]
        }

    with mock.patch.object(critic_mod, "complete_json", flood), \
         mock.patch.object(critic_mod.settings, "council_available", True):
        report = await critic_mod.run_critic(None, [_cand()], "caption")
    assert len(report.issues) == critic_mod._MAX_ISSUES


def test_to_jsonable_round_trips_cleanly():
    report = critic_mod.CriticReport(
        applied=True, issues=(critic_mod.CriticIssue(slide=1, issue="x", severity="warn"),)
    )
    assert critic_mod.to_jsonable(report) == {
        "applied": True,
        "issues": [
            {"slide": 1, "issue": "x", "severity": "warn", "fix": "none", "duplicate_of": None}
        ],
        "fixed": [],
    }


def _picked(*scores: float) -> list[selection.Candidate]:
    return [
        selection.Candidate(
            row={"id": str(i), "title": f"Event {i}", "venue": "V", "blurb": f"blurb {i}"},
            key=f"k{i}",
            score=s,
            start_local=datetime(2026, 9, 23, 19, tzinfo=ZoneInfo(TZ)),
        )
        for i, s in enumerate(scores, start=1)
    ]


def _report(*issues: critic_mod.CriticIssue) -> critic_mod.CriticReport:
    return critic_mod.CriticReport(applied=True, issues=issues)


def test_a_duplicate_reported_on_both_slides_drops_only_the_lower_scored_one():
    """Exactly the 2026-09-23 shape: slides 6 and 8 each flagged as the other's
    duplicate. One listing must survive, and it is the better-scored one."""
    picked = _picked(1, 1, 1, 1, 1, 5.0, 1, 3.0)
    plan = critic_mod.plan_fixes(
        _report(
            critic_mod.CriticIssue(6, "dup", "block", "drop", 8),
            critic_mod.CriticIssue(8, "dup", "block", "drop", 6),
        ),
        picked,
    )
    assert plan.drop == frozenset({7})  # slide 8, the 3.0
    assert len(plan.resolved) == 2
    assert "duplicate of" in plan.notes[0]


def test_drops_are_capped():
    picked = _picked(*([1.0] * 9))
    issues = [critic_mod.CriticIssue(i, "bad", "block", "drop") for i in range(1, 6)]
    plan = critic_mod.plan_fixes(_report(*issues), picked)
    assert len(plan.drop) == critic_mod._MAX_DROPS


def test_a_warn_or_caption_level_issue_is_never_acted_on():
    raw = [
        {"slide": 1, "issue": "x", "severity": "warn", "fix": "drop"},
        {"slide": 0, "issue": "y", "severity": "block", "fix": "drop"},
    ]
    issues = critic_mod._clean_issues(raw, 3)
    assert all(i.fix == "none" for i in issues)
    assert critic_mod.plan_fixes(_report(*issues), _picked(1, 1, 1)).drop == frozenset()


def test_clear_blurb_is_moot_on_an_event_being_dropped():
    plan = critic_mod.plan_fixes(
        _report(
            critic_mod.CriticIssue(1, "made up", "block", "clear_blurb"),
            critic_mod.CriticIssue(1, "wrong", "block", "drop"),
        ),
        _picked(1, 1),
    )
    assert plan.drop == frozenset({0})
    assert plan.clear_blurb == frozenset()


def test_an_implausible_midnight_is_shown_to_the_model_as_no_time():
    """The caption hides a date-only listing's stored midnight; the critic was
    shown the raw 12:00AM and flagged the caption for being silent about it."""
    cand = selection.Candidate(
        row={"id": "1", "title": "$5.55 Movie Ticket Wednesdays!", "venue": "Flix Brewhouse"},
        key="k",
        score=1.0,
        start_local=datetime(2026, 9, 23, 0, 0, tzinfo=ZoneInfo(TZ)),
    )
    payload = critic_mod._payload([cand], "caption")
    assert "12:00AM" not in payload
    assert "(no time listed)" in payload


async def test_a_dropped_slot_is_backfilled_from_the_bench():
    from scraper.social import __main__ as social_main

    picked = _picked(5.0, 3.0)
    bench = selection.Candidate(
        row={"id": "bench", "title": "Bench Event", "venue": "W", "image_url": None},
        key="kb",
        score=2.0,
        start_local=datetime(2026, 9, 23, 20, tzinfo=ZoneInfo(TZ)),
    )
    plan = critic_mod.FixPlan(drop=frozenset({1}), clear_blurb=frozenset({0}))

    async def noop(*_a, **_kw):
        return None

    with mock.patch.object(social_main.clarify_mod, "fill_pillars", noop), \
         mock.patch.object(social_main.clarify_mod, "fill_blurbs", noop), \
         mock.patch.object(social_main, "fetch_photo", noop):
        new_picked, new_photos = await social_main._apply_critic_fixes(
            None, None, plan, picked, ["p1", "p2"], [*picked, bench],
            tz_name=TZ, dry_run=True,
        )

    assert [c.row["id"] for c in new_picked] == ["1", "bench"]
    assert new_picked[0].row["blurb"] is None
    assert picked[0].row["blurb"] == "blurb 1"  # the shared row is not mutated
    assert new_photos == ["p1", None]
