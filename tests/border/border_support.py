"""Fakes and fixtures shared by the border tests.

The engine's tests are self-contained files with no conftest; this suite pins one
feed from a dozen angles, and copying MemoryStore, StubSource and FakeHttp into every
file would let the copies drift from the real BorderStore / Source / HttpClient.
Imported by name from each test module (tests/border has no __init__.py, so pytest
puts this directory on sys.path).
"""

from __future__ import annotations

import json
import logging
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

from scraper.border.service import BorderFeed
from scraper.border.sources.base import Source
from scraper.border.storage import BorderStore

ROOT = Path(__file__).resolve().parents[2]

FIXTURES = Path(__file__).resolve().parent / "fixtures"

NOW = datetime(2026, 9, 22, 3, 40, tzinfo=UTC)   # 9:40 pm MDT — Santa Teresa shuts at 10

MIDDAY = datetime(2026, 9, 22, 19, 40, tzinfo=UTC)  # 1:40 pm MDT — every bridge open for hours

def load(name: str) -> list[dict]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))

def feed_for(*files: str, now: datetime = NOW, **kwargs) -> BorderFeed:
    """A feed whose successive fetches return the given fixtures in order."""
    payloads = [load(f) for f in files]
    calls = {"n": 0}

    async def fetcher(_http):
        i = min(calls["n"], len(payloads) - 1)
        calls["n"] += 1
        return payloads[i]

    # extra_sources=[] by default: no test may depend on a website being up.
    options = {"clock": lambda: now, "cache_seconds": 0, "extra_sources": [],
               "storage": BorderStore(client=None), **kwargs}
    return BorderFeed(fetcher=fetcher, **options)

def returns(make):
    """A fetcher that answers with make() — the BorderFeed fetcher contract, minus CBP."""
    async def fetcher(_http):
        return make()
    return fetcher

def raises(error):
    async def fetcher(_http):
        raise error
    return fetcher

def lane(raw_status, minutes="", stamp="", lanes_open=""):
    return {"operational_status": raw_status, "delay_minutes": minutes,
            "update_time": stamp, "lanes_open": lanes_open}

class MemoryStore(BorderStore):
    """Supabase in a dict: enough of each table to exercise what survives a restart."""

    def __init__(self, readings=(), enabled=True):
        # enabled=False is Supabase unconfigured: every call is a no-op, as in production.
        super().__init__(client=object() if enabled else None)
        self.readings, self.alerts, self.crossings, self.typical = list(readings), {}, {}, []
        self.runs: list[str] = []
        self.calls: Counter = Counter()
        self.typical_fails = False

    async def upsert_ports(self):
        self.calls["upsert_ports"] += 1

    async def save_readings(self, snapshot):
        self.calls["save_readings"] += 1
        return 0

    async def upsert_current(self, rows):
        self.calls["upsert_current"] += 1
        self.current = {(r["port_number"], r["lane"]): r for r in rows}
        return len(rows)

    async def log_run(self, tool, snapshot, written, status, error=None, params=None):
        self.runs.append(status)

    async def recent_readings(self, port_number, lane, limit=3):
        self.calls["recent_readings"] += 1
        rows = [r for r in self.readings if r["port_number"] == port_number and r["lane"] == lane]
        return list(reversed(rows))[:limit]

    async def readings_since(self, since_iso):
        return [r for r in self.readings if r.get("captured_at", "") >= since_iso]

    async def typical_waits(self, weeks=8, min_days=3):
        self.calls["typical_waits"] += 1
        return None if self.typical_fails else self.typical

    async def prune(self, **kwargs):
        self.calls["prune"] += 1
        return {"readings": 0, "runs": 0, "alerts": 0}

    async def save_alert(self, row):
        self.alerts.setdefault(row["id"], dict(row))

    async def rearm_alert(self, alert_id, at_iso):
        self.alerts[alert_id]["rearmed_at"] = at_iso

    async def alerts_since(self, since_iso):
        return [dict(a) for a in self.alerts.values()
                if a["detected_at"] > since_iso or a["rearmed_at"] is None]

    async def save_crossing(self, row):
        self.crossings[row["id"]] = dict(row)

    async def finish_crossing(self, crossing_id, finished_iso, actual_minutes):
        self.crossings[crossing_id].update(finished_at=finished_iso, actual_minutes=actual_minutes)

    async def get_crossing(self, crossing_id):
        row = self.crossings.get(crossing_id)
        return dict(row) if row else None

    async def crossings_since(self, since_iso):
        return [dict(c) for c in self.crossings.values()
                if c.get("finished_at") and c["finished_at"] >= since_iso]

class Always60Store(MemoryStore):
    """Every lane's previous reading was 60 minutes, whatever is asked."""

    async def recent_readings(self, port_number, lane, limit=3):
        self.calls["recent_readings"] += 1
        return [{"port_number": port_number, "lane": lane, "state": "open",
                 "delay_minutes": 60, "content_hash": "older"}]

class StubSource(Source):
    """A source that answers with fixed readings and counts how often it was asked."""

    def __init__(self, readings=(), name="stub", independent=True, expected_ports=frozenset()):
        self.readings, self.name, self.independent = list(readings), name, independent
        self.expected_ports = frozenset(expected_ports)
        self.calls = 0

    async def fetch(self, http, now):
        self.calls += 1
        return self.readings

class FakeHttp:
    """Just enough of core.http.HttpClient: canned JSON, a page, a robots.txt verdict."""

    def __init__(self, payload=None, allowed=True, page=""):
        self.payload, self.allowed, self.page = payload, allowed, page
        self.requested: list[str] = []

    async def get_json(self, url, **kwargs):
        self.requested.append(url)
        return self.payload

    async def get_text(self, url, **kwargs):
        self.requested.append(url)
        return self.page

    async def can_fetch(self, url):
        return self.allowed

def setUpModule():
    # Failure paths are exercised on purpose; their warnings are the code working.
    logging.disable(logging.WARNING)

def tearDownModule():
    logging.disable(logging.NOTSET)
