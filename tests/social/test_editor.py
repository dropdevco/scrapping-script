"""The editor: one per-build model call over the whole diversity-capped pool.

Every guarantee here is enforced in pure Python, never trusted from the model
response — the whole design rests on the fact that "model down" and "model
misbehaved" must both come back byte-identical to "council never ran".
"""

from __future__ import annotations

from datetime import datetime, timedelta
from unittest import mock
from zoneinfo import ZoneInfo

from scraper.core.llm import LLMUnavailable
from scraper.social import editor as editor_mod
from scraper.social import selection

TZ = "America/Denver"


def _cand(i: int, score: float = 5.0) -> selection.Candidate:
    start = datetime(2026, 9, 26, 8, tzinfo=ZoneInfo(TZ)) + timedelta(minutes=15 * i)
    return selection.Candidate(
        row={"id": str(i), "title": f"Event {i}", "venue": f"Venue {i}"},
        key=f"key{i}",
        score=score,
        start_local=start,
    )


def _pool(n: int) -> list[selection.Candidate]:
    """Descending scores, so index 0 is the top-ranked candidate."""
    return [_cand(i, score=float(n - i)) for i in range(n)]


async def test_council_off_leaves_the_pool_byte_identical():
    ranked = _pool(20)
    with mock.patch.object(editor_mod.settings, "council_available", False):
        report = await editor_mod.run_editor(None, ranked, tz_name=TZ)
    assert report.applied is False
    assert report.ranked == ranked


async def test_a_pool_of_one_is_never_sent_to_the_model():
    calls = []

    async def counting(*_a, **_kw):
        calls.append(1)
        return {"verdicts": []}

    ranked = _pool(1)
    with mock.patch.object(editor_mod, "complete_json", counting), \
         mock.patch.object(editor_mod.settings, "council_available", True):
        report = await editor_mod.run_editor(None, ranked, tz_name=TZ)
    assert calls == []
    assert report.applied is False
    assert report.ranked == ranked


async def test_a_model_outage_leaves_the_pool_byte_identical():
    async def boom(*_a, **_kw):
        raise LLMUnavailable("network is down")

    ranked = _pool(20)
    with mock.patch.object(editor_mod, "complete_json", boom), \
         mock.patch.object(editor_mod.settings, "council_available", True):
        report = await editor_mod.run_editor(None, ranked, tz_name=TZ)
    assert report.applied is False
    assert report.ranked == ranked


async def test_a_malformed_response_is_a_no_op_not_a_crash():
    async def garbage(*_a, **_kw):
        return {"verdicts": "not a list"}

    ranked = _pool(20)
    with mock.patch.object(editor_mod, "complete_json", garbage), \
         mock.patch.object(editor_mod.settings, "council_available", True):
        report = await editor_mod.run_editor(None, ranked, tz_name=TZ)
    assert report.applied is True  # a response WAS parsed, just with zero usable verdicts
    assert report.ranked == ranked


async def test_excludes_are_capped_and_applied_in_rank_order():
    """20 candidates, ig_max_slides patched to 5 (floor = 7). Requesting 8
    excludes must only apply the allowed number, taken in the order they
    appear in the ranked pool, not the order the model listed them."""
    ranked = _pool(20)
    # Model asks to exclude 8 of them (indices high to low, out of rank order,
    # to prove the cap is applied in POOL order, not response order).
    exclude_indices = [15, 3, 7, 10, 12, 1, 18, 5]

    async def excludes_many(*_a, **_kw):
        return {
            "verdicts": [
                {"i": i, "verdict": "exclude", "score_delta": 0, "reason": "low value"}
                for i in exclude_indices
            ]
        }

    with mock.patch.object(editor_mod, "complete_json", excludes_many), \
         mock.patch.object(editor_mod.settings, "council_available", True), \
         mock.patch.object(editor_mod.settings, "ig_max_slides", 5):
        report = await editor_mod.run_editor(None, ranked, tz_name=TZ)

    assert report.applied is True
    max_excludes = max(2, 20 // 4)  # 5
    floor = 5 + 2  # 7
    allowed = min(max_excludes, 20 - floor)  # 5
    assert len(ranked) - len(report.ranked) == allowed
    # The ones actually dropped are the LOWEST-INDEX requested excludes —
    # i.e. rank order, not response order.
    expected_dropped = sorted(exclude_indices)[:allowed]
    kept_titles = {c.row["title"] for c in report.ranked}
    for i in expected_dropped:
        assert f"Event {i}" not in kept_titles


async def test_more_than_half_excluded_discards_the_whole_response():
    ranked = _pool(10)

    async def excludes_most(*_a, **_kw):
        return {
            "verdicts": [
                {"i": i, "verdict": "exclude", "score_delta": 0, "reason": "x"}
                for i in range(6)  # 6 of 10, > half
            ]
        }

    with mock.patch.object(editor_mod, "complete_json", excludes_most), \
         mock.patch.object(editor_mod.settings, "council_available", True):
        report = await editor_mod.run_editor(None, ranked, tz_name=TZ)

    assert report.applied is False
    assert report.ranked == ranked
    assert report.error is not None


async def test_score_delta_is_clamped_and_reshuffles_neighbours():
    ranked = _pool(5)  # scores 5,4,3,2,1 for indices 0..4

    async def nudge(*_a, **_kw):
        # Push index 4 (score 1.0) up by way more than the clamp allows.
        return {"verdicts": [{"i": 4, "verdict": "include", "score_delta": 999, "reason": "great"}]}

    with mock.patch.object(editor_mod, "complete_json", nudge), \
         mock.patch.object(editor_mod.settings, "council_available", True):
        report = await editor_mod.run_editor(None, ranked, tz_name=TZ)

    nudged = next(c for c in report.ranked if c.row["title"] == "Event 4")
    assert nudged.score == 1.0 + 2.0  # clamped to +2.0, not +999
    # Even at max clamp, index 4 (now 3.0) cannot leapfrog index 0 (5.0) —
    # the clamp is deliberately too small to invert a real ranking gap.
    assert report.ranked[0].row["title"] == "Event 0"


async def test_a_verdict_for_an_index_outside_the_pool_is_ignored():
    ranked = _pool(5)

    async def out_of_range(*_a, **_kw):
        return {"verdicts": [{"i": 99, "verdict": "exclude", "score_delta": 0, "reason": "x"}]}

    with mock.patch.object(editor_mod, "complete_json", out_of_range), \
         mock.patch.object(editor_mod.settings, "council_available", True):
        report = await editor_mod.run_editor(None, ranked, tz_name=TZ)
    assert len(report.ranked) == 5


def test_to_jsonable_round_trips_cleanly():
    report = editor_mod.EditorReport(applied=True, ranked=[], notes=["dropped x — reason"])
    assert editor_mod.to_jsonable(report) == {
        "applied": True,
        "notes": ["dropped x — reason"],
        "error": None,
    }
