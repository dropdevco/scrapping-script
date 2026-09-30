"""Captions and replies: what a follower actually reads.

Minutes appear only for an open lane, a disputed number is shown as its range rather
than its optimistic end, a future stamp is never quoted as "updated", and a caption
past Instagram's 2,200-character limit fails at publish time — so it is capped at 2,000,
dropping the compact lane lines before the headline.
"""

from __future__ import annotations

import unittest
from unittest import mock

from border_support import (  # noqa: F401 - setUpModule/tearDownModule are hooks
    MIDDAY,
    StubSource,
    feed_for,
    setUpModule,
    tearDownModule,
)

from scraper.border import text
from scraper.border.sources.base import Reading


class Captions(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.feed = feed_for("cbp_feed.json", now=MIDDAY)
        self.snap = await self.feed.snapshot()

    def caption(self, lang="es"):
        return text.caption(self.feed, self.snap, lang)

    async def test_caption_leads_with_the_fastest_bridge(self):
        second = self.caption().splitlines()[1]
        self.assertTrue(second.startswith("Más rápido ahora:"), second)
        self.assertIn("Jerónimo–Santa Teresa 40 min", second)

    async def test_headline_offers_the_crossing_time(self):
        self.assertIn("cruzas", self.caption().splitlines()[1])

    async def test_cars_are_listed_fastest_first(self):
        lines = self.caption().splitlines()
        cars = lines[lines.index("Autos") + 1:]
        minutes = [int(l.split()[-2]) for l in cars if l.endswith("min")]
        self.assertEqual(minutes, sorted(minutes))

    async def test_a_bridge_closing_too_soon_is_labelled_in_the_list(self):
        night = feed_for("cbp_feed.json")
        line = next(l for l in text.caption(night, (await night.snapshot()), "es").splitlines()
                    if l.startswith("Santa Teresa"))
        self.assertIn("cierra", line)

    async def test_other_lanes_are_one_compact_line_each(self):
        sentri = [l for l in self.caption().splitlines() if l.startswith("SENTRI:")]
        self.assertEqual(len(sentri), 1)
        self.assertIn("·", sentri[0])

    async def test_caption_never_quotes_a_future_stamp(self):
        # At 9:40 pm the fixture carries a 10:00 pm stamp; the caption must use 9:00.
        night = feed_for("cbp_feed.json")
        stamp_line = next(l for l in text.caption(night, (await night.snapshot()), "es").splitlines()
                          if l.startswith("Datos de CBP"))
        self.assertIn("9:00 p. m.", stamp_line)

    async def test_caption_names_the_bridge_with_no_data(self):
        self.assertIn("Tornillo", self.caption())

    async def test_closed_lane_says_closed_not_zero(self):
        reply = (await self.feed.bridge("240203", "car", "es"))["reply"]
        self.assertIn("cerrado", reply)
        self.assertNotIn("0 min", reply)

    async def test_no_data_lane_says_so(self):
        self.assertIn("no está reportando", (await self.feed.bridge("tornillo", "car", "es"))["reply"])

    async def test_unknown_bridge_gets_a_helpful_reply(self):
        self.assertIn("puentes", (await self.feed.bridge("narnia", "car", "es"))["reply"])

    async def test_english_reply_carries_update_time(self):
        reply = (await self.feed.bridge("paso-del-norte", "car", "en"))["reply"]
        self.assertRegex(reply, r"Paso del Norte · Cars: \d+ min")
        self.assertRegex(reply, r"CBP updated at \d+:\d\d [ap]m")

    async def test_a_reply_cites_the_source_the_number_came_from(self):
        stub = Reading("240202", "car", "open", 62, age_minutes=3, source="otrositio")
        feed = feed_for("cbp_feed.json", extra_sources=[StubSource([stub], "otrositio")])
        reply = (await feed.bridge("paso-del-norte", "car", "es"))["reply"]
        self.assertIn("otrositio, hace 3 min", reply)
        self.assertNotIn("CBP actualizó", reply)

class CaptionLimits(unittest.IsolatedAsyncioTestCase):
    async def test_a_normal_caption_is_well_inside_instagram_limits(self):
        feed = feed_for("cbp_feed.json")
        self.assertLess(len(text.caption(feed, (await feed.snapshot()), "es")), text.CAPTION_LIMIT)

    async def test_an_overlong_caption_is_trimmed_to_fit(self):
        feed = feed_for("cbp_feed.json")
        snap = await feed.snapshot()
        with mock.patch.object(text, "CAPTION_LIMIT", 200):
            caption = text.caption(feed, snap, "es")
        self.assertLessEqual(len(caption), 200)
        self.assertIn("Más rápido ahora", caption)      # the headline always survives
