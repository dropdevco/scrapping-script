"""CBP feed parsing: the four traps in the feed, and what a bridge is called.

CBP's feed is the only source that covers all six ports, and it lies in small ways that
each reached a caption before they were caught: delay_minutes is a string whose missing
value is "" or "N/A", status casing varies ("no delay" / "Lanes Closed"), an unnamed
"El Paso" port (240221) reads "Update Pending" forever and was registered as Tornillo
(240401) until 2026-10, and update stamps can sit an hour ahead of the clock ("At 10:00
pm" at 9:40 pm) or arrive as words ("At Noon MDT"). A missing number rendered as 0
tells someone the bridge is clear when nobody knows.

The four cbp_feed*.json fixtures are CBP captures from 2026-09-22 with one edit: all
seven of Tornillo's (240401) lanes were rewritten to "Update Pending", which is how
Tornillo read while it was registered as 240221. The rest of the suite, and the chat and
simulate demos that replay these files, were built on that, and their scenarios (which
bridge is fastest, which is disputed) rank the other five. Production is not like that:
as captured, Tornillo is the fastest bridge by car and on foot. TornilloPort pins what
production does with the captured numbers, so the edit hides nothing from the suite.
"""

from __future__ import annotations

import unittest
from datetime import UTC, datetime

from border_support import (  # noqa: F401 - setUpModule/tearDownModule are hooks
    MIDDAY,
    NOW,
    ROOT,
    FakeHttp,
    lane,
    load,
    returns,
    setUpModule,
    tearDownModule,
)

from scraper.border import cbp, text
from scraper.border.service import BorderFeed
from scraper.border.sources import borderswaittime, pasosfronterizos
from scraper.border.storage import BorderStore


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
        self.assertEqual(cbp._lane("240401", "car", lane("Update Pending"), NOW).state, "no_data")

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
                         ["240202", "240203", "240201", "240204", "240801", "240401"])

    async def test_tornillo_is_named_by_the_registry_not_the_feed(self):
        # The feed calls it "Marcelino Serna" / "Tornillo"; followers know it by neither alone.
        tornillo = self.snap.port("240401")
        self.assertEqual((tornillo.name, tornillo.name_es), ("Tornillo–Guadalupe", "Guadalupe–Tornillo"))

    async def test_lookup_by_slug_and_loose_name(self):
        self.assertEqual(self.snap.port("paso-del-norte").port_number, "240202")
        self.assertEqual(self.snap.port("zaragoza").port_number, "240203")

    async def test_empty_feed_reports_missing_ports_instead_of_crashing(self):
        empty = cbp.build([], NOW)
        self.assertEqual((empty.ports, len(empty.missing_ports)), ([], 6))

# Both candidates for Tornillo, as the live feed read them at 12:28 pm MDT on 2026-10-26:
# the unnamed 240221, pending on every lane as it always is, and 240401, the real
# crossing, at 20 minutes.
TORNILLO_LIVE_AT = datetime(2026, 10, 26, 18, 28, tzinfo=UTC)
TORNILLO_LIVE = [
    {"port_number": "240221", "port_name": "El Paso", "crossing_name": "",
     "hours": "24 hrs/day", "port_status": "Open",
     "passenger_vehicle_lanes": {"standard_lanes": lane("Update Pending")}},
    {"port_number": "240401", "port_name": "Marcelino Serna", "crossing_name": "Tornillo",
     "hours": "6 am-10 pm", "port_status": "Open",
     "passenger_vehicle_lanes": {"standard_lanes": lane("delay", "20", "At Noon MDT", "2")}},
]

def midday_as_captured() -> list[dict]:
    """cbp_feed_midday.json without the edit: Tornillo's lanes as CBP sent them at
    1:00 pm MDT on 2026-09-22 (the file as committed in e2df7e2)."""
    feed = load("cbp_feed_midday.json")
    tornillo = next(entry for entry in feed if entry["port_number"] == "240401")
    for group in tornillo.values():
        if isinstance(group, dict):
            for kind, value in group.items():
                if isinstance(value, dict):
                    group[kind] = lane("Lanes Closed")
    tornillo["passenger_vehicle_lanes"]["standard_lanes"] = lane("delay", "10", "At 1:00 pm MDT", "2")
    tornillo["pedestrian_lanes"]["standard_lanes"] = lane("no delay", "0", "At 1:00 pm MDT", "2")
    return feed

class TornilloPort(unittest.IsolatedAsyncioTestCase):
    """Roadmap 0.1: Tornillo read "no data" for as long as it was registered as 240221."""

    async def test_tornillo_reads_the_real_crossing_not_the_pending_one(self):
        snap = cbp.build(TORNILLO_LIVE, TORNILLO_LIVE_AT)
        self.assertEqual([p.port_number for p in snap.ports], ["240401"])
        car = snap.port("tornillo").lanes["car"]
        self.assertEqual((car.state, car.delay_minutes, car.stale), ("open", 20, False))

    async def test_with_its_captured_numbers_tornillo_heads_the_caption(self):
        # What production shows once 0.1 ships. Ranking has no notion of distance, so
        # the most remote bridge leads the public caption whenever it is quickest. Whether
        # it should is the owner's call, still open; this test changes when it is made.
        feed = BorderFeed(fetcher=returns(midday_as_captured), clock=lambda: MIDDAY,
                          cache_seconds=0, extra_sources=[], storage=BorderStore(client=None))
        snap = await feed.snapshot()
        headline = text.caption(feed, snap).splitlines()[1]
        self.assertTrue(headline.startswith("Más rápido ahora: Guadalupe–Tornillo 10 min"), headline)
        self.assertEqual(feed.best_in(snap, "walk")["port_number"], "240401")

    async def test_every_registry_uses_the_feeds_ids(self):
        # 0.1 had to move Tornillo's id in three registries; one left behind fails here.
        self.assertEqual(set(text.SHORT_NAMES), set(cbp.PORTS))
        for source in (borderswaittime, pasosfronterizos):
            with self.subTest(source=source.__name__):
                self.assertLessEqual(set(source.PORTS.values()), set(cbp.PORTS))

    async def test_the_seed_fix_moves_tornillo_to_the_id_the_registry_reads(self):
        # border_ports must hold every port the poller writes readings for (foreign key).
        sql = next((ROOT / "supabase/migrations").glob("0020_*.sql")).read_text(encoding="utf-8")
        statements = "\n".join(line for line in sql.splitlines() if not line.lstrip().startswith("--"))
        tornillo = next(n for n, meta in cbp.PORTS.items() if meta["slug"] == "tornillo")
        self.assertIn(f"('{tornillo}', '{cbp.PORTS[tornillo]['name']}'", statements)
        for word in ("drop ", "delete ", "truncate ", "alter column", "rename "):
            self.assertNotIn(word, statements.lower())

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
                                ("tornilo", "240401"), ("stantn", "240204"),
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
