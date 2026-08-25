"""Regression tests for the timestamp invariant in core/eventtime.py.

The bug these lock down: a naive wall-clock time parsed off a listing page was
written to a `timestamptz` column, where Postgres read it as UTC and shifted the
event six or seven hours early — an 8pm concert surfacing on the carousel as
2pm, a 5pm social as 11am.
"""

from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from scraper.core.eventtime import event_tz, local_day, to_event_local
from scraper.core.models import Event
from scraper.core.orchestrator import _localize_times
from scraper.core.storage import _event_row
from scraper.sources.events_ticketmaster import _start_datetime

TZ = ZoneInfo("America/Denver")


def test_naive_time_is_read_as_local_wall_clock_not_utc():
    # "Aug 26, 2026 8:00 PM" off El Paso Live's listing.
    got = to_event_local(datetime(2026, 8, 26, 20, 0))
    assert got.hour == 20
    assert got.utcoffset().total_seconds() == -6 * 3600
    # The absolute instant is the following day in UTC — which is the point.
    assert got.astimezone(timezone.utc) == datetime(2026, 8, 27, 2, 0, tzinfo=timezone.utc)


def test_aware_time_keeps_its_instant_and_is_re_expressed_locally():
    # Ticketmaster's dates.start.dateTime for the same 8pm show.
    got = to_event_local(datetime(2026, 8, 27, 2, 0, tzinfo=timezone.utc))
    assert (got.hour, got.date().day) == (20, 26)


def test_both_conventions_agree_on_the_local_calendar_day():
    """Why dedupe needs this: the same concert from two sources, one reporting
    wall-clock and one reporting UTC, used to land on two different days and so
    read as two different events."""
    from_listing = to_event_local(datetime(2026, 8, 26, 20, 0))
    from_api = to_event_local(datetime(2026, 8, 27, 2, 0, tzinfo=timezone.utc))
    assert from_listing == from_api
    assert from_listing.date() == from_api.date()


def test_stored_row_always_carries_an_offset():
    row = _event_row(Event(source="events_web", title="x", start_time=datetime(2026, 8, 26, 20, 0)))
    assert row["start_time"].endswith("-06:00")


def test_orchestrator_normalizes_both_ends_of_an_event():
    event = _localize_times(
        Event(
            source="events_web",
            title="x",
            start_time=datetime(2026, 8, 26, 20, 0),
            end_time=datetime(2026, 8, 26, 23, 0),
        )
    )
    assert event.start_time.tzinfo is not None
    assert event.end_time.tzinfo is not None
    assert (event.start_time.hour, event.end_time.hour) == (20, 23)


def test_dst_is_resolved_per_date_not_by_a_fixed_offset():
    winter = to_event_local(datetime(2026, 1, 15, 20, 0))
    summer = to_event_local(datetime(2026, 7, 15, 20, 0))
    assert winter.utcoffset().total_seconds() == -7 * 3600
    assert summer.utcoffset().total_seconds() == -6 * 3600


def test_none_passes_through():
    assert to_event_local(None) is None
    assert local_day(None) is None


def test_local_day_reads_a_stored_utc_string_back_as_local():
    assert local_day("2026-08-27T02:00:00+00:00").date().day == 26
    assert local_day("2026-08-27T02:00:00Z").hour == 20


def test_local_day_returns_none_for_unparseable_input():
    assert local_day("not a timestamp") is None


def test_ticketmaster_prefers_the_absolute_instant():
    dates = {"start": {"dateTime": "2026-08-27T02:00:00Z", "localDate": "2026-08-26", "localTime": "20:00:00"}}
    assert to_event_local(_start_datetime(dates)).hour == 20


def test_ticketmaster_falls_back_to_local_date_plus_time_not_midnight():
    """Without localTime the old code kept only localDate, turning every
    dateTime-less event into midnight — which then read as 6pm the day before."""
    dates = {"start": {"localDate": "2026-08-26", "localTime": "20:00:00"}}
    got = to_event_local(_start_datetime(dates))
    assert (got.date().day, got.hour) == (26, 20)


def test_ticketmaster_keeps_a_date_only_event_date_only():
    dates = {"start": {"localDate": "2026-08-26", "localTime": "20:00:00", "timeTBA": True}}
    got = to_event_local(_start_datetime(dates))
    assert (got.date().day, got.hour) == (26, 0)


def test_ticketmaster_with_no_date_at_all():
    assert _start_datetime({"start": {}}) is None


def test_event_tz_is_configured_not_hardcoded():
    assert event_tz().key


def test_address_label_drops_a_venue_name_the_slide_already_prints():
    """Visit El Paso stores "<venue> - <street>", so the slide printed the venue
    twice and the duplicate crowded out the street."""
    from scraper.social.render import _address_label

    row = {
        "venue": "El Paso County Coliseum",
        "location": "El Paso County Coliseum - 4100 E Paisano Dr, El Paso, TX 79905",
    }
    assert _address_label(row) == "4100 E Paisano Dr, El Paso"


def test_address_label_keeps_an_address_that_merely_mentions_the_venue():
    from scraper.social.render import _address_label

    row = {"venue": "Flix Brewhouse", "location": "6450 N Desert Blvd Suite 12, El Paso, TX 79912"}
    assert _address_label(row) == "6450 N Desert Blvd Suite 12, El Paso"


def test_address_label_survives_a_venue_name_with_regex_metacharacters():
    from scraper.social.render import _address_label

    row = {"venue": "Cafe (Downtown)", "location": "Cafe (Downtown), 1 Main St, El Paso, TX 79901"}
    assert _address_label(row) == "1 Main St, El Paso"


def test_the_same_venue_under_two_ids_is_one_venue_to_the_carousel():
    """The Abraham Chavez Theatre holds three venue_ids because three sources
    punctuate its address differently, which put the same concert on the
    carousel three times."""
    from scraper.social.selection import dedupe_key, venue_key

    tm = {"title": "Jason Bonham's Led Zeppelin Evening", "venue": "Abraham Chavez Theatre",
          "venue_id": "7a2f90f8", "location": "1 Civic Center Plaza, El Paso, TX 79901, US"}
    web = {"title": "Jason Bonham's Led Zeppelin Evening", "venue": "Abraham Chavez Theatre",
           "venue_id": "91858f05", "location": "1 Civic Center Plaza, El Paso, TX 79901"}
    assert venue_key(tm) == venue_key(web)
    assert dedupe_key(tm) == dedupe_key(web)


def test_venue_key_falls_back_when_there_is_no_venue_name():
    from scraper.social.selection import venue_key

    assert venue_key({"venue_id": "abc"}) == "abc"
    assert venue_key({"location": "1 Ballpark Plaza"}) == "1 ballpark plaza"
    assert venue_key({}) == ""


def test_different_venues_stay_different():
    from scraper.social.selection import venue_key

    assert venue_key({"venue": "Lowbrow Palace"}) != venue_key({"venue": "El Paso Museum of Art"})
