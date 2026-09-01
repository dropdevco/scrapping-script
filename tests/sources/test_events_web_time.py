"""Start-time handling for scraped JSON-LD listings.

Two failure modes, both of which reached customers as a missing or wrong hour:
a listing that publishes only a date, and an offset that contradicts the venue.
"""

from __future__ import annotations

from datetime import datetime

from scraper.core.eventtime import to_event_local
from scraper.sources.events_web import _dt, _is_date_only


class TestDateOnly:
    def test_bare_date_is_recognized(self):
        assert _is_date_only("2026-08-28")
        assert _is_date_only("  2026-08-28  ")

    def test_a_real_timestamp_is_not_date_only(self):
        assert not _is_date_only("2026-08-28T20:00:00-05:00")
        assert not _is_date_only("2026-08-28T20:00:00")

    def test_non_strings_are_not_date_only(self):
        assert not _is_date_only(None)
        assert not _is_date_only(20260828)


class TestOffsetPolicy:
    """Trust an offset only when it matches what this region was actually on."""

    def test_mismatched_offset_is_dropped_keeping_the_wall_clock(self):
        # Eventbrite publishes El Paso events with the organizer's Chicago
        # offset. The venue is Mountain, so -05:00 is not ours: keep 8:00 PM.
        parsed = _dt("2026-08-28T20:00:00-05:00")
        assert parsed == datetime(2026, 8, 28, 20, 0)
        assert parsed.tzinfo is None
        assert to_event_local(parsed).hour == 20  # customer sees 8 PM, as on the ticket page

    def test_matching_offset_is_preserved(self):
        parsed = _dt("2026-08-28T20:00:00-06:00")  # MDT, which is ours in August
        assert parsed.tzinfo is not None
        assert to_event_local(parsed).hour == 20

    def test_winter_offset_is_judged_against_that_date_not_today(self):
        # MST (-07:00) is correct in January and wrong in August; the check is
        # per-instant, so a January timestamp keeps its offset.
        parsed = _dt("2026-01-15T20:00:00-07:00")
        assert parsed.tzinfo is not None
        assert to_event_local(parsed).hour == 20

    def test_utc_is_trusted_as_a_real_instant(self):
        # Meetup normalizes to UTC: "2026-08-30T13:00:00.000Z" is a 7am El Paso
        # hike. Re-reading that as a wall clock would move it to 1pm. Only an
        # offset that ASSERTS a local zone can be asserting the wrong one.
        parsed = _dt("2026-08-30T13:00:00.000Z")
        assert parsed.tzinfo is not None
        assert to_event_local(parsed).hour == 7

    def test_naive_input_is_unchanged(self):
        assert _dt("2026-08-28T20:00:00") == datetime(2026, 8, 28, 20, 0)

    def test_bare_date_still_parses_to_midnight(self):
        assert _dt("2026-08-28") == datetime(2026, 8, 28, 0, 0)

    def test_garbage_is_none_not_an_exception(self):
        assert _dt("next Friday") is None
        assert _dt(None) is None
