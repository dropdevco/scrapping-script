"""CBP feed parsing: the four traps in the feed, and what a bridge is called.

CBP's feed is the only source that covers all six ports, and it lies in small ways that
each reached a caption before they were caught: delay_minutes is a string whose missing
value is "" or "N/A", status casing varies ("no delay" / "Lanes Closed"), Tornillo has an
empty crossing_name, and update stamps can sit an hour ahead of the clock ("At 10:00 pm"
at 9:40 pm) or arrive as words ("At Noon MDT"). A missing number rendered as 0 tells
someone the bridge is clear when nobody knows.
"""

from __future__ import annotations

import unittest
from datetime import UTC, datetime

from border_support import (  # noqa: F401 - setUpModule/tearDownModule are hooks
    NOW,
    FakeHttp,
    lane,
    load,
    setUpModule,
    tearDownModule,
)

from scraper.border import cbp


class Cleaning(unittest.IsolatedAsyncioTestCase):
    async def test_no_delay_becomes_zero_not_missing(self):
        parsed = cbp._lane("240202", "car", lane("no delay", "", "At 9:00 pm MDT"), NOW)
        self.assertEqual((parsed.state, parsed.delay_minutes), ("open", 0))

    async def test_minutes_parsed_from_string(self):
        self.assertEqual(cbp._lane("240202", "car", lane("delay", "48"), NOW).delay_minutes, 48)

    async def test_closed_carries_no_minutes(self):
        parsed = cbp._lane("240203", "car", lane("Lanes Closed", "0"), NOW)
        self.assertEqual((parsed.state, parsed.delay_minutes), ("closed", None))

    async def test_update_pending_is_no_data(self):
        self.assertEqual(cbp._lane("240221", "car", lane("Update Pending"), NOW).state, "no_data")

    async def test_absent_lane_type_is_dropped(self):
        self.assertIsNone(cbp._lane("240204", "walk", lane("N/A"), NOW))

    async def test_future_stamp_is_flagged_and_age_never_negative(self):
        parsed = cbp._lane("240201", "car", lane("delay", "55", "At 10:00 pm MDT"), NOW)
        self.assertTrue(parsed.stamp_ahead_of_clock)
        self.assertEqual(parsed.age_minutes, 0)

    async def test_stamp_hours_ahead_belongs_to_yesterday(self):
        morning = datetime(2026, 9, 22, 13, 30, tzinfo=UTC)  # 7:30 am MDT
        self.assertEqual(cbp.parse_stamp("At 11:00 pm MDT", morning).isoformat(),
                         "2026-09-21T23:00:00-06:00")

    async def test_noon_and_midnight_are_words_in_the_feed(self):
        self.assertEqual(cbp.parse_stamp("At Noon MDT", NOW).strftime("%H:%M"), "12:00")
        self.assertEqual(cbp.parse_stamp("At Midnight MDT", NOW).strftime("%H:%M"), "00:00")

    async def test_noon_stamp_is_not_treated_as_stale(self):
        one_pm = datetime(2026, 9, 22, 19, 6, tzinfo=UTC)  # 1:06 pm MDT
        parsed = cbp._lane("240202", "car", lane("delay", "38", "At Noon MDT"), one_pm)
        self.assertEqual(parsed.age_minutes, 66)
        self.assertFalse(parsed.stale)

    async def test_unparsable_stamp_is_none_not_a_guess(self):
        self.assertIsNone(cbp.parse_stamp("At some point MDT", NOW))

    async def test_content_hash_is_stable_and_lane_specific(self):
        a = cbp._lane("240202", "car", lane("delay", "48", "At 9:00 pm MDT"), NOW)
        b = cbp._lane("240202", "car", lane("delay", "48", "At 9:00 pm MDT"), NOW)
        c = cbp._lane("240202", "car_ready", lane("delay", "48", "At 9:00 pm MDT"), NOW)
        self.assertEqual(a.content_hash, b.content_hash)
        self.assertNotEqual(a.content_hash, c.content_hash)

class SnapshotShape(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.snap = cbp.build(load("cbp_feed.json"), NOW)

    async def test_six_ports_in_display_order(self):
        self.assertEqual([p.port_number for p in self.snap.ports],
                         ["240202", "240203", "240201", "240204", "240801", "240221"])

    async def test_tornillo_named_despite_blank_feed_name(self):
        self.assertEqual(self.snap.port("240221").name, "Tornillo–Guadalupe")

    async def test_tornillo_has_no_live_data(self):
        self.assertFalse(self.snap.port("240221").has_live_data)

    async def test_lookup_by_slug_and_loose_name(self):
        self.assertEqual(self.snap.port("paso-del-norte").port_number, "240202")
        self.assertEqual(self.snap.port("zaragoza").port_number, "240203")

    async def test_empty_feed_reports_missing_ports_instead_of_crashing(self):
        empty = cbp.build([], NOW)
        self.assertEqual((empty.ports, len(empty.missing_ports)), ([], 6))

class Aliases(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.snap = cbp.build(load("cbp_feed.json"), NOW)

    async def test_people_type_what_they_call_the_bridge(self):
        for typed, expected in [("libre", "240201"), ("el puente libre", "240201"),
                                ("zaragoza", "240203"), ("santa fe", "240202"),
                                ("pdn", "240202"), ("lerdo", "240204")]:
            with self.subTest(typed=typed):
                self.assertEqual(self.snap.port(typed).port_number, expected)

    async def test_typing_fast_on_a_phone_still_finds_the_bridge(self):
        for typed, expected in [("sarragoza", "240203"), ("zaragosa", "240203"),
                                ("tornilo", "240221"), ("stantn", "240204"),
                                ("pasodelnorte", "240202"), ("santa fee", "240202")]:
            with self.subTest(typed=typed):
                self.assertEqual(self.snap.port(typed).port_number, expected)

    async def test_a_typo_too_far_from_any_bridge_is_refused(self):
        for typed in ("pizza", "narnia", "aeropuerto"):
            with self.subTest(typed=typed):
                self.assertIsNone(self.snap.port(typed))

    async def test_a_word_that_fits_every_bridge_matches_none(self):
        self.assertIsNone(self.snap.port("puente"))

    async def test_nonsense_matches_none(self):
        self.assertIsNone(self.snap.port("narnia"))

class CbpFetch(unittest.IsolatedAsyncioTestCase):
    """Retries, backoff, Retry-After and gzip are core.http's job now; cbp.fetch only
    has to ask the right URL and refuse a payload that is not the list it parses."""

    async def test_it_reads_the_feed_through_the_shared_client(self):
        http = FakeHttp(payload=[{"port_number": "240202"}])
        self.assertEqual((await cbp.fetch(http))[0]["port_number"], "240202")
        self.assertEqual(http.requested, [cbp.FEED_URL])

    async def test_a_payload_that_is_not_a_list_is_refused(self):
        with self.assertRaises(TypeError):
            await cbp.fetch(FakeHttp(payload={"error": "maintenance"}))
