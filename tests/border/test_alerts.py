"""Drop alerts: once per crossing, remembered across restarts, never consumed.

A wait bouncing 28, 31, 29, 32 around a 30-minute limit alerted on every poll; a
restart resent the last drop, because the previous wait survived in the database and
"already sent" did not; and asking was consuming, so a second worker, or someone
checking by hand, took an alert from its real subscriber.
"""

from __future__ import annotations

import json
import unittest
from datetime import UTC, datetime, timedelta

from border_support import (  # noqa: F401 - setUpModule/tearDownModule are hooks
    NOW,
    MemoryStore,
    feed_for,
    load,
    setUpModule,
    tearDownModule,
)

from scraper.border import api
from scraper.border.devtools import fake_instagram
from scraper.border.service import BorderFeed
from scraper.border.storage import BorderStore


class DropHysteresis(unittest.IsolatedAsyncioTestCase):
    """A wait hovering around the limit must not alert on every poll."""

    def feed_with(self, *minutes: int, storage=None):
        base = load("cbp_feed.json")
        payloads = []
        for m in minutes:
            payload = json.loads(json.dumps(base))
            for port in payload:
                if port["port_number"] == "240202":
                    port["passenger_vehicle_lanes"]["standard_lanes"].update(
                        {"delay_minutes": str(m), "operational_status": "delay",
                         "update_time": "At 9:00 pm MDT"})
            payloads.append(payload)
        state = {"n": 0}

        async def fetcher(_http):
            payload = payloads[min(state["n"], len(payloads) - 1)]
            state["n"] += 1
            return payload

        return BorderFeed(storage=storage or BorderStore(client=None), fetcher=fetcher,
                          clock=lambda: NOW, cache_seconds=0, extra_sources=[])

    async def test_one_alert_per_crossing(self):
        feed = self.feed_with(40, 20, 22, 19)
        (await feed.snapshot())                                  # 40
        self.assertEqual(len((await feed.drops(30))["drops"]), 1)   # 40 -> 20: fires
        self.assertEqual((await feed.drops(30))["drops"], [])       # 22: still below, quiet
        self.assertEqual((await feed.drops(30))["drops"], [])       # 19: still below, quiet

    async def test_it_re_arms_after_the_wait_climbs_back(self):
        feed = self.feed_with(40, 20, 38, 21)
        await feed.snapshot()
        self.assertEqual(len((await feed.drops(30))["drops"]), 1)   # fires
        self.assertEqual((await feed.drops(30))["drops"], [])       # 38: above the band, re-arms
        self.assertEqual(len((await feed.drops(30))["drops"]), 1)   # 21: fires again

    async def test_a_wobble_inside_the_band_does_not_re_arm(self):
        feed = self.feed_with(40, 20, 31, 25)
        await feed.snapshot()
        self.assertEqual(len((await feed.drops(30))["drops"]), 1)
        (await feed.drops(30))                                      # 31: inside 30-35, not re-armed
        self.assertEqual((await feed.drops(30))["drops"], [])

class AlertsAreEvents(unittest.IsolatedAsyncioTestCase):
    """Alerts survive restarts and are never taken from one consumer by another."""

    def previous(self, store, minutes):
        store.readings.append({"port_number": "240202", "lane": "car", "state": "open",
                               "delay_minutes": minutes, "content_hash": "older",
                               "captured_at": (NOW - timedelta(hours=1)).isoformat()})

    async def test_a_restart_does_not_resend_a_drop(self):
        store = MemoryStore()
        self.previous(store, 48)
        first = await feed_for("cbp_feed_after_drop.json", storage=store).drops(30)
        self.assertEqual(len(first["drops"]), 1)

        restarted = feed_for("cbp_feed_after_drop.json", storage=store)     # a new process
        again = await restarted.drops(30, since=datetime.fromisoformat(first["cursor"]))
        self.assertEqual(again["drops"], [])
        self.assertEqual(len(store.alerts), 1)

    async def test_a_restart_remembers_the_lane_is_not_re_armed(self):
        store = MemoryStore()
        before = DropHysteresis.feed_with(self, 40, 20, 31, storage=store)
        await before.snapshot()
        self.assertEqual(len((await before.drops(30))["drops"]), 1)    # 40 -> 20 fires
        (await before.drops(30))                                        # 31: inside the band

        restarted = DropHysteresis.feed_with(self, 25, storage=store)   # a new process, now 25
        restarted._previous[("240202", "car")] = 31
        # 31 -> 25 crosses the limit again, but the lane never climbed back to 35.
        self.assertEqual((await restarted.drops(30))["drops"], [])
        self.assertEqual(len(store.alerts), 1)

    async def test_two_consumers_both_receive_the_alert(self):
        feed = feed_for("cbp_feed.json", "cbp_feed_after_drop.json")
        await feed.snapshot()
        worker_a = await feed.drops(30)
        worker_b = await feed.drops(30)
        self.assertEqual([d["id"] for d in worker_a["drops"]], [d["id"] for d in worker_b["drops"]])
        self.assertEqual(len(worker_b["drops"]), 1)

    async def test_a_consumer_that_was_away_catches_up_with_its_cursor(self):
        feed = feed_for("cbp_feed.json", "cbp_feed_after_drop.json")
        cursor = datetime(2000, 1, 1, tzinfo=UTC)
        await feed.snapshot()
        (await feed.drops(30))                                   # someone else asked
        caught_up = await feed.drops(30, since=cursor)
        self.assertEqual(len(caught_up["drops"]), 1)

    async def test_the_fake_pipeline_alerts_every_subscriber(self):
        feed = feed_for("cbp_feed.json", "cbp_feed_after_drop.json")
        await feed.snapshot()
        pipeline = fake_instagram.FakePipeline(feed, fake_instagram.FakeGraph())
        await pipeline.on_message("a", "avísame cuando baje de 30")
        await pipeline.on_message("b", "avísame cuando baje de 30")
        self.assertEqual((await pipeline.alert_sweep()), 2)
        self.assertEqual((await pipeline.alert_sweep()), 0)      # cursors: nothing new

    async def test_drops_rejects_a_bad_cursor(self):
        status, _ = await api.route(feed_for("cbp_feed.json"), "/drops", {"below": ["30"], "since": ["soon"]})
        self.assertEqual(status, 400)
