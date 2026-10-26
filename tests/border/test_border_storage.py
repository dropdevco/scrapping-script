"""BorderStore with Supabase unconfigured: a silent no-op, and rows the migration accepts.

The estimator's tables (0021) are exercised through Tables, an in-memory stand-in for the
Supabase client's query chain, so the real BorderStore methods and their failure count
run, not a copy of them. test_border_poll uses it for the same reason.
"""

from __future__ import annotations

import unittest
from datetime import timedelta
from unittest import mock

from border_support import (  # noqa: F401 - setUpModule/tearDownModule are hooks
    NOW,
    ROOT,
    load,
    setUpModule,
    tearDownModule,
)

from scraper.border import cbp, fusion
from scraper.border.signals import Signal
from scraper.border.storage import PAGE_ROWS, SIGNAL_COLUMNS, BorderStore

ESTIMATOR_SQL = ROOT / "supabase" / "migrations" / "0021_border_estimator.sql"


class _Result:
    """A response, and (for rpc) the request that is already answered."""

    def __init__(self, data):
        self.data = data

    def execute(self):
        return self


class _Query:
    """One request: an action (upsert, insert or select), then filters, order and range."""

    def __init__(self, db: Tables, name: str):
        self.db, self.name = db, name
        self.filters: list[tuple[str, str]] = []
        self.orders: list[tuple[str, bool]] = []
        self.window = (0, db.max_rows - 1)
        self.columns: list[str] | None = None
        self.action = None

    def upsert(self, rows, on_conflict, ignore_duplicates=False):
        keys = on_conflict.split(",")
        self.action = lambda: self.db.write(self.name, rows, keys, ignore_duplicates)
        return self

    def insert(self, rows):
        self.action = lambda: self.db.write(self.name, rows, None, False)
        return self

    def select(self, columns):
        self.columns = columns.split(",")
        self.action = self._select
        return self

    def gte(self, column, value):
        self.filters.append((column, value))
        return self

    def order(self, column, desc=False):
        self.orders.append((column, desc))
        return self

    def range(self, start, end):
        self.window = (start, end)
        return self

    def _select(self):
        rows = [r for r in self.db.rows[self.name] if all(r[c] >= v for c, v in self.filters)]
        for column, desc in reversed(self.orders):     # stable sorts, last key first
            rows.sort(key=lambda r, c=column: r[c], reverse=desc)
        start, end = self.window
        end = min(end, start + self.db.max_rows - 1)   # PostgREST's silent cap
        self.db.requests.append((self.name, "select", (start, end)))
        return [{c: r.get(c) for c in self.columns} for r in rows[start:end + 1]]

    def execute(self):
        return _Result(self.action())


class Tables:
    """The estimator's tables in memory, behind just enough of the Supabase client for
    storage.py and trust.save. Like the database it stamps captured_at on a new signal,
    and like PostgREST it returns at most max_rows rows a request and says nothing.

    missing names relations and functions the migrations never created: every call to
    one raises, the way PostgREST answers for them."""

    def __init__(self, clock=lambda: NOW, missing=(), max_rows: int = PAGE_ROWS):
        self.rows: dict[str, list[dict]] = {
            "border_signals": [], "border_estimates": [], "border_source_trust": []}
        self.clock, self.missing, self.max_rows = clock, set(missing), max_rows
        self.requests: list[tuple] = []

    def table(self, name):
        if name in self.missing:
            raise RuntimeError(f'relation "public.{name}" does not exist')
        return _Query(self, name)

    def rpc(self, name, params):
        if name in self.missing:
            raise RuntimeError(f"function public.{name} does not exist")
        self.requests.append((name, "rpc", params))
        return _Result({"signals": 0, "estimates": 0, "reports": 0})

    def write(self, name, rows, keys, ignore_duplicates):
        """An insert, or an upsert on keys. Returns the rows written, as PostgREST does
        with return=representation; a duplicate it ignored is not among them."""
        self.requests.append((name, "write", len(rows)))
        table, written = self.rows[name], []
        for row in rows:
            same = next((r for r in table if keys and all(r[k] == row[k] for k in keys)), None)
            if same is not None:
                if not ignore_duplicates:
                    same.update(row)
                    written.append(same)
                continue
            new = dict(row)
            if name == "border_signals":
                new["captured_at"] = self.clock().isoformat()
            table.append(new)
            written.append(new)
        return written

    def store(self, signal: Signal, read_at) -> None:
        """A signal already in the table, first read at read_at."""
        self.rows["border_signals"].append({**signal.to_row(), "captured_at": read_at.isoformat()})


def columns_of(sql: str, table: str) -> set[str]:
    """The column names a create table statement in sql declares."""
    body = sql.split(f"create table if not exists {table} (", 1)[1].split("\n);", 1)[0]
    return {line.split()[0] for line in body.strip().splitlines()
            if line.strip() and not line.strip().startswith(("--", "primary key"))}


def cbp_signal(minutes: int, at=NOW, lane="car", port="240202") -> Signal:
    return Signal(port_number=port, lane=lane, direction="north", kind="published",
                  source="cbp", state="open", minutes=minutes, observed_at=at, independent=True)


class StorageOff(unittest.IsolatedAsyncioTestCase):
    async def test_unconfigured_storage_is_a_silent_no_op(self):
        store = BorderStore(client=None)
        self.assertFalse(store.enabled)
        self.assertEqual((await store.save_readings(cbp.build(load("cbp_feed.json"), NOW))), 0)
        self.assertIsNone(await store.log_run("snapshot", None, 0, "ok"))
        self.assertEqual((await store.recent_readings("240202", "car")), [])

    async def test_the_estimator_s_calls_are_no_ops_too(self):
        store = BorderStore(client=None)
        self.assertEqual(await store.save_signals([cbp_signal(20).to_row()]), 0)
        self.assertEqual(await store.save_estimates([{"lane": "car"}]), 0)
        # None, not []: "storage is off" must never read as "no signals" or "no trust".
        self.assertIsNone(await store.signals_since(NOW.isoformat()))
        self.assertIsNone(await store.source_trust("2026-09-01"))
        self.assertIsNone(await store.prune_estimator())
        self.assertEqual(store.failures, 0)

    async def test_rows_match_the_migration_columns(self):
        snap = cbp.build(load("cbp_feed.json"), NOW)
        row = next(iter(snap.ports[0].lanes.values())).to_row()
        sql = (ROOT / "supabase" / "migrations" / "0013_border_waits.sql").read_text(encoding="utf-8")
        for column in row:
            self.assertIn(column, sql, f"{column} is written but not in the migration")


class EstimatorTables(unittest.IsolatedAsyncioTestCase):
    def test_what_is_read_and_written_are_columns_of_0021(self):
        sql = ESTIMATOR_SQL.read_text(encoding="utf-8")
        self.assertLessEqual(set(cbp_signal(20).to_row()), columns_of(sql, "border_signals"))
        self.assertLessEqual(set(SIGNAL_COLUMNS.split(",")), columns_of(sql, "border_signals"))
        estimate = fusion.fuse([cbp_signal(20)], NOW)
        self.assertLessEqual({*estimate.to_row(), "shown"}, columns_of(sql, "border_estimates"))
        self.assertLessEqual({"source", "direction", "day", "trust"},
                             columns_of(sql, "border_source_trust"))

    def test_the_prune_call_matches_the_function_0021_defines(self):
        sql = ESTIMATOR_SQL.read_text(encoding="utf-8")
        self.assertIn("function border_prune_estimator(keep_days int default 60", sql)
        self.assertIn("keep_reports_days int default 30", sql)

    async def test_a_claim_read_again_is_not_written_again(self):
        db = Tables()
        store = BorderStore(client=db)
        rows = [cbp_signal(20).to_row(), cbp_signal(25, lane="car_sentri").to_row()]
        self.assertEqual(await store.save_signals(rows), 2)
        self.assertEqual(await store.save_signals(rows), 0)
        self.assertEqual(len(db.rows["border_signals"]), 2)

    async def test_signals_come_back_oldest_first_with_when_they_were_first_read(self):
        db = Tables()
        store = BorderStore(client=db)
        newer, older = cbp_signal(20), cbp_signal(30, at=NOW - timedelta(minutes=50))
        await store.save_signals([newer.to_row(), older.to_row()])
        rows = await store.signals_since((NOW - timedelta(hours=1)).isoformat())
        self.assertEqual([Signal.from_row(r) for r in rows], [older, newer])
        self.assertTrue(all(r["captured_at"] == NOW.isoformat() for r in rows))
        self.assertEqual(await store.signals_since((NOW - timedelta(minutes=10)).isoformat()),
                         rows[1:])

    async def test_a_long_history_is_read_whole_not_cut_at_the_row_cap(self):
        db = Tables(max_rows=4)
        for i in range(10):
            db.store(cbp_signal(i, at=NOW - timedelta(minutes=i)), NOW)
        store = BorderStore(client=db)
        with mock.patch("scraper.border.storage.PAGE_ROWS", 4):
            rows = await store.signals_since((NOW - timedelta(hours=1)).isoformat())
        self.assertEqual(len(rows), 10)
        self.assertEqual(len({r["content_hash"] for r in rows}), 10)
        self.assertEqual([r for _, action, r in db.requests if action == "select"],
                         [(0, 3), (4, 7), (8, 11)])

    async def test_a_failed_page_fails_the_whole_read(self):
        class FailsOnSecondPage(Tables):
            def table(self, name):
                if sum(1 for r in self.requests if r[1] == "select") == 1:
                    raise RuntimeError("connection reset")
                return super().table(name)

        db = FailsOnSecondPage(max_rows=2)
        for i in range(3):
            db.store(cbp_signal(i, at=NOW - timedelta(minutes=i)), NOW)
        store = BorderStore(client=db)
        with mock.patch("scraper.border.storage.PAGE_ROWS", 2), \
             self.assertLogs("scraper.border.storage", "ERROR"):
            self.assertIsNone(await store.signals_since((NOW - timedelta(hours=1)).isoformat()))
        self.assertEqual(store.failures, 1)

    async def test_trust_is_read_from_a_day_on_newest_first(self):
        db = Tables()
        db.rows["border_source_trust"] = [
            {"source": "cbp", "direction": "north", "day": "2026-09-01", "trust": 0.5},
            {"source": "cbp", "direction": "north", "day": "2026-09-20", "trust": 0.7},
            {"source": "cbp", "direction": "north", "day": "2026-07-01", "trust": 0.2}]
        rows = await BorderStore(client=db).source_trust("2026-08-23")
        self.assertEqual([r["day"] for r in rows], ["2026-09-20", "2026-09-01"])

    async def test_estimates_are_added_never_replaced(self):
        db = Tables()
        store = BorderStore(client=db)
        row = {**fusion.fuse([cbp_signal(20)], NOW).to_row(), "shown": False}
        self.assertEqual(await store.save_estimates([row]), 1)
        self.assertEqual(await store.save_estimates([row]), 1)
        self.assertEqual(len(db.rows["border_estimates"]), 2)

    async def test_prune_names_both_windows(self):
        db = Tables()
        removed = await BorderStore(client=db).prune_estimator(keep_days=60, keep_reports_days=30)
        self.assertEqual(removed, {"signals": 0, "estimates": 0, "reports": 0})
        self.assertEqual(db.requests, [("border_prune_estimator", "rpc",
                                        {"keep_days": 60, "keep_reports_days": 30})])

    async def test_a_missing_table_is_counted_and_logged_never_raised(self):
        db = Tables(missing={"border_signals", "border_estimates", "border_source_trust",
                             "border_prune_estimator"})
        store = BorderStore(client=db)
        with self.assertLogs("scraper.border.storage", "ERROR"):
            self.assertEqual(await store.save_signals([cbp_signal(20).to_row()]), 0)
            self.assertIsNone(await store.signals_since(NOW.isoformat()))
            self.assertIsNone(await store.source_trust("2026-09-01"))
            self.assertEqual(await store.save_estimates([{"lane": "car"}]), 0)
            self.assertIsNone(await store.prune_estimator())
        self.assertEqual(store.failures, 5)
