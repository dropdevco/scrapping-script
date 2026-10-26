"""The poller: one reading per run, retention on request, and a red run when writes fail.

Since Roadmap 1.3 and 2.4 every run also stores its published readings as signals and
a hidden estimate per lane. The first thing proved here is the 'done when' of both: the
answer the poller already gives is exactly what it was, whatever the estimator does.
"""

from __future__ import annotations

import contextlib
import io
import json
import unittest
from datetime import datetime, timedelta
from unittest import mock

from border_support import (  # noqa: F401 - setUpModule/tearDownModule are hooks
    NOW,
    ROOT,
    MemoryStore,
    StubSource,
    feed_for,
    load,
    raises,
    setUpModule,
    tearDownModule,
)
from test_border_storage import Tables

from scraper.border import cbp, current, fusion, poll
from scraper.border.service import BorderFeed, FeedError
from scraper.border.signals import Signal
from scraper.border.sources import Reading, readings_from
from scraper.border.storage import BorderStore

CHECKED = NOW + timedelta(seconds=7)     # when the poll writes the answer, after reading
ESTIMATOR_TABLES = ("border_signals", "border_estimates", "border_source_trust",
                    "border_prune_estimator")


class _Frozen(datetime):
    """poll.datetime, stopped, so two polls write the same checked_at."""

    @classmethod
    def now(cls, tz=None):
        return CHECKED


def frozen():
    return mock.patch.object(poll, "datetime", _Frozen)


class EstimatorStore(MemoryStore):
    """MemoryStore for the tables the answer uses; Tables for the estimator's (0021), so
    the real BorderStore methods write them and count their failures."""

    def __init__(self, db: Tables | None = None, **kwargs):
        super().__init__(**kwargs)
        self.db = db or Tables()
        self._client = self.db

    def table(self, name: str) -> list[dict]:
        return self.db.rows[name]


def sites(pasos_fails: bool = False) -> list[StubSource]:
    """The two other published sources as they behave: pasosfronterizos fresher than CBP
    and disagreeing with it, borderswaittime copying CBP's 9:00 pm update."""
    pasos = StubSource([
        Reading("240202", "car", "open", 20, age_minutes=8, source="pasosfronterizos"),
        Reading("240203", "car", "open", 30, age_minutes=8, source="pasosfronterizos"),
        Reading("240801", "car", "open", 38, age_minutes=8, source="pasosfronterizos"),
    ], name="pasosfronterizos", expected_ports={"240202", "240203", "240801"})
    if pasos_fails:
        down(pasos)
    mirror = StubSource([Reading("240202", "car", "open", 48, source="borderswaittime",
                                 independent=False, label="At 9:00 pm MDT")],
                        name="borderswaittime", independent=False)
    return [pasos, mirror]


def down(source: StubSource) -> StubSource:
    """From now on every read of source fails, as a site that is down does."""
    async def fetch(http, now):
        raise OSError(f"{source.name} is down")
    source.fetch = fetch
    return source


def estimate_for(store: EstimatorStore, port: str = "240202", lane: str = "car") -> dict:
    rows = [r for r in store.table("border_estimates")
            if (r["port_number"], r["lane"]) == (port, lane)]
    return rows[-1]


class Poller(unittest.IsolatedAsyncioTestCase):
    async def test_one_pass_reports_what_it_saw(self):
        feed = feed_for("cbp_feed.json")
        result = await poll.once(feed, dry_run=True)
        self.assertEqual(result["ports"], 6)
        self.assertGreater(result["open_lanes"], 0)
        self.assertEqual(result["would_write"], result["lanes"])
        self.assertFalse(result["stored"])

    async def test_a_failed_read_raises_for_the_caller_to_handle(self):
        boom = raises(OSError("down"))
        feed = BorderFeed(storage=BorderStore(client=None), fetcher=boom,
                          clock=lambda: NOW, extra_sources=[])
        with self.assertRaises(FeedError):
            await poll.once(feed)


class TheAnswerIsUnchanged(unittest.IsolatedAsyncioTestCase):
    """Before: the feed's own reading made into border_current_waits rows, which is all
    the poller did. After: the same through the poller, with the estimator storing,
    failing on a missing migration, or failing in its own code."""

    async def before(self) -> tuple[dict, dict, dict]:
        feed = feed_for("cbp_feed.json", extra_sources=sites())
        snap = await feed.snapshot(force=True, allow_stale=False)
        rows = current.rows(feed, snap, CHECKED)
        return ({(r["port_number"], r["lane"]): r for r in rows},
                {key: d.to_dict() for key, d in feed.decisions.items()},
                {name: str(status) for name, status in feed.source_status.items()})

    async def after(self, store: EstimatorStore) -> tuple[dict, dict, dict, dict]:
        feed = feed_for("cbp_feed.json", extra_sources=sites(), storage=store)
        with frozen():
            result = await poll.once(feed)
        return (store.current, {key: d.to_dict() for key, d in feed.decisions.items()},
                result["sources"], result)

    async def test_the_answer_is_the_same_whatever_the_estimator_does(self):
        rows, decisions, statuses = await self.before()
        self.assertTrue(any(d["agreement"] == "conflict" for d in decisions.values()),
                        "the fixture must make the sources disagree, or this proves little")
        cases = {   # name: (store, what fusion raises, estimates stored)
            "stored": (EstimatorStore(), None, True),
            "migration 0021 missing": (EstimatorStore(Tables(missing=ESTIMATOR_TABLES)), None, False),
            "fusion raises": (EstimatorStore(), RuntimeError("fusion broke"), False),
        }
        for name, (store, error, stored) in cases.items():
            with self.subTest(name):
                with contextlib.ExitStack() as stack:
                    if error is not None:
                        stack.enter_context(mock.patch.object(poll.fusion, "fuse_all",
                                                              side_effect=error))
                    if not stored:
                        stack.enter_context(self.assertLogs("scraper.border", "ERROR"))
                    got_rows, got_decisions, got_statuses, result = await self.after(store)
                self.assertEqual(got_rows, rows)
                self.assertEqual(got_decisions, decisions)
                self.assertEqual(got_statuses, statuses)
                self.assertEqual(result["current_rows"], len(rows))
                self.assertEqual(bool(store.db.rows["border_estimates"]), stored)

    async def test_the_report_keeps_every_field_it_had(self):
        """The printed report gains one key, estimate, and every other is as it was."""
        before_store, after_store = EstimatorStore(), EstimatorStore()
        with mock.patch.object(poll, "estimate", mock.AsyncMock(return_value={})), frozen():
            before = await poll.once(feed_for("cbp_feed.json", extra_sources=sites(),
                                              storage=before_store))
        with frozen():
            after = await poll.once(feed_for("cbp_feed.json", extra_sources=sites(),
                                             storage=after_store))
        self.assertEqual(set(after) - {"estimate"},
                         {"at", "ms", "ports", "lanes", "open_lanes", "disputed", "sources",
                          "stored", "current_rows"})
        drop = ("ms", "estimate")
        self.assertEqual({k: v for k, v in after.items() if k not in drop},
                         {k: v for k, v in before.items() if k not in drop})
        self.assertEqual(before_store.table("border_signals"), [])
        self.assertGreater(len(after_store.table("border_signals")), 0)


class SignalsAndEstimates(unittest.IsolatedAsyncioTestCase):
    async def poll(self, store: EstimatorStore, sources=None, now=NOW) -> dict:
        feed = feed_for("cbp_feed.json", extra_sources=sites() if sources is None else sources,
                        storage=store, now=now)
        return (await poll.once(feed))["estimate"]

    async def test_every_published_reading_is_stored_once_as_a_signal(self):
        store = EstimatorStore()
        report = await self.poll(store)
        lanes = sum(len(p.lanes) for p in cbp.build(load("cbp_feed.json"), NOW).ports)
        rows = store.table("border_signals")
        self.assertEqual(len(rows), lanes + 3 + 1)      # CBP's lanes, pasosfronterizos, mirror
        self.assertEqual(report["signal_rows"], len(rows))
        self.assertEqual(report["refused"], 0)
        self.assertEqual({r["source"] for r in rows}, {"cbp", "pasosfronterizos", "borderswaittime"})
        self.assertTrue(all(r["kind"] == "published" and r["direction"] == "north" for r in rows))
        mirror = next(r for r in rows if r["source"] == "borderswaittime")
        self.assertFalse(mirror["independent"])         # stored, and never a vote

    async def test_every_lane_gets_one_hidden_estimate(self):
        store = EstimatorStore()
        report = await self.poll(store)
        snap = cbp.build(load("cbp_feed.json"), NOW)
        rows = store.table("border_estimates")
        self.assertEqual({(r["port_number"], r["lane"], r["direction"]) for r in rows},
                         {(p.port_number, lane, "north") for p in snap.ports for lane in p.lanes})
        self.assertEqual(len(rows), report["estimate_rows"])
        self.assertTrue(all(r["shown"] is False for r in rows))
        self.assertTrue(all(r["method"] == fusion.METHOD for r in rows))
        self.assertTrue(all(r["computed_at"] == NOW.isoformat() for r in rows))
        # pasosfronterizos' 20 is fresher than CBP's 40-minute-old 48 and outweighs it.
        self.assertEqual(estimate_for(store)["minutes"], 20)
        # CBP closes Ysleta's car lane; pasosfronterizos' 30 does not reopen it.
        self.assertEqual(estimate_for(store, "240203")["state"], "closed")

    async def test_each_poll_adds_estimates_and_only_the_claims_that_are_new(self):
        clock = {"now": NOW}
        store = EstimatorStore(Tables(clock=lambda: clock["now"]))
        first = await self.poll(store)
        clock["now"] = NOW + timedelta(minutes=10)
        second = await self.poll(store, now=clock["now"])
        # CBP's 9:00 pm numbers are the same claims; what is dated at the read is new.
        self.assertLess(second["signal_rows"], first["signal_rows"])
        self.assertGreater(second["signal_rows"], 0)
        self.assertEqual(len(store.table("border_estimates")),
                         first["estimate_rows"] + second["estimate_rows"])

    async def test_stored_trust_weighs_the_sources_and_the_newest_day_wins(self):
        store = EstimatorStore()
        store.table("border_source_trust").extend([
            {"source": "cbp", "direction": "north", "day": "2026-09-20", "trust": 0.9},
            {"source": "cbp", "direction": "north", "day": "2026-09-12", "trust": 0.2}])
        await self.poll(store)
        chosen = estimate_for(store)
        self.assertEqual(chosen["minutes"], 48)          # 20 at CBP's prior, 0.6
        cbp_vote = next(v for v in chosen["inputs"] if v["source"] == "cbp")
        self.assertEqual(cbp_vote["trust"], 0.9)

    async def test_a_source_down_this_poll_still_votes_with_its_last_word(self):
        db = Tables()
        last = Signal(port_number="240202", lane="car", direction="north", kind="published",
                      source="pasosfronterizos", state="open", minutes=20,
                      observed_at=NOW - timedelta(minutes=8), independent=True)
        db.store(last, NOW - timedelta(minutes=5))
        store = EstimatorStore(db)
        await self.poll(store, sources=sites(pasos_fails=True))
        chosen = estimate_for(store)
        self.assertEqual(chosen["minutes"], 20)
        self.assertIn(last.content_hash, [v["signal"] for v in chosen["inputs"]])

    async def test_a_reopening_read_after_a_closure_is_cbp_s_newest_word(self):
        """CBP closed Paso del Norte's car lane at 9:05 pm by our read, then reopened it with
        its 9:00 pm number. By stamp the closure is newer; by read the reopening is, and
        only the stored first read (captured_at) can tell fusion so."""
        db = Tables()
        closed_at = NOW - timedelta(minutes=35)
        db.store(Signal(port_number="240202", lane="car", direction="north", kind="published",
                        source="cbp", state="closed", observed_at=closed_at, independent=True),
                 closed_at)
        store = EstimatorStore(db)
        await self.poll(store, sources=[])
        chosen = estimate_for(store)
        self.assertEqual((chosen["state"], chosen["minutes"]), ("open", 48))

    async def test_cbp_repeating_a_claim_it_made_before_a_closure_has_reopened_the_lane(self):
        """The 9:00 pm 48 was first read at 9:03, before a closure read at 9:20. At 9:40 CBP
        shows the 48 again, and the answer written this poll says open 48. Dated by its first
        read the 48 lost to the closure, and the hidden estimate said closed."""
        made, _ = poll.published_signals(readings_from(cbp.build(load("cbp_feed.json"), NOW)), NOW)
        again = next(s for s in made if (s.port_number, s.lane) == ("240202", "car"))
        self.assertEqual((again.state, again.minutes), ("open", 48))
        db = Tables()
        db.store(again, NOW - timedelta(minutes=37))
        closed_at = NOW - timedelta(minutes=20)
        db.store(Signal(port_number="240202", lane="car", direction="north", kind="published",
                        source="cbp", state="closed", observed_at=closed_at, independent=True),
                 closed_at)
        store = EstimatorStore(db)
        await self.poll(store, sources=[])
        self.assertEqual(store.current[("240202", "car")]["state"], "open")   # the answer
        chosen = estimate_for(store)
        self.assertEqual((chosen["state"], chosen["minutes"]), ("open", 48))

    async def test_a_reading_the_contract_refuses_is_counted_and_the_rest_are_kept(self):
        odd = StubSource([Reading("240202", "bike", "open", 5, age_minutes=1, source="odd")],
                         name="odd")
        store = EstimatorStore()
        with self.assertLogs("scraper.border.poll", "ERROR") as logs:
            report = await self.poll(store, sources=[*sites(), odd])
        self.assertEqual(report["refused"], 1)
        self.assertIn("odd 240202 bike", logs.output[0])
        self.assertEqual(report["signal_rows"], len(store.table("border_signals")))
        self.assertGreater(report["estimate_rows"], 0)

    async def test_a_failed_signal_write_still_estimates_from_this_poll(self):
        class SignalWritesFail(Tables):
            def write(self, name, rows, keys, ignore_duplicates):
                if name == "border_signals":
                    raise RuntimeError("permission denied for table border_signals")
                return super().write(name, rows, keys, ignore_duplicates)

        store = EstimatorStore(SignalWritesFail())
        with self.assertLogs("scraper.border.storage", "ERROR"):
            report = await self.poll(store)
        self.assertEqual(report["failed_calls"], 1)
        self.assertEqual(report["signal_rows"], 0)
        self.assertEqual(estimate_for(store)["minutes"], 20)

    async def test_without_its_history_or_trust_no_estimate_is_stored(self):
        for missing in ("border_source_trust", "border_signals"):
            with self.subTest(missing):
                store = EstimatorStore(Tables(missing={missing}))
                with self.assertLogs("scraper.border.storage", "ERROR"):
                    report = await self.poll(store)
                self.assertEqual(report["estimate_rows"], 0)
                self.assertEqual(store.table("border_estimates"), [])
                self.assertGreater(report["failed_calls"], 0)

    async def test_a_dry_run_estimates_from_this_poll_and_writes_nothing(self):
        result = await poll.once(feed_for("cbp_feed.json", extra_sources=sites()), dry_run=True)
        lanes = sum(len(p.lanes) for p in cbp.build(load("cbp_feed.json"), NOW).ports)
        self.assertEqual(result["estimate"], {"signals": lanes + 4, "refused": 0,
                                              "signal_rows": 0, "estimates": lanes,
                                              "estimate_rows": 0, "failed_calls": 0})

    async def test_a_source_that_fails_now_brings_nothing_from_the_last_poll(self):
        flaky = StubSource([Reading("240202", "car", "open", 20, age_minutes=8,
                                    source="pasosfronterizos")], name="pasosfronterizos")
        feed = feed_for("cbp_feed.json", extra_sources=[flaky])
        first = (await poll.once(feed, dry_run=True))["estimate"]
        down(flaky)
        second = (await poll.once(feed, dry_run=True))["estimate"]
        self.assertEqual(first["signals"], second["signals"] + 1)
        self.assertIs(feed.extra_sources[0].inner, flaky)      # wrapped once, not twice


class Helpers(unittest.TestCase):
    def test_the_newest_trust_per_source_and_direction_whatever_the_order(self):
        rows = [{"source": "cbp", "direction": "north", "day": "2026-09-01", "trust": 0.5},
                {"source": "cbp", "direction": "north", "day": "2026-09-20", "trust": 0.7},
                {"source": "cbp", "direction": "south", "day": "2026-09-02", "trust": 0.3},
                {"source": "cbp", "direction": "north", "day": "2026-09-10", "trust": 0.9}]
        self.assertEqual(poll.latest_trust(rows), {("cbp", "north"): 0.7, ("cbp", "south"): 0.3})
        self.assertEqual(poll.latest_trust(list(reversed(rows))), poll.latest_trust(rows))
        self.assertEqual(poll.latest_trust([]), {})

    def test_a_claim_read_again_counts_as_read_now_and_one_not_repeated_keeps_its_read(self):
        def claim(source, minutes, ago):
            return Signal(port_number="240202", lane="car", direction="north", kind="published",
                          source=source, state="open", minutes=minutes,
                          observed_at=NOW - timedelta(minutes=ago), independent=True)

        again, new, unrepeated = claim("cbp", 48, 40), claim("pasosfronterizos", 20, 8), \
            claim("pasosfronterizos", 30, 18)
        stored = [{**again.to_row(), "captured_at": (NOW - timedelta(minutes=37)).isoformat()},
                  {**unrepeated.to_row(), "captured_at": (NOW - timedelta(minutes=10)).isoformat()}]
        signals, read = poll.merged(stored, [again, new], NOW)
        self.assertEqual(sorted(s.content_hash for s in signals),
                         sorted(s.content_hash for s in (again, new, unrepeated)))
        self.assertEqual(read, {again.content_hash: NOW, new.content_hash: NOW,
                                unrepeated.content_hash: NOW - timedelta(minutes=10)})

    def test_the_same_claim_twice_is_one_signal(self):
        reading = Reading("240202", "car", "open", 20, age_minutes=8, source="pasosfronterizos")
        made, refused = poll.published_signals([reading, reading], NOW)
        self.assertEqual((len(made), refused), (1, 0))


class MissingTables:
    """A Supabase client for a database the migrations never reached: every query fails,
    the way PostgREST answers for a relation that does not exist."""

    def table(self, name):
        raise RuntimeError(f'relation "public.{name}" does not exist')

    def rpc(self, name, params):
        raise RuntimeError(f"function public.{name} does not exist")


def as_poller(store, sources=None):
    """Patch run()'s store and feed: the fixture feed and the given sites, no network."""
    extra = {} if sources is None else {"extra_sources": sources}

    def feed(**options):
        return feed_for("cbp_feed.json", **{**options, **extra})

    stack = contextlib.ExitStack()
    stack.enter_context(mock.patch.object(poll, "BorderStore", return_value=store))
    stack.enter_context(mock.patch.object(poll, "BorderFeed", feed))
    return stack


class LoudFailures(unittest.IsolatedAsyncioTestCase):
    """Every write swallows its own failure, so without this a missing migration or a
    revoked key leaves the workflow green while nothing is recorded (ADR-0011)."""

    async def test_a_failed_write_turns_the_run_red(self):
        store = BorderStore(client=MissingTables())
        with as_poller(store), self.assertLogs("scraper.border", "ERROR"):
            self.assertEqual(await poll.run(quiet=True), 3)
        self.assertGreater(store.failures, 0)

    async def test_a_clean_run_with_storage_on_exits_zero(self):
        store = EstimatorStore()
        with as_poller(store):
            self.assertEqual(await poll.run(quiet=True), 0)
        self.assertEqual(store.calls["save_readings"], 1)
        self.assertEqual(store.calls["upsert_current"], 1)
        lanes = sum(len(p.lanes) for p in cbp.build(load("cbp_feed.json"), NOW).ports)
        self.assertEqual(len(store.current), lanes)    # every lane of every bridge
        self.assertEqual(len(store.table("border_estimates")), lanes)

    async def test_an_estimator_failure_alone_is_its_own_red(self):
        """The reading landed, so it is not code 3; nor is it green (ADR-0011)."""
        store = EstimatorStore(Tables(missing=ESTIMATOR_TABLES))
        with as_poller(store), self.assertLogs("scraper.border", "ERROR") as logs:
            self.assertEqual(await poll.run(quiet=True), 4)
        self.assertEqual(store.calls["upsert_current"], 1)
        self.assertIn("the answer is unaffected", logs.output[-1])
        self.assertRegex(logs.output[-1], r"estimate: \d+ failed database call")

    async def test_with_storage_off_the_run_says_so_whatever_the_estimator_reports(self):
        """Exit 2 is the news when nothing was stored; a refused reading once hid it
        behind exit 4 and a log line saying the reading was recorded."""
        with as_poller(BorderStore(client=None), sources=[]):
            self.assertEqual(await poll.run(quiet=True), 2)
        odd = StubSource([Reading("240202", "bike", "open", 5, source="odd")], name="odd")
        with as_poller(BorderStore(client=None), sources=[odd]), \
             self.assertLogs("scraper.border", "ERROR") as logs:
            self.assertEqual(await poll.run(quiet=True), 2)
        self.assertEqual(len(logs.output), 1)       # the refusal itself, named
        self.assertIn("signal refused: odd 240202 bike", logs.output[0])

    async def test_a_dry_run_with_a_refused_reading_is_red_and_claims_no_record(self):
        odd = StubSource([Reading("240202", "bike", "open", 5, source="odd")], name="odd")
        with as_poller(BorderStore(client=None), sources=[odd]), \
             self.assertLogs("scraper.border", "ERROR") as logs:
            self.assertEqual(await poll.run(dry_run=True, quiet=True), 4)
        self.assertIn("estimate: 1 reading(s) the Signal contract refused", logs.output[-1])
        self.assertNotIn("recorded", logs.output[-1])

    async def test_an_estimator_error_is_reported_and_the_run_goes_on(self):
        store = EstimatorStore()
        out = io.StringIO()
        with as_poller(store), contextlib.redirect_stdout(out), \
             mock.patch.object(poll.fusion, "fuse_all", side_effect=RuntimeError("fusion broke")), \
             self.assertLogs("scraper.border", "ERROR"):
            self.assertEqual(await poll.run(prune=True), 4)
        printed = json.loads(out.getvalue())
        self.assertEqual(printed["estimate"]["error"], "RuntimeError: fusion broke")
        self.assertEqual(store.calls["prune"], 1)            # the daily work still ran

    async def test_a_refused_reading_turns_the_run_red(self):
        odd = StubSource([Reading("240202", "bike", "open", 5, source="odd")], name="odd")
        with as_poller(EstimatorStore(), sources=[odd]), self.assertLogs("scraper.border", "ERROR"):
            self.assertEqual(await poll.run(quiet=True), 4)

    async def test_the_reading_s_failure_outranks_the_estimator_s(self):
        class CurrentFails(EstimatorStore):
            async def upsert_current(self, rows):
                self.failures += 1      # what BorderStore._call does with a failed write
                return 0

        store = CurrentFails(Tables(missing=ESTIMATOR_TABLES))
        with as_poller(store), self.assertLogs("scraper.border", "ERROR") as logs:
            self.assertEqual(await poll.run(quiet=True), 3)
        self.assertIn("may not have been recorded", logs.output[-1])


class Retention(unittest.IsolatedAsyncioTestCase):
    async def run_poll(self, store, **options):
        with mock.patch.object(poll, "BorderStore", return_value=store), \
             mock.patch.object(poll, "once", mock.AsyncMock(return_value={"estimate": {}})), \
             contextlib.redirect_stdout(io.StringIO()):
            return await poll.run(quiet=True, **options)

    async def test_prune_runs_when_asked(self):
        store = EstimatorStore()
        self.assertEqual(await self.run_poll(store, prune=True), 0)
        self.assertEqual(store.calls["prune"], 1)
        self.assertIn(("border_prune_estimator", "rpc", {"keep_days": 60, "keep_reports_days": 30}),
                      store.db.requests)

    async def test_the_daily_run_stores_each_source_s_trust(self):
        store = EstimatorStore()
        await self.run_poll(store, prune=True)
        rows = store.table("border_source_trust")
        self.assertEqual({r["source"] for r in rows}, {"cbp", "pasosfronterizos", "borderswaittime"})
        # No timed crossings yet, so every source is at its prior: weights do not move.
        self.assertTrue(all(r["n"] == 0 and r["trust"] == r["prior"] for r in rows))

    async def test_a_failed_trust_job_is_red_but_retention_still_ran(self):
        store = EstimatorStore()
        with mock.patch.object(poll.trust, "recompute", side_effect=ValueError("bad crossing")), \
             self.assertLogs("scraper.border", "ERROR") as logs:
            self.assertEqual(await self.run_poll(store, prune=True), 4)
        self.assertEqual(store.calls["prune"], 1)
        self.assertIn("daily job: ValueError: bad crossing", logs.output[-1])

    async def test_a_single_reading_does_not_prune_by_default(self):
        store = EstimatorStore()
        await self.run_poll(store)
        self.assertEqual(store.calls["prune"], 0)
        self.assertEqual(store.db.requests, [])
        self.assertEqual(store.table("border_source_trust"), [])

    async def test_dry_run_never_prunes(self):
        store = EstimatorStore()
        await self.run_poll(store, prune=True, dry_run=True)
        self.assertEqual(store.calls["prune"], 0)
        self.assertEqual(store.db.requests, [])

    async def test_the_migration_keeps_enough_history_for_typical_waits(self):
        sql = (ROOT / "supabase" / "migrations" / "0015_border_alerts_crossings_typical.sql").read_text(encoding="utf-8")
        self.assertIn("keep_readings_days int default 60", sql)
        self.assertIn("weeks int default 8", sql)
        self.assertGreaterEqual(poll.KEEP_READINGS_DAYS, 8 * 7)
