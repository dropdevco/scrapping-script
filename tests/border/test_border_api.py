"""The local HTTP pull surface: shaped answers, never a dropped connection.

An unknown lane once raised straight out of the handler and the client saw a reset
connection instead of an error; every path now answers with a status and a body.
"""

from __future__ import annotations

import unittest
from datetime import datetime

from border_support import (  # noqa: F401 - setUpModule/tearDownModule are hooks
    NOW,
    feed_for,
    raises,
    setUpModule,
    tearDownModule,
)

from scraper.border import api
from scraper.border.service import BorderFeed, FeedError
from scraper.border.storage import BorderStore


class PullSurface(unittest.IsolatedAsyncioTestCase):
    async def test_waits_carries_both_captions(self):
        body = await feed_for("cbp_feed.json").waits()
        self.assertTrue(body["caption_es"] and body["caption_en"])
        self.assertEqual(len(body["ports"]), 6)

    async def test_bridge_lookup_returns_reply_and_minutes(self):
        body = await feed_for("cbp_feed.json").bridge("paso-del-norte", "car")
        self.assertTrue(body["found"])
        self.assertEqual(body["minutes"], 48)

    async def test_a_bridge_name_with_spaces_survives_the_url(self):
        status, body = await api.route(feed_for("cbp_feed.json"), "/waits/el%20puente%20libre", {})
        self.assertEqual(status, 200)
        self.assertEqual(body["port_number"], "240201")

    async def test_unknown_bridge_is_404_with_options(self):
        status, body = await api.route(feed_for("cbp_feed.json"), "/waits/narnia", {})
        self.assertEqual(status, 404)
        self.assertIn("options", body)

    async def test_drops_requires_a_numeric_limit(self):
        status, body = await api.route(feed_for("cbp_feed.json"), "/drops", {})
        self.assertEqual(status, 400)
        self.assertIn("below", body["error"])

    async def test_alert_text_has_no_doubled_full_stop(self):
        feed = feed_for("cbp_feed.json", "cbp_feed_after_drop.json")
        await feed.snapshot()
        message = (await feed.drops(30))["drops"][0]["message"]
        self.assertNotIn("..", message)
        self.assertIn("Cruzas alrededor de", message)

    async def test_first_reading_is_not_a_drop(self):
        self.assertEqual((await feed_for("cbp_feed.json").drops(30))["drops"], [])

    async def test_crossing_under_the_limit_is_a_drop_once(self):
        feed = feed_for("cbp_feed.json", "cbp_feed_after_drop.json")
        (await feed.snapshot())                       # 48 min
        answer = (await feed.drops(30))               # 20 min -> crossed
        drops = answer["drops"]
        self.assertEqual([d["port_number"] for d in drops], ["240202"])
        self.assertEqual((drops[0]["previous_minutes"], drops[0]["minutes"]), (48, 20))
        since = datetime.fromisoformat(answer["cursor"])
        self.assertEqual((await feed.drops(30, since=since))["drops"], [])   # already below: not a new drop
        # Asking again without a cursor is not consuming: same alert, same id.
        self.assertEqual([d["id"] for d in (await feed.drops(30))["drops"]], [drops[0]["id"]])

    async def test_cbp_failure_is_503_not_a_crash(self):
        boom = raises(OSError("connection reset"))
        feed = BorderFeed(storage=BorderStore(client=None), fetcher=boom, clock=lambda: NOW,
                          extra_sources=[])
        status, body = await api.route(feed, "/waits", {})
        self.assertEqual(status, 503)
        self.assertIn("connection reset", body["error"])
        with self.assertRaises(FeedError):
            await feed.snapshot()

    async def test_health_on_a_fresh_process_takes_a_reading(self):
        feed = feed_for("cbp_feed.json")
        health = (await feed.health())                      # no snapshot taken first
        self.assertTrue(health["ok"])
        self.assertIsNotNone(health["last_ok"])

    async def test_health_stays_honest_when_cbp_is_down(self):
        boom = raises(OSError("no route to host"))
        feed = BorderFeed(storage=BorderStore(client=None), fetcher=boom, clock=lambda: NOW,
                          extra_sources=[])
        health = await feed.health()
        self.assertFalse(health["ok"])
        self.assertIn("no route to host", health["last_error"])

    async def test_health_reports_storage_disabled(self):
        feed = feed_for("cbp_feed.json")
        await feed.snapshot()
        self.assertFalse((await feed.health())["storage_enabled"])
        self.assertTrue((await feed.health())["ok"])
