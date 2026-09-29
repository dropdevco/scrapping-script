"""The rehearsal tools: the fake Instagram and the HTTP client it pulls through.
"""

from __future__ import annotations

import contextlib
import io
import json
import unittest
import urllib.error
from unittest import mock

from border_support import (  # noqa: F401 - setUpModule/tearDownModule are hooks
    feed_for,
    setUpModule,
    tearDownModule,
)

from scraper.border.devtools import fake_instagram


class HttpPull(unittest.IsolatedAsyncioTestCase):
    """The pipeline pulls over HTTP, where a 404 arrives as an exception."""

    async def test_a_404_body_is_returned_not_raised(self):
        payload = json.dumps({"found": False, "reply": "No encuentro ese puente.",
                              "options": ["Zaragoza"]}).encode()
        error = urllib.error.HTTPError("u", 404, "Not Found", {}, io.BytesIO(payload))
        with mock.patch("urllib.request.urlopen", side_effect=error):
            body = await fake_instagram.HttpFeed("http://x").bridge("narnia")
        self.assertFalse(body["found"])
        self.assertIn("options", body)

    async def test_a_503_body_is_returned_not_raised(self):
        payload = json.dumps({"error": "CBP feed unavailable"}).encode()
        error = urllib.error.HTTPError("u", 503, "Unavailable", {}, io.BytesIO(payload))
        with mock.patch("urllib.request.urlopen", side_effect=error):
            body = await fake_instagram.HttpFeed("http://x").waits()
        self.assertIn("CBP feed unavailable", body["error"])

    async def test_spaces_in_a_bridge_name_are_encoded(self):
        seen = {}

        class Fake(io.BytesIO):
            def __enter__(self): return self
            def __exit__(self, *a): return False

        def opener(url, timeout=None):
            seen["url"] = url
            return Fake(b"{}")

        with mock.patch("urllib.request.urlopen", opener):
            await fake_instagram.HttpFeed("http://x").bridge("el puente libre")
        self.assertIn("el%20puente%20libre", seen["url"])
        self.assertNotIn(" ", seen["url"])

class InstagramSimulation(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.graph = fake_instagram.FakeGraph()
        self.feed = feed_for("cbp_feed.json", "cbp_feed_after_drop.json")
        self.pipeline = fake_instagram.FakePipeline(self.feed, self.graph)

    async def test_daily_post_publishes_a_caption_and_sends_nothing(self):
        caption = await self.pipeline.daily_post()
        self.assertIn("Puentes Juárez–El Paso", caption)
        self.assertEqual([c["endpoint"].rsplit("/", 1)[-1] for c in self.graph.calls],
                         ["media", "media_publish"])

    async def test_bridge_question_is_answered_with_live_minutes(self):
        await self.pipeline.on_message("user-1", "paso del norte")
        self.assertIn("48 min", self.graph.calls[-1]["params"]["message"]["text"])

    async def test_menu_offers_quick_replies(self):
        await self.pipeline.on_message("user-1", "puentes")
        self.assertTrue(self.graph.calls[-1]["params"]["message"]["quick_replies"])

    async def test_alert_subscription_then_drop_sends_one_message(self):
        await self.pipeline.on_message("user-2", "avísame cuando baje de 30 min")
        (await self.feed.snapshot())                       # first reading: 48 min
        self.assertEqual((await self.pipeline.alert_sweep()), 1)
        self.assertIn("20 min", self.graph.calls[-1]["params"]["message"]["text"])

    async def test_saving_a_bridge_then_asking_returns_only_that_bridge(self):
        await self.pipeline.on_message("user-1", "guardar paso del norte sentri")
        await self.pipeline.on_message("user-1", "puentes")
        sent = self.graph.calls[-1]["params"]["message"]["text"]
        self.assertIn("Tus puentes", sent)
        self.assertIn("SENTRI", sent)
        self.assertNotIn("Santa Teresa", sent)

    async def test_scheduled_message_uses_their_saved_bridges(self):
        await self.pipeline.on_message("user-1", "guardar zaragoza")
        await self.pipeline.scheduled_message("user-1")
        self.assertIn("Tus puentes", self.graph.calls[-1]["params"]["message"]["text"])

    async def test_stop_cancels_subscriptions(self):
        await self.pipeline.on_message("user-2", "avísame cuando baje de 30")
        await self.pipeline.on_message("user-2", "alto")
        self.assertEqual(self.pipeline.subscriptions, [])

    async def test_simulation_run_records_calls_without_network(self):
        with contextlib.redirect_stdout(io.StringIO()):
            graph = await fake_instagram.run(feed_for("cbp_feed.json", "cbp_feed_after_drop.json"))
        self.assertTrue(graph.calls)
        self.assertTrue(all(c["endpoint"].startswith("https://graph.facebook.com") for c in graph.calls))
