"""BorderStore with Supabase unconfigured: a silent no-op, and rows the migration accepts.
"""

from __future__ import annotations

import unittest

from border_support import (  # noqa: F401 - setUpModule/tearDownModule are hooks
    NOW,
    ROOT,
    load,
    setUpModule,
    tearDownModule,
)

from scraper.border import cbp
from scraper.border.storage import BorderStore


class StorageOff(unittest.IsolatedAsyncioTestCase):
    async def test_unconfigured_storage_is_a_silent_no_op(self):
        store = BorderStore(client=None)
        self.assertFalse(store.enabled)
        self.assertEqual((await store.save_readings(cbp.build(load("cbp_feed.json"), NOW))), 0)
        self.assertIsNone(await store.log_run("snapshot", None, 0, "ok"))
        self.assertEqual((await store.recent_readings("240202", "car")), [])

    async def test_rows_match_the_migration_columns(self):
        snap = cbp.build(load("cbp_feed.json"), NOW)
        row = next(iter(snap.ports[0].lanes.values())).to_row()
        sql = (ROOT / "supabase" / "migrations" / "0013_border_waits.sql").read_text(encoding="utf-8")
        for column in row:
            self.assertIn(column, sql, f"{column} is written but not in the migration")
