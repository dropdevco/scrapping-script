"""The replay harness (Roadmap 2.5): both numbers graded against timed crossings, per
bridge and direction, with a verdict against the go-live target (Q-6, SPEC §8).

What matters most is that the estimate is graded on what could have been known when the
crossing began: a claim read after the start, the crossing's own day of trust, and a
southbound signal all stay out of it. The verdict is exact, never a rounded average.
Stored rows go through the real BorderStore reads over Tables, so the columns, the
paging and the failure count are the ones production uses; nothing touches a database.
"""

from __future__ import annotations

import contextlib
import io
import unittest
import uuid
from datetime import date, datetime, timedelta
from unittest import mock

from border_support import (  # noqa: F401 - setUpModule/tearDownModule are hooks
    NOW,
    MemoryStore,
    setUpModule,
    tearDownModule,
)
from test_border_storage import Tables

from scraper.border import __main__ as cli
from scraper.border import fusion, poll, replay, trust
from scraper.border.replay import Bridge, Graded, Score
from scraper.border.signals import Signal
from scraper.border.storage import BorderStore

PDN, BOTA = "240202", "240201"
TODAY = "2026-09-21"            # NOW is 9:40 pm on the 21st in El Paso, 03:40 UTC on the 22nd


def claim(minutes: int | None, at: datetime, source: str = "cbp", state: str = "open",
          port: str = PDN, lane: str = "car", direction: str = "north") -> Signal:
    return Signal(port_number=port, lane=lane, direction=direction, kind="published",
                  source=source, state=state, minutes=minutes, observed_at=at,
                  independent=True)


def stored(signal: Signal, read_at: datetime) -> dict:
    """A border_signals row as signals_since returns it: captured_at is the first read."""
    return {**signal.to_row(), "captured_at": read_at.isoformat()}


def crossing(started: datetime, actual: int | None, shown: int | None = None,
             port: str = PDN, lane: str = "car") -> dict:
    """A border_crossings row; actual None is a crossing still under way."""
    finished = None if actual is None else (started + timedelta(minutes=actual)).isoformat()
    return {"id": str(uuid.uuid4()), "port_number": port, "lane": lane,
            "started_at": started.isoformat(), "finished_at": finished,
            "actual_minutes": actual, "shown_minutes": shown, "claims": [], "reporter": None}


def trust_row(source: str, day: str, n: int = 0, mae: float | None = None) -> dict:
    """The border_source_trust row trust.trust_rows writes for one source on one day."""
    graded = {"crossings": n, "mean_abs_error": mae, "bias": 0.0, "within_10": 1.0}
    rows = trust.trust_rows({"north": {"sources": {source: graded}}}, trust.published_roster(),
                            date.fromisoformat(day))
    return next(row for row in rows if row["source"] == source)


# Thirty exact crossings lift pasosfronterizos from its prior of 0.4 to 0.85, past CBP's 0.6.
EARNED = {"n": 30, "mae": 0.0}


def scored(*errors: int) -> Score:
    """A record with these errors (claimed - actual), against an actual of 30 minutes."""
    score = Score()
    for error in errors:
        score = score.add(30 + error, 30)
    return score


def graded(estimate: int | None, actual: int = 30, today: int | None = None,
           port: str = PDN, lane: str = "car") -> Graded:
    return Graded(port_number=port, lane=lane, direction="north", started_at=NOW,
                  actual=actual, today=today, estimate=estimate)


class TheVerdictIsExact(unittest.TestCase):
    def test_errors_are_claimed_minus_actual_as_in_accuracy(self):
        score = Score().add(25, 20).add(10, 30)
        self.assertEqual((score.n, score.mae, score.bias, score.within_10), (2, 12.5, -7.5, 0.5))

    def test_an_average_error_of_exactly_five_passes(self):
        self.assertEqual(scored(*[5] * 30).shortfalls(), [])

    def test_an_average_error_that_rounds_to_five_still_fails(self):
        score = scored(*[5] * 29, 6)           # 151 / 30 = 5.03, printed as 5.0
        self.assertEqual(f"{score.mae:.1f}", "5.0")
        self.assertEqual(score.shortfalls(), ["average error 5.03 min, over 5"])

    def test_eighty_percent_within_ten_passes_and_one_fewer_fails(self):
        self.assertEqual(scored(*[0] * 24, *[11] * 6).shortfalls(), [])
        self.assertEqual(scored(*[0] * 23, *[11] * 7).shortfalls(),
                         ["76.7% within 10 min, under 80%"])

    def test_ten_minutes_off_is_within_ten(self):
        self.assertEqual(scored(10, -10).within, 2)

    def test_twenty_nine_perfect_crossings_are_not_enough(self):
        self.assertEqual(scored(*[0] * 29).shortfalls(), ["29 of 30 crossings graded"])

    def test_no_crossings_has_no_average_and_only_one_shortfall(self):
        self.assertIsNone(Score().mae)
        self.assertEqual(Score().shortfalls(), ["0 of 30 crossings graded"])


class FrozenAtTheStart(unittest.TestCase):
    """The estimate a crossing is graded on uses only what was read before it began."""

    def test_a_claim_read_after_the_start_is_not_known_yet(self):
        old = claim(20, NOW - timedelta(minutes=40))
        new = claim(50, NOW - timedelta(minutes=10))     # observed before the start ...
        rows = [stored(old, NOW - timedelta(minutes=37)),
                stored(new, NOW + timedelta(minutes=3))]  # ... but read after it
        at_start, later = replay.replay(
            [crossing(NOW, 30), crossing(NOW + timedelta(minutes=10), 30)], rows, [])
        self.assertEqual((at_start.estimate, later.estimate), (20, 50))

    def test_a_claim_older_than_fusion_s_history_is_left_out(self):
        old = claim(20, NOW - fusion.HISTORY - timedelta(minutes=1))
        [only] = replay.replay([crossing(NOW, 30, shown=25)], [stored(old, old.observed_at)], [])
        self.assertIsNone(only.estimate)
        self.assertEqual(only.today, 25)

    def test_a_lane_cbp_has_closed_has_no_estimate(self):
        shut = claim(None, NOW - timedelta(minutes=5), state="closed")
        [only] = replay.replay([crossing(NOW, 30)], [stored(shut, shut.observed_at)], [])
        self.assertIsNone(only.estimate)

    def test_a_southbound_signal_never_grades_a_crossing(self):
        north = claim(20, NOW - timedelta(minutes=5))
        south = claim(90, NOW - timedelta(minutes=5), source="maps", direction="south")
        rows = [stored(north, NOW - timedelta(minutes=4)), stored(south, NOW - timedelta(minutes=4))]
        [only] = replay.replay([crossing(NOW, 30)], rows, [])
        self.assertEqual((only.direction, only.estimate), ("north", 20))

    def test_unfinished_crossings_are_left_out_and_the_rest_come_oldest_first(self):
        rows = [crossing(NOW, 30), crossing(NOW - timedelta(hours=1), None),
                crossing(NOW - timedelta(hours=2), 40)]
        self.assertEqual([g.actual for g in replay.replay(rows, [], [])], [40, 30])

    def test_a_repeated_row_is_one_claim(self):
        cbp = claim(20, NOW - timedelta(minutes=5))
        row = stored(cbp, NOW - timedelta(minutes=4))
        [once] = replay.replay([crossing(NOW, 30)], [row], [])
        [twice] = replay.replay([crossing(NOW, 30)], [row, dict(row)], [])
        self.assertEqual(once, twice)

    def test_a_start_with_no_time_zone_is_refused(self):
        naive = crossing(NOW, 30)
        naive["started_at"] = NOW.replace(tzinfo=None).isoformat()
        with self.assertRaises(ValueError):
            replay.replay([naive], [], [])


def cbp_and_pasos() -> list[dict]:
    """CBP says 20 and pasosfronterizos 40 about Paso del Norte, equally fresh, both read
    before the crossing at NOW began."""
    cbp = claim(20, NOW - timedelta(minutes=5))
    pasos = claim(40, NOW - timedelta(minutes=5), source="pasosfronterizos")
    return [stored(cbp, NOW - timedelta(minutes=4)), stored(pasos, NOW - timedelta(minutes=4))]


class TrustAsItStoodTheDayBefore(unittest.TestCase):
    """At their priors (CBP 0.6, pasos 0.4) the weighted median of cbp_and_pasos is CBP's
    20; once pasos has EARNED 0.85 it is 40."""

    def estimate(self, trust_rows: list[dict]) -> int:
        [only] = replay.replay([crossing(NOW, 30)], cbp_and_pasos(), trust_rows)
        return only.estimate

    def test_with_no_rows_every_source_is_at_its_prior(self):
        self.assertEqual(self.estimate([]), 20)

    def test_the_day_before_s_row_weighs_the_crossing(self):
        self.assertEqual(self.estimate([trust_row("pasosfronterizos", "2026-09-20", **EARNED)]), 40)

    def test_the_crossing_s_own_day_is_never_used(self):
        # That row may already count this very crossing (replay.trust_at).
        self.assertEqual(self.estimate([trust_row("pasosfronterizos", TODAY, **EARNED)]), 20)

    def test_a_row_from_before_the_trust_window_is_not_used(self):
        self.assertEqual(self.estimate([trust_row("pasosfronterizos", "2026-08-21", **EARNED)]), 20)
        self.assertEqual(self.estimate([trust_row("pasosfronterizos", "2026-08-22", **EARNED)]), 40)

    def test_the_newest_row_in_the_window_wins(self):
        rows = [trust_row("pasosfronterizos", "2026-09-19", **EARNED),
                trust_row("pasosfronterizos", "2026-09-20")]
        self.assertEqual(self.estimate(rows), 20)

    def test_a_prior_changed_since_the_rows_were_written_is_replayed(self):
        # The daily job's rows while no crossing has graded anyone: each trust is the prior.
        # Taken as stored, CBP would keep its old 0.6 and the change would show nothing.
        rows = trust.trust_rows({}, trust.published_roster(), date(2026, 9, 20))
        self.assertEqual(self.estimate(rows), 20)
        with mock.patch.object(fusion, "AUTHORITY_PRIOR", 0.3):     # now under pasos' 0.4
            self.assertEqual(self.estimate(rows), 40)

    def test_a_changed_k_is_replayed(self):
        rows = [trust_row("pasosfronterizos", "2026-09-20", **EARNED)]
        with mock.patch.object(trust, "PRIOR_CROSSINGS", 100):      # pasos back down to 0.54
            self.assertEqual(self.estimate(rows), 20)

    def test_with_nothing_changed_replay_weighs_as_the_poller_did(self):
        graded = {"cbp": {"crossings": 7, "mean_abs_error": 9.1, "bias": 2.0, "within_10": 0.71},
                  "pasosfronterizos": {"crossings": 12, "mean_abs_error": 3.4, "bias": -1.0,
                                       "within_10": 0.92}}
        rows = trust.trust_rows({"north": {"sources": graded}}, trust.published_roster(),
                                date(2026, 9, 20))
        self.assertEqual(replay.trust_at(rows, date(2026, 9, 21)), poll.latest_trust(rows))

    def test_a_source_the_roster_no_longer_names_keeps_its_stored_trust(self):
        retired = {"source": "retired", "direction": "north", "day": "2026-09-20",
                   "n": 0, "mae": None, "trust": 0.9}
        self.assertEqual(replay.trust_at([retired], date(2026, 9, 21)), {("retired", "north"): 0.9})


class Efficiency(unittest.TestCase):
    def test_each_stored_claim_is_hashed_once_however_many_crossings(self):
        real, hashed = Signal.content_hash.fget, []

        def counted(signal: Signal) -> str:
            hashed.append(signal)
            return real(signal)

        # Read after every crossing began, so each crossing filters them all and knows none.
        late = [stored(claim(20 + i, NOW + timedelta(minutes=i)), NOW + timedelta(hours=1))
                for i in range(20)]

        def hashes(crossings: int) -> int:
            hashed.clear()
            with mock.patch.object(Signal, "content_hash", property(counted)):
                replay.replay([crossing(NOW - timedelta(minutes=i), 30) for i in range(crossings)],
                              late, [])
            return len(hashed)

        self.assertEqual(hashes(10), hashes(1))


class PerBridgeAndDirection(unittest.TestCase):
    def test_lanes_pool_into_their_bridge_and_bridges_stay_apart(self):
        bridges = replay.grade([graded(30, lane="car"), graded(30, lane="walk"),
                                graded(30, port=BOTA)])
        self.assertEqual([(b.port_number, b.direction, b.crossings) for b in bridges],
                         [(BOTA, "north", 1), (PDN, "north", 2)])

    def test_each_number_is_graded_only_where_it_had_one(self):
        [pdn] = replay.grade([graded(None, today=40), graded(35, today=None)])
        self.assertEqual((pdn.crossings, pdn.today.n, pdn.estimate.n), (2, 1, 1))
        self.assertEqual((pdn.today.bias, pdn.estimate.bias), (10, 5))

    def test_the_verdict_is_the_estimate_s_alone(self):
        [good_today] = replay.grade([graded(None, today=30)] * 30)
        self.assertFalse(good_today.passes)
        [good_estimate] = replay.grade([graded(32, today=60)] * 30)
        self.assertTrue(good_estimate.passes)
        self.assertTrue(good_estimate.today.shortfalls())


class TheReport(unittest.TestCase):
    def test_it_prints_both_numbers_and_a_verdict_per_bridge(self):
        passing = Bridge(PDN, "north", 31, scored(*[12] * 31), scored(*[2] * 30))
        failing = Bridge(BOTA, "north", 3, scored(0, 0, 0), scored(0, 0))
        text = replay.render([failing, passing], NOW)
        self.assertIn("in the 30 days to 2026-09-21 21:40 El Paso time", text)
        self.assertIn("Paso del Norte (240202), north: 31 crossings\n"
                      "  today     31 graded, error 12.0 min, bias +12.0 min, within 10 min 0%\n"
                      "  estimate  30 graded, error 2.0 min, bias +2.0 min, within 10 min 100%\n"
                      "  pass", text)
        self.assertIn("Bridge of the Americas (240201), north: 3 crossings", text)
        self.assertIn("  FAIL: 2 of 30 crossings graded", text)

    def test_a_number_with_no_crossings_says_so(self):
        text = replay.render([Bridge(PDN, "north", 1, scored(0), Score())], NOW)
        self.assertIn("  estimate  0 graded\n  FAIL: 0 of 30 crossings graded", text)

    def test_no_crossings_at_all_is_said_out_loud(self):
        self.assertIn("No finished crossings to grade, so no bridge passes.",
                      replay.render([], NOW))


class Store(MemoryStore):
    """MemoryStore's crossings, with Tables behind the real BorderStore reads of signals
    and trust (as test_border_poll's EstimatorStore does)."""

    def __init__(self, db: Tables | None = None):
        super().__init__()
        self.db = db or Tables()
        self._client = self.db

    def add_crossing(self, row: dict) -> None:
        self.crossings[row["id"]] = row


def thirty_crossings(store: Store) -> None:
    """Thirty crossings at Paso del Norte, two hours apart. CBP's claim read three minutes
    before each start is two minutes long; the answer the follower was told, twelve."""
    for i in range(30):
        start, actual = NOW - timedelta(hours=2 * i), 20 + i
        cbp = claim(actual + 2, start - timedelta(minutes=5))
        store.db.store(cbp, start - timedelta(minutes=3))
        store.add_crossing(crossing(start, actual, shown=actual + 12))


def thirty_crossings_without_signals(store: Store) -> None:
    for i in range(30):
        store.add_crossing(crossing(NOW - timedelta(hours=2 * i), 20, shown=20))


class FromTheStore(unittest.IsolatedAsyncioTestCase):
    LATER = NOW + timedelta(hours=1)

    async def run_replay(self, store: BorderStore) -> tuple[int, str]:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = await replay.run(store, self.LATER)
        return code, out.getvalue()

    async def test_thirty_close_estimates_pass_where_today_s_answer_does_not(self):
        store = Store()
        thirty_crossings(store)
        code, text = await self.run_replay(store)
        self.assertEqual(code, 0)
        self.assertIn("Paso del Norte (240202), north: 30 crossings\n"
                      "  today     30 graded, error 12.0 min, bias +12.0 min, within 10 min 0%\n"
                      "  estimate  30 graded, error 2.0 min, bias +2.0 min, within 10 min 100%\n"
                      "  pass", text)

    async def test_crossings_finished_before_the_window_are_not_graded(self):
        store = Store()
        thirty_crossings(store)
        old = crossing(NOW - timedelta(days=31), 30, shown=30)
        store.add_crossing(old)
        self.assertEqual(len(await replay.collect(store, self.LATER)), 30)

    async def test_no_crossings_reads_no_signals(self):
        store = Store()
        code, text = await self.run_replay(store)
        self.assertEqual(code, 0)
        self.assertIn("No finished crossings", text)
        self.assertEqual(store.db.requests, [])

    async def test_a_failed_signals_read_gives_no_verdict(self):
        store = Store(Tables(missing={"border_signals"}))
        thirty_crossings_without_signals(store)
        code, text = await self.run_replay(store)
        self.assertEqual((code, text), (1, ""))
        self.assertEqual(store.failures, 1)

    async def test_a_failed_trust_read_gives_no_verdict(self):
        store = Store(Tables(missing={"border_source_trust"}))
        thirty_crossings_without_signals(store)
        self.assertEqual(await self.run_replay(store), (1, ""))
        self.assertEqual(store.failures, 1)

    async def test_stored_trust_weighs_the_estimate_from_the_first_day_of_its_window(self):
        # The crossing's El Paso day is TODAY, so the trust read starts thirty days before.
        for day, expected in (("2026-08-21", 20), ("2026-08-22", 40)):
            with self.subTest(day=day):
                store = Store()
                store.db.rows["border_signals"].extend(cbp_and_pasos())
                store.db.rows["border_source_trust"].append(
                    trust_row("pasosfronterizos", day, **EARNED))
                store.add_crossing(crossing(NOW, 30))
                [only] = await replay.collect(store, self.LATER)
                self.assertEqual(only.estimate, expected)

    async def test_a_trust_read_at_the_row_cap_gives_no_verdict(self):
        store = Store()
        store.add_crossing(crossing(NOW, 30))
        store.db.rows["border_source_trust"].extend(
            trust.trust_rows({}, trust.published_roster(), date(2026, 9, 20)))
        with (mock.patch.object(replay, "PAGE_ROWS", 3),
              self.assertLogs("scraper.border.replay", "ERROR") as logs):
            self.assertIsNone(await replay.collect(store, self.LATER))
        self.assertIn("border_source_trust returned 3 rows", logs.output[0])

    async def test_a_failed_crossings_read_is_not_mistaken_for_none(self):
        class CrossingsFail(Store):
            async def crossings_since(self, since_iso):
                self.failures += 1          # as BorderStore._call counts it
                return []

        self.assertEqual(await self.run_replay(CrossingsFail()), (1, ""))

    async def test_a_crossings_read_at_the_row_cap_gives_no_verdict(self):
        store = Store()
        thirty_crossings(store)
        with mock.patch.object(replay, "PAGE_ROWS", 30):
            self.assertIsNone(await replay.collect(store, self.LATER))

    async def test_storage_off_is_exit_two(self):
        code, text = await self.run_replay(BorderStore(client=None))
        self.assertEqual((code, text), (2, ""))
        self.assertIsNone(await replay.collect(BorderStore(client=None), self.LATER))


class TheCommand(unittest.TestCase):
    def test_python_m_scraper_border_replay_runs_it(self):
        self.assertEqual(cli.COMMANDS["replay"], "scraper.border.replay")
        # Never a real store in a test: storage off is the exit code that proves the path.
        with mock.patch.object(replay, "BorderStore", lambda: BorderStore(client=None)):
            self.assertEqual(cli.main(["replay"]), 2)

    def test_it_takes_no_arguments(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            replay.main(["--days", "7"])


if __name__ == "__main__":
    unittest.main()
