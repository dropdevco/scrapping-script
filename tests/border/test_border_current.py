"""border_current_waits: the crossing times the knowledge base reads.

Agreed 2026-09-29: GitHub Actions -> Supabase -> Google Sheet -> GoHighLevel, whose LLM
answers Instagram questions from that knowledge base, and there was no crossing-times
table for it to read. Three rules come from the importer and the bot, not from taste:
every lane of every bridge has a row on every run (a lane that drops out of the table
drops out of the bot's answers); no text cell is ever empty (GoHighLevel rejects the
whole row); and times are absolute dates, because "hace 5 min" is false by the time
anyone asks.
"""

from __future__ import annotations

import re
import unittest
from datetime import UTC, datetime

from border_support import (  # noqa: F401 - setUpModule/tearDownModule are hooks
    MIDDAY,
    StubSource,
    feed_for,
    setUpModule,
    tearDownModule,
)

from scraper.border import current
from scraper.border.sources.base import Reading

TEXT_COLUMNS = ("also_known_as", "bridge_es", "bridge_en", "lane_es", "lane_en", "state", "wait_es", "wait_en",
                "summary_es", "summary_en", "source", "port_number", "lane", "checked_at")
# The lanes agreed per bridge (walking SENTRI is published by no source and was dropped).
AGREED = {
    "240201": {"walk", "truck", "car_ready", "car"},                    # Puente Libre
    "240202": {"car_sentri", "car_ready", "car", "walk"},               # Centro (Paso del Norte)
    "240204": {"car_sentri"},                                           # Lerdo
    "240203": {"car_ready", "car", "car_sentri", "walk"},               # Zaragoza
}


class CurrentWaits(unittest.IsolatedAsyncioTestCase):
    async def rows(self, extra_sources=()):
        feed = feed_for("cbp_feed_midday.json", now=MIDDAY, extra_sources=list(extra_sources))
        snap = await feed.snapshot()
        return current.rows(feed, snap, MIDDAY), snap

    async def test_every_lane_cbp_lists_has_a_row(self):
        rows, snap = await self.rows()
        expected = {(p.port_number, lane) for p in snap.ports for lane in p.lanes}
        self.assertEqual({(r["port_number"], r["lane"]) for r in rows}, expected)

    async def test_the_agreed_lanes_are_all_there(self):
        rows, _ = await self.rows()
        by_port: dict[str, set] = {}
        for r in rows:
            by_port.setdefault(r["port_number"], set()).add(r["lane"])
        for port, lanes in AGREED.items():
            with self.subTest(port=port):
                self.assertLessEqual(lanes, by_port[port])

    async def test_no_text_cell_is_ever_empty(self):
        rows, _ = await self.rows()
        for r in rows:
            for column in TEXT_COLUMNS:
                with self.subTest(lane=(r["port_number"], r["lane"]), column=column):
                    self.assertTrue(str(r[column]).strip())

    async def test_a_shut_lane_says_so_instead_of_disappearing(self):
        rows, _ = await self.rows()
        zaragoza_cars = next(r for r in rows if (r["port_number"], r["lane"]) == ("240203", "car"))
        self.assertEqual((zaragoza_cars["state"], zaragoza_cars["minutes"]), ("closed", None))
        self.assertEqual((zaragoza_cars["wait_es"], zaragoza_cars["wait_en"]), ("cerrado", "closed"))
        self.assertIn("Zaragoza–Ysleta · Autos: cerrado", zaragoza_cars["summary_es"])

    async def test_times_are_absolute_never_relative(self):
        rows, _ = await self.rows()
        pdn = next(r for r in rows if (r["port_number"], r["lane"]) == ("240202", "car"))
        self.assertEqual(pdn["summary_es"],
                         "Paso del Norte (Santa Fe) · Autos: 48 min según CBP "
                         "(revisado el 22 sep 2026, 1:40 p. m.). También le dicen Centro, Santa Fe o PDN.")
        self.assertEqual(pdn["summary_en"],
                         "Paso del Norte · Cars: 48 min per CBP (checked Sep 22, 2026, 1:40 pm). "
                         "Also known as Centro, Santa Fe or PDN.")
        for r in rows:
            for column in ("summary_es", "summary_en"):
                self.assertNotRegex(r[column], re.compile(r"\bhace\b|\bago\b|\bhoy\b|\btoday\b"))

    async def test_the_minutes_are_what_a_follower_would_be_told(self):
        fresher = Reading("240202", "car", "open", 20, age_minutes=2, source="pasosfronterizos")
        rows, _ = await self.rows([StubSource([fresher], "pasosfronterizos")])
        pdn = next(r for r in rows if (r["port_number"], r["lane"]) == ("240202", "car"))
        self.assertEqual((pdn["minutes"], pdn["cbp_minutes"], pdn["source"]), (20, 48, "pasosfronterizos"))
        self.assertIn("según pasosfronterizos.com", pdn["summary_es"])

    async def test_the_names_from_the_meeting_are_in_every_row(self):
        """"¿cómo está Centro?" must find a row: Centro is in no official name."""
        rows, _ = await self.rows()
        by_port = {r["port_number"]: r for r in rows}
        for port, name in (("240202", "Centro"), ("240201", "Puente Libre"),
                           ("240204", "Lerdo"), ("240203", "Zaragoza")):
            with self.subTest(name=name):
                self.assertIn(name, by_port[port]["also_known_as"])
                self.assertIn(name, by_port[port]["summary_es"])

    async def test_checked_at_is_the_run_time(self):
        rows, _ = await self.rows()
        self.assertTrue(all(datetime.fromisoformat(r["checked_at"]) == MIDDAY.astimezone(UTC) for r in rows))
