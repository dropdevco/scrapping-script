"""Start-time handling for scraped JSON-LD listings.

Two failure modes, both of which reached customers as a missing or wrong hour:
a listing that publishes only a date, and an offset that contradicts the venue.
"""

from __future__ import annotations

from datetime import datetime

from scraper.core.eventtime import to_event_local
from scraper.sources.events_web import _dt, _is_date_only, _is_detail_url, _walk_for_events


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


class TestWalkForEventsTypeMatching:
    """_walk_for_events decides which JSON-LD nodes are even LOOKED at for a
    startDate/endDate. Real incident, 2026-09-20: an Eventbrite festival's
    detail page carried a complete, correctly-offset JSON-LD block --
    startDate "2026-09-26T12:00:00-06:00", endDate "...T18:00:00-06:00" --
    typed "Festival". The substring check here (`"event" in x.lower()`) does
    not match "festival", so the node was invisible and the event was stored
    with start_time == end_time == local midnight: the date-only listing
    value that _is_date_only's detail-page revisit exists specifically to
    replace, defeated because the revisit's own walker found nothing to
    revisit WITH.
    """

    def _festival_node(self):
        return {
            "@context": "https://schema.org",
            "@type": "Festival",
            "name": "Patron Tequila Presents the El Paso Margarita Festival",
            "startDate": "2026-09-26T12:00:00-06:00",
            "endDate": "2026-09-26T18:00:00-06:00",
            "location": {"@type": "Place", "name": "The Yard Patio Beer Garden"},
        }

    def test_a_festival_typed_node_is_yielded(self):
        assert list(_walk_for_events(self._festival_node())) != []

    def test_a_hackathon_typed_node_is_yielded(self):
        node = {"@type": "Hackathon", "name": "El Paso Hackathon", "startDate": "2026-09-26T09:00:00-06:00"}
        assert list(_walk_for_events(node)) != []

    def test_an_ordinary_event_suffixed_type_still_matches(self):
        """The substring check itself is not being removed, only extended."""
        node = {"@type": "MusicEvent", "name": "A Concert", "startDate": "2026-09-26T20:00:00-06:00"}
        assert list(_walk_for_events(node)) != []

    def test_a_non_event_type_is_still_correctly_ignored(self):
        """The allowlist must not become a general amnesty -- an unrelated
        page section (FAQPage, BreadcrumbList, WebPage) must stay invisible."""
        for bad_type in ("FAQPage", "BreadcrumbList", "WebPage"):
            assert list(_walk_for_events({"@type": bad_type, "name": "x"})) == []

    def test_the_festival_node_survives_inside_a_graph(self):
        """Real pages wrap the event in @graph alongside other, irrelevant
        blocks -- the walker must still find it nested."""
        page = {"@graph": [{"@type": "WebPage", "name": "x"}, self._festival_node()]}
        found = list(_walk_for_events(page))
        assert len(found) == 1
        assert found[0]["startDate"] == "2026-09-26T12:00:00-06:00"


class TestEventbriteDetailUrlShapes:
    """A second, independent bug hit the SAME symptom (start_time == end_time
    == local midnight) for a reason unrelated to _walk_for_events: "AIA El
    Paso 2027 Architecture Gala...-registration-1996013710740" is a real,
    live, free/RSVP Eventbrite listing -- correctly typed "SocialEvent" (which
    the substring check already matches) and carrying a perfectly good
    18:00-21:00 JSON-LD time on its own detail page. But _is_detail_url only
    recognized "-tickets-<id>", Eventbrite's URL shape for PAID events, so the
    date-only-time revisit never even attempted to re-fetch this page at all.
    """

    def test_a_paid_event_url_is_a_detail_page(self):
        url = "https://www.eventbrite.com/e/patron-tequila-presents-the-el-paso-margarita-festival-tickets-1992011285378"
        assert _is_detail_url(url)

    def test_a_free_rsvp_event_url_is_also_a_detail_page(self):
        url = (
            "https://www.eventbrite.com/e/aia-el-paso-2027-architecture-gala-annual-"
            "design-awards-celebration-registration-1996013710740"
        )
        assert _is_detail_url(url)

    def test_an_eventbrite_listing_page_is_still_not_a_detail_page(self):
        assert not _is_detail_url("https://www.eventbrite.com/d/tx--el-paso/all-events/")
