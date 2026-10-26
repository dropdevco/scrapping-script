"""Trust per source and direction, earned only against timed crossings (SPEC §4.2).

CBP is graded like any other source, so agreeing with it earns nothing; the prior
outweighs the evidence until a source has ENOUGH_TO_JUDGE crossings; the "chisme"
consensus answer is never ranked among the sources it is made from; a direction with
no crossings stays at the prior; and a failed read of the crossings is not mistaken for
"no crossings", which would reset every source to its prior.
"""

from __future__ import annotations

import re
import unittest
import uuid
from datetime import date, datetime, timedelta
from types import SimpleNamespace

from border_support import (  # noqa: F401 - setUpModule/tearDownModule are hooks
    NOW,
    ROOT,
    MemoryStore,
    setUpModule,
    tearDownModule,
)

from scraper.border import fusion, trust
from scraper.border.crossings import ENOUGH_TO_JUDGE, MAX_PLAUSIBLE, CrossingLog
from scraper.border.signals import KINDS
from scraper.border.storage import BorderStore

DAY = date(2026, 9, 21)


def crossing(actual: int, claims: dict, shown: int | None = None, finished: datetime = NOW) -> dict:
    """A finished crossing as border_crossings stores it, with each source's frozen claim."""
    return {"id": str(uuid.uuid4()), "port_number": "240201", "lane": "car",
            "started_at": (finished - timedelta(minutes=actual)).isoformat(),
            "finished_at": finished.isoformat(), "actual_minutes": actual,
            "shown_minutes": shown, "reporter": None,
            "claims": [{"source": s, "minutes": m, "age_minutes": 0} for s, m in claims.items()]}


class FakeClient:
    """Enough of the Supabase client for one upsert: records it, echoes the rows back."""

    def __init__(self, fail: bool = False):
        self.fail, self.calls = fail, []

    def table(self, name):
        self.calls.append(("table", name))
        return self

    def upsert(self, rows, **kwargs):
        self.calls.append(("upsert", rows, kwargs))
        self._rows = rows
        return self

    def execute(self):
        if self.fail:
            raise RuntimeError("relation border_source_trust does not exist")
        return SimpleNamespace(data=list(self._rows))


class Store(MemoryStore):
    """MemoryStore's crossings, with the trust upsert going through a recording client.
    read_fails fails the crossings read the way BorderStore does: counted, and []."""

    def __init__(self, *crossings: dict, client: FakeClient | None = None, read_fails=False):
        super().__init__()
        self._client = client or FakeClient()       # trust.save goes through BorderStore._call
        self.crossings = {c["id"]: c for c in crossings}
        self.read_fails = read_fails

    async def crossings_since(self, since_iso):
        if self.read_fails:
            self.failures += 1
            return []
        return await super().crossings_since(since_iso)


def log_of(*crossings: dict) -> CrossingLog:
    return CrossingLog(Store(*crossings), lambda: NOW)


def by_source(rows: list[dict], direction: str = "north") -> dict[str, dict]:
    return {r["source"]: r for r in rows if r["direction"] == direction}


class Formula(unittest.TestCase):
    def test_the_published_priors_follow_the_spec(self):
        rows = by_source(trust.trust_rows({}, trust.published_roster(), DAY))
        self.assertEqual({s: r["prior"] for s, r in rows.items()},
                         {"cbp": 0.6, "pasosfronterizos": 0.4, "borderswaittime": 0.4})

    def test_an_ungraded_row_keeps_the_weight_fusion_already_gave(self):
        # Regression: trust kept its own table of priors and it disagreed with fusion's
        # (a position report 0.3 against 0.5), so the first n = 0 row cut that source's
        # weight with no crossing behind the change.
        for source in ("cbp", "pasosfronterizos", "cam:pdn-north", "tg"):
            for kind in KINDS:
                row, = trust.trust_rows({}, {(source, "north"): kind}, DAY)
                expected = fusion.prior(source, kind)
                self.assertEqual((row["prior"], row["trust"]), (expected, expected), (source, kind))

    def test_every_trust_is_one_fusion_accepts(self):
        # fusion refuses a trust outside (0, 1]; rounding must not push a bad source to 0.
        for n in (0, 1, ENOUGH_TO_JUDGE, 1000):
            for mae in (0, 5, MAX_PLAUSIBLE, 10_000):
                for source in ("cbp", "pasosfronterizos"):
                    row, = trust.trust_rows({"north": trust_report(**{source: (n, mae)})},
                                            {(source, "north"): "published"}, DAY)
                    self.assertTrue(0 < row["trust"] <= 1, (source, n, mae))

    def test_empirical_trust_matches_the_spec_table(self):
        for mae, expected in [(0, 1.0), (10, 0.5), (30, 0.25)]:
            self.assertAlmostEqual(trust.empirical(mae), expected)
        with self.assertRaises(ValueError):
            trust.empirical(-1)

    def test_with_no_crossings_trust_is_the_prior(self):
        self.assertEqual(trust.trust_from(0, None, 0.4), 0.4)
        with self.assertRaises(ValueError):
            trust.trust_from(3, None, 0.4)       # graded but no error is a broken report
        with self.assertRaises(ValueError):
            trust.trust_from(-1, 5, 0.4)

    def test_the_prior_outweighs_the_evidence_until_enough_to_judge(self):
        for prior in (0.3, 0.6):
            for mae in (0, 5, 30, 120):
                pull = trust.empirical(mae) - prior
                for n in range(1, ENOUGH_TO_JUDGE):
                    moved = trust.trust_from(n, mae, prior) - prior
                    self.assertLess(abs(moved), abs(pull) / 2 + 1e-12, (prior, mae, n))
                self.assertAlmostEqual(trust.trust_from(ENOUGH_TO_JUDGE, mae, prior) - prior, pull / 2)
                # With plenty of crossings the evidence wins.
                self.assertAlmostEqual(trust.trust_from(10_000, mae, prior), trust.empirical(mae), 2)

    def test_one_lucky_crossing_does_not_crown_a_site(self):
        rows = trust.trust_rows({"north": trust_report(pasosfronterizos=(1, 0.0))},
                                trust.published_roster(), DAY)
        ranked = [r["source"] for r in rows]
        self.assertEqual(ranked[0], "cbp")       # still at its prior, 0.6
        self.assertAlmostEqual(by_source(rows)["pasosfronterizos"]["trust"], round(5 / 11, 3))


def trust_report(**graded: tuple[int, float]) -> dict:
    """An accuracy() report with given (crossings, mean_abs_error) per source."""
    return {"days": 30, "crossings": max((n for n, _ in graded.values()), default=0),
            "sources": {s: {"crossings": n, "mean_abs_error": mae, "bias": 0.0,
                            "within_10": 1.0, "enough_data": n >= ENOUGH_TO_JUDGE}
                        for s, (n, mae) in graded.items()}}


class GradedAgainstCrossings(unittest.IsolatedAsyncioTestCase):
    """Synthetic crossings through the real CrossingLog.accuracy()."""

    async def rows(self, *crossings, roster=None):
        report = await log_of(*crossings).accuracy(trust.TRUST_WINDOW_DAYS)
        return trust.trust_rows({"north": report}, roster or trust.published_roster(), DAY)

    async def test_cbp_is_graded_like_any_other_source(self):
        # CBP says 20 and the mirror copies it; people actually wait 50, as pasos says.
        crossings = [crossing(50, {"cbp": 20, "borderswaittime": 20, "pasosfronterizos": 50},
                              shown=20) for _ in range(12)]
        rows = by_source(await self.rows(*crossings))
        self.assertEqual(rows["cbp"]["mae"], 30.0)
        self.assertEqual(rows["cbp"]["bias"], -30.0)
        self.assertAlmostEqual(rows["cbp"]["trust"], round((12 * 0.25 + 10 * 0.6) / 22, 3))
        self.assertLess(rows["cbp"]["trust"], 0.6)
        self.assertGreater(rows["pasosfronterizos"]["trust"], rows["cbp"]["trust"])
        # Agreeing with CBP earns nothing: the mirror is graded on the same crossings.
        self.assertLess(rows["borderswaittime"]["trust"], 0.4)
        self.assertEqual(rows["borderswaittime"]["mae"], 30.0)

    async def test_cbp_earns_its_trust_when_crossings_back_it(self):
        crossings = [crossing(30, {"cbp": 30, "pasosfronterizos": 55}) for _ in range(30)]
        rows = await self.rows(*crossings)
        north = by_source(rows)
        self.assertAlmostEqual(north["cbp"]["trust"], round((30 * 1 + 10 * 0.6) / 40, 3))
        self.assertEqual(north["cbp"]["within_10"], 1.0)
        self.assertEqual(north["pasosfronterizos"]["within_10"], 0.0)
        self.assertEqual(rows[0]["source"], "cbp")

    async def test_our_own_answer_is_graded_but_never_ranked(self):
        report = await log_of(crossing(30, {"cbp": 30}, shown=30)).accuracy(trust.TRUST_WINDOW_DAYS)
        self.assertIn("chisme", report["sources"])
        rows = trust.trust_rows({"north": report}, trust.published_roster(), DAY)
        self.assertNotIn("chisme", by_source(rows))

    async def test_a_source_with_no_crossings_stays_at_its_prior(self):
        rows = by_source(await self.rows(crossing(30, {"cbp": 25})))
        self.assertEqual(rows["pasosfronterizos"],
                         {"source": "pasosfronterizos", "direction": "north", "day": "2026-09-21",
                          "n": 0, "mae": None, "bias": None, "within_10": None,
                          "prior": 0.4, "trust": 0.4})

    async def test_a_retired_source_in_old_claims_gets_no_row(self):
        rows = by_source(await self.rows(crossing(30, {"cbp": 25, "bordertraffic": 30})))
        self.assertNotIn("bordertraffic", rows)

    async def test_southbound_stays_at_the_prior_while_crossings_are_northbound(self):
        roster = {**trust.published_roster(), ("cam:pdn-south", "south"): "measured",
                  ("cbp", "south"): "published"}
        rows = await self.rows(*[crossing(30, {"cbp": 30}) for _ in range(20)], roster=roster)
        south = by_source(rows, "south")
        self.assertEqual(south["cam:pdn-south"]["trust"], 0.5)
        # CBP's northbound record says nothing about a southbound line.
        self.assertEqual((south["cbp"]["n"], south["cbp"]["trust"]), (0, 0.6))
        self.assertEqual(by_source(rows)["cbp"]["n"], 20)

    def test_a_misnamed_direction_is_refused_not_ignored(self):
        # Silently filing evidence under "northbound" would leave every north row at its prior.
        with self.assertRaises(ValueError):
            trust.trust_rows({"northbound": trust_report(cbp=(12, 3.0))},
                             trust.published_roster(), DAY)
        with self.assertRaises(ValueError):
            trust.trust_rows({}, {("cbp", "sur"): "published"}, DAY)

    def test_the_roster_is_every_published_source_northbound(self):
        roster = trust.published_roster()
        self.assertEqual(set(roster), {("cbp", "north"), ("pasosfronterizos", "north"),
                                       ("borderswaittime", "north")})
        self.assertEqual(set(roster.values()), {"published"})

    def test_the_day_is_el_paso_s_not_utc_s(self):
        self.assertEqual(trust.el_paso_day(NOW), DAY)           # 03:40 UTC is 9:40 pm the day before
        with self.assertRaises(ValueError):
            trust.el_paso_day(NOW.replace(tzinfo=None))

    def test_rows_match_the_migration_columns(self):
        sql = (ROOT / "supabase" / "migrations" / "0021_border_estimator.sql").read_text(encoding="utf-8")
        table = re.search(r"create table if not exists border_source_trust \((.*?)\n\);", sql, re.DOTALL)
        columns = {line.split()[0] for line in table.group(1).splitlines()
                   if line.strip() and not line.strip().startswith(("primary", "--"))}
        row = trust.trust_rows({}, trust.published_roster(), DAY)[0]
        self.assertEqual(set(row), columns)


class Stored(unittest.IsolatedAsyncioTestCase):
    async def test_the_daily_run_upserts_one_row_per_source_and_direction(self):
        client = FakeClient()
        old = crossing(30, {"cbp": 90}, finished=NOW - timedelta(days=trust.TRUST_WINDOW_DAYS + 1))
        store = Store(crossing(30, {"cbp": 35}), old, client=client)
        self.assertEqual(await trust.recompute(store, NOW), 3)
        self.assertEqual(client.calls[0], ("table", "border_source_trust"))
        _, rows, kwargs = client.calls[1]
        self.assertEqual(kwargs, {"on_conflict": "source,direction,day"})
        self.assertEqual({(r["source"], r["direction"]) for r in rows}, set(trust.published_roster()))
        cbp = by_source(rows)["cbp"]
        self.assertEqual((cbp["n"], cbp["mae"], cbp["day"]), (1, 5.0, "2026-09-21"))  # the old one is out
        self.assertEqual(store.failures, 0)

    async def test_a_failed_read_writes_nothing(self):
        # Writing "no crossings" would reset every source to its prior for the day.
        client = FakeClient()
        store = Store(crossing(30, {"cbp": 35}), client=client, read_fails=True)
        self.assertEqual(await trust.recompute(store, NOW), 0)
        self.assertEqual(client.calls, [])
        self.assertEqual(store.failures, 1)

    async def test_a_failed_write_is_counted_not_raised(self):
        # Regression: recompute returned the 3 rows it computed, so a caller logging them
        # would report rows that never reached the table.
        client = FakeClient(fail=True)
        store = Store(client=client)
        self.assertEqual(await trust.recompute(store, NOW), 0)
        self.assertEqual(client.calls[1][0], "upsert")              # it did try
        self.assertEqual(store.failures, 1)

    async def test_with_storage_off_nothing_is_written(self):
        store = BorderStore(client=None)
        self.assertEqual(await trust.recompute(store, NOW), 0)
        self.assertEqual(await trust.save(store, trust.trust_rows({}, trust.published_roster(), DAY)), 0)
        self.assertEqual(await trust.save(Store(), []), 0)
        self.assertEqual(store.failures, 0)

    async def test_a_naive_clock_is_refused_before_anything_is_read(self):
        store = Store(read_fails=True)
        with self.assertRaises(ValueError):
            await trust.recompute(store, NOW.replace(tzinfo=None))
        self.assertEqual(store.failures, 0)


if __name__ == "__main__":
    unittest.main()
