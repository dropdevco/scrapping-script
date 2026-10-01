"""Real crossing times: the only ground truth for a "CBP 20, pasosfronterizos 58" dispute.

Every source's claim is frozen when they join the line, so each is judged on what it
said then; a zero-minute or six-hour crossing is a mistyped message, not data; and the
crossing id reaches a query only after it parses as a UUID.
"""

from __future__ import annotations

import unittest
from datetime import timedelta

from border_support import (  # noqa: F401 - setUpModule/tearDownModule are hooks
    NOW,
    MemoryStore,
    feed_for,
    setUpModule,
    tearDownModule,
)

from scraper.border import api
from scraper.border.crossings import CrossingError
from scraper.border.devtools import fake_instagram


class RealCrossingTimes(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.now = [NOW]
        self.store = MemoryStore()
        self.feed = feed_for("cbp_feed.json", storage=self.store, clock=lambda: self.now[0])

    async def cross(self, port, minutes):
        started = await self.feed.start_crossing(port, "car")
        self.now[0] += timedelta(minutes=minutes)
        return started, (await self.feed.finish_crossing(started["id"]))

    async def test_a_crossing_freezes_the_claims_and_measures_the_wait(self):
        started, done = await self.cross("paso-del-norte", 55)
        self.assertEqual([c["source"] for c in started["claims"]], ["cbp"])
        self.assertEqual(done["actual_minutes"], 55)
        self.assertEqual(done["errors"], {"cbp": 48 - 55})
        self.assertIn("Tardaste 55 min", done["reply"])
        self.assertEqual(self.store.crossings[started["id"]]["actual_minutes"], 55)

    async def test_accuracy_ranks_sources_by_error(self):
        await self.cross("paso-del-norte", 55)
        report = await self.feed.accuracy()
        self.assertEqual(report["crossings"], 1)
        self.assertEqual(report["sources"]["cbp"]["mean_abs_error"], 7.0)
        self.assertEqual(report["sources"]["cbp"]["bias"], -7.0)
        self.assertFalse(report["sources"]["cbp"]["enough_data"])

    async def test_it_finishes_after_a_restart(self):
        started = await self.feed.start_crossing("paso-del-norte", "car")
        restarted = feed_for("cbp_feed.json", storage=self.store,
                             clock=lambda: NOW + timedelta(minutes=40))
        self.assertEqual((await restarted.finish_crossing(started["id"]))["actual_minutes"], 40)

    async def test_nonsense_is_refused(self):
        started = await self.feed.start_crossing("paso-del-norte", "car")
        with self.assertRaises(CrossingError):
            (await self.feed.finish_crossing(started["id"]))               # zero minutes
        with self.assertRaises(CrossingError):
            await self.feed.finish_crossing("'; drop table border_crossings; --")
        with self.assertRaises(CrossingError):
            (await self.feed.start_crossing("zaragoza", "car"))            # closed lane

    async def test_over_http(self):
        status, started = await api.route(self.feed, "/crossings", {}, "POST", {"port": "pdn", "lane": "car"})
        self.assertEqual(status, 201)
        self.now[0] += timedelta(minutes=30)
        status, done = await api.route(self.feed, f"/crossings/{started['id']}/done", {}, "POST", {})
        self.assertEqual((status, done["actual_minutes"]), (200, 30))
        status, _ = await api.route(self.feed, f"/crossings/{started['id']}/done", {}, "POST", {})
        self.assertEqual(status, 422)
        self.assertEqual((await api.route(self.feed, "/accuracy", {}))[1]["crossings"], 1)
        self.assertEqual((await api.route(self.feed, "/crossings/nope/done", {}, "POST", {}))[0], 404)

    async def test_the_conversation(self):
        graph = fake_instagram.FakeGraph()
        pipeline = fake_instagram.FakePipeline(self.feed, graph)
        await pipeline.on_message("u", "voy a cruzar paso del norte")
        self.assertIn("ya crucé", graph.calls[-1]["params"]["message"]["text"])
        self.now[0] += timedelta(minutes=50)
        await pipeline.on_message("u", "ya crucé")
        self.assertIn("Tardaste 50 min", graph.calls[-1]["params"]["message"]["text"])
