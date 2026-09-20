"""The critic: a last look at the FINISHED carousel.

A "block" verdict never blocks anything — it becomes one line in the Telegram
message, because the human already has drop/edit/swap buttons for exactly
this situation. Every test here is really testing the failure contract: on
any problem, the critic comes back with no issues, indistinguishable from a
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
        "issues": [{"slide": 1, "issue": "x", "severity": "warn"}],
    }
