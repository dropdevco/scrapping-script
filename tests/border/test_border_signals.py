"""The Signal contract (SPEC.md §3) and the Reading -> Signal adapter.

Every published reading must become a Signal without losing when it was true. The
fixture feeds carry the three cases that matter: a normal CBP stamp, a stamp ahead of
the clock (Bridge of the Americas "At 10:00 pm" read at 9:40 pm), and closures with no
stamp at all. Dating any of them wrong makes a stale number look fresh, or lets an old
"open" outrank a current closure.
"""

from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta

from border_support import FIXTURES, NOW, load

from scraper.border import cbp
from scraper.border.signals import (
    DIRECTIONS,
    FROM_PEOPLE,
    KINDS,
    PUBLISHED_DIRECTION,
    Signal,
    from_reading,
)
from scraper.border.sources.base import Reading
from scraper.border.sources.borderswaittime import parse as parse_mirror
from scraper.border.sources.cbp_source import readings_from
from scraper.border.sources.pasosfronterizos import parse as parse_pasos

REPORTER = "9f" * 32        # stands in for a sender's hash; the format is Phase 3's to choose
COLUMNS = {"port_number", "lane", "direction", "kind", "source", "state", "minutes", "low",
           "high", "observed_at", "independent", "reporter_hash", "evidence", "content_hash"}


def signal(**changes) -> Signal:
    """A valid measured signal, changed where a test needs it."""
    base = {"port_number": "240202", "lane": "car", "direction": "north", "kind": "measured",
            "source": "cam:pdn-north", "state": "open", "minutes": 30, "low": 25, "high": 40,
            "observed_at": NOW, "independent": True}
    return Signal(**{**base, **changes})


def cbp_readings(now: datetime = NOW) -> dict[tuple[str, str], Reading]:
    return {(r.port_number, r.lane): r for r in readings_from(cbp.build(load("cbp_feed.json"), now))}


class Contract(unittest.TestCase):
    def test_every_kind_in_both_directions_is_a_signal(self):
        for kind in KINDS:
            for direction in DIRECTIONS:
                with self.subTest(kind=kind, direction=direction):
                    reporter = REPORTER if kind in FROM_PEOPLE else None
                    s = signal(kind=kind, direction=direction, reporter=reporter)
                    self.assertEqual((s.kind, s.direction), (kind, direction))

    def test_malformed_signals_are_refused(self):
        cases = [
            ("unknown kind", {"kind": "rumour"}),
            ("unknown state", {"state": "slow"}),
            ("unknown direction", {"direction": "east"}),
            ("no direction", {"direction": None}),
            ("SPEC's `commercial` is not a lane id; `truck` is", {"lane": "commercial"}),
            ("a camera strip whose lane nobody confirmed", {"lane": None}),
            ("empty source", {"source": ""}),
            ("blank port", {"port_number": " "}),
            ("naive observed_at", {"observed_at": NOW.replace(tzinfo=None)}),
            ("observed_at as text", {"observed_at": NOW.isoformat()}),
            ("negative wait", {"minutes": -5, "low": None, "high": None}),
            ("fractional wait", {"minutes": 30.5}),
            ("a bool is not a wait", {"minutes": True, "low": None, "high": None}),
            ("low above high", {"minutes": None, "low": 45, "high": 40}),
            ("minutes outside its own range", {"minutes": 50}),
            ("minutes below its own floor", {"minutes": 20, "high": None}),
            ("a wait on a closed lane", {"state": "closed"}),
            ("a range on a no_data lane", {"state": "no_data", "minutes": None}),
            ("independent as text", {"independent": "yes"}),
            ("a published signal with a reporter", {"kind": "published", "reporter": REPORTER}),
            ("a measured signal with a reporter", {"reporter": REPORTER}),
            ("a crowd report with no reporter", {"kind": "crowd"}),
            ("a position report with no reporter", {"kind": "position"}),
            ("a crowd report with a blank reporter", {"kind": "crowd", "reporter": ""}),
            ("evidence that is not a dict", {"evidence": ["queue_m", 120]}),
        ]
        for why, changes in cases:
            with self.subTest(why), self.assertRaises(ValueError):
                signal(**changes)

    def test_open_ended_and_empty_claims_are_valid(self):
        # "llevo 40 min": still in line, so a floor (SPEC §5.3).
        still_in_line = signal(kind="crowd", source="ig_dm", reporter=REPORTER,
                               minutes=None, low=40, high=None)
        # A camera whose queue runs past its view: a wait with a floor and no ceiling.
        past_the_view = signal(minutes=50, low=45, high=None)
        # A stalled queue: open, moving too slowly to give any wait.
        stalled = signal(minutes=None, low=None, high=None)
        closed = signal(kind="published", source="cbp", state="closed",
                        minutes=None, low=None, high=None)
        self.assertEqual((still_in_line.low, still_in_line.high), (40, None))
        self.assertEqual((past_the_view.minutes, past_the_view.high), (50, None))
        self.assertIsNone(stalled.minutes)
        self.assertIsNone(closed.minutes)

    def test_zero_minutes_is_a_wait(self):
        # CBP's "no delay" is 0, not missing.
        self.assertEqual(signal(minutes=0, low=0, high=5).minutes, 0)

    def test_evidence_is_kept_as_it_was_given(self):
        evidence = {"queue_m": 120}
        s = signal(evidence=evidence)
        evidence["queue_m"] = 999
        self.assertEqual(s.evidence, {"queue_m": 120})


class FromReading(unittest.TestCase):
    def test_every_cbp_reading_becomes_a_published_northbound_signal(self):
        readings = cbp_readings()
        self.assertTrue(readings)
        for reading in readings.values():
            with self.subTest(port=reading.port_number, lane=reading.lane):
                s = from_reading(reading, NOW)
                self.assertEqual((s.kind, s.direction, s.source, s.independent),
                                 ("published", PUBLISHED_DIRECTION, "cbp", True))
                self.assertEqual((s.port_number, s.lane, s.state, s.minutes),
                                 (reading.port_number, reading.lane, reading.state, reading.minutes))
                self.assertLessEqual(s.observed_at, NOW)

    def test_a_stamped_reading_is_dated_by_its_stamp_not_the_read(self):
        s = from_reading(cbp_readings()[("240202", "car")], NOW)
        self.assertEqual(s.observed_at, datetime(2026, 9, 22, 3, 0, tzinfo=UTC))  # 9:00 pm MDT
        self.assertEqual(s.evidence, {"lanes_open": 1, "age_minutes": 40,
                                      "label": "At 9:00 pm MDT", "observed_at_from": "stamp"})

    def test_a_stamp_ahead_of_the_clock_counts_as_unknown_age(self):
        reading = cbp_readings()[("240201", "car")]
        self.assertGreater(datetime.fromisoformat(reading.observed_at), NOW)   # the trap itself
        s = from_reading(reading, NOW)
        self.assertEqual(s.observed_at, NOW - timedelta(minutes=cbp.UNKNOWN_AGE))
        self.assertEqual(s.evidence["observed_at_from"], "age")

    def test_a_closure_with_no_stamp_is_dated_when_it_was_read(self):
        reading = cbp_readings()[("240203", "car")]
        self.assertEqual((reading.state, reading.observed_at), ("closed", None))
        s = from_reading(reading, NOW)
        self.assertEqual((s.state, s.minutes, s.observed_at), ("closed", None, NOW))
        self.assertEqual(s.evidence, {"observed_at_from": "read"})

    def test_pasosfronterizos_is_dated_by_its_stated_age(self):
        page = (FIXTURES / "pasosfronterizos.html").read_text(encoding="utf-8")
        s = from_reading(parse_pasos(page, NOW)[0], NOW)
        self.assertEqual(s.observed_at, NOW - timedelta(minutes=8))   # "Actualizado hace 8 minutos"
        self.assertEqual((s.source, s.independent), ("pasosfronterizos", True))

    def test_the_mirror_is_dated_by_the_cbp_update_it_names_and_never_votes(self):
        page = (FIXTURES / "borderswaittime.html").read_text(encoding="utf-8")
        mirror = {r.port_number: r for r in parse_mirror(page, NOW)}
        s = from_reading(mirror["240202"], NOW)
        self.assertEqual(s.observed_at, datetime(2026, 9, 22, 1, 0, tzinfo=UTC))  # 7:00 pm MDT
        self.assertEqual((s.independent, s.evidence["observed_at_from"]), (False, "stamp"))
        closed = from_reading(mirror["240203"], NOW)
        self.assertEqual((closed.state, closed.observed_at), ("closed", NOW))

    def test_a_wait_with_no_time_at_all_is_never_fresh(self):
        reading = Reading("240202", "car", "open", 30, source="borderswaittime", independent=False)
        s = from_reading(reading, NOW)
        self.assertEqual(s.observed_at, NOW - timedelta(minutes=cbp.UNKNOWN_AGE))
        self.assertEqual(s.evidence["observed_at_from"], "assumed")

    def test_a_reading_the_contract_refuses_raises(self):
        cases = [
            ("negative wait", Reading("240202", "car", "open", -5, age_minutes=5, source="cbp")),
            ("no source name", Reading("240202", "car", "open", 30, age_minutes=5)),
            ("unknown lane", Reading("240202", "bus", "open", 30, age_minutes=5, source="cbp")),
            ("naive stamp", Reading("240202", "car", "open", 30, source="cbp",
                                    observed_at="2026-09-21T21:00:00")),
        ]
        for why, reading in cases:
            with self.subTest(why), self.assertRaises(ValueError):
                from_reading(reading, NOW)

    def test_the_read_time_must_be_aware(self):
        with self.assertRaises(ValueError):
            from_reading(cbp_readings()[("240202", "car")], NOW.replace(tzinfo=None))


class Rows(unittest.TestCase):
    def test_a_row_has_the_columns_of_border_signals(self):
        row = signal(kind="crowd", source="ig_dm", reporter=REPORTER).to_row()
        self.assertEqual(set(row), COLUMNS)
        self.assertEqual(row["reporter_hash"], REPORTER)
        self.assertEqual(row["observed_at"], "2026-09-22T03:40:00+00:00")

    def test_a_row_reads_back_as_the_same_signal(self):
        signals = [signal(evidence={"queue_m": 120.5, "speed_m_s": 0.4}),
                   signal(kind="crowd", source="ig_dm", reporter=REPORTER,
                          minutes=None, low=40, high=None),
                   from_reading(cbp_readings()[("240202", "car")], NOW),
                   from_reading(cbp_readings()[("240203", "car")], NOW)]
        for s in signals:
            with self.subTest(kind=s.kind, state=s.state):
                back = Signal.from_row(s.to_row())
                self.assertEqual(back, s)
                self.assertEqual(back.content_hash, s.content_hash)

    def test_a_row_as_postgrest_returns_it_reads_back(self):
        # CBP's stamp is local time; the database hands it back in UTC with its own columns.
        written = from_reading(cbp_readings()[("240202", "car")], NOW)
        self.assertEqual(written.observed_at.utcoffset(), timedelta(hours=-6))
        row = {**written.to_row(), "id": "6f1c0e52-0000-4000-8000-000000000000",
               "captured_at": "2026-09-22T03:40:07.12345+00:00",
               "observed_at": "2026-09-22T03:00:00Z"}
        back = Signal.from_row(row)
        self.assertEqual(back, written)
        self.assertEqual(back.content_hash, written.content_hash)

    def test_the_same_claim_read_again_hashes_the_same(self):
        # Ten minutes later CBP has not updated: the stated age grew, the claim did not.
        first, later = cbp_readings(NOW), cbp_readings(NOW + timedelta(minutes=10))
        a = from_reading(first[("240202", "car")], NOW)
        b = from_reading(later[("240202", "car")], NOW + timedelta(minutes=10))
        self.assertNotEqual(a.evidence, b.evidence)
        self.assertEqual(a.content_hash, b.content_hash)
        # A closure carries no stamp, so each read is a new observation of it.
        closed_a = from_reading(first[("240203", "car")], NOW)
        closed_b = from_reading(later[("240203", "car")], NOW + timedelta(minutes=10))
        self.assertNotEqual(closed_a.content_hash, closed_b.content_hash)

    def test_a_different_claim_hashes_differently(self):
        base = signal()
        for changes in ({"minutes": 31}, {"direction": "south"}, {"source": "cam:pdn-south"},
                        {"low": 24}, {"observed_at": NOW + timedelta(minutes=1)}):
            with self.subTest(**{k: str(v) for k, v in changes.items()}):
                self.assertNotEqual(signal(**changes).content_hash, base.content_hash)

    def test_a_camera_signal_reads_in(self):
        # The shape camera.reader.as_signal builds: no reporter_hash, no content_hash.
        row = {"port_number": "240202", "lane": "car", "direction": "north", "kind": "measured",
               "source": "cam:pdn-north", "state": "open", "minutes": 34, "low": 28, "high": None,
               "observed_at": "2026-09-21T21:39:30-06:00", "independent": True,
               "evidence": {"queue_m": 410.0, "lane_verified": True}}
        s = Signal.from_row(row)
        self.assertEqual((s.minutes, s.low, s.high, s.reporter), (34, 28, None, None))

    def test_a_row_without_independent_is_refused(self):
        # Defaulting it to True would let a mirror that lost the flag vote.
        row = signal().to_row()
        del row["independent"]
        with self.assertRaises(KeyError):
            Signal.from_row(row)


if __name__ == "__main__":
    unittest.main()
