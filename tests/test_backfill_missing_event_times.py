"""Recovering both start AND end times for a date-only-listing event.

Real incident, 2026-09-20: "Patron Tequila Presents the El Paso Margarita
Festival" was stored with start_time == end_time == local midnight, because
the listing page gave "startDate": "2026-09-26" with no hour -- and so did
"endDate". The original script only ever recovered start_time; end_time was
left wrong even after a successful start-time fix.
"""

from __future__ import annotations

from scraper.backfill_missing_event_times import _needs_a_time, _needs_an_end_time


def _row(start_time="2026-09-26T00:00:00-06:00", end_time="2026-09-26T00:00:00-06:00",
         start_date="2026-09-26", end_date="2026-09-26"):
    return {
        "start_time": start_time,
        "end_time": end_time,
        "raw": {"startDate": start_date, "endDate": end_date},
    }


def test_the_real_margarita_festival_row_needs_both_fixed():
    row = _row()
    assert _needs_a_time(row)
    assert _needs_an_end_time(row)


def test_a_real_late_end_time_is_never_touched():
    """No hour heuristic for end_time -- a real event legitimately ending at
    or after midnight must not be mistaken for a placeholder."""
    row = _row(end_time="2026-09-27T00:30:00-06:00", end_date="2026-09-27T00:30:00-06:00")
    assert not _needs_an_end_time(row)


def test_a_real_early_start_is_never_touched_by_the_date_only_gate():
    """A row with a real timestamp that merely lands early is a DIFFERENT bug
    (backfill_event_timezones' territory) and must not be overwritten here."""
    row = _row(start_time="2026-09-26T03:00:00-06:00", start_date="2026-09-26T03:00:00-06:00")
    assert not _needs_a_time(row)


def test_a_row_with_no_end_time_at_all_is_left_alone():
    row = _row(end_time=None)
    assert not _needs_an_end_time(row)


def test_a_row_whose_raw_end_date_was_never_date_only_is_left_alone():
    """The stored end_time might legitimately be midnight for a reason
    unrelated to this bug; only an ORIGINAL date-only endDate justifies
    overwriting it."""
    row = _row(end_date="2026-09-26T00:00:00-06:00")
    assert not _needs_an_end_time(row)


def test_a_row_missing_raw_entirely_needs_nothing():
    assert not _needs_a_time({"start_time": "2026-09-26T00:00:00-06:00", "raw": None})
    assert not _needs_an_end_time({"end_time": "2026-09-26T00:00:00-06:00", "raw": None})
