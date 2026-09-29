"""The scraped sources: their parsers, robots.txt, and the checks that catch drift.

A scraper whose page changes shape parses to nothing rather than raising, which read as
"ok (0 readings)". Overnight the CBP mirror prints "Update pending" on every card (seen
live at 00:12 MDT, 2026-09-29), which first tripped the empty-page alarm and then got
compared against a fresh CBP number and blamed our parser. Both were false alarms that
would have fired every night.
"""

from __future__ import annotations

import unittest
from unittest import mock

from border_support import (  # noqa: F401 - setUpModule/tearDownModule are hooks
    FIXTURES,
    NOW,
    FakeHttp,
    StubSource,
    feed_for,
    load,
    setUpModule,
    tearDownModule,
)

from scraper.border import cbp
from scraper.border.sources.base import Reading, Source
from scraper.border.sources.borderswaittime import EXPECTED_PORTS
from scraper.border.sources.borderswaittime import parse as parse_mirror
from scraper.border.sources.pasosfronterizos import PasosFronterizosSource
from scraper.border.sources.pasosfronterizos import parse as parse_pasos
from scraper.core.config import settings

# The update stamp CBP put on Paso del Norte's car lane in the saved feed; a mirror
# reading only counts as a check on our parsing when it carries the same one.
PDN_CAR_LABEL = cbp.build(load("cbp_feed.json"), NOW).port("240202").lanes["car"].cbp_update_label

class SecondOpinionParsing(unittest.IsolatedAsyncioTestCase):
    """pasosfronterizos.com, parsed from a saved page."""

    @classmethod
    def setUpClass(cls):
        page = (FIXTURES / "pasosfronterizos.html").read_text(errors="replace")
        cls.readings = parse_pasos(page, NOW)

    def by(self, port, lane):
        return next((r for r in self.readings if r.port_number == port and r.lane == lane), None)

    async def test_it_finds_the_four_bridges_it_covers(self):
        self.assertEqual({r.port_number for r in self.readings}, {"240201", "240202", "240203", "240204"})

    async def test_car_and_walking_lanes_are_separated(self):
        self.assertEqual(self.by("240202", "car").minutes, 50)
        self.assertEqual(self.by("240202", "walk").minutes, 25)

    async def test_sentri_and_ready_lanes_are_read(self):
        self.assertEqual(self.by("240202", "car_sentri").minutes, 5)
        self.assertEqual(self.by("240202", "car_ready").minutes, 45)

    async def test_the_page_age_travels_with_every_reading(self):
        self.assertTrue(all(r.age_minutes == 8 for r in self.readings))

    async def test_an_unreadable_page_yields_nothing_rather_than_guesses(self):
        self.assertEqual(parse_pasos("<html><body>nothing here</body></html>", NOW), [])

class MirrorAsParserCheck(unittest.IsolatedAsyncioTestCase):
    """borderswaittime republishes CBP, so a difference points at our parsing."""

    @classmethod
    def setUpClass(cls):
        cls.page = (FIXTURES / "borderswaittime.html").read_text(errors="replace")

    async def test_it_reads_the_data_card_not_the_navigation(self):
        readings = {r.port_number: (r.state, r.minutes) for r in parse_mirror(self.page, NOW)}
        self.assertEqual(readings["240201"], ("open", 20))     # not the pedestrian "Lanes Closed"
        self.assertEqual(readings["240202"], ("open", 39))
        self.assertEqual(readings["240203"], ("closed", None))

    async def test_an_overnight_page_of_pending_updates_is_still_a_readable_page(self):
        """Every card reads "Update pending" after midnight. That is CBP resting, not the
        page changing shape, and must not trip the empty-page alarm every night."""
        cards = "".join(
            f"<div>{name} Hours: 24 hrs/day Passenger Vehicles Maximum Lanes: 4 General Lanes: "
            "Update pending: Please refresh the page after a few minutes. Sentri Lanes: N/A</div>"
            for name in ("Paso del Norte", "Ysleta", "Bridge of the Americas", "Stanton"))
        readings = parse_mirror(cards, NOW)
        self.assertEqual({r.port_number for r in readings}, EXPECTED_PORTS)
        self.assertEqual({r.state for r in readings}, {"no_data"})

    async def test_mirror_readings_are_never_independent(self):
        self.assertTrue(all(not r.independent for r in parse_mirror(self.page, NOW)))

    async def test_a_mirror_agreeing_reports_no_mismatch(self):
        agreeing = [Reading("240202", "car", "open", 48, source="mirror", independent=False,
                            label=PDN_CAR_LABEL)]
        feed = feed_for("cbp_feed.json", extra_sources=[
            StubSource(agreeing, "mirror", independent=False)])
        await feed.snapshot()
        check = (await feed.health())["parser_check"]
        self.assertEqual((check["compared"], check["mismatches"]), (1, []))

    async def test_a_mirror_disagreeing_flags_our_parsing(self):
        differing = [Reading("240202", "car", "open", 999, source="mirror", independent=False,
                             label=PDN_CAR_LABEL)]
        feed = feed_for("cbp_feed.json", extra_sources=[
            StubSource(differing, "mirror", independent=False)])
        await feed.snapshot()
        mismatches = (await feed.health())["parser_check"]["mismatches"]
        self.assertEqual(len(mismatches), 1)
        self.assertEqual(mismatches[0]["ours"]["minutes"], 48)

    async def test_a_mirror_never_changes_the_answer(self):
        differing = [Reading("240202", "car", "open", 999, source="mirror", independent=False,
                             label=PDN_CAR_LABEL)]
        feed = feed_for("cbp_feed.json", extra_sources=[
            StubSource(differing, "mirror", independent=False)])
        self.assertEqual((await feed.bridge("paso-del-norte", "car"))["minutes"], 48)

class Politeness(unittest.IsolatedAsyncioTestCase):
    async def test_the_scraper_asks_robots_before_fetching(self):
        source, http = PasosFronterizosSource(), FakeHttp(allowed=False)
        with self.assertRaises(PermissionError):
            await source.fetch(http, NOW)
        self.assertEqual(http.requested, [], "a disallowed page must never be requested")
        self.assertFalse(source.is_configured(), "the refusal sticks for the process")

    async def test_an_allowed_page_is_fetched_and_parsed(self):
        page = (FIXTURES / "pasosfronterizos.html").read_text(errors="replace")
        readings = await PasosFronterizosSource().fetch(FakeHttp(page=page), NOW)
        self.assertEqual({r.port_number for r in readings}, PasosFronterizosSource.expected_ports)

    async def test_disabled_sources_is_honoured_by_name(self):
        with mock.patch.object(settings, "disabled_sources", {"pasosfronterizos"}):
            self.assertFalse(PasosFronterizosSource().is_configured())

    async def test_a_disallowed_source_is_skipped_not_failed(self):
        class Forbidden(Source):
            name = "forbidden"

            def is_configured(self):
                return False

            async def fetch(self, http, now):
                raise AssertionError("must not be fetched")

        feed = feed_for("cbp_feed.json", extra_sources=[Forbidden()])
        await feed.snapshot()
        self.assertEqual((await feed.health())["sources"]["forbidden"], "not configured")

class ScrapersThatBreakQuietly(unittest.IsolatedAsyncioTestCase):
    def page(self, readings):
        return StubSource(readings, "pasosfronterizos", expected_ports={"240202", "240201"})

    async def test_a_page_that_parses_to_nothing_is_a_problem_not_ok(self):
        source = self.page([])
        feed = feed_for("cbp_feed.json", extra_sources=[source], cache_seconds=300)
        await feed.snapshot()
        health = await feed.health()
        self.assertTrue(health["sources"]["pasosfronterizos"].startswith("empty"))
        self.assertIn("pasosfronterizos", health["source_problems"])
        await feed.snapshot(force=True)
        self.assertEqual(source.calls, 2, "an empty parse must not be cached")

    async def test_a_missing_bridge_is_reported(self):
        source = self.page([Reading("240202", "car", "open", 40, age_minutes=5, source="pasosfronterizos")])
        feed = feed_for("cbp_feed.json", extra_sources=[source])
        await feed.snapshot()
        status = (await feed.health())["sources"]["pasosfronterizos"]
        self.assertTrue(status.startswith("partial"))
        self.assertIn("240201", status)

    async def test_the_real_parsers_declare_what_a_healthy_page_shows(self):
        page = (FIXTURES / "pasosfronterizos.html").read_text(errors="replace")
        found = {r.port_number for r in parse_pasos(page, NOW)}
        self.assertEqual(PasosFronterizosSource.expected_ports - found, set())
        mirror = (FIXTURES / "borderswaittime.html").read_text(errors="replace")
        self.assertEqual(EXPECTED_PORTS - {r.port_number for r in parse_mirror(mirror, NOW)}, set())

class MirrorComparesTheSameUpdate(unittest.IsolatedAsyncioTestCase):
    def feed_with_mirror(self, label):
        reading = Reading("240202", "car", "open", 999, source="mirror", independent=False, label=label)
        return feed_for("cbp_feed.json", extra_sources=[
            StubSource([reading], "mirror", independent=False)])

    def ours(self):
        return cbp.build(load("cbp_feed.json"), NOW).port("240202").lanes["car"].cbp_update_label

    async def test_a_mirror_describing_an_older_update_is_skipped(self):
        feed = self.feed_with_mirror("At 1:00 am MDT")
        await feed.snapshot()
        check = (await feed.health())["parser_check"]
        self.assertEqual((check["compared"], check["skipped_other_update"], check["mismatches"]), (0, 1, []))

    async def test_an_unstamped_mirror_reading_is_skipped_not_compared(self):
        """Overnight the mirror says "Update pending" with no stamp while CBP may already
        have a number: which update it describes is unknowable, so it proves nothing."""
        feed = self.feed_with_mirror(None)
        await feed.snapshot()
        check = (await feed.health())["parser_check"]
        self.assertEqual((check["compared"], check["skipped_other_update"], check["mismatches"]), (0, 1, []))

    async def test_the_same_update_is_still_compared(self):
        feed = self.feed_with_mirror(self.ours().upper())
        await feed.snapshot()
        self.assertEqual(len((await feed.health())["parser_check"]["mismatches"]), 1)

    async def test_the_mirror_parser_reads_the_update_label(self):
        page = (FIXTURES / "borderswaittime.html").read_text(errors="replace")
        labels = {r.port_number: r.label for r in parse_mirror(page, NOW) if r.state == "open"}
        self.assertEqual(labels["240201"], "At 7:00 pm MDT")
