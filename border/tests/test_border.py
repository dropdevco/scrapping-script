"""Tests for the cleaning rules, the pull surface, and the Instagram simulation.

Run: python3 -m unittest discover -s tests -v
"""
from __future__ import annotations

import contextlib
import io
import json
import logging
import sys
import unittest
import urllib.error
from collections import Counter
from unittest import mock
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tools"))

from border import api, consensus  # noqa: E402
from border.sources.base import Reading, Source  # noqa: E402
from border.sources.pasosfronterizos import PasosFronterizosSource, parse as parse_pasos  # noqa: E402
from border.sources.borderswaittime import EXPECTED_PORTS, parse as parse_mirror  # noqa: E402
from border import poll, selfcheck  # noqa: E402
from border.core import cbp, text  # noqa: E402
from border.core.storage import Storage  # noqa: E402
from border import service as service_mod  # noqa: E402
from border.crossings import CrossingError  # noqa: E402
from border.service import BorderFeed, FeedError  # noqa: E402

import fake_instagram  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures"
NOW = datetime(2026, 9, 22, 3, 40, tzinfo=timezone.utc)   # 9:40 pm MDT — Santa Teresa shuts at 10
MIDDAY = datetime(2026, 9, 22, 19, 40, tzinfo=timezone.utc)  # 1:40 pm MDT — every bridge open for hours


def load(name: str) -> list[dict]:
    return json.loads((FIXTURES / name).read_text())


def feed_for(*files: str, now: datetime = NOW, **kwargs) -> BorderFeed:
    """A feed whose successive fetches return the given fixtures in order."""
    payloads = [load(f) for f in files]
    calls = {"n": 0}

    def fetcher():
        i = min(calls["n"], len(payloads) - 1)
        calls["n"] += 1
        return payloads[i]

    # extra_sources=[] by default: no test may depend on a website being up.
    options = {"clock": lambda: now, "cache_seconds": 0, "extra_sources": [],
               "storage": Storage(url="", key=""), **kwargs}
    return BorderFeed(fetcher=fetcher, **options)


def lane(raw_status, minutes="", stamp="", lanes_open=""):
    return {"operational_status": raw_status, "delay_minutes": minutes,
            "update_time": stamp, "lanes_open": lanes_open}


class MemoryStore(Storage):
    """Supabase in a dict: enough of each table to exercise what survives a restart."""

    def __init__(self, readings=(), enabled=True):
        # enabled=False is the unconfigured store whose read fallbacks still answer.
        super().__init__(url="memory" if enabled else "", key="memory" if enabled else "")
        self.readings, self.alerts, self.crossings, self.typical = list(readings), {}, {}, []
        self.runs: list[str] = []
        self.calls: Counter = Counter()
        self.typical_fails = False

    def upsert_ports(self):
        self.calls["upsert_ports"] += 1

    def save_readings(self, snapshot):
        self.calls["save_readings"] += 1
        return 0

    def log_run(self, tool, snapshot, written, status, error=None, params=None):
        self.runs.append(status)

    def recent_readings(self, port_number, lane, limit=3):
        self.calls["recent_readings"] += 1
        rows = [r for r in self.readings if r["port_number"] == port_number and r["lane"] == lane]
        return list(reversed(rows))[:limit]

    def readings_since(self, since_iso):
        return [r for r in self.readings if r.get("captured_at", "") >= since_iso]

    def typical_waits(self, weeks=8, min_days=3):
        self.calls["typical_waits"] += 1
        return None if self.typical_fails else self.typical

    def prune(self, **kwargs):
        self.calls["prune"] += 1
        return {"readings": 0, "runs": 0, "alerts": 0}

    def save_alert(self, row):
        self.alerts.setdefault(row["id"], dict(row))

    def rearm_alert(self, alert_id, at_iso):
        self.alerts[alert_id]["rearmed_at"] = at_iso

    def alerts_since(self, since_iso):
        return [dict(a) for a in self.alerts.values()
                if a["detected_at"] > since_iso or a["rearmed_at"] is None]

    def save_crossing(self, row):
        self.crossings[row["id"]] = dict(row)

    def finish_crossing(self, crossing_id, finished_iso, actual_minutes):
        self.crossings[crossing_id].update(finished_at=finished_iso, actual_minutes=actual_minutes)

    def get_crossing(self, crossing_id):
        row = self.crossings.get(crossing_id)
        return dict(row) if row else None

    def crossings_since(self, since_iso):
        return [dict(c) for c in self.crossings.values()
                if c.get("finished_at") and c["finished_at"] >= since_iso]




class Always60Store(MemoryStore):
    """Every lane's previous reading was 60 minutes, whatever is asked."""

    def recent_readings(self, port_number, lane, limit=3):
        self.calls["recent_readings"] += 1
        return [{"port_number": port_number, "lane": lane, "state": "open",
                 "delay_minutes": 60, "content_hash": "older"}]


class StubSource(Source):
    """A source that answers with fixed readings and counts how often it was asked."""

    def __init__(self, readings=(), name="stub", independent=True, expected_ports=frozenset()):
        self.readings, self.name, self.independent = list(readings), name, independent
        self.expected_ports = frozenset(expected_ports)
        self.calls = 0

    def fetch(self, now):
        self.calls += 1
        return self.readings


def setUpModule():
    # Failure paths are exercised on purpose; their warnings are the code working.
    logging.disable(logging.WARNING)


def tearDownModule():
    logging.disable(logging.NOTSET)


class Cleaning(unittest.TestCase):
    def test_no_delay_becomes_zero_not_missing(self):
        parsed = cbp._lane("240202", "car", lane("no delay", "", "At 9:00 pm MDT"), NOW)
        self.assertEqual((parsed.state, parsed.delay_minutes), ("open", 0))

    def test_minutes_parsed_from_string(self):
        self.assertEqual(cbp._lane("240202", "car", lane("delay", "48"), NOW).delay_minutes, 48)

    def test_closed_carries_no_minutes(self):
        parsed = cbp._lane("240203", "car", lane("Lanes Closed", "0"), NOW)
        self.assertEqual((parsed.state, parsed.delay_minutes), ("closed", None))

    def test_update_pending_is_no_data(self):
        self.assertEqual(cbp._lane("240221", "car", lane("Update Pending"), NOW).state, "no_data")

    def test_absent_lane_type_is_dropped(self):
        self.assertIsNone(cbp._lane("240204", "walk", lane("N/A"), NOW))

    def test_future_stamp_is_flagged_and_age_never_negative(self):
        parsed = cbp._lane("240201", "car", lane("delay", "55", "At 10:00 pm MDT"), NOW)
        self.assertTrue(parsed.stamp_ahead_of_clock)
        self.assertEqual(parsed.age_minutes, 0)

    def test_stamp_hours_ahead_belongs_to_yesterday(self):
        morning = datetime(2026, 9, 22, 13, 30, tzinfo=timezone.utc)  # 7:30 am MDT
        self.assertEqual(cbp.parse_stamp("At 11:00 pm MDT", morning).isoformat(),
                         "2026-09-21T23:00:00-06:00")

    def test_noon_and_midnight_are_words_in_the_feed(self):
        self.assertEqual(cbp.parse_stamp("At Noon MDT", NOW).strftime("%H:%M"), "12:00")
        self.assertEqual(cbp.parse_stamp("At Midnight MDT", NOW).strftime("%H:%M"), "00:00")

    def test_noon_stamp_is_not_treated_as_stale(self):
        one_pm = datetime(2026, 9, 22, 19, 6, tzinfo=timezone.utc)  # 1:06 pm MDT
        parsed = cbp._lane("240202", "car", lane("delay", "38", "At Noon MDT"), one_pm)
        self.assertEqual(parsed.age_minutes, 66)
        self.assertFalse(parsed.stale)

    def test_unparsable_stamp_is_none_not_a_guess(self):
        self.assertIsNone(cbp.parse_stamp("At some point MDT", NOW))

    def test_content_hash_is_stable_and_lane_specific(self):
        a = cbp._lane("240202", "car", lane("delay", "48", "At 9:00 pm MDT"), NOW)
        b = cbp._lane("240202", "car", lane("delay", "48", "At 9:00 pm MDT"), NOW)
        c = cbp._lane("240202", "car_ready", lane("delay", "48", "At 9:00 pm MDT"), NOW)
        self.assertEqual(a.content_hash, b.content_hash)
        self.assertNotEqual(a.content_hash, c.content_hash)


class SnapshotShape(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.snap = cbp.build(load("cbp_feed.json"), NOW)

    def test_six_ports_in_display_order(self):
        self.assertEqual([p.port_number for p in self.snap.ports],
                         ["240202", "240203", "240201", "240204", "240801", "240221"])

    def test_tornillo_named_despite_blank_feed_name(self):
        self.assertEqual(self.snap.port("240221").name, "Tornillo–Guadalupe")

    def test_tornillo_has_no_live_data(self):
        self.assertFalse(self.snap.port("240221").has_live_data)

    def test_lookup_by_slug_and_loose_name(self):
        self.assertEqual(self.snap.port("paso-del-norte").port_number, "240202")
        self.assertEqual(self.snap.port("zaragoza").port_number, "240203")

    def test_empty_feed_reports_missing_ports_instead_of_crashing(self):
        empty = cbp.build([], NOW)
        self.assertEqual((empty.ports, len(empty.missing_ports)), ([], 6))


class Captions(unittest.TestCase):
    def setUp(self):
        self.feed = feed_for("cbp_feed.json", now=MIDDAY)
        self.snap = self.feed.snapshot()

    def caption(self, lang="es"):
        return text.caption(self.feed, self.snap, lang)

    def test_caption_leads_with_the_fastest_bridge(self):
        second = self.caption().splitlines()[1]
        self.assertTrue(second.startswith("Más rápido ahora:"), second)
        self.assertIn("Jerónimo–Santa Teresa 40 min", second)

    def test_headline_offers_the_crossing_time(self):
        self.assertIn("cruzas", self.caption().splitlines()[1])

    def test_cars_are_listed_fastest_first(self):
        lines = self.caption().splitlines()
        cars = lines[lines.index("Autos") + 1:]
        minutes = [int(l.split()[-2]) for l in cars if l.endswith("min")]
        self.assertEqual(minutes, sorted(minutes))

    def test_a_bridge_closing_too_soon_is_labelled_in_the_list(self):
        night = feed_for("cbp_feed.json")
        line = next(l for l in text.caption(night, night.snapshot(), "es").splitlines()
                    if l.startswith("Santa Teresa"))
        self.assertIn("cierra", line)

    def test_other_lanes_are_one_compact_line_each(self):
        sentri = [l for l in self.caption().splitlines() if l.startswith("SENTRI:")]
        self.assertEqual(len(sentri), 1)
        self.assertIn("·", sentri[0])

    def test_caption_never_quotes_a_future_stamp(self):
        # At 9:40 pm the fixture carries a 10:00 pm stamp; the caption must use 9:00.
        night = feed_for("cbp_feed.json")
        stamp_line = next(l for l in text.caption(night, night.snapshot(), "es").splitlines()
                          if l.startswith("Datos de CBP"))
        self.assertIn("9:00 p. m.", stamp_line)

    def test_caption_names_the_bridge_with_no_data(self):
        self.assertIn("Tornillo", self.caption())

    def test_closed_lane_says_closed_not_zero(self):
        reply = self.feed.bridge("240203", "car", "es")["reply"]
        self.assertIn("cerrado", reply)
        self.assertNotIn("0 min", reply)

    def test_no_data_lane_says_so(self):
        self.assertIn("no está reportando", self.feed.bridge("tornillo", "car", "es")["reply"])

    def test_unknown_bridge_gets_a_helpful_reply(self):
        self.assertIn("puentes", self.feed.bridge("narnia", "car", "es")["reply"])

    def test_english_reply_carries_update_time(self):
        reply = self.feed.bridge("paso-del-norte", "car", "en")["reply"]
        self.assertRegex(reply, r"Paso del Norte · Cars: \d+ min")
        self.assertRegex(reply, r"CBP updated at \d+:\d\d [ap]m")

    def test_a_reply_cites_the_source_the_number_came_from(self):
        stub = Reading("240202", "car", "open", 62, age_minutes=3, source="otrositio")
        feed = feed_for("cbp_feed.json", extra_sources=[
            type("S", (Source,), {"name": "otrositio", "fetch": lambda self, now: [stub]})()])
        reply = feed.bridge("paso-del-norte", "car", "es")["reply"]
        self.assertIn("otrositio, hace 3 min", reply)
        self.assertNotIn("CBP actualizó", reply)


class PullSurface(unittest.TestCase):
    def test_waits_carries_both_captions(self):
        body = feed_for("cbp_feed.json").waits()
        self.assertTrue(body["caption_es"] and body["caption_en"])
        self.assertEqual(len(body["ports"]), 6)

    def test_bridge_lookup_returns_reply_and_minutes(self):
        body = feed_for("cbp_feed.json").bridge("paso-del-norte", "car")
        self.assertTrue(body["found"])
        self.assertEqual(body["minutes"], 48)

    def test_a_bridge_name_with_spaces_survives_the_url(self):
        status, body = api.route(feed_for("cbp_feed.json"), "/waits/el%20puente%20libre", {})
        self.assertEqual(status, 200)
        self.assertEqual(body["port_number"], "240201")

    def test_unknown_bridge_is_404_with_options(self):
        status, body = api.route(feed_for("cbp_feed.json"), "/waits/narnia", {})
        self.assertEqual(status, 404)
        self.assertIn("options", body)

    def test_drops_requires_a_numeric_limit(self):
        status, body = api.route(feed_for("cbp_feed.json"), "/drops", {})
        self.assertEqual(status, 400)
        self.assertIn("below", body["error"])

    def test_alert_text_has_no_doubled_full_stop(self):
        feed = feed_for("cbp_feed.json", "cbp_feed_after_drop.json")
        feed.snapshot()
        message = feed.drops(30)["drops"][0]["message"]
        self.assertNotIn("..", message)
        self.assertIn("Cruzas alrededor de", message)

    def test_first_reading_is_not_a_drop(self):
        self.assertEqual(feed_for("cbp_feed.json").drops(30)["drops"], [])

    def test_crossing_under_the_limit_is_a_drop_once(self):
        feed = feed_for("cbp_feed.json", "cbp_feed_after_drop.json")
        feed.snapshot()                       # 48 min
        answer = feed.drops(30)               # 20 min -> crossed
        drops = answer["drops"]
        self.assertEqual([d["port_number"] for d in drops], ["240202"])
        self.assertEqual((drops[0]["previous_minutes"], drops[0]["minutes"]), (48, 20))
        since = datetime.fromisoformat(answer["cursor"])
        self.assertEqual(feed.drops(30, since=since)["drops"], [])   # already below: not a new drop
        # Asking again without a cursor is not consuming: same alert, same id.
        self.assertEqual([d["id"] for d in feed.drops(30)["drops"]], [drops[0]["id"]])

    def test_cbp_failure_is_503_not_a_crash(self):
        def boom():
            raise OSError("connection reset")
        feed = BorderFeed(storage=Storage(url="", key=""), fetcher=boom, clock=lambda: NOW,
                          extra_sources=[])
        status, body = api.route(feed, "/waits", {})
        self.assertEqual(status, 503)
        self.assertIn("connection reset", body["error"])
        with self.assertRaises(FeedError):
            feed.snapshot()

    def test_health_on_a_fresh_process_takes_a_reading(self):
        feed = feed_for("cbp_feed.json")
        health = feed.health()                      # no snapshot taken first
        self.assertTrue(health["ok"])
        self.assertIsNotNone(health["last_ok"])

    def test_health_stays_honest_when_cbp_is_down(self):
        def boom():
            raise OSError("no route to host")
        feed = BorderFeed(storage=Storage(url="", key=""), fetcher=boom, clock=lambda: NOW,
                          extra_sources=[])
        health = feed.health()
        self.assertFalse(health["ok"])
        self.assertIn("no route to host", health["last_error"])

    def test_health_reports_storage_disabled(self):
        feed = feed_for("cbp_feed.json")
        feed.snapshot()
        self.assertFalse(feed.health()["storage_enabled"])
        self.assertTrue(feed.health()["ok"])


class CommuterFacts(unittest.TestCase):
    def test_delta_shows_movement_between_readings(self):
        feed = feed_for("cbp_feed.json", "cbp_feed_after_drop.json")
        feed.snapshot()                                  # 48 min
        body = feed.bridge("paso-del-norte", "car")      # 20 min
        self.assertEqual((body["delta"]["change"], body["delta"]["direction"]), (-28, "down"))
        self.assertIn("↓28", body["reply"])

    def test_small_change_is_not_shouted_about(self):
        feed = feed_for("cbp_feed.json")
        feed._previous[("240202", "car")] = 50           # 48 now: a 2-minute wobble
        self.assertFalse(feed.bridge("paso-del-norte", "car")["delta"]["meaningful"])
        self.assertNotIn("↓", feed.bridge("paso-del-norte", "car")["reply"])

    def test_crosses_at_is_now_plus_the_wait(self):
        body = feed_for("cbp_feed.json").bridge("paso-del-norte", "car")
        crossing = datetime.fromisoformat(body["crosses_at"])
        self.assertEqual(crossing.strftime("%H:%M"), "22:28")   # 21:40 MDT + 48 min
        self.assertIn("cruzas", body["reply"])

    def test_best_is_the_fastest_open_bridge(self):
        best = feed_for("cbp_feed.json", now=MIDDAY).best("car")
        self.assertEqual((best["port_number"], best["minutes"]), ("240801", 40))

    def test_ranked_puts_every_unusable_bridge_after_the_open_ones(self):
        rows = feed_for("cbp_feed.json", now=MIDDAY).ranked("car")
        states = [r["state"] for r in rows]
        self.assertEqual(rows[0]["port_number"], "240801")          # fastest first
        self.assertEqual(states, sorted(states, key=lambda s: s != "open"))
        self.assertIn(states[-1], ("closed", "no_data"))

    def test_reply_suggests_a_materially_faster_bridge(self):
        body = feed_for("cbp_feed.json", now=MIDDAY).bridge("bota", "car")   # 55 min vs 40
        self.assertEqual(body["alternative"]["port_number"], "240801")
        self.assertIn("15 menos", body["reply"])

    def test_no_suggestion_when_the_saving_is_small(self):
        feed = feed_for("cbp_feed.json", now=MIDDAY)
        self.assertIsNone(feed.bridge("paso-del-norte", "car")["alternative"])  # 48 vs 40

    def test_my_digest_is_only_their_bridges(self):
        digest = feed_for("cbp_feed.json").my_digest(["paso-del-norte", "bota"], "car_sentri")
        self.assertEqual(len(digest["bridges"]), 2)
        self.assertLessEqual(len(digest["message"].splitlines()), 4)
        self.assertIn("Tus puentes", digest["message"])

    def test_my_digest_flags_a_faster_bridge_they_did_not_save(self):
        message = feed_for("cbp_feed.json", now=MIDDAY).my_digest(["bota"], "car")["message"]
        self.assertIn("Más rápido", message)

    def test_empty_saved_list_asks_them_to_pick(self):
        self.assertIn("puentes", feed_for("cbp_feed.json").my_digest([], "car")["message"])


class ClosingTimes(unittest.TestCase):
    """Santa Teresa runs 6 am-10 pm; Stanton 6 am-midnight; the rest are 24 hours."""

    def test_hours_are_parsed_from_the_feed_text(self):
        self.assertEqual(cbp.parse_hours("6 am-10 pm"), (360, 1320))
        self.assertEqual(cbp.parse_hours("6 am-Midnight"), (360, 1440))
        self.assertIsNone(cbp.parse_hours("24 hrs/day"))

    def test_bridge_that_shuts_before_arrival_is_flagged(self):
        body = feed_for("cbp_feed.json").bridge("santa-teresa", "car")   # 21:40 + 40 min > 22:00
        self.assertTrue(body["closes_before_you_cross"])
        self.assertIn("cierra", body["reply"])
        self.assertIn("no alcanzas", body["reply"])

    def test_the_same_bridge_is_fine_at_midday(self):
        body = feed_for("cbp_feed.json", now=MIDDAY).bridge("santa-teresa", "car")
        self.assertFalse(body["closes_before_you_cross"])
        self.assertNotIn("cierra", body["reply"])

    def test_best_skips_a_bridge_that_closes_too_soon(self):
        night = feed_for("cbp_feed.json").best("car")
        self.assertEqual(night["port_number"], "240202")   # not Santa Teresa, faster but shutting
        day = feed_for("cbp_feed.json", now=MIDDAY).best("car")
        self.assertEqual(day["port_number"], "240801")

    def test_a_tight_bridge_is_offered_an_alternative(self):
        body = feed_for("cbp_feed.json").bridge("santa-teresa", "car")
        self.assertEqual(body["alternative"]["port_number"], "240202")
        self.assertIn("Mejor", body["reply"])

    def test_round_the_clock_bridges_have_no_closing_time(self):
        self.assertIsNone(feed_for("cbp_feed.json").bridge("bota", "car")["closes_at"])


class Ranking(unittest.TestCase):
    """A recommendation is a bet about arrival, not a copy of CBP's newest number."""

    def score(self, minutes, age, change=None, stamp_ahead=False):
        feed = feed_for("cbp_feed.json")
        delta = None if change is None else {"change": change, "previous_minutes": minutes - change,
                                             "direction": "up" if change > 0 else "down",
                                             "meaningful": abs(change) >= 5}
        return feed._score(minutes, age, delta, stamp_ahead)

    def test_a_fresh_reading_is_not_penalised(self):
        self.assertEqual(self.score(30, 0)["effective_minutes"], 30)

    def test_an_old_reading_is_discounted(self):
        self.assertEqual(self.score(30, 120)["staleness_penalty"], 16)
        self.assertEqual(self.score(30, 120)["effective_minutes"], 46)

    def test_staleness_is_capped(self):
        self.assertEqual(self.score(30, 600)["staleness_penalty"], 25)

    def test_a_growing_line_is_expected_to_keep_growing(self):
        self.assertEqual(self.score(30, 0, change=20)["trend_penalty"], 12)

    def test_a_shrinking_line_is_trusted_less_than_a_growing_one(self):
        self.assertEqual(self.score(30, 0, change=-20)["trend_penalty"], -6)

    def test_a_stamp_ahead_of_the_clock_is_treated_as_unknown_age(self):
        honest = self.score(30, 0)["staleness_penalty"]
        suspect = self.score(30, 0, stamp_ahead=True)["staleness_penalty"]
        self.assertGreater(suspect, honest)

    def test_a_slower_but_much_fresher_bridge_can_win(self):
        feed = feed_for("cbp_feed.json")
        rows = {r["port_number"]: r for r in feed.ranked("car")}
        # Paso del Norte: 48 min, Bridge of the Americas: 55 min with a suspect stamp
        self.assertLess(rows["240202"]["effective_minutes"], rows["240201"]["effective_minutes"])

    def test_confidence_is_reported_per_lane(self):
        feed = feed_for("cbp_feed.json")
        self.assertEqual(feed.bridge("paso-del-norte", "car")["confidence"], "medium")
        self.assertIsNone(feed.bridge("tornillo", "car")["confidence"])

    def test_the_working_is_shown_not_hidden(self):
        score = feed_for("cbp_feed.json").bridge("paso-del-norte", "car")["score"]
        self.assertEqual(set(score), {"effective_minutes", "staleness_penalty",
                                      "trend_penalty", "age_used"})


class DropHysteresis(unittest.TestCase):
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

        def fetcher():
            payload = payloads[min(state["n"], len(payloads) - 1)]
            state["n"] += 1
            return payload

        return BorderFeed(storage=storage or Storage(url="", key=""), fetcher=fetcher,
                          clock=lambda: NOW, cache_seconds=0, extra_sources=[])

    def test_one_alert_per_crossing(self):
        feed = self.feed_with(40, 20, 22, 19)
        feed.snapshot()                                  # 40
        self.assertEqual(len(feed.drops(30)["drops"]), 1)   # 40 -> 20: fires
        self.assertEqual(feed.drops(30)["drops"], [])       # 22: still below, quiet
        self.assertEqual(feed.drops(30)["drops"], [])       # 19: still below, quiet

    def test_it_re_arms_after_the_wait_climbs_back(self):
        feed = self.feed_with(40, 20, 38, 21)
        feed.snapshot()
        self.assertEqual(len(feed.drops(30)["drops"]), 1)   # fires
        self.assertEqual(feed.drops(30)["drops"], [])       # 38: above the band, re-arms
        self.assertEqual(len(feed.drops(30)["drops"]), 1)   # 21: fires again

    def test_a_wobble_inside_the_band_does_not_re_arm(self):
        feed = self.feed_with(40, 20, 31, 25)
        feed.snapshot()
        self.assertEqual(len(feed.drops(30)["drops"]), 1)
        feed.drops(30)                                      # 31: inside 30-35, not re-armed
        self.assertEqual(feed.drops(30)["drops"], [])


class Aliases(unittest.TestCase):
    def setUp(self):
        self.snap = cbp.build(load("cbp_feed.json"), NOW)

    def test_people_type_what_they_call_the_bridge(self):
        for typed, expected in [("libre", "240201"), ("el puente libre", "240201"),
                                ("zaragoza", "240203"), ("santa fe", "240202"),
                                ("pdn", "240202"), ("lerdo", "240204")]:
            with self.subTest(typed=typed):
                self.assertEqual(self.snap.port(typed).port_number, expected)

    def test_typing_fast_on_a_phone_still_finds_the_bridge(self):
        for typed, expected in [("sarragoza", "240203"), ("zaragosa", "240203"),
                                ("tornilo", "240221"), ("stantn", "240204"),
                                ("pasodelnorte", "240202"), ("santa fee", "240202")]:
            with self.subTest(typed=typed):
                self.assertEqual(self.snap.port(typed).port_number, expected)

    def test_a_typo_too_far_from_any_bridge_is_refused(self):
        for typed in ("pizza", "narnia", "aeropuerto"):
            with self.subTest(typed=typed):
                self.assertIsNone(self.snap.port(typed))

    def test_a_word_that_fits_every_bridge_matches_none(self):
        self.assertIsNone(self.snap.port("puente"))

    def test_nonsense_matches_none(self):
        self.assertIsNone(self.snap.port("narnia"))


class PreviousFromDatabase(unittest.TestCase):
    """A restarted process has no memory; border_readings is the fallback."""

    def feed_with_history(self, minutes):
        rows = [{"port_number": "240202", "lane": "car", "state": "open",
                 "delay_minutes": minutes, "content_hash": "older"}]
        return feed_for("cbp_feed.json", storage=MemoryStore(rows, enabled=False))

    def test_delta_survives_a_restart(self):
        body = self.feed_with_history(60).bridge("paso-del-norte", "car")
        self.assertEqual(body["delta"]["previous_minutes"], 60)
        self.assertIn("↓12", body["reply"])

    def test_drop_alert_survives_a_restart(self):
        drops = self.feed_with_history(60).drops(50)["drops"]
        self.assertEqual([d["port_number"] for d in drops], ["240202"])

    def test_memory_wins_over_the_database(self):
        feed = self.feed_with_history(60)
        feed._previous[("240202", "car")] = 45
        self.assertEqual(feed.bridge("paso-del-norte", "car")["delta"]["previous_minutes"], 45)


class StorageOff(unittest.TestCase):
    def test_unconfigured_storage_is_a_silent_no_op(self):
        store = Storage(url="", key="")
        self.assertFalse(store.enabled)
        self.assertEqual(store.save_readings(cbp.build(load("cbp_feed.json"), NOW)), 0)
        self.assertIsNone(store.log_run("snapshot", None, 0, "ok"))
        self.assertEqual(store.recent_readings("240202", "car"), [])

    def test_rows_match_the_migration_columns(self):
        snap = cbp.build(load("cbp_feed.json"), NOW)
        row = next(iter(snap.ports[0].lanes.values())).to_row()
        sql = (ROOT / "supabase" / "migrations" / "0001_border_waits.sql").read_text()
        for column in row:
            self.assertIn(column, sql, f"{column} is written but not in the migration")


class FetchRetries(unittest.TestCase):
    """One flaky moment must not cost a post."""

    class Response(io.BytesIO):
        """Enough of an HTTP response for the fetcher: body plus headers."""

        def __init__(self, payload, encoding=None):
            super().__init__(payload)
            self.headers = {"Content-Encoding": encoding} if encoding else {}

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def _urlopen(self, *outcomes):
        """Each outcome is an exception to raise or a payload to return."""
        calls = {"n": 0}

        def opener(req, timeout=None):
            outcome = outcomes[min(calls["n"], len(outcomes) - 1)]
            calls["n"] += 1
            if isinstance(outcome, Exception):
                raise outcome
            return FetchRetries.Response(json.dumps(outcome).encode())

        return opener, calls

    def test_a_gzipped_response_is_decompressed(self):
        import gzip as gziplib
        payload = gziplib.compress(json.dumps([{"port_number": "240202"}]).encode())
        with mock.patch("urllib.request.urlopen",
                        lambda req, timeout=None: FetchRetries.Response(payload, "gzip")):
            self.assertEqual(cbp.fetch()[0]["port_number"], "240202")

    def http_error(self, code, retry_after=None):
        headers = {"Retry-After": retry_after} if retry_after else {}
        error = urllib.error.HTTPError("u", code, "boom", headers, None)
        self.addCleanup(error.close)
        return error

    def test_retries_a_503_then_succeeds(self):
        opener, calls = self._urlopen(self.http_error(503), [{"port_number": "240202"}])
        with mock.patch("urllib.request.urlopen", opener):
            payload = cbp.fetch(sleep=lambda s: None)
        self.assertEqual(calls["n"], 2)
        self.assertEqual(payload[0]["port_number"], "240202")

    def test_retries_a_timeout(self):
        opener, calls = self._urlopen(TimeoutError("slow"), [])
        with mock.patch("urllib.request.urlopen", opener):
            cbp.fetch(sleep=lambda s: None)
        self.assertEqual(calls["n"], 2)

    def test_gives_up_after_the_limit(self):
        opener, calls = self._urlopen(self.http_error(500))
        with mock.patch("urllib.request.urlopen", opener):
            with self.assertRaises(urllib.error.HTTPError):
                cbp.fetch(retries=2, sleep=lambda s: None)
        self.assertEqual(calls["n"], 3)

    def test_a_404_is_not_retried(self):
        opener, calls = self._urlopen(self.http_error(404))
        with mock.patch("urllib.request.urlopen", opener):
            with self.assertRaises(urllib.error.HTTPError):
                cbp.fetch(sleep=lambda s: None)
        self.assertEqual(calls["n"], 1)

    def test_retry_after_header_is_honoured_and_capped(self):
        slept = []
        opener, _ = self._urlopen(self.http_error(429, retry_after="7"), [])
        with mock.patch("urllib.request.urlopen", opener):
            cbp.fetch(sleep=slept.append)
        self.assertEqual(slept, [7.0])

    def test_a_silly_retry_after_is_clamped(self):
        slept = []
        opener, _ = self._urlopen(self.http_error(429, retry_after="9000"), [])
        with mock.patch("urllib.request.urlopen", opener):
            cbp.fetch(sleep=slept.append)
        self.assertEqual(slept, [cbp.MAX_BACKOFF])


class FrozenFeed(unittest.TestCase):
    """CBP answering happily with numbers that stopped moving is the quiet failure."""

    def feed_at(self, *times):
        clock = {"n": 0}

        def tick():
            now = times[min(clock["n"], len(times) - 1)]
            clock["n"] += 1
            return now

        return feed_for("cbp_feed.json", clock=tick)

    def test_identical_numbers_for_hours_is_degraded(self):
        feed = self.feed_at(NOW, NOW + timedelta(hours=7))
        feed.snapshot()
        feed.snapshot()
        health = feed.health()
        self.assertTrue(health["frozen"])
        self.assertTrue(health["degraded"])
        self.assertFalse(health["ok"])
        self.assertGreaterEqual(health["frozen_hours"], 6)

    def test_a_short_quiet_spell_is_normal(self):
        feed = self.feed_at(NOW, NOW + timedelta(hours=1))
        feed.snapshot()
        feed.snapshot()
        self.assertFalse(feed.health()["frozen"])
        self.assertTrue(feed.health()["ok"])

    def test_the_clock_restarts_when_numbers_move(self):
        payloads = [load("cbp_feed.json"), load("cbp_feed_after_drop.json")]
        clock = [NOW, NOW + timedelta(hours=7)]
        state = {"n": 0}
        fetched = {"n": 0}

        def fetcher():
            i = min(fetched["n"], len(payloads) - 1)
            fetched["n"] += 1
            return payloads[i]

        def tick():
            now = clock[min(state["n"], len(clock) - 1)]
            state["n"] += 1
            return now

        feed = BorderFeed(storage=Storage(url="", key=""), fetcher=fetcher, clock=tick,
                          cache_seconds=0, extra_sources=[])
        feed.snapshot()
        feed.snapshot()
        self.assertFalse(feed.health()["frozen"])

    def test_every_bridge_dark_is_degraded(self):
        blanked = load("cbp_feed.json")
        for port in blanked:
            for group in ("passenger_vehicle_lanes", "pedestrian_lanes", "commercial_vehicle_lanes"):
                for lane_cfg in port.get(group, {}).values():
                    if isinstance(lane_cfg, dict):
                        lane_cfg["operational_status"] = "Update Pending"
        feed = BorderFeed(storage=Storage(url="", key=""), fetcher=lambda: blanked,
                          clock=lambda: NOW, cache_seconds=0, extra_sources=[])
        feed.snapshot()
        self.assertTrue(feed.health()["all_bridges_dark"])
        self.assertFalse(feed.health()["ok"])


class SecondOpinionParsing(unittest.TestCase):
    """pasosfronterizos.com, parsed from a saved page."""

    @classmethod
    def setUpClass(cls):
        page = (FIXTURES / "pasosfronterizos.html").read_text(errors="replace")
        cls.readings = parse_pasos(page, NOW)

    def by(self, port, lane):
        return next((r for r in self.readings if r.port_number == port and r.lane == lane), None)

    def test_it_finds_the_four_bridges_it_covers(self):
        self.assertEqual({r.port_number for r in self.readings}, {"240201", "240202", "240203", "240204"})

    def test_car_and_walking_lanes_are_separated(self):
        self.assertEqual(self.by("240202", "car").minutes, 50)
        self.assertEqual(self.by("240202", "walk").minutes, 25)

    def test_sentri_and_ready_lanes_are_read(self):
        self.assertEqual(self.by("240202", "car_sentri").minutes, 5)
        self.assertEqual(self.by("240202", "car_ready").minutes, 45)

    def test_the_page_age_travels_with_every_reading(self):
        self.assertTrue(all(r.age_minutes == 8 for r in self.readings))

    def test_an_unreadable_page_yields_nothing_rather_than_guesses(self):
        self.assertEqual(parse_pasos("<html><body>nothing here</body></html>", NOW), [])


class Consensus(unittest.TestCase):
    def reading(self, source, minutes, age=5, state="open", independent=True, lane="car"):
        return Reading(port_number="240202", lane=lane, state=state, minutes=minutes,
                       age_minutes=age, source=source, independent=independent)

    def test_one_source_is_reported_as_single(self):
        d = consensus.reconcile([self.reading("cbp", 40)])
        self.assertEqual((d.agreement, d.minutes, d.chosen_source), ("single", 40, "cbp"))

    def test_close_numbers_agree_and_the_fresher_one_wins(self):
        d = consensus.reconcile([self.reading("cbp", 43, age=45),
                                 self.reading("pasosfronterizos", 48, age=8)])
        self.assertEqual(d.agreement, "agree")
        self.assertEqual((d.minutes, d.chosen_source), (48, "pasosfronterizos"))

    def test_far_apart_numbers_are_a_conflict_with_the_spread_kept(self):
        d = consensus.reconcile([self.reading("cbp", 20, age=50),
                                 self.reading("pasosfronterizos", 60, age=5)])
        self.assertEqual(d.agreement, "conflict")
        self.assertEqual(d.spread_minutes, 40)
        self.assertTrue(d.disputed)

    def test_long_waits_get_proportional_tolerance(self):
        d = consensus.reconcile([self.reading("cbp", 80, age=50),
                                 self.reading("pasosfronterizos", 95, age=5)])
        self.assertEqual(d.agreement, "agree")   # 15 apart on an 80-minute line

    def test_a_closure_follows_cbp_even_when_a_site_shows_minutes(self):
        d = consensus.reconcile([self.reading("cbp", None, state="closed"),
                                 self.reading("pasosfronterizos", 35)])
        self.assertEqual((d.state, d.minutes, d.agreement), ("closed", None, "closed"))

    def test_mirrors_do_not_count_as_corroboration(self):
        d = consensus.reconcile([self.reading("cbp", 43, age=45),
                                 self.reading("mirror", 43, age=45, independent=False)])
        self.assertEqual(d.agreement, "single")
        self.assertIsNone(d.spread_minutes)

    def test_every_opinion_is_kept_for_audit(self):
        d = consensus.reconcile([self.reading("cbp", 43), self.reading("pasosfronterizos", 50)])
        self.assertEqual({o["source"] for o in d.opinions}, {"cbp", "pasosfronterizos"})


class MultiSourceFeed(unittest.TestCase):
    """A second source refines the minutes; it can never invent or remove a bridge."""

    class Stub(Source):
        name = "stub"

        def __init__(self, readings, boom=False):
            self.readings, self.boom = readings, boom

        def fetch(self, now):
            if self.boom:
                raise TimeoutError("site slow")
            return self.readings

    def feed_with(self, source, now=None, fixture="cbp_feed.json"):
        return feed_for(fixture, extra_sources=[source], now=now or NOW)

    def test_a_fresher_second_opinion_replaces_the_shown_number(self):
        stub = self.Stub([Reading("240202", "car", "open", 62, age_minutes=3, source="stub")])
        body = self.feed_with(stub).bridge("paso-del-norte", "car")
        self.assertEqual(body["minutes"], 62)        # shown
        self.assertEqual(body["cbp_minutes"], 48)    # kept for audit
        self.assertEqual(body["consensus"]["chosen_source"], "stub")

    def test_a_conflict_lowers_confidence_and_is_flagged(self):
        stub = self.Stub([Reading("240202", "car", "open", 95, age_minutes=3, source="stub")])
        body = self.feed_with(stub).bridge("paso-del-norte", "car")
        self.assertTrue(body["sources_disagree"])
        self.assertEqual(body["confidence"], "low")
        self.assertEqual(body["consensus"]["spread_minutes"], 47)

    def test_a_disputed_lane_is_ranked_on_its_worst_case(self):
        stub = self.Stub([Reading("240202", "car", "open", 95, age_minutes=3, source="stub")])
        row = next(r for r in self.feed_with(stub, now=MIDDAY, fixture="cbp_feed_midday.json")
                   .ranked("car") if r["port_number"] == "240202")
        self.assertEqual((row["optimistic_minutes"], row["planning_minutes"]), (48, 95))
        self.assertGreaterEqual(row["effective_minutes"], 95)

    def test_an_undisputed_bridge_wins_the_headline_over_a_disputed_faster_one(self):
        # Santa Teresa is undisputed at 40; Paso del Norte is 48 by CBP but 12 by a
        # second site. The optimistic 12 must not win.
        # Midday fixture: nothing is demoted for closing, and every reading is fresh,
        # so the only thing separating the bridges is the disagreement.
        stub = self.Stub([Reading("240202", "car", "open", 12, age_minutes=1, source="stub")])
        best = self.feed_with(stub, now=MIDDAY, fixture="cbp_feed_midday.json").best("car", None)
        self.assertEqual(best["port_number"], "240801")

    def test_a_disputed_bridge_still_wins_when_even_its_worst_case_is_best(self):
        stub = self.Stub([Reading("240801", "car", "open", 12, age_minutes=1, source="stub")])
        best = self.feed_with(stub, now=MIDDAY, fixture="cbp_feed_midday.json").best("car", None)
        self.assertEqual(best["port_number"], "240801")    # worst case 40 still beats 48

    def test_a_disputed_headline_shows_the_range_not_the_optimistic_end(self):
        stub = self.Stub([Reading("240801", "car", "open", 12, age_minutes=1, source="stub")])
        feed = self.feed_with(stub, now=MIDDAY, fixture="cbp_feed_midday.json")
        headline = text.caption(feed, feed.snapshot(), "es").splitlines()[1]
        self.assertIn("12–40 min", headline)

    def test_a_broken_source_never_stops_the_feed(self):
        feed = self.feed_with(self.Stub([], boom=True))
        self.assertEqual(feed.bridge("paso-del-norte", "car")["minutes"], 48)
        self.assertIn("failed: TimeoutError", feed.health()["sources"]["stub"])

    def test_a_source_cannot_add_a_bridge_cbp_does_not_know(self):
        stub = self.Stub([Reading("999999", "car", "open", 5, age_minutes=1, source="stub")])
        feed = self.feed_with(stub)
        self.assertEqual(len(feed.waits()["ports"]), 6)
        self.assertIsNone(feed.snapshot().port("999999"))

    def test_health_lists_every_source_and_the_dispute_count(self):
        stub = self.Stub([Reading("240202", "car", "open", 95, age_minutes=3, source="stub")])
        health = self.feed_with(stub).health()
        self.assertEqual(set(health["sources"]), {"cbp", "stub"})
        self.assertEqual(health["lanes_disputed"], 1)


class Efficiency(unittest.TestCase):
    """Every answer costs an upstream call, so the cheap wins are measured, not assumed."""

    def test_one_snapshot_serves_many_questions(self):
        calls = {"n": 0}
        payload = load("cbp_feed.json")

        def fetcher():
            calls["n"] += 1
            return payload

        feed = BorderFeed(storage=Storage(url="", key=""), fetcher=fetcher,
                          clock=lambda: NOW, extra_sources=[])
        feed.waits()
        feed.bridge("paso-del-norte", "car")
        feed.drops(30)
        feed.best("walk")
        self.assertEqual(calls["n"], 1)

    def test_a_lane_is_decorated_once_per_snapshot(self):
        feed = feed_for("cbp_feed.json", cache_seconds=300)
        feed.snapshot()
        counted = {"n": 0}
        original = BorderFeed._decorate_uncached

        def counting(self, snap, port, lane):
            counted["n"] += 1
            return original(self, snap, port, lane)

        with mock.patch.object(BorderFeed, "_decorate_uncached", counting):
            feed.waits()
            lanes = sum(len(p.lanes) for p in feed.snapshot().ports)
            self.assertLessEqual(counted["n"], lanes)

    def test_the_previous_reading_is_read_from_storage_once_per_lane(self):
        store = Always60Store(enabled=False)
        feed = feed_for("cbp_feed.json", cache_seconds=300, storage=store)
        feed.snapshot()
        feed.waits()
        feed.waits()
        lanes = sum(len(p.lanes) for p in feed.snapshot().ports)
        self.assertLessEqual(store.calls["recent_readings"], lanes)

    def test_sources_are_read_at_the_same_time_not_one_after_another(self):
        import time as clock_module

        class Slow(Source):
            def __init__(self, name):
                self.name = name

            def fetch(self, now):
                clock_module.sleep(0.25)
                return []

        feed = feed_for("cbp_feed.json", extra_sources=[Slow("a"), Slow("b"), Slow("c")])
        start = clock_module.perf_counter()
        feed.snapshot()
        elapsed = clock_module.perf_counter() - start
        self.assertLess(elapsed, 0.6, "three 0.25s sources should overlap, not queue")

    def test_simultaneous_callers_share_one_upstream_read(self):
        import threading as thread_module
        calls = {"n": 0}
        payload = load("cbp_feed.json")

        def slow_fetcher():
            calls["n"] += 1
            import time as t
            t.sleep(0.2)
            return payload

        feed = BorderFeed(storage=Storage(url="", key=""), fetcher=slow_fetcher,
                          clock=lambda: NOW, extra_sources=[], cache_seconds=300)
        threads = [thread_module.Thread(target=feed.snapshot) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(calls["n"], 1)

    def test_health_reports_how_stale_the_cache_is(self):
        feed = feed_for("cbp_feed.json", cache_seconds=300)
        feed.snapshot()
        self.assertIsNotNone(feed.health()["cache_age_seconds"])


class SourceWindows(unittest.TestCase):
    """Each source is re-read on its own schedule, and backs off while it repeats."""

    class Counting(Source):
        name = "pasosfronterizos"      # borrows that source's window

        def __init__(self, minutes=40):
            self.calls = 0
            self.minutes = minutes

        def fetch(self, now):
            self.calls += 1
            return [Reading("240202", "car", "open", self.minutes, age_minutes=5, source=self.name)]

    def feed_with(self, source, cache_seconds=300):
        return feed_for("cbp_feed.json", extra_sources=[source], cache_seconds=cache_seconds)

    def test_a_source_is_not_re_read_inside_its_window(self):
        source = self.Counting()
        feed = self.feed_with(source)
        feed.snapshot()
        feed.snapshot(force=True)          # rebuild, but the source window still holds
        self.assertEqual(source.calls, 1)

    def test_the_window_widens_while_the_numbers_repeat(self):
        source = self.Counting()
        feed = self.feed_with(source)
        feed.snapshot()
        base = feed.source_windows()["pasosfronterizos"]["ttl_seconds"]
        feed._source_cache["pasosfronterizos"]["at"] -= 10_000     # pretend it expired
        feed.snapshot(force=True)
        widened = feed.source_windows()["pasosfronterizos"]
        self.assertEqual(source.calls, 2)
        self.assertGreater(widened["ttl_seconds"], base)
        self.assertEqual(widened["unchanged_reads"], 1)

    def test_the_window_snaps_back_when_the_numbers_move(self):
        source = self.Counting()
        feed = self.feed_with(source)
        feed.snapshot()
        feed._source_cache["pasosfronterizos"]["at"] -= 10_000
        feed.snapshot(force=True)                                  # repeat: widened
        source.minutes = 75                                        # the line moved
        feed._source_cache["pasosfronterizos"]["at"] -= 10_000
        feed.snapshot(force=True)
        window = feed.source_windows()["pasosfronterizos"]
        self.assertEqual(window["unchanged_reads"], 0)
        self.assertEqual(window["ttl_seconds"], 240)

    def test_windows_are_ignored_when_caching_is_off(self):
        source = self.Counting()
        feed = self.feed_with(source, cache_seconds=0)
        feed.snapshot()
        feed.snapshot()
        self.assertEqual(source.calls, 2)

    def test_health_shows_each_window(self):
        feed = self.feed_with(self.Counting())
        feed.snapshot()
        self.assertIn("pasosfronterizos", feed.health()["source_windows"])


class RushHours(unittest.TestCase):
    """Windows tighten while people are actually crossing."""

    def feed_at(self, hour: int, minute: int = 0):
        clock = datetime(2026, 9, 22, (hour + 6) % 24, minute, tzinfo=timezone.utc)  # MDT
        return BorderFeed(storage=Storage(url="", key=""), fetcher=lambda: load("cbp_feed.json"),
                          clock=lambda: clock, extra_sources=[], cache_seconds=300)

    def test_the_morning_and_evening_peaks_count_as_rush(self):
        for hour in (5, 7, 8, 15, 17, 18):
            with self.subTest(hour=hour):
                self.assertTrue(self.feed_at(hour).is_rush_hour())

    def test_the_quiet_hours_do_not(self):
        for hour in (2, 4, 9, 12, 19, 22):
            with self.subTest(hour=hour):
                self.assertFalse(self.feed_at(hour).is_rush_hour())

    def test_ceilings_halve_during_rush(self):
        self.assertEqual(self.feed_at(12)._window("cbp"), (300, 1200))
        self.assertEqual(self.feed_at(7)._window("cbp"), (300, 600))
        self.assertEqual(self.feed_at(7)._window("pasosfronterizos"), (240, 360))

    def test_a_widened_window_tightens_as_rush_begins(self):
        feed = self.feed_at(14, 50)                       # quiet: ceiling 20 min
        feed.snapshot()
        feed._source_cache["cbp"]["repeats"] = 3          # pretend it had widened fully
        self.assertEqual(feed.source_windows()["cbp"]["ttl_seconds"], 1200)

        rush = self.feed_at(15, 10)                       # 3:10 pm: rush has started
        rush._source_cache = feed._source_cache
        self.assertEqual(rush.source_windows()["cbp"]["ttl_seconds"], 600)

    def test_health_says_whether_it_is_rush_hour(self):
        self.assertTrue(self.feed_at(7).health()["rush_hour"])
        self.assertFalse(self.feed_at(12).health()["rush_hour"])


class StaleWhileRevalidate(unittest.TestCase):
    """A failed refresh must not turn a good 2-minute-old reading into silence."""

    def feed_that_breaks_after_one_read(self):
        payload = load("cbp_feed.json")
        state = {"n": 0}

        def fetcher():
            state["n"] += 1
            if state["n"] > 1:
                raise OSError("connection reset")
            return payload

        return BorderFeed(storage=Storage(url="", key=""), fetcher=fetcher,
                          clock=lambda: NOW, cache_seconds=0, extra_sources=[])

    def test_the_last_good_reading_is_served_and_flagged(self):
        feed = self.feed_that_breaks_after_one_read()
        feed.snapshot()
        snap = feed.snapshot()                       # the fetch fails here
        self.assertEqual(len(snap.ports), 6)
        health = feed.health()
        self.assertTrue(health["serving_stale"])
        self.assertFalse(health["ok"])
        self.assertIn("connection reset", health["last_error"])

    def test_a_failure_with_nothing_cached_still_fails_loudly(self):
        def boom():
            raise OSError("no route to host")
        feed = BorderFeed(storage=Storage(url="", key=""), fetcher=boom,
                          clock=lambda: NOW, extra_sources=[])
        with self.assertRaises(FeedError):
            feed.snapshot()

    def test_stale_serving_stops_after_the_grace_period(self):
        feed = self.feed_that_breaks_after_one_read()
        feed.snapshot()
        feed._fetched_monotonic -= service_mod.STALE_GRACE_SECONDS + 1
        with self.assertRaises(FeedError):
            feed.snapshot()


class ResponseTrimming(unittest.TestCase):
    def test_waits_can_be_limited_to_one_lane(self):
        feed = feed_for("cbp_feed.json")
        full = feed.waits()
        trimmed = feed.waits(lanes=("car",))
        self.assertEqual(set(trimmed["ranked"]), {"car"})
        self.assertLess(len(json.dumps(trimmed)), len(json.dumps(full)))

    def test_the_lane_filter_travels_over_http(self):
        status, body = api.route(feed_for("cbp_feed.json"), "/waits", {"lane": ["car_sentri"]})
        self.assertEqual(status, 200)
        self.assertEqual(set(body["ranked"]), {"car_sentri"})


class UnchangedWrites(unittest.TestCase):
    def test_an_unchanged_reading_is_not_written_again(self):
        store = MemoryStore(enabled=False)
        feed = feed_for("cbp_feed.json", storage=store)
        feed.snapshot()
        feed.snapshot(force=True)       # identical numbers
        self.assertEqual(store.calls["save_readings"], 1)
        self.assertEqual(store.runs, ["ok", "unchanged"])


class MirrorAsParserCheck(unittest.TestCase):
    """borderswaittime republishes CBP, so a difference points at our parsing."""

    @classmethod
    def setUpClass(cls):
        cls.page = (FIXTURES / "borderswaittime.html").read_text(errors="replace")

    def test_it_reads_the_data_card_not_the_navigation(self):
        readings = {r.port_number: (r.state, r.minutes) for r in parse_mirror(self.page, NOW)}
        self.assertEqual(readings["240201"], ("open", 20))     # not the pedestrian "Lanes Closed"
        self.assertEqual(readings["240202"], ("open", 39))
        self.assertEqual(readings["240203"], ("closed", None))

    def test_mirror_readings_are_never_independent(self):
        self.assertTrue(all(not r.independent for r in parse_mirror(self.page, NOW)))

    def test_a_mirror_agreeing_reports_no_mismatch(self):
        agreeing = [Reading("240202", "car", "open", 48, source="mirror", independent=False)]
        feed = feed_for("cbp_feed.json", extra_sources=[
            StubSource(agreeing, "mirror", independent=False)])
        feed.snapshot()
        check = feed.health()["parser_check"]
        self.assertEqual((check["compared"], check["mismatches"]), (1, []))

    def test_a_mirror_disagreeing_flags_our_parsing(self):
        differing = [Reading("240202", "car", "open", 999, source="mirror", independent=False)]
        feed = feed_for("cbp_feed.json", extra_sources=[
            StubSource(differing, "mirror", independent=False)])
        feed.snapshot()
        mismatches = feed.health()["parser_check"]["mismatches"]
        self.assertEqual(len(mismatches), 1)
        self.assertEqual(mismatches[0]["ours"]["minutes"], 48)

    def test_a_mirror_never_changes_the_answer(self):
        differing = [Reading("240202", "car", "open", 999, source="mirror", independent=False)]
        feed = feed_for("cbp_feed.json", extra_sources=[
            StubSource(differing, "mirror", independent=False)])
        self.assertEqual(feed.bridge("paso-del-norte", "car")["minutes"], 48)


class Politeness(unittest.TestCase):
    def test_the_scraper_asks_robots_before_fetching(self):
        source = PasosFronterizosSource()
        with mock.patch.object(PasosFronterizosSource, "_robots_allows", return_value=False):
            self.assertFalse(source.is_configured())

    def test_an_unreachable_robots_file_means_allowed(self):
        source = PasosFronterizosSource()
        with mock.patch("urllib.request.urlopen", side_effect=OSError("offline")):
            self.assertTrue(source.is_configured())

    def test_a_disallowed_source_is_skipped_not_failed(self):
        class Forbidden(Source):
            name = "forbidden"

            def is_configured(self):
                return False

            def fetch(self, now):
                raise AssertionError("must not be fetched")

        feed = feed_for("cbp_feed.json", extra_sources=[Forbidden()])
        feed.snapshot()
        self.assertEqual(feed.health()["sources"]["forbidden"], "not configured")


class CaptionLimits(unittest.TestCase):
    def test_a_normal_caption_is_well_inside_instagram_limits(self):
        feed = feed_for("cbp_feed.json")
        self.assertLess(len(text.caption(feed, feed.snapshot(), "es")), text.CAPTION_LIMIT)

    def test_an_overlong_caption_is_trimmed_to_fit(self):
        feed = feed_for("cbp_feed.json")
        snap = feed.snapshot()
        with mock.patch.object(text, "CAPTION_LIMIT", 200):
            caption = text.caption(feed, snap, "es")
        self.assertLessEqual(len(caption), 200)
        self.assertIn("Más rápido ahora", caption)      # the headline always survives


class SelfCheck(unittest.TestCase):
    """The check that catches CBP renaming something while the tests stay green."""

    def test_a_healthy_feed_passes(self):
        check = selfcheck.Check()
        selfcheck.check_feed(load("cbp_feed.json"), check)
        self.assertTrue(check.ok, check.problems)

    def test_a_renamed_field_is_caught(self):
        payload = load("cbp_feed.json")
        for port in payload:
            if port["port_number"] == "240202":
                port["passenger_vehicle_lanes"]["standard_lanes"]["wait_minutes"] = (
                    port["passenger_vehicle_lanes"]["standard_lanes"].pop("delay_minutes"))
        check = selfcheck.Check()
        selfcheck.check_feed(payload, check)
        self.assertFalse(check.ok)
        self.assertIn("delay_minutes", check.problems[0])

    def test_a_missing_port_is_caught(self):
        payload = [p for p in load("cbp_feed.json") if p["port_number"] != "240202"]
        check = selfcheck.Check()
        selfcheck.check_feed(payload, check)
        self.assertIn("no longer lists", check.problems[0])

    def test_an_unknown_status_word_is_caught(self):
        payload = load("cbp_feed.json")
        for port in payload:
            if port["port_number"] == "240202":
                port["passenger_vehicle_lanes"]["standard_lanes"]["operational_status"] = "Backed Up"
        check = selfcheck.Check()
        selfcheck.check_feed(payload, check)
        self.assertIn("unknown status", check.problems[0])


class Poller(unittest.TestCase):
    def test_one_pass_reports_what_it_saw(self):
        feed = feed_for("cbp_feed.json")
        result = poll.once(feed, dry_run=True)
        self.assertEqual(result["ports"], 6)
        self.assertGreater(result["open_lanes"], 0)
        self.assertEqual(result["would_write"], result["lanes"])
        self.assertFalse(result["stored"])

    def test_a_failed_read_raises_for_the_caller_to_handle(self):
        def boom():
            raise OSError("down")
        feed = BorderFeed(storage=Storage(url="", key=""), fetcher=boom,
                          clock=lambda: NOW, extra_sources=[])
        with self.assertRaises(FeedError):
            poll.once(feed)


class PortRegistrySync(unittest.TestCase):
    def test_ports_are_synced_once_per_process_when_storage_is_on(self):
        store = MemoryStore()
        feed = feed_for("cbp_feed.json", storage=store)
        feed.snapshot()
        feed.snapshot(force=True)
        self.assertEqual(store.calls["upsert_ports"], 1)

    def test_nothing_is_synced_when_storage_is_off(self):
        feed = feed_for("cbp_feed.json")
        feed.snapshot()
        self.assertFalse(feed._ports_synced)


class ForTheCrosser(unittest.TestCase):
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

        def fetcher():
            payload = payloads[min(fetched["n"], len(payloads) - 1)]
            fetched["n"] += 1
            return payload

        def clock():
            when = start + timedelta(minutes=30 * state["n"])
            state["n"] += 1
            return when

        return BorderFeed(storage=Storage(url="", key=""), fetcher=fetcher, clock=clock,
                          cache_seconds=0, extra_sources=[])

    def test_a_growing_line_is_reported_in_plain_words(self):
        feed = self.moving_feed(30, 45, 60)
        feed.snapshot(); feed.snapshot(); feed.snapshot()
        trend = feed.trend("240202", "car")
        self.assertEqual((trend["change"], trend["direction"]), (30, "up"))
        self.assertTrue(trend["worth_saying"])
        self.assertIn("subió 30 min", text.trend_phrase(trend, "es"))

    def test_a_small_wobble_is_not_worth_saying(self):
        feed = self.moving_feed(30, 33, 34)
        for _ in range(3):
            feed.snapshot()
        self.assertFalse(feed.trend("240202", "car")["worth_saying"])
        self.assertIsNone(text.trend_phrase(feed.trend("240202", "car"), "es"))

    def test_one_reading_is_not_a_trend(self):
        feed = self.moving_feed(30)
        feed.snapshot()
        self.assertIsNone(feed.trend("240202", "car"))

    def test_the_biggest_saving_at_the_same_bridge_is_offered(self):
        # PDN: 48 by car, 8 SENTRI, 0 Ready Lane, 10 on foot. Ready Lane saves most.
        body = feed_for("cbp_feed.json").bridge("paso-del-norte", "car")
        self.assertEqual(body["faster_lane_here"]["lane"], "car_ready")
        self.assertIn("Si tienes Ready Lane", body["reply"])   # a card, not an instruction

    def test_walking_is_offered_when_the_car_lanes_are_the_slow_ones(self):
        payload = load("cbp_feed.json")
        for port in payload:
            if port["port_number"] == "240202":
                lanes = port["passenger_vehicle_lanes"]
                lanes["ready_lanes"]["operational_status"] = "Lanes Closed"
                lanes["NEXUS_SENTRI_lanes"]["operational_status"] = "Lanes Closed"
        feed = BorderFeed(storage=Storage(url="", key=""), fetcher=lambda: payload,
                          clock=lambda: NOW, cache_seconds=0, extra_sources=[])
        body = feed.bridge("paso-del-norte", "car")
        self.assertEqual(body["faster_lane_here"]["lane"], "walk")
        self.assertIn("A pie son 10 min", body["reply"])
        self.assertNotIn("Si tienes", body["reply"])           # walking needs no card

    def test_no_lane_tip_when_the_saving_is_small(self):
        feed = feed_for("cbp_feed.json")
        self.assertIsNone(feed.bridge("santa-teresa", "walk")["faster_lane_here"])

    def test_long_waits_read_as_hours(self):
        self.assertEqual(text.duration(45), "45 min")
        self.assertEqual(text.duration(60), "1 h")
        self.assertEqual(text.duration(75), "1 h 15")
        self.assertEqual(text.duration(130), "2 h 10")

    def test_a_long_wait_is_phrased_as_hours_in_a_reply(self):
        feed = self.moving_feed(95)
        feed.snapshot()
        self.assertIn("1 h 35", feed.bridge("paso-del-norte", "car")["reply"])

    def test_quiet_hours_are_flagged_so_alerts_can_wait(self):
        night = feed_for("cbp_feed.json", now=datetime(2026, 9, 22, 9, 0, tzinfo=timezone.utc))
        self.assertTrue(night.is_quiet_hour())        # 3 am local
        day = feed_for("cbp_feed.json", now=MIDDAY)
        self.assertFalse(day.is_quiet_hour())
        self.assertIn("quiet_hour", day.drops(30))


class HttpPull(unittest.TestCase):
    """The pipeline pulls over HTTP, where a 404 arrives as an exception."""

    def test_a_404_body_is_returned_not_raised(self):
        payload = json.dumps({"found": False, "reply": "No encuentro ese puente.",
                              "options": ["Zaragoza"]}).encode()
        error = urllib.error.HTTPError("u", 404, "Not Found", {}, io.BytesIO(payload))
        with mock.patch("urllib.request.urlopen", side_effect=error):
            body = fake_instagram.HttpFeed("http://x").bridge("narnia")
        self.assertFalse(body["found"])
        self.assertIn("options", body)

    def test_a_503_body_is_returned_not_raised(self):
        payload = json.dumps({"error": "CBP feed unavailable"}).encode()
        error = urllib.error.HTTPError("u", 503, "Unavailable", {}, io.BytesIO(payload))
        with mock.patch("urllib.request.urlopen", side_effect=error):
            body = fake_instagram.HttpFeed("http://x").waits()
        self.assertIn("CBP feed unavailable", body["error"])

    def test_spaces_in_a_bridge_name_are_encoded(self):
        seen = {}

        class Fake(io.BytesIO):
            def __enter__(self): return self
            def __exit__(self, *a): return False

        def opener(url, timeout=None):
            seen["url"] = url
            return Fake(b"{}")

        with mock.patch("urllib.request.urlopen", opener):
            fake_instagram.HttpFeed("http://x").bridge("el puente libre")
        self.assertIn("el%20puente%20libre", seen["url"])
        self.assertNotIn(" ", seen["url"])


class InstagramSimulation(unittest.TestCase):
    def setUp(self):
        self.graph = fake_instagram.FakeGraph()
        self.feed = feed_for("cbp_feed.json", "cbp_feed_after_drop.json")
        self.pipeline = fake_instagram.FakePipeline(self.feed, self.graph)

    def test_daily_post_publishes_a_caption_and_sends_nothing(self):
        caption = self.pipeline.daily_post()
        self.assertIn("Puentes Juárez–El Paso", caption)
        self.assertEqual([c["endpoint"].rsplit("/", 1)[-1] for c in self.graph.calls],
                         ["media", "media_publish"])

    def test_bridge_question_is_answered_with_live_minutes(self):
        self.pipeline.on_message("user-1", "paso del norte")
        self.assertIn("48 min", self.graph.calls[-1]["params"]["message"]["text"])

    def test_menu_offers_quick_replies(self):
        self.pipeline.on_message("user-1", "puentes")
        self.assertTrue(self.graph.calls[-1]["params"]["message"]["quick_replies"])

    def test_alert_subscription_then_drop_sends_one_message(self):
        self.pipeline.on_message("user-2", "avísame cuando baje de 30 min")
        self.feed.snapshot()                       # first reading: 48 min
        self.assertEqual(self.pipeline.alert_sweep(), 1)
        self.assertIn("20 min", self.graph.calls[-1]["params"]["message"]["text"])

    def test_saving_a_bridge_then_asking_returns_only_that_bridge(self):
        self.pipeline.on_message("user-1", "guardar paso del norte sentri")
        self.pipeline.on_message("user-1", "puentes")
        sent = self.graph.calls[-1]["params"]["message"]["text"]
        self.assertIn("Tus puentes", sent)
        self.assertIn("SENTRI", sent)
        self.assertNotIn("Santa Teresa", sent)

    def test_scheduled_message_uses_their_saved_bridges(self):
        self.pipeline.on_message("user-1", "guardar zaragoza")
        self.pipeline.scheduled_message("user-1")
        self.assertIn("Tus puentes", self.graph.calls[-1]["params"]["message"]["text"])

    def test_stop_cancels_subscriptions(self):
        self.pipeline.on_message("user-2", "avísame cuando baje de 30")
        self.pipeline.on_message("user-2", "alto")
        self.assertEqual(self.pipeline.subscriptions, [])

    def test_simulation_run_records_calls_without_network(self):
        with contextlib.redirect_stdout(io.StringIO()):
            graph = fake_instagram.run(feed_for("cbp_feed.json", "cbp_feed_after_drop.json"))
        self.assertTrue(graph.calls)
        self.assertTrue(all(c["endpoint"].startswith("https://graph.facebook.com") for c in graph.calls))


class ReviewRegressions(unittest.TestCase):
    """Bugs found in review: each of these failed before the fix."""

    def test_a_drop_is_not_lost_when_another_request_refreshed_first(self):
        feed = feed_for("cbp_feed.json", "cbp_feed_after_drop.json")
        feed.snapshot()                  # 48
        feed.waits()                     # 20: the post consumed the new reading
        drops = feed.drops(30)["drops"]  # the sweep runs after, on the same 20
        self.assertEqual([d["previous_minutes"] for d in drops], [48])

    def test_the_delta_holds_until_cbp_publishes_something_new(self):
        feed = feed_for("cbp_feed.json", "cbp_feed_after_drop.json")
        feed.snapshot()
        feed.snapshot()
        feed.snapshot()                  # same 20 again: not a new reading
        self.assertEqual(feed.bridge("paso-del-norte", "car")["delta"]["change"], -28)

    def test_an_unknown_lane_is_a_400_not_a_dropped_connection(self):
        feed = feed_for("cbp_feed.json")
        for path in ("/waits/pdn", "/mine", "/best", "/waits"):
            with self.subTest(path=path):
                status, body = api.route(feed, path, {"lane": ["bike"], "ports": ["pdn"]})
                self.assertEqual(status, 400)
                self.assertIn("car", body["lanes"])

    def test_an_unexpected_error_is_a_500(self):
        feed = feed_for("cbp_feed.json")
        with mock.patch.object(BorderFeed, "health", side_effect=ZeroDivisionError("x")):
            with self.assertLogs("border.api", "ERROR"):
                status, _ = api.route(feed, "/health", {})
        self.assertEqual(status, 500)

    def test_the_cbp_window_widens_while_the_payload_repeats(self):
        calls = {"n": 0}
        payload = load("cbp_feed.json")

        def fetcher():
            calls["n"] += 1
            return payload

        feed = BorderFeed(storage=Storage(url="", key=""), fetcher=fetcher,
                          clock=lambda: MIDDAY, extra_sources=[])
        feed.snapshot()
        feed._source_cache["cbp"]["at"] -= 10_000
        feed.snapshot(force=True)
        window = feed.source_windows()["cbp"]
        self.assertEqual(calls["n"], 2)
        self.assertEqual((window["unchanged_reads"], window["ttl_seconds"]), (1, 600))

    def test_a_cached_scrape_ages_while_it_is_held(self):
        source = StubSource([Reading("240202", "car", "open", 40, age_minutes=5, source="pasosfronterizos")],
                            "pasosfronterizos")
        feed = feed_for("cbp_feed.json", extra_sources=[source], cache_seconds=300)
        feed.snapshot()
        feed._source_cache["pasosfronterizos"]["at"] -= 180      # held 3 minutes
        feed.snapshot(force=True)
        opinion = next(o for o in feed._decisions[("240202", "car")].opinions
                       if o["source"] == "pasosfronterizos")
        self.assertEqual(opinion["age_minutes"], 8)

    def test_storage_lookups_for_a_fresh_process_happen_in_one_pass(self):
        store = Always60Store()
        feed = feed_for("cbp_feed.json", cache_seconds=300, storage=store)
        feed.snapshot()
        after_refresh = store.calls["recent_readings"]
        feed.waits()
        feed.bridge("paso-del-norte", "car")
        self.assertEqual(store.calls["recent_readings"], after_refresh,
                         "answers must not go back to the database")
        self.assertEqual(feed.bridge("paso-del-norte", "car")["delta"]["previous_minutes"], 60)

    def test_poll_does_not_count_a_stale_fallback_as_a_reading(self):
        state = {"fail": False}
        payload = load("cbp_feed.json")

        def fetcher():
            if state["fail"]:
                raise OSError("down")
            return payload

        feed = BorderFeed(storage=Storage(url="", key=""), fetcher=fetcher,
                          clock=lambda: NOW, extra_sources=[], cache_seconds=0)
        poll.once(feed)
        state["fail"] = True
        with self.assertRaises(FeedError):
            poll.once(feed)


class AlertsAreEvents(unittest.TestCase):
    """Alerts survive restarts and are never taken from one consumer by another."""

    def previous(self, store, minutes):
        store.readings.append({"port_number": "240202", "lane": "car", "state": "open",
                               "delay_minutes": minutes, "content_hash": "older",
                               "captured_at": (NOW - timedelta(hours=1)).isoformat()})

    def test_a_restart_does_not_resend_a_drop(self):
        store = MemoryStore()
        self.previous(store, 48)
        first = feed_for("cbp_feed_after_drop.json", storage=store).drops(30)
        self.assertEqual(len(first["drops"]), 1)

        restarted = feed_for("cbp_feed_after_drop.json", storage=store)     # a new process
        again = restarted.drops(30, since=datetime.fromisoformat(first["cursor"]))
        self.assertEqual(again["drops"], [])
        self.assertEqual(len(store.alerts), 1)

    def test_a_restart_remembers_the_lane_is_not_re_armed(self):
        store = MemoryStore()
        before = DropHysteresis.feed_with(self, 40, 20, 31, storage=store)
        before.snapshot()
        self.assertEqual(len(before.drops(30)["drops"]), 1)    # 40 -> 20 fires
        before.drops(30)                                        # 31: inside the band

        restarted = DropHysteresis.feed_with(self, 25, storage=store)   # a new process, now 25
        restarted._previous[("240202", "car")] = 31
        # 31 -> 25 crosses the limit again, but the lane never climbed back to 35.
        self.assertEqual(restarted.drops(30)["drops"], [])
        self.assertEqual(len(store.alerts), 1)

    def test_two_consumers_both_receive_the_alert(self):
        feed = feed_for("cbp_feed.json", "cbp_feed_after_drop.json")
        feed.snapshot()
        worker_a = feed.drops(30)
        worker_b = feed.drops(30)
        self.assertEqual([d["id"] for d in worker_a["drops"]], [d["id"] for d in worker_b["drops"]])
        self.assertEqual(len(worker_b["drops"]), 1)

    def test_a_consumer_that_was_away_catches_up_with_its_cursor(self):
        feed = feed_for("cbp_feed.json", "cbp_feed_after_drop.json")
        cursor = datetime(2000, 1, 1, tzinfo=timezone.utc)
        feed.snapshot()
        feed.drops(30)                                   # someone else asked
        caught_up = feed.drops(30, since=cursor)
        self.assertEqual(len(caught_up["drops"]), 1)

    def test_the_fake_pipeline_alerts_every_subscriber(self):
        feed = feed_for("cbp_feed.json", "cbp_feed_after_drop.json")
        feed.snapshot()
        pipeline = fake_instagram.FakePipeline(feed, fake_instagram.FakeGraph())
        pipeline.on_message("a", "avísame cuando baje de 30")
        pipeline.on_message("b", "avísame cuando baje de 30")
        self.assertEqual(pipeline.alert_sweep(), 2)
        self.assertEqual(pipeline.alert_sweep(), 0)      # cursors: nothing new

    def test_drops_rejects_a_bad_cursor(self):
        status, _ = api.route(feed_for("cbp_feed.json"), "/drops", {"below": ["30"], "since": ["soon"]})
        self.assertEqual(status, 400)


class ScrapersThatBreakQuietly(unittest.TestCase):
    def page(self, readings):
        return StubSource(readings, "pasosfronterizos", expected_ports={"240202", "240201"})

    def test_a_page_that_parses_to_nothing_is_a_problem_not_ok(self):
        source = self.page([])
        feed = feed_for("cbp_feed.json", extra_sources=[source], cache_seconds=300)
        feed.snapshot()
        health = feed.health()
        self.assertTrue(health["sources"]["pasosfronterizos"].startswith("empty"))
        self.assertIn("pasosfronterizos", health["source_problems"])
        feed.snapshot(force=True)
        self.assertEqual(source.calls, 2, "an empty parse must not be cached")

    def test_a_missing_bridge_is_reported(self):
        source = self.page([Reading("240202", "car", "open", 40, age_minutes=5, source="pasosfronterizos")])
        feed = feed_for("cbp_feed.json", extra_sources=[source])
        feed.snapshot()
        status = feed.health()["sources"]["pasosfronterizos"]
        self.assertTrue(status.startswith("partial"))
        self.assertIn("240201", status)

    def test_the_real_parsers_declare_what_a_healthy_page_shows(self):
        page = (FIXTURES / "pasosfronterizos.html").read_text(errors="replace")
        found = {r.port_number for r in parse_pasos(page, NOW)}
        self.assertEqual(PasosFronterizosSource.expected_ports - found, set())
        mirror = (FIXTURES / "borderswaittime.html").read_text(errors="replace")
        self.assertEqual(EXPECTED_PORTS - {r.port_number for r in parse_mirror(mirror, NOW)}, set())


class MirrorComparesTheSameUpdate(unittest.TestCase):
    def feed_with_mirror(self, label):
        reading = Reading("240202", "car", "open", 999, source="mirror", independent=False, label=label)
        return feed_for("cbp_feed.json", extra_sources=[
            StubSource([reading], "mirror", independent=False)])

    def ours(self):
        return cbp.build(load("cbp_feed.json"), NOW).port("240202").lanes["car"].cbp_update_label

    def test_a_mirror_describing_an_older_update_is_skipped(self):
        feed = self.feed_with_mirror("At 1:00 am MDT")
        feed.snapshot()
        check = feed.health()["parser_check"]
        self.assertEqual((check["compared"], check["skipped_other_update"], check["mismatches"]), (0, 1, []))

    def test_the_same_update_is_still_compared(self):
        feed = self.feed_with_mirror(self.ours().upper())
        feed.snapshot()
        self.assertEqual(len(feed.health()["parser_check"]["mismatches"]), 1)

    def test_the_mirror_parser_reads_the_update_label(self):
        page = (FIXTURES / "borderswaittime.html").read_text(errors="replace")
        labels = {r.port_number: r.label for r in parse_mirror(page, NOW) if r.state == "open"}
        self.assertEqual(labels["240201"], "At 7:00 pm MDT")


class Retention(unittest.TestCase):
    def run_poll(self, argv, store):
        with mock.patch.object(poll, "Storage", return_value=store), \
             mock.patch.object(poll, "once", return_value={}), \
             mock.patch.object(poll.signal, "signal"), \
             contextlib.redirect_stdout(io.StringIO()):
            return poll.main(argv)

    def test_prune_runs_when_asked(self):
        store = MemoryStore()
        self.run_poll(["--prune", "--quiet"], store)
        self.assertEqual(store.calls["prune"], 1)

    def test_a_single_reading_does_not_prune_by_default(self):
        store = MemoryStore()
        self.run_poll(["--quiet"], store)
        self.assertEqual(store.calls["prune"], 0)

    def test_dry_run_never_prunes(self):
        store = MemoryStore()
        self.run_poll(["--prune", "--dry-run", "--quiet"], store)
        self.assertEqual(store.calls["prune"], 0)

    def test_the_migration_keeps_enough_history_for_typical_waits(self):
        sql = (ROOT / "supabase" / "migrations" / "0003_border_alerts_crossings_typical.sql").read_text()
        self.assertIn("keep_readings_days int default 60", sql)
        self.assertIn("weeks int default 8", sql)
        self.assertGreaterEqual(poll.KEEP_READINGS_DAYS, 8 * 7)


class NormalForThisHour(unittest.TestCase):
    # NOW is Monday 9:40 pm MDT: Postgres dow 1, hour 21.
    def feed_with_typical(self, median, days=5):
        store = MemoryStore()
        store.typical = [{"port_number": "240202", "lane": "car", "dow": 1, "hour": 21,
                          "median_minutes": median, "p75_minutes": median + 10, "days": days}]
        return feed_for("cbp_feed.json", storage=store), store

    def test_a_worse_than_usual_wait_says_so(self):
        feed, _ = self.feed_with_typical(25)
        body = feed.bridge("paso-del-norte", "car")          # 48 min
        self.assertEqual((body["typical"]["difference"], body["typical"]["compared"]), (23, "worse"))
        self.assertIn("Un lunes a esta hora suele estar en 25 min: hoy 23 min más.", body["reply"])

    def test_an_ordinary_day_is_not_mentioned(self):
        feed, _ = self.feed_with_typical(45)
        body = feed.bridge("paso-del-norte", "car")
        self.assertEqual(body["typical"]["compared"], "usual")
        self.assertNotIn("suele", body["reply"])

    def test_no_history_means_no_claim(self):
        self.assertIsNone(feed_for("cbp_feed.json").bridge("paso-del-norte", "car")["typical"])

    def test_the_baseline_is_not_re_read_on_every_refresh(self):
        feed, store = self.feed_with_typical(25)
        feed.snapshot()
        feed.snapshot(force=True)
        self.assertEqual(store.calls["typical_waits"], 1)

    def test_a_failed_read_is_retried_and_keeps_what_it_had(self):
        feed, store = self.feed_with_typical(25)
        feed.snapshot()
        store.typical_fails = True
        feed._typical_due = 0
        feed.snapshot(force=True)
        self.assertEqual(store.calls["typical_waits"], 2)
        self.assertIsNotNone(feed.bridge("paso-del-norte", "car")["typical"])


class TrendSurvivesARestart(unittest.TestCase):
    def test_movement_is_seeded_from_the_database(self):
        store = MemoryStore()
        store.readings.append({"port_number": "240202", "lane": "car", "state": "open",
                               "delay_minutes": 18, "content_hash": "older",
                               "captured_at": (NOW - timedelta(minutes=60)).isoformat()})
        feed = feed_for("cbp_feed.json", storage=store)          # fresh process, 48 now
        trend = feed.bridge("paso-del-norte", "car")["trend"]
        self.assertEqual((trend["change"], trend["over_minutes"], trend["worth_saying"]), (30, 60, True))

    def test_readings_outside_the_window_are_ignored(self):
        store = MemoryStore()
        store.readings.append({"port_number": "240202", "lane": "car", "state": "open",
                               "delay_minutes": 18, "content_hash": "older",
                               "captured_at": (NOW - timedelta(hours=5)).isoformat()})
        self.assertIsNone(feed_for("cbp_feed.json", storage=store).bridge("paso-del-norte", "car")["trend"])


class RealCrossingTimes(unittest.TestCase):
    def setUp(self):
        self.now = [NOW]
        self.store = MemoryStore()
        self.feed = feed_for("cbp_feed.json", storage=self.store, clock=lambda: self.now[0])

    def cross(self, port, minutes):
        started = self.feed.start_crossing(port, "car")
        self.now[0] += timedelta(minutes=minutes)
        return started, self.feed.finish_crossing(started["id"])

    def test_a_crossing_freezes_the_claims_and_measures_the_wait(self):
        started, done = self.cross("paso-del-norte", 55)
        self.assertEqual([c["source"] for c in started["claims"]], ["cbp"])
        self.assertEqual(done["actual_minutes"], 55)
        self.assertEqual(done["errors"], {"cbp": 48 - 55})
        self.assertIn("Tardaste 55 min", done["reply"])
        self.assertEqual(self.store.crossings[started["id"]]["actual_minutes"], 55)

    def test_accuracy_ranks_sources_by_error(self):
        self.cross("paso-del-norte", 55)
        report = self.feed.accuracy()
        self.assertEqual(report["crossings"], 1)
        self.assertEqual(report["sources"]["cbp"]["mean_abs_error"], 7.0)
        self.assertEqual(report["sources"]["cbp"]["bias"], -7.0)
        self.assertFalse(report["sources"]["cbp"]["enough_data"])

    def test_it_finishes_after_a_restart(self):
        started = self.feed.start_crossing("paso-del-norte", "car")
        restarted = feed_for("cbp_feed.json", storage=self.store,
                             clock=lambda: NOW + timedelta(minutes=40))
        self.assertEqual(restarted.finish_crossing(started["id"])["actual_minutes"], 40)

    def test_nonsense_is_refused(self):
        started = self.feed.start_crossing("paso-del-norte", "car")
        with self.assertRaises(CrossingError):
            self.feed.finish_crossing(started["id"])               # zero minutes
        with self.assertRaises(CrossingError):
            self.feed.finish_crossing("'; drop table border_crossings; --")
        with self.assertRaises(CrossingError):
            self.feed.start_crossing("zaragoza", "car")            # closed lane

    def test_over_http(self):
        status, started = api.route(self.feed, "/crossings", {}, "POST", {"port": "pdn", "lane": "car"})
        self.assertEqual(status, 201)
        self.now[0] += timedelta(minutes=30)
        status, done = api.route(self.feed, f"/crossings/{started['id']}/done", {}, "POST", {})
        self.assertEqual((status, done["actual_minutes"]), (200, 30))
        status, _ = api.route(self.feed, f"/crossings/{started['id']}/done", {}, "POST", {})
        self.assertEqual(status, 422)
        self.assertEqual(api.route(self.feed, "/accuracy", {})[1]["crossings"], 1)
        self.assertEqual(api.route(self.feed, "/crossings/nope/done", {}, "POST", {})[0], 404)

    def test_the_conversation(self):
        graph = fake_instagram.FakeGraph()
        pipeline = fake_instagram.FakePipeline(self.feed, graph)
        pipeline.on_message("u", "voy a cruzar paso del norte")
        self.assertIn("ya crucé", graph.calls[-1]["params"]["message"]["text"])
        self.now[0] += timedelta(minutes=50)
        pipeline.on_message("u", "ya crucé")
        self.assertIn("Tardaste 50 min", graph.calls[-1]["params"]["message"]["text"])


if __name__ == "__main__":
    unittest.main()
