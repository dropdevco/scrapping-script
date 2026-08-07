from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

from scraper.social import selection

TZ = "America/Denver"


def _hours_between(start_iso: str, end_iso: str) -> float:
    delta = datetime.fromisoformat(end_iso) - datetime.fromisoformat(start_iso)
    return delta.total_seconds() / 3600


def test_day_bounds_is_24h_on_a_normal_day():
    start, end = selection.day_bounds(date(2026, 8, 5), TZ)
    assert _hours_between(start, end) == 24


def test_day_bounds_handles_dst_transitions():
    # US DST 2026: forward Mar 8 (23h day), back Nov 1 (25h day). Getting this
    # wrong shifts every evening event onto the wrong date twice a year.
    spring_start, spring_end = selection.day_bounds(date(2026, 3, 8), TZ)
    assert _hours_between(spring_start, spring_end) == 23

    fall_start, fall_end = selection.day_bounds(date(2026, 11, 1), TZ)
    assert _hours_between(fall_start, fall_end) == 25


def test_day_bounds_is_half_open():
    """An event at exactly midnight tomorrow belongs to tomorrow."""
    start, end = selection.day_bounds(date(2026, 8, 5), TZ)
    next_midnight = datetime(2026, 8, 6, 0, 0, tzinfo=ZoneInfo(TZ))
    assert datetime.fromisoformat(end) == next_midnight
    assert datetime.fromisoformat(start) < next_midnight


def _event(**kw):
    row = {
        "id": kw.pop("id", "00000000-0000-0000-0000-000000000001"),
        "title": "Some Event",
        "start_time": "2026-08-05T19:00:00-06:00",
        "categories": ["Community"],
        "location": "El Paso, TX",
    }
    row.update(kw)
    return row


def test_dedupe_key_is_stable_across_occurrences():
    """Recurring events get a fresh uuid per date, so only a content key can
    detect the repeat."""
    keys = {
        selection.dedupe_key(
            _event(
                id=f"id-{d}",
                title=f"Toddler Storytime 2026-08-{d:02d}",
                start_time=f"2026-08-{d:02d}T10:30:00-06:00",
                venue="Main Library",
            )
        )
        for d in (5, 6, 7)
    }
    assert len(keys) == 1


def test_dedupe_key_strips_varied_occurrence_markers():
    base = selection.dedupe_key(_event(title="Yoga in the Park", venue="Park"))
    variants = (
        "Yoga in the Park — Aug 5",
        "Yoga in the Park (Session 3)",
        "Yoga in the Park #4",
    )
    for variant in variants:
        assert selection.dedupe_key(_event(title=variant, venue="Park")) == base


def test_dedupe_key_differs_by_venue():
    a = selection.dedupe_key(_event(title="Trivia Night", venue="Bar One"))
    b = selection.dedupe_key(_event(title="Trivia Night", venue="Bar Two"))
    assert a != b


def _start(hour: int) -> datetime:
    return datetime(2026, 8, 5, hour, 0, tzinfo=ZoneInfo(TZ))


def test_ticketed_evening_music_outranks_morning_library_event():
    music = selection.score_event(
        _event(categories=["Music"], ticket_links=[{"url": "x"}], venue_id="v1",
               description="x" * 200),
        start_local=_start(20),
        image_short_side=1200,
    )
    library = selection.score_event(
        _event(categories=["Libraries", "Community"]),
        start_local=_start(10),
        recurrence_count=10,
        image_short_side=700,
    )
    assert music > library + 3


def test_recently_posted_is_penalised():
    row = _event(categories=["Music"])
    fresh = selection.score_event(row, start_local=_start(20))
    repeat = selection.score_event(row, start_local=_start(20), recently_posted=True)
    assert repeat == fresh - 2.5


def test_choose_applies_caps_and_returns_chronological_order():
    rows = [
        _event(id="a", title="Late Concert", categories=["Music"], venue="Venue A",
               start_time="2026-08-05T22:00:00-06:00", ticket_links=[{"url": "x"}]),
        _event(id="b", title="Morning Market", categories=["Food & Drink"], venue="Venue B",
               start_time="2026-08-05T09:00:00-06:00"),
        _event(id="c", title="Afternoon Gallery", categories=["Arts & Theatre"], venue="Venue C",
               start_time="2026-08-05T14:00:00-06:00"),
    ]
    picked = selection.choose(rows, tz_name=TZ, max_slides=5)
    assert [c.row["id"] for c in picked] == ["b", "c", "a"]


def test_choose_enforces_max_per_venue():
    rows = [
        _event(id=str(i), title=f"Show {i}", categories=["Music"], venue="Same Venue",
               start_time=f"2026-08-05T{18 + i}:00:00-06:00")
        for i in range(4)
    ]
    picked = selection.choose(rows, tz_name=TZ, max_slides=9, max_per_venue=2)
    assert len(picked) == 2


def test_choose_collapses_same_day_repeats():
    rows = [
        _event(id="x", title="Storytime — Aug 5", venue="Library",
               start_time="2026-08-05T10:00:00-06:00"),
        _event(id="y", title="Storytime (Session 2)", venue="Library",
               start_time="2026-08-05T14:00:00-06:00"),
    ]
    picked = selection.choose(rows, tz_name=TZ, max_slides=9)
    assert len(picked) == 1


def test_choose_respects_max_slides():
    rows = [
        _event(id=str(i), title=f"Distinct Event {i}", categories=["Music"], venue=f"V{i}",
               start_time="2026-08-05T19:00:00-06:00")
        for i in range(20)
    ]
    picked = selection.choose(rows, tz_name=TZ, max_slides=9, max_per_category=99)
    assert len(picked) == 9


def test_is_postable_rejects_boilerplate_and_undated():
    assert not selection.is_postable(_event(title="Events"))
    assert not selection.is_postable(_event(title="", start_time=None))
    assert selection.is_postable(_event(title="Real Concert"))


def test_parse_start_converts_to_local_zone():
    row = _event(start_time="2026-08-06T01:00:00+00:00")  # 7pm previous day in El Paso
    local = selection.parse_start(row, TZ)
    assert local is not None
    assert (local.year, local.month, local.day, local.hour) == (2026, 8, 5, 19)
