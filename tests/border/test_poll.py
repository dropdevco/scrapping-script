"""The poller: one reading per run, retention on request, and a red run when writes fail.
"""

from __future__ import annotations

import contextlib
import io
import unittest
from unittest import mock

from border_support import (  # noqa: F401 - setUpModule/tearDownModule are hooks
    NOW,
    ROOT,
    MemoryStore,
    feed_for,
    raises,
    setUpModule,
    tearDownModule,
)

from scraper.border import poll
from scraper.border.service import BorderFeed, FeedError
from scraper.border.storage import BorderStore


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

class MissingTables:
    """A Supabase client for a database the migrations never reached: every query fails,
    the way PostgREST answers for a relation that does not exist."""

    def table(self, name):
        raise RuntimeError(f'relation "public.{name}" does not exist')

    def rpc(self, name, params):
        raise RuntimeError(f"function public.{name} does not exist")


class LoudFailures(unittest.IsolatedAsyncioTestCase):
    """Every write swallows its own failure, so without this a missing migration or a
    revoked key leaves the workflow green while nothing is recorded (ADR-0011)."""

    async def test_a_failed_write_turns_the_run_red(self):
        store = BorderStore(client=MissingTables())
        with mock.patch.object(poll, "BorderStore", return_value=store), \
             mock.patch.object(poll, "BorderFeed", lambda **options: feed_for("cbp_feed.json", **options)), \
             self.assertLogs("scraper.border", "ERROR"):
            self.assertEqual(await poll.run(quiet=True), 3)
        self.assertGreater(store.failures, 0)

    async def test_a_clean_run_with_storage_on_exits_zero(self):
        store = MemoryStore()
        with mock.patch.object(poll, "BorderStore", return_value=store), \
             mock.patch.object(poll, "BorderFeed", lambda **options: feed_for("cbp_feed.json", **options)):
            self.assertEqual(await poll.run(quiet=True), 0)
        self.assertEqual(store.calls["save_readings"], 1)


class Retention(unittest.IsolatedAsyncioTestCase):
    async def run_poll(self, store, **options):
        with mock.patch.object(poll, "BorderStore", return_value=store), \
             mock.patch.object(poll, "once", mock.AsyncMock(return_value={})), \
             contextlib.redirect_stdout(io.StringIO()):
            return await poll.run(quiet=True, **options)

    async def test_prune_runs_when_asked(self):
        store = MemoryStore()
        await self.run_poll(store, prune=True)
        self.assertEqual(store.calls["prune"], 1)

    async def test_a_single_reading_does_not_prune_by_default(self):
        store = MemoryStore()
        await self.run_poll(store)
        self.assertEqual(store.calls["prune"], 0)

    async def test_dry_run_never_prunes(self):
        store = MemoryStore()
        await self.run_poll(store, prune=True, dry_run=True)
        self.assertEqual(store.calls["prune"], 0)

    async def test_the_migration_keeps_enough_history_for_typical_waits(self):
        sql = (ROOT / "supabase" / "migrations" / "0015_border_alerts_crossings_typical.sql").read_text()
        self.assertIn("keep_readings_days int default 60", sql)
        self.assertIn("weeks int default 8", sql)
        self.assertGreaterEqual(poll.KEEP_READINGS_DAYS, 8 * 7)
