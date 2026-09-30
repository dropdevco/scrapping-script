"""The live selfcheck: unit tests run on saved responses, so they stay green through
exactly the drift this catches — a renamed field, a missing port, a new status word.
"""

from __future__ import annotations

import unittest

from border_support import (  # noqa: F401 - setUpModule/tearDownModule are hooks
    load,
    setUpModule,
    tearDownModule,
)

from scraper.border import selfcheck


class SelfCheck(unittest.IsolatedAsyncioTestCase):
    """The check that catches CBP renaming something while the tests stay green."""

    async def test_a_healthy_feed_passes(self):
        check = selfcheck.Check()
        selfcheck.check_feed(load("cbp_feed.json"), check)
        self.assertTrue(check.ok, check.problems)

    async def test_a_renamed_field_is_caught(self):
        payload = load("cbp_feed.json")
        for port in payload:
            if port["port_number"] == "240202":
                port["passenger_vehicle_lanes"]["standard_lanes"]["wait_minutes"] = (
                    port["passenger_vehicle_lanes"]["standard_lanes"].pop("delay_minutes"))
        check = selfcheck.Check()
        selfcheck.check_feed(payload, check)
        self.assertFalse(check.ok)
        self.assertIn("delay_minutes", check.problems[0])

    async def test_a_missing_port_is_caught(self):
        payload = [p for p in load("cbp_feed.json") if p["port_number"] != "240202"]
        check = selfcheck.Check()
        selfcheck.check_feed(payload, check)
        self.assertIn("no longer lists", check.problems[0])

    async def test_an_unknown_status_word_is_caught(self):
        payload = load("cbp_feed.json")
        for port in payload:
            if port["port_number"] == "240202":
                port["passenger_vehicle_lanes"]["standard_lanes"]["operational_status"] = "Backed Up"
        check = selfcheck.Check()
        selfcheck.check_feed(payload, check)
        self.assertIn("unknown status", check.problems[0])
