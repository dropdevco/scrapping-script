"""BorderFeed: one reading, reconciled, ranked and explained.

The failures pinned here were all silent. A delta compared against the previous fetch
rather than the previous CBP update read a 48 -> 20 drop as "flat" five minutes later.
The fastest bridge at 9:40 pm closes at 10, so recommending it sends someone to a shut
booth. CBP answering happily with numbers frozen for hours looks exactly like a quiet
border. And a fresh process that forgets the previous reading loses a cycle of deltas
after every deploy.
"""

from __future__ import annotations

import asyncio
import json
import unittest
from datetime import UTC, datetime, timedelta
from unittest import mock

from border_support import (  # noqa: F401 - setUpModule/tearDownModule are hooks
    MIDDAY,
    NOW,
    Always60Store,
    MemoryStore,
    StubSource,
    feed_for,
    load,
    raises,
    returns,
    setUpModule,
    tearDownModule,
)

from scraper.border import api, cbp, poll, text
from scraper.border import service as service_mod
from scraper.border.service import BorderFeed, FeedError
from scraper.border.sources.base import Reading, Source
from scraper.border.storage import BorderStore


class CommuterFacts(unittest.IsolatedAsyncioTestCase):
    async def test_delta_shows_movement_between_readings(self):
        feed = feed_for("cbp_feed.json", "cbp_feed_after_drop.json")
        (await feed.snapshot())                                  # 48 min
        body = (await feed.bridge("paso-del-norte", "car"))      # 20 min
        self.assertEqual((body["delta"]["change"], body["delta"]["direction"]), (-28, "down"))
        self.assertIn("↓28", body["reply"])

    async def test_small_change_is_not_shouted_about(self):
        feed = feed_for("cbp_feed.json")
        feed._previous[("240202", "car")] = 50           # 48 now: a 2-minute wobble
        self.assertFalse((await feed.bridge("paso-del-norte", "car"))["delta"]["meaningful"])
        self.assertNotIn("↓", (await feed.bridge("paso-del-norte", "car"))["reply"])

    async def test_crosses_at_is_now_plus_the_wait(self):
        body = await feed_for("cbp_feed.json").bridge("paso-del-norte", "car")
        crossing = datetime.fromisoformat(body["crosses_at"])
        self.assertEqual(crossing.strftime("%H:%M"), "22:28")   # 21:40 MDT + 48 min
        self.assertIn("cruzas", body["reply"])

    async def test_best_is_the_fastest_open_bridge(self):
        best = await feed_for("cbp_feed.json", now=MIDDAY).best("car")
        self.assertEqual((best["port_number"], best["minutes"]), ("240801", 40))

    async def test_ranked_puts_every_unusable_bridge_after_the_open_ones(self):
        rows = await feed_for("cbp_feed.json", now=MIDDAY).ranked("car")
        states = [r["state"] for r in rows]
        self.assertEqual(rows[0]["port_number"], "240801")          # fastest first
        self.assertEqual(states, sorted(states, key=lambda s: s != "open"))
        self.assertIn(states[-1], ("closed", "no_data"))

    async def test_reply_suggests_a_materially_faster_bridge(self):
        body = (await feed_for("cbp_feed.json", now=MIDDAY).bridge("bota", "car"))   # 55 min vs 40
        self.assertEqual(body["alternative"]["port_number"], "240801")
        self.assertIn("15 menos", body["reply"])

    async def test_no_suggestion_when_the_saving_is_small(self):
        feed = feed_for("cbp_feed.json", now=MIDDAY)
        self.assertIsNone((await feed.bridge("paso-del-norte", "car"))["alternative"])  # 48 vs 40

    async def test_my_digest_is_only_their_bridges(self):
        digest = await feed_for("cbp_feed.json").my_digest(["paso-del-norte", "bota"], "car_sentri")
        self.assertEqual(len(digest["bridges"]), 2)
        self.assertLessEqual(len(digest["message"].splitlines()), 4)
        self.assertIn("Tus puentes", digest["message"])

    async def test_my_digest_flags_a_faster_bridge_they_did_not_save(self):
        message = (await feed_for("cbp_feed.json", now=MIDDAY).my_digest(["bota"], "car"))["message"]
        self.assertIn("Más rápido", message)

    async def test_empty_saved_list_asks_them_to_pick(self):
        self.assertIn("puentes", (await feed_for("cbp_feed.json").my_digest([], "car"))["message"])

class ClosingTimes(unittest.IsolatedAsyncioTestCase):
    """Santa Teresa runs 6 am-10 pm; Stanton 6 am-midnight; the rest are 24 hours."""

    async def test_hours_are_parsed_from_the_feed_text(self):
        self.assertEqual(cbp.parse_hours("6 am-10 pm"), (360, 1320))
        self.assertEqual(cbp.parse_hours("6 am-Midnight"), (360, 1440))
        self.assertIsNone(cbp.parse_hours("24 hrs/day"))

    async def test_bridge_that_shuts_before_arrival_is_flagged(self):
        body = (await feed_for("cbp_feed.json").bridge("santa-teresa", "car"))   # 21:40 + 40 min > 22:00
        self.assertTrue(body["closes_before_you_cross"])
        self.assertIn("cierra", body["reply"])
        self.assertIn("no alcanzas", body["reply"])

    async def test_the_same_bridge_is_fine_at_midday(self):
        body = await feed_for("cbp_feed.json", now=MIDDAY).bridge("santa-teresa", "car")
        self.assertFalse(body["closes_before_you_cross"])
        self.assertNotIn("cierra", body["reply"])

    async def test_best_skips_a_bridge_that_closes_too_soon(self):
        night = await feed_for("cbp_feed.json").best("car")
        self.assertEqual(night["port_number"], "240202")   # not Santa Teresa, faster but shutting
        day = await feed_for("cbp_feed.json", now=MIDDAY).best("car")
        self.assertEqual(day["port_number"], "240801")

    async def test_a_tight_bridge_is_offered_an_alternative(self):
        body = await feed_for("cbp_feed.json").bridge("santa-teresa", "car")
        self.assertEqual(body["alternative"]["port_number"], "240202")
        self.assertIn("Mejor", body["reply"])

    async def test_round_the_clock_bridges_have_no_closing_time(self):
        self.assertIsNone((await feed_for("cbp_feed.json").bridge("bota", "car"))["closes_at"])

class Ranking(unittest.IsolatedAsyncioTestCase):
    """A recommendation is a bet about arrival, not a copy of CBP's newest number."""

    def score(self, minutes, age, change=None, stamp_ahead=False):
        feed = feed_for("cbp_feed.json")
        delta = None if change is None else {"change": change, "previous_minutes": minutes - change,
                                             "direction": "up" if change > 0 else "down",
                                             "meaningful": abs(change) >= 5}
        return feed._score(minutes, age, delta, stamp_ahead)

    async def test_a_fresh_reading_is_not_penalised(self):
        self.assertEqual(self.score(30, 0)["effective_minutes"], 30)

    async def test_an_old_reading_is_discounted(self):
        self.assertEqual(self.score(30, 120)["staleness_penalty"], 16)
        self.assertEqual(self.score(30, 120)["effective_minutes"], 46)

    async def test_staleness_is_capped(self):
        self.assertEqual(self.score(30, 600)["staleness_penalty"], 25)

    async def test_a_growing_line_is_expected_to_keep_growing(self):
        self.assertEqual(self.score(30, 0, change=20)["trend_penalty"], 12)

    async def test_a_shrinking_line_is_trusted_less_than_a_growing_one(self):
        self.assertEqual(self.score(30, 0, change=-20)["trend_penalty"], -6)

    async def test_a_stamp_ahead_of_the_clock_is_treated_as_unknown_age(self):
        honest = self.score(30, 0)["staleness_penalty"]
        suspect = self.score(30, 0, stamp_ahead=True)["staleness_penalty"]
        self.assertGreater(suspect, honest)

    async def test_a_slower_but_much_fresher_bridge_can_win(self):
        feed = feed_for("cbp_feed.json")
        rows = {r["port_number"]: r for r in (await feed.ranked("car"))}
        # Paso del Norte: 48 min, Bridge of the Americas: 55 min with a suspect stamp
        self.assertLess(rows["240202"]["effective_minutes"], rows["240201"]["effective_minutes"])

    async def test_confidence_is_reported_per_lane(self):
        feed = feed_for("cbp_feed.json")
        self.assertEqual((await feed.bridge("paso-del-norte", "car"))["confidence"], "medium")
        self.assertIsNone((await feed.bridge("tornillo", "car"))["confidence"])

    async def test_the_working_is_shown_not_hidden(self):
        score = (await feed_for("cbp_feed.json").bridge("paso-del-norte", "car"))["score"]
        self.assertEqual(set(score), {"effective_minutes", "staleness_penalty",
                                      "trend_penalty", "age_used"})

class PreviousFromDatabase(unittest.IsolatedAsyncioTestCase):
    """A restarted process has no memory; border_readings is the fallback."""

    def feed_with_history(self, minutes):
        rows = [{"port_number": "240202", "lane": "car", "state": "open",
                 "delay_minutes": minutes, "content_hash": "older"}]
        return feed_for("cbp_feed.json", storage=MemoryStore(rows))

    async def test_delta_survives_a_restart(self):
        body = await self.feed_with_history(60).bridge("paso-del-norte", "car")
        self.assertEqual(body["delta"]["previous_minutes"], 60)
        self.assertIn("↓12", body["reply"])

    async def test_drop_alert_survives_a_restart(self):
        drops = (await self.feed_with_history(60).drops(50))["drops"]
        self.assertEqual([d["port_number"] for d in drops], ["240202"])

    async def test_memory_wins_over_the_database(self):
        feed = self.feed_with_history(60)
        feed._previous[("240202", "car")] = 45
        self.assertEqual((await feed.bridge("paso-del-norte", "car"))["delta"]["previous_minutes"], 45)

class FrozenFeed(unittest.IsolatedAsyncioTestCase):
    """CBP answering happily with numbers that stopped moving is the quiet failure."""

    def feed_at(self, *times):
        clock = {"n": 0}

        def tick():
            now = times[min(clock["n"], len(times) - 1)]
            clock["n"] += 1
            return now

        return feed_for("cbp_feed.json", clock=tick)

    async def test_identical_numbers_for_hours_is_degraded(self):
        feed = self.feed_at(NOW, NOW + timedelta(hours=7))
        await feed.snapshot()
        await feed.snapshot()
        health = await feed.health()
        self.assertTrue(health["frozen"])
        self.assertTrue(health["degraded"])
        self.assertFalse(health["ok"])
        self.assertGreaterEqual(health["frozen_hours"], 6)

    async def test_a_short_quiet_spell_is_normal(self):
        feed = self.feed_at(NOW, NOW + timedelta(hours=1))
        await feed.snapshot()
        await feed.snapshot()
        self.assertFalse((await feed.health())["frozen"])
        self.assertTrue((await feed.health())["ok"])

    async def test_the_clock_restarts_when_numbers_move(self):
        payloads = [load("cbp_feed.json"), load("cbp_feed_after_drop.json")]
        clock = [NOW, NOW + timedelta(hours=7)]
        state = {"n": 0}
        fetched = {"n": 0}

        async def fetcher(_http):
            i = min(fetched["n"], len(payloads) - 1)
            fetched["n"] += 1
            return payloads[i]

        def tick():
            now = clock[min(state["n"], len(clock) - 1)]
            state["n"] += 1
            return now

        feed = BorderFeed(storage=BorderStore(client=None), fetcher=fetcher, clock=tick,
                          cache_seconds=0, extra_sources=[])
        await feed.snapshot()
        await feed.snapshot()
        self.assertFalse((await feed.health())["frozen"])

    async def test_every_bridge_dark_is_degraded(self):
        blanked = load("cbp_feed.json")
        for port in blanked:
            for group in ("passenger_vehicle_lanes", "pedestrian_lanes", "commercial_vehicle_lanes"):
                for lane_cfg in port.get(group, {}).values():
                    if isinstance(lane_cfg, dict):
                        lane_cfg["operational_status"] = "Update Pending"
        feed = BorderFeed(storage=BorderStore(client=None), fetcher=returns(lambda: blanked),
                          clock=lambda: NOW, cache_seconds=0, extra_sources=[])
        await feed.snapshot()
        self.assertTrue((await feed.health())["all_bridges_dark"])
        self.assertFalse((await feed.health())["ok"])

class MultiSourceFeed(unittest.IsolatedAsyncioTestCase):
    """A second source refines the minutes; it can never invent or remove a bridge."""

    class Stub(Source):
        name = "stub"

        def __init__(self, readings, boom=False):
            self.readings, self.boom = readings, boom

        async def fetch(self, http, now):
            if self.boom:
                raise TimeoutError("site slow")
            return self.readings

    def feed_with(self, source, now=None, fixture="cbp_feed.json"):
        return feed_for(fixture, extra_sources=[source], now=now or NOW)

    async def test_a_fresher_second_opinion_replaces_the_shown_number(self):
        stub = self.Stub([Reading("240202", "car", "open", 62, age_minutes=3, source="stub")])
        body = await self.feed_with(stub).bridge("paso-del-norte", "car")
        self.assertEqual(body["minutes"], 62)        # shown
        self.assertEqual(body["cbp_minutes"], 48)    # kept for audit
        self.assertEqual(body["consensus"]["chosen_source"], "stub")

    async def test_a_conflict_lowers_confidence_and_is_flagged(self):
        stub = self.Stub([Reading("240202", "car", "open", 95, age_minutes=3, source="stub")])
        body = await self.feed_with(stub).bridge("paso-del-norte", "car")
        self.assertTrue(body["sources_disagree"])
        self.assertEqual(body["confidence"], "low")
        self.assertEqual(body["consensus"]["spread_minutes"], 47)

    async def test_a_disputed_lane_is_ranked_on_its_worst_case(self):
        stub = self.Stub([Reading("240202", "car", "open", 95, age_minutes=3, source="stub")])
        row = next(r for r in (await self.feed_with(stub, now=MIDDAY, fixture="cbp_feed_midday.json")
                   .ranked("car")) if r["port_number"] == "240202")
        self.assertEqual((row["optimistic_minutes"], row["planning_minutes"]), (48, 95))
        self.assertGreaterEqual(row["effective_minutes"], 95)

    async def test_an_undisputed_bridge_wins_the_headline_over_a_disputed_faster_one(self):
        # Santa Teresa is undisputed at 40; Paso del Norte is 48 by CBP but 12 by a
        # second site. The optimistic 12 must not win.
        # Midday fixture: nothing is demoted for closing, and every reading is fresh,
        # so the only thing separating the bridges is the disagreement.
        stub = self.Stub([Reading("240202", "car", "open", 12, age_minutes=1, source="stub")])
        best = await self.feed_with(stub, now=MIDDAY, fixture="cbp_feed_midday.json").best("car")
        self.assertEqual(best["port_number"], "240801")

    async def test_a_disputed_bridge_still_wins_when_even_its_worst_case_is_best(self):
        stub = self.Stub([Reading("240801", "car", "open", 12, age_minutes=1, source="stub")])
        best = await self.feed_with(stub, now=MIDDAY, fixture="cbp_feed_midday.json").best("car")
        self.assertEqual(best["port_number"], "240801")    # worst case 40 still beats 48

    async def test_a_disputed_headline_shows_the_range_not_the_optimistic_end(self):
        stub = self.Stub([Reading("240801", "car", "open", 12, age_minutes=1, source="stub")])
        feed = self.feed_with(stub, now=MIDDAY, fixture="cbp_feed_midday.json")
        headline = text.caption(feed, (await feed.snapshot()), "es").splitlines()[1]
        self.assertIn("12–40 min", headline)

    async def test_a_broken_source_never_stops_the_feed(self):
        feed = self.feed_with(self.Stub([], boom=True))
        self.assertEqual((await feed.bridge("paso-del-norte", "car"))["minutes"], 48)
        self.assertIn("failed: TimeoutError", (await feed.health())["sources"]["stub"])

    async def test_a_source_cannot_add_a_bridge_cbp_does_not_know(self):
        stub = self.Stub([Reading("999999", "car", "open", 5, age_minutes=1, source="stub")])
        feed = self.feed_with(stub)
        self.assertEqual(len((await feed.waits())["ports"]), 6)
        self.assertIsNone((await feed.snapshot()).port("999999"))

    async def test_health_lists_every_source_and_the_dispute_count(self):
        stub = self.Stub([Reading("240202", "car", "open", 95, age_minutes=3, source="stub")])
        health = await self.feed_with(stub).health()
        self.assertEqual(set(health["sources"]), {"cbp", "stub"})
        self.assertEqual(health["lanes_disputed"], 1)

class Efficiency(unittest.IsolatedAsyncioTestCase):
    """Every answer costs an upstream call, so the cheap wins are measured, not assumed."""

    async def test_one_snapshot_serves_many_questions(self):
        calls = {"n": 0}
        payload = load("cbp_feed.json")

        async def fetcher(_http):
            calls["n"] += 1
            return payload

        feed = BorderFeed(storage=BorderStore(client=None), fetcher=fetcher,
                          clock=lambda: NOW, extra_sources=[])
        await feed.waits()
        await feed.bridge("paso-del-norte", "car")
        await feed.drops(30)
        await feed.best("walk")
        self.assertEqual(calls["n"], 1)

    async def test_a_lane_is_decorated_once_per_snapshot(self):
        feed = feed_for("cbp_feed.json", cache_seconds=300)
        await feed.snapshot()
        counted = {"n": 0}
        original = BorderFeed._decorate_uncached

        def counting(self, snap, port, lane):
            counted["n"] += 1
            return original(self, snap, port, lane)

        with mock.patch.object(BorderFeed, "_decorate_uncached", counting):
            await feed.waits()
            lanes = sum(len(p.lanes) for p in (await feed.snapshot()).ports)
            self.assertLessEqual(counted["n"], lanes)

    async def test_the_previous_reading_is_read_from_storage_once_per_lane(self):
        store = Always60Store()
        feed = feed_for("cbp_feed.json", cache_seconds=300, storage=store)
        await feed.snapshot()
        await feed.waits()
        await feed.waits()
        lanes = sum(len(p.lanes) for p in (await feed.snapshot()).ports)
        self.assertLessEqual(store.calls["recent_readings"], lanes)

    async def test_sources_are_read_at_the_same_time_not_one_after_another(self):
        import time as clock_module

        class Slow(Source):
            def __init__(self, name):
                self.name = name

            async def fetch(self, http, now):
                await asyncio.sleep(0.25)
                return []

        feed = feed_for("cbp_feed.json", extra_sources=[Slow("a"), Slow("b"), Slow("c")])
        start = clock_module.perf_counter()
        await feed.snapshot()
        elapsed = clock_module.perf_counter() - start
        self.assertLess(elapsed, 0.7, "three 0.25s sources should overlap, not queue")

    async def test_simultaneous_callers_share_one_upstream_read(self):
        calls = {"n": 0}
        payload = load("cbp_feed.json")

        async def slow_fetcher(_http):
            calls["n"] += 1
            await asyncio.sleep(0.2)
            return payload

        feed = BorderFeed(storage=BorderStore(client=None), fetcher=slow_fetcher,
                          clock=lambda: NOW, extra_sources=[], cache_seconds=300)
        await asyncio.gather(*(feed.snapshot() for _ in range(5)))
        self.assertEqual(calls["n"], 1)

    async def test_health_reports_how_stale_the_cache_is(self):
        feed = feed_for("cbp_feed.json", cache_seconds=300)
        await feed.snapshot()
        self.assertIsNotNone((await feed.health())["cache_age_seconds"])

class SourceWindows(unittest.IsolatedAsyncioTestCase):
    """Each source is re-read on its own schedule, and backs off while it repeats."""

    class Counting(Source):
        name = "pasosfronterizos"      # borrows that source's window

        def __init__(self, minutes=40):
            self.calls = 0
            self.minutes = minutes

        async def fetch(self, http, now):
            self.calls += 1
            return [Reading("240202", "car", "open", self.minutes, age_minutes=5, source=self.name)]

    def feed_with(self, source, cache_seconds=300):
        return feed_for("cbp_feed.json", extra_sources=[source], cache_seconds=cache_seconds)

    async def test_a_source_is_not_re_read_inside_its_window(self):
        source = self.Counting()
        feed = self.feed_with(source)
        await feed.snapshot()
        (await feed.snapshot(force=True))          # rebuild, but the source window still holds
        self.assertEqual(source.calls, 1)

    async def test_the_window_widens_while_the_numbers_repeat(self):
        source = self.Counting()
        feed = self.feed_with(source)
        await feed.snapshot()
        base = feed.source_windows()["pasosfronterizos"]["ttl_seconds"]
        feed._source_cache["pasosfronterizos"]["at"] -= 10_000     # pretend it expired
        await feed.snapshot(force=True)
        widened = feed.source_windows()["pasosfronterizos"]
        self.assertEqual(source.calls, 2)
        self.assertGreater(widened["ttl_seconds"], base)
        self.assertEqual(widened["unchanged_reads"], 1)

    async def test_the_window_snaps_back_when_the_numbers_move(self):
        source = self.Counting()
        feed = self.feed_with(source)
        await feed.snapshot()
        feed._source_cache["pasosfronterizos"]["at"] -= 10_000
        (await feed.snapshot(force=True))                                  # repeat: widened
        source.minutes = 75                                        # the line moved
        feed._source_cache["pasosfronterizos"]["at"] -= 10_000
        await feed.snapshot(force=True)
        window = feed.source_windows()["pasosfronterizos"]
        self.assertEqual(window["unchanged_reads"], 0)
        self.assertEqual(window["ttl_seconds"], 240)

    async def test_windows_are_ignored_when_caching_is_off(self):
        source = self.Counting()
        feed = self.feed_with(source, cache_seconds=0)
        await feed.snapshot()
        await feed.snapshot()
        self.assertEqual(source.calls, 2)

    async def test_health_shows_each_window(self):
        feed = self.feed_with(self.Counting())
        await feed.snapshot()
        self.assertIn("pasosfronterizos", (await feed.health())["source_windows"])

class RushHours(unittest.IsolatedAsyncioTestCase):
    """Windows tighten while people are actually crossing."""

    def feed_at(self, hour: int, minute: int = 0):
        clock = datetime(2026, 9, 22, (hour + 6) % 24, minute, tzinfo=UTC)  # MDT
        return BorderFeed(storage=BorderStore(client=None), fetcher=returns(lambda: load("cbp_feed.json")),
                          clock=lambda: clock, extra_sources=[], cache_seconds=300)

    async def test_the_morning_and_evening_peaks_count_as_rush(self):
        for hour in (5, 7, 8, 15, 17, 18):
            with self.subTest(hour=hour):
                self.assertTrue(self.feed_at(hour).is_rush_hour())

    async def test_the_quiet_hours_do_not(self):
        for hour in (2, 4, 9, 12, 19, 22):
            with self.subTest(hour=hour):
                self.assertFalse(self.feed_at(hour).is_rush_hour())

    async def test_ceilings_halve_during_rush(self):
        self.assertEqual(self.feed_at(12)._window("cbp"), (300, 1200))
        self.assertEqual(self.feed_at(7)._window("cbp"), (300, 600))
        self.assertEqual(self.feed_at(7)._window("pasosfronterizos"), (240, 360))

    async def test_a_widened_window_tightens_as_rush_begins(self):
        feed = self.feed_at(14, 50)                       # quiet: ceiling 20 min
        await feed.snapshot()
        feed._source_cache["cbp"]["repeats"] = 3          # pretend it had widened fully
        self.assertEqual(feed.source_windows()["cbp"]["ttl_seconds"], 1200)

        rush = self.feed_at(15, 10)                       # 3:10 pm: rush has started
        rush._source_cache = feed._source_cache
        self.assertEqual(rush.source_windows()["cbp"]["ttl_seconds"], 600)

    async def test_health_says_whether_it_is_rush_hour(self):
        self.assertTrue((await self.feed_at(7).health())["rush_hour"])
        self.assertFalse((await self.feed_at(12).health())["rush_hour"])

class StaleWhileRevalidate(unittest.IsolatedAsyncioTestCase):
    """A failed refresh must not turn a good 2-minute-old reading into silence."""

    def feed_that_breaks_after_one_read(self):
        payload = load("cbp_feed.json")
        state = {"n": 0}

        async def fetcher(_http):
            state["n"] += 1
            if state["n"] > 1:
                raise OSError("connection reset")
            return payload

        return BorderFeed(storage=BorderStore(client=None), fetcher=fetcher,
                          clock=lambda: NOW, cache_seconds=0, extra_sources=[])

    async def test_the_last_good_reading_is_served_and_flagged(self):
        feed = self.feed_that_breaks_after_one_read()
        await feed.snapshot()
        snap = (await feed.snapshot())                       # the fetch fails here
        self.assertEqual(len(snap.ports), 6)
        health = await feed.health()
        self.assertTrue(health["serving_stale"])
        self.assertFalse(health["ok"])
        self.assertIn("connection reset", health["last_error"])

    async def test_a_failure_with_nothing_cached_still_fails_loudly(self):
        boom = raises(OSError("no route to host"))
        feed = BorderFeed(storage=BorderStore(client=None), fetcher=boom,
                          clock=lambda: NOW, extra_sources=[])
        with self.assertRaises(FeedError):
            await feed.snapshot()

    async def test_stale_serving_stops_after_the_grace_period(self):
        feed = self.feed_that_breaks_after_one_read()
        await feed.snapshot()
        feed._fetched_monotonic -= service_mod.STALE_GRACE_SECONDS + 1
        with self.assertRaises(FeedError):
            await feed.snapshot()

class ResponseTrimming(unittest.IsolatedAsyncioTestCase):
    async def test_waits_can_be_limited_to_one_lane(self):
        feed = feed_for("cbp_feed.json")
        full = await feed.waits()
        trimmed = await feed.waits(lanes=("car",))
        self.assertEqual(set(trimmed["ranked"]), {"car"})
        self.assertLess(len(json.dumps(trimmed)), len(json.dumps(full)))

    async def test_the_lane_filter_travels_over_http(self):
        status, body = await api.route(feed_for("cbp_feed.json"), "/waits", {"lane": ["car_sentri"]})
        self.assertEqual(status, 200)
        self.assertEqual(set(body["ranked"]), {"car_sentri"})

class UnchangedWrites(unittest.IsolatedAsyncioTestCase):
    async def test_an_unchanged_reading_is_not_written_again(self):
        store = MemoryStore(enabled=False)
        feed = feed_for("cbp_feed.json", storage=store)
        await feed.snapshot()
        (await feed.snapshot(force=True))       # identical numbers
        self.assertEqual(store.calls["save_readings"], 1)
        self.assertEqual(store.runs, ["ok", "unchanged"])

class PortRegistrySync(unittest.IsolatedAsyncioTestCase):
    async def test_ports_are_synced_once_per_process_when_storage_is_on(self):
        store = MemoryStore()
        feed = feed_for("cbp_feed.json", storage=store)
        await feed.snapshot()
        await feed.snapshot(force=True)
        self.assertEqual(store.calls["upsert_ports"], 1)

    async def test_nothing_is_synced_when_storage_is_off(self):
        feed = feed_for("cbp_feed.json")
        await feed.snapshot()
        self.assertFalse(feed._ports_synced)

class ForTheCrosser(unittest.IsolatedAsyncioTestCase):
    """Features that change what someone actually does."""

    def moving_feed(self, *minutes: int, now=None):
        """Successive readings for Paso del Norte's car lane, 30 minutes apart."""
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
        fetched = {"n": 0}
        start = now or NOW

        async def fetcher(_http):
            payload = payloads[min(fetched["n"], len(payloads) - 1)]
            fetched["n"] += 1
            return payload

        def clock():
            when = start + timedelta(minutes=30 * state["n"])
            state["n"] += 1
            return when

        return BorderFeed(storage=BorderStore(client=None), fetcher=fetcher, clock=clock,
                          cache_seconds=0, extra_sources=[])

    async def test_a_growing_line_is_reported_in_plain_words(self):
        feed = self.moving_feed(30, 45, 60)
        (await feed.snapshot()); (await feed.snapshot()); (await feed.snapshot())
        trend = feed.trend("240202", "car")
        self.assertEqual((trend["change"], trend["direction"]), (30, "up"))
        self.assertTrue(trend["worth_saying"])
        self.assertIn("subió 30 min", text.trend_phrase(trend, "es"))

    async def test_a_small_wobble_is_not_worth_saying(self):
        feed = self.moving_feed(30, 33, 34)
        for _ in range(3):
            await feed.snapshot()
        self.assertFalse(feed.trend("240202", "car")["worth_saying"])
        self.assertIsNone(text.trend_phrase(feed.trend("240202", "car"), "es"))

    async def test_one_reading_is_not_a_trend(self):
        feed = self.moving_feed(30)
        await feed.snapshot()
        self.assertIsNone(feed.trend("240202", "car"))

    async def test_the_biggest_saving_at_the_same_bridge_is_offered(self):
        # PDN: 48 by car, 8 SENTRI, 0 Ready Lane, 10 on foot. Ready Lane saves most.
        body = await feed_for("cbp_feed.json").bridge("paso-del-norte", "car")
        self.assertEqual(body["faster_lane_here"]["lane"], "car_ready")
        self.assertIn("Si tienes Ready Lane", body["reply"])   # a card, not an instruction

    async def test_walking_is_offered_when_the_car_lanes_are_the_slow_ones(self):
        payload = load("cbp_feed.json")
        for port in payload:
            if port["port_number"] == "240202":
                lanes = port["passenger_vehicle_lanes"]
                lanes["ready_lanes"]["operational_status"] = "Lanes Closed"
                lanes["NEXUS_SENTRI_lanes"]["operational_status"] = "Lanes Closed"
        feed = BorderFeed(storage=BorderStore(client=None), fetcher=returns(lambda: payload),
                          clock=lambda: NOW, cache_seconds=0, extra_sources=[])
        body = await feed.bridge("paso-del-norte", "car")
        self.assertEqual(body["faster_lane_here"]["lane"], "walk")
        self.assertIn("A pie son 10 min", body["reply"])
        self.assertNotIn("Si tienes", body["reply"])           # walking needs no card

    async def test_no_lane_tip_when_the_saving_is_small(self):
        feed = feed_for("cbp_feed.json")
        self.assertIsNone((await feed.bridge("santa-teresa", "walk"))["faster_lane_here"])

    async def test_long_waits_read_as_hours(self):
        self.assertEqual(text.duration(45), "45 min")
        self.assertEqual(text.duration(60), "1 h")
        self.assertEqual(text.duration(75), "1 h 15")
        self.assertEqual(text.duration(130), "2 h 10")

    async def test_a_long_wait_is_phrased_as_hours_in_a_reply(self):
        feed = self.moving_feed(95)
        await feed.snapshot()
        self.assertIn("1 h 35", (await feed.bridge("paso-del-norte", "car"))["reply"])

    async def test_quiet_hours_are_flagged_so_alerts_can_wait(self):
        night = feed_for("cbp_feed.json", now=datetime(2026, 9, 22, 9, 0, tzinfo=UTC))
        self.assertTrue(night.is_quiet_hour())        # 3 am local
        day = feed_for("cbp_feed.json", now=MIDDAY)
        self.assertFalse(day.is_quiet_hour())
        self.assertIn("quiet_hour", (await day.drops(30)))

class ReviewRegressions(unittest.IsolatedAsyncioTestCase):
    """Bugs found in review: each of these failed before the fix."""

    async def test_a_drop_is_not_lost_when_another_request_refreshed_first(self):
        feed = feed_for("cbp_feed.json", "cbp_feed_after_drop.json")
        (await feed.snapshot())                  # 48
        (await feed.waits())                     # 20: the post consumed the new reading
        drops = (await feed.drops(30))["drops"]  # the sweep runs after, on the same 20
        self.assertEqual([d["previous_minutes"] for d in drops], [48])

    async def test_the_delta_holds_until_cbp_publishes_something_new(self):
        feed = feed_for("cbp_feed.json", "cbp_feed_after_drop.json")
        await feed.snapshot()
        await feed.snapshot()
        (await feed.snapshot())                  # same 20 again: not a new reading
        self.assertEqual((await feed.bridge("paso-del-norte", "car"))["delta"]["change"], -28)

    async def test_an_unknown_lane_is_a_400_not_a_dropped_connection(self):
        feed = feed_for("cbp_feed.json")
        for path in ("/waits/pdn", "/mine", "/best", "/waits"):
            with self.subTest(path=path):
                status, body = await api.route(feed, path, {"lane": ["bike"], "ports": ["pdn"]})
                self.assertEqual(status, 400)
                self.assertIn("car", body["lanes"])

    async def test_an_unexpected_error_is_a_500(self):
        feed = feed_for("cbp_feed.json")
        with mock.patch.object(BorderFeed, "health", side_effect=ZeroDivisionError("x")), \
             self.assertLogs("scraper.border.api", "ERROR"):
            status, _ = await api.route(feed, "/health", {})
        self.assertEqual(status, 500)

    async def test_the_cbp_window_widens_while_the_payload_repeats(self):
        calls = {"n": 0}
        payload = load("cbp_feed.json")

        async def fetcher(_http):
            calls["n"] += 1
            return payload

        feed = BorderFeed(storage=BorderStore(client=None), fetcher=fetcher,
                          clock=lambda: MIDDAY, extra_sources=[])
        await feed.snapshot()
        feed._source_cache["cbp"]["at"] -= 10_000
        await feed.snapshot(force=True)
        window = feed.source_windows()["cbp"]
        self.assertEqual(calls["n"], 2)
        self.assertEqual((window["unchanged_reads"], window["ttl_seconds"]), (1, 600))

    async def test_a_cached_scrape_ages_while_it_is_held(self):
        source = StubSource([Reading("240202", "car", "open", 40, age_minutes=5, source="pasosfronterizos")],
                            "pasosfronterizos")
        feed = feed_for("cbp_feed.json", extra_sources=[source], cache_seconds=300)
        await feed.snapshot()
        feed._source_cache["pasosfronterizos"]["at"] -= 180      # held 3 minutes
        await feed.snapshot(force=True)
        opinion = next(o for o in feed._decisions[("240202", "car")].opinions
                       if o["source"] == "pasosfronterizos")
        self.assertEqual(opinion["age_minutes"], 8)

    async def test_storage_lookups_for_a_fresh_process_happen_in_one_pass(self):
        store = Always60Store()
        feed = feed_for("cbp_feed.json", cache_seconds=300, storage=store)
        await feed.snapshot()
        after_refresh = store.calls["recent_readings"]
        await feed.waits()
        await feed.bridge("paso-del-norte", "car")
        self.assertEqual(store.calls["recent_readings"], after_refresh,
                         "answers must not go back to the database")
        self.assertEqual((await feed.bridge("paso-del-norte", "car"))["delta"]["previous_minutes"], 60)

    async def test_poll_does_not_count_a_stale_fallback_as_a_reading(self):
        state = {"fail": False}
        payload = load("cbp_feed.json")

        async def fetcher(_http):
            if state["fail"]:
                raise OSError("down")
            return payload

        feed = BorderFeed(storage=BorderStore(client=None), fetcher=fetcher,
                          clock=lambda: NOW, extra_sources=[], cache_seconds=0)
        await poll.once(feed)
        state["fail"] = True
        with self.assertRaises(FeedError):
            await poll.once(feed)

class NormalForThisHour(unittest.IsolatedAsyncioTestCase):
    # NOW is Monday 9:40 pm MDT: Postgres dow 1, hour 21.
    def feed_with_typical(self, median, days=5):
        store = MemoryStore()
        store.typical = [{"port_number": "240202", "lane": "car", "dow": 1, "hour": 21,
                          "median_minutes": median, "p75_minutes": median + 10, "days": days}]
        return feed_for("cbp_feed.json", storage=store), store

    async def test_a_worse_than_usual_wait_says_so(self):
        feed, _ = self.feed_with_typical(25)
        body = (await feed.bridge("paso-del-norte", "car"))          # 48 min
        self.assertEqual((body["typical"]["difference"], body["typical"]["compared"]), (23, "worse"))
        self.assertIn("Un lunes a esta hora suele estar en 25 min: hoy 23 min más.", body["reply"])

    async def test_an_ordinary_day_is_not_mentioned(self):
        feed, _ = self.feed_with_typical(45)
        body = await feed.bridge("paso-del-norte", "car")
        self.assertEqual(body["typical"]["compared"], "usual")
        self.assertNotIn("suele", body["reply"])

    async def test_no_history_means_no_claim(self):
        self.assertIsNone((await feed_for("cbp_feed.json").bridge("paso-del-norte", "car"))["typical"])

    async def test_the_baseline_is_not_re_read_on_every_refresh(self):
        feed, store = self.feed_with_typical(25)
        await feed.snapshot()
        await feed.snapshot(force=True)
        self.assertEqual(store.calls["typical_waits"], 1)

    async def test_a_failed_read_is_retried_and_keeps_what_it_had(self):
        feed, store = self.feed_with_typical(25)
        await feed.snapshot()
        store.typical_fails = True
        feed._typical_due = 0
        await feed.snapshot(force=True)
        self.assertEqual(store.calls["typical_waits"], 2)
        self.assertIsNotNone((await feed.bridge("paso-del-norte", "car"))["typical"])

class TrendSurvivesARestart(unittest.IsolatedAsyncioTestCase):
    async def test_movement_is_seeded_from_the_database(self):
        store = MemoryStore()
        store.readings.append({"port_number": "240202", "lane": "car", "state": "open",
                               "delay_minutes": 18, "content_hash": "older",
                               "captured_at": (NOW - timedelta(minutes=60)).isoformat()})
        feed = feed_for("cbp_feed.json", storage=store)          # fresh process, 48 now
        trend = (await feed.bridge("paso-del-norte", "car"))["trend"]
        self.assertEqual((trend["change"], trend["over_minutes"], trend["worth_saying"]), (30, 60, True))

    async def test_readings_outside_the_window_are_ignored(self):
        store = MemoryStore()
        store.readings.append({"port_number": "240202", "lane": "car", "state": "open",
                               "delay_minutes": 18, "content_hash": "older",
                               "captured_at": (NOW - timedelta(hours=5)).isoformat()})
        self.assertIsNone((await feed_for("cbp_feed.json", storage=store).bridge("paso-del-norte", "car"))["trend"])
