"""Four post formats sharing one renderer.

The load-bearing test here is the LAST section: adding weekend/monthly/horizon
must not change a single byte of the daily digest, which is the format the
account actually runs on every day.
"""

from __future__ import annotations

from datetime import date, datetime

import pytest

from scraper.social import caption as caption_mod
from scraper.social import render, selection

TZ = "America/Denver"


# ── bounds ────────────────────────────────────────────────────────────────────
def _dates(pair):
    return tuple(datetime.fromisoformat(x).date() for x in pair)


@pytest.mark.parametrize(
    "day, expect_friday",
    [
        (date(2026, 9, 1), date(2026, 9, 4)),   # Tuesday -> that Friday
        (date(2026, 9, 3), date(2026, 9, 4)),   # Thursday -> tomorrow
        (date(2026, 9, 4), date(2026, 9, 4)),   # Friday -> itself, not next week
        (date(2026, 9, 6), date(2026, 9, 11)),  # Sunday -> the NEXT weekend
    ],
)
def test_weekend_bounds_target_the_weekend_on_or_after_the_day(day, expect_friday):
    start, end = _dates(selection.weekend_bounds(day, TZ))
    assert start == expect_friday
    assert (end - start).days == 3  # Fri, Sat, Sun; end is exclusive Monday


def test_weekend_bounds_survive_a_dst_boundary():
    """Bounds are built from local midnights, so the span stays three calendar
    days even across the hour that DST adds or removes."""
    start, end = _dates(selection.weekend_bounds(date(2026, 11, 6), TZ))
    assert (end - start).days == 3


def test_month_bounds_cover_exactly_the_calendar_month():
    start, end = _dates(selection.month_bounds(date(2026, 9, 17), TZ))
    assert start == date(2026, 9, 1) and end == date(2026, 10, 1)


def test_month_bounds_roll_over_december():
    start, end = _dates(selection.month_bounds(date(2026, 12, 5), TZ))
    assert start == date(2026, 12, 1) and end == date(2027, 1, 1)


def test_horizon_bounds_land_about_six_months_out():
    start, end = _dates(selection.horizon_bounds(date(2026, 9, 3), TZ))
    assert 170 <= (start - date(2026, 9, 3)).days <= 190
    assert (end - start).days == 60


# ── period_key ────────────────────────────────────────────────────────────────
def test_daily_posts_have_no_period_key():
    """They are already constrained by (post_date, slot) from migration 0006."""
    assert selection.period_key("digest", date(2026, 9, 3), TZ) is None


def test_two_days_in_the_same_weekend_share_a_period_key():
    a = selection.period_key("weekend", date(2026, 9, 1), TZ)
    b = selection.period_key("weekend", date(2026, 9, 3), TZ)
    assert a == b and a.startswith("2026-W")


def test_period_key_changes_across_the_weekend_boundary():
    """Sunday belongs to the weekend that just ended for a reader, but the
    builder targets the NEXT one — so its key must differ from Thursday's."""
    assert selection.period_key("weekend", date(2026, 9, 3), TZ) != selection.period_key(
        "weekend", date(2026, 9, 6), TZ
    )


def test_monthly_period_key_is_the_month():
    assert selection.period_key("monthly", date(2026, 9, 17), TZ) == "2026-09"
    assert selection.period_key("monthly", date(2026, 10, 1), TZ) == "2026-10"


# ── profiles ──────────────────────────────────────────────────────────────────
def _row(**kw):
    base = {"title": "A show", "categories": ["Music"], "venue": "The Venue"}
    base.update(kw)
    return base


def test_digest_profile_is_the_historical_default():
    """The acceptance criterion for the ScoreProfile refactor: scoring with
    the digest profile equals scoring with no profile at all."""
    row = _row(ticket_links=[{"url": "x"}])
    when = datetime(2026, 9, 3, 19, 0)
    assert selection.score_event(row, start_local=when) == selection.score_event(
        row, start_local=when, profile=selection.PROFILES["digest"]
    )


def test_horizon_weights_tickets_far_more_heavily_than_the_digest():
    row = _row(ticket_links=[{"url": "x"}])
    plain = _row()
    when = datetime(2026, 9, 3, 19, 0)
    digest_gap = selection.score_event(row, start_local=when) - selection.score_event(
        plain, start_local=when
    )
    horizon_gap = selection.score_event(
        row, start_local=when, profile=selection.PROFILES["horizon"]
    ) - selection.score_event(plain, start_local=when, profile=selection.PROFILES["horizon"])
    assert horizon_gap > digest_gap


def test_horizon_drops_events_with_no_ticket_link():
    """Six months out, an event without tickets on sale is almost always a
    recurring fixture scheduled far ahead, not something to plan around."""
    rows = [
        _row(id=f"{i}", title=f"Show {i}", start_time="2027-03-05T19:00:00+00:00")
        for i in range(6)
    ]
    rows[0]["ticket_links"] = [{"url": "x"}]
    picked = selection.choose(rows, tz_name=TZ, profile=selection.PROFILES["horizon"])
    assert [c.row["title"] for c in picked] == ["Show 0"]


def test_the_digest_keeps_events_without_tickets():
    rows = [
        _row(id=f"{i}", title=f"Show {i}", start_time="2026-09-03T19:00:00+00:00")
        for i in range(6)
    ]
    assert len(selection.choose(rows, tz_name=TZ)) > 1


# ── render ────────────────────────────────────────────────────────────────────
def test_variant_cycles_over_the_actual_builder_count():
    """The bug this replaces: `% 3` was hardcoded while _accent_for used
    len(), so a fourth layout would sit in the tuple and never be reached."""
    seen = {render._variant_for(i, 4) for i in range(2, 10)}
    assert seen == {0, 1, 2, 3}


def test_every_kind_has_builders_and_a_cover_spec():
    for kind in selection.BOUNDS_FOR_KIND:
        assert kind in render._BUILDERS_BY_KIND
        assert kind in render._COVER_SPECS


def test_formats_open_with_different_layouts():
    """Otherwise a weekend post looks like that morning's daily one."""
    assert render._BUILDERS_BY_KIND["weekend"][0] is not render._BUILDERS_BY_KIND["digest"][0]


def test_covers_render_for_every_kind():
    for kind in selection.BOUNDS_FOR_KIND:
        blob = render.render_cover(date(2026, 9, 3), 5, "epchisme.com", kind=kind)
        assert blob.startswith(b"\xff\xd8"), f"{kind} cover is not a JPEG"


def test_a_long_period_label_still_fits():
    """"SEP 28 - OCT 4" is far wider than the single date the line was
    originally sized for; wrap_to_fit has to shrink rather than overflow."""
    blob = render.render_cover(
        date(2026, 9, 28), 9, "epchisme.com", kind="weekend", period_label="SEP 28 - OCT 4"
    )
    assert blob.startswith(b"\xff\xd8")


# ── caption ───────────────────────────────────────────────────────────────────
class _Cand:
    def __init__(self):
        self.row = _row(id="1", categories=["Music"])
        self.start_local = None


def test_weekend_caption_uses_the_period_label_not_a_single_date():
    text = caption_mod.build_caption(
        date(2026, 9, 3), [_Cand()], kind="weekend", period_label="SEP 4-6"
    )
    assert "SEP 4-6" in text


def test_each_kind_gets_its_own_voice():
    day, picked = date(2026, 9, 3), [_Cand()]
    heads = {
        k: caption_mod.build_caption(day, picked, kind=k).splitlines()[0]
        for k in ("digest", "weekend", "monthly", "horizon")
    }
    assert len(set(heads.values())) == 4


def test_every_kind_stays_under_the_caption_limit_with_a_full_carousel():
    picked = [_Cand() for _ in range(9)]
    for cand in picked:
        cand.row["title"] = "An Extremely Long Event Title That Goes On " * 3
    for kind in ("digest", "weekend", "monthly", "horizon"):
        assert len(caption_mod.build_caption(date(2026, 9, 3), picked, kind=kind)) <= caption_mod.MAX_CAPTION


# ── the digest must not have moved ────────────────────────────────────────────
def test_digest_cover_is_unchanged_by_the_kind_parameter():
    assert render.render_cover(date(2026, 9, 3), 5) == render.render_cover(
        date(2026, 9, 3), 5, kind="digest"
    )


def test_digest_slides_are_unchanged_by_the_kind_parameter():
    row = _row(id="abc", title="A Show", venue="Lowbrow")
    for i in range(2, 11):
        assert render.render_event_slide(i, 10, row, None) == render.render_event_slide(
            i, 10, row, None, kind="digest"
        )


def test_digest_caption_is_unchanged_by_the_kind_parameter():
    day, picked = date(2026, 9, 3), [_Cand()]
    assert caption_mod.build_caption(day, picked) == caption_mod.build_caption(
        day, picked, kind="digest"
    )
