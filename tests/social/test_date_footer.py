"""Every event slide carries its own date.

Until now the only date on a carousel was the cover's, so nine slides all said
just "7:00 PM". That made a season of identically-titled home games
indistinguishable -- "El Paso Rhinos" appeared 24 times in the database, once
per game, and nothing on a slide told them apart. It also meant an event whose
stored hour is not believable (has_plausible_time suppresses the time stamp)
showed no temporal information whatsoever.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from scraper.social import render

TZ = ZoneInfo("America/Denver")


def _row(**kw):
    row = {"title": "Some Event", "venue": "A Venue", "location": "El Paso, TX"}
    row.update(kw)
    return row


def test_a_single_day_event_shows_its_weekday_and_date():
    start = datetime(2026, 9, 18, 19, 30, tzinfo=TZ)
    assert render._date_footer_text(_row(), start) == "FRI · SEP 18"


def test_a_multi_day_event_shows_the_range():
    """The "open until X" signal, drawn rather than described -- 12 upcoming
    events genuinely span more than one local day."""
    start = datetime(2026, 9, 18, 18, 0, tzinfo=TZ)
    row = _row(end_time="2026-09-20T23:59:00-06:00")
    assert render._date_footer_text(row, start) == "SEP 18 – SEP 20"


def test_an_end_time_later_the_same_day_is_not_a_range():
    """Most end_times are a closing hour, not a closing date. Rendering
    "SEP 18 - SEP 18" would be noise."""
    start = datetime(2026, 9, 18, 18, 0, tzinfo=TZ)
    row = _row(end_time="2026-09-18T23:00:00-06:00")
    assert render._date_footer_text(row, start) == "FRI · SEP 18"


def test_the_date_is_shown_even_when_the_time_is_not_believable():
    """has_plausible_time gates the CLOCK, not the DAY. A date-only listing
    stored at local midnight has a trustworthy date and an untrustworthy hour,
    and it used to render neither."""
    start = datetime(2026, 9, 18, 0, 0, tzinfo=TZ)
    assert render._time_stamp(start) is None
    assert render._date_footer_text(_row(), start) == "FRI · SEP 18"


def test_a_single_digit_day_has_no_leading_zero():
    """%-d is glibc-only and %d gives "SEP 05", so the day is built by hand --
    the same reason _default_period_label does it."""
    start = datetime(2026, 9, 5, 19, 30, tzinfo=TZ)
    assert render._date_footer_text(_row(), start) == "SAT · SEP 5"


def test_an_unparseable_end_time_falls_back_to_the_single_day_form():
    start = datetime(2026, 9, 18, 19, 30, tzinfo=TZ)
    assert render._date_footer_text(_row(end_time="not a date"), start) == "FRI · SEP 18"


def test_no_start_time_means_no_footer():
    assert render._date_footer_text(_row(), None) is None


def test_every_layout_draws_the_footer_and_still_produces_a_valid_slide():
    """Guards the reserved strip: the venue/address/chip stack reflows, and
    before _DATE_FOOTER_H was reserved a long address ran into the footer."""
    start = datetime(2026, 9, 18, 19, 30, tzinfo=TZ)
    row = _row(
        title="A Very Long Event Title That Will Wrap Across Several Lines Indeed",
        location="11388 Sgt Major Blvd, Fort Bliss, El Paso, TX 79916",
        categories=["Community"],
    )
    for builder in render._SLIDE_BUILDERS:
        img = builder(row, None, start, render.COSMO, seed=3)
        assert img.size == render.CANVAS
