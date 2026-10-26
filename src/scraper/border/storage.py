"""Persistence for the border_* tables (migrations 0013-0017, and 0021 for the estimator).

Same contract as core/storage.py: with SUPABASE_URL / SUPABASE_KEY unset the whole
layer is a no-op and the feed still answers with live numbers, and a failed write is
logged, never raised — a broken database must never stop an answer. The client is
the engine's own (built by core.storage.Storage from the same settings), and since
it is synchronous every call is pushed to a thread, as core/storage.py does.

Needs the service-role key, which bypasses RLS; every border_ table is deny-all for anon.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from ..core.storage import Storage
from .cbp import PORTS, Snapshot

log = logging.getLogger("scraper.border.storage")

# PostgREST returns at most this many rows a request and says nothing when it cut an
# answer short (the same cap the engine's backfills page around). ASSUMPTION: the
# project keeps Supabase's default max_rows of 1000; a lower cap would end a paged read
# after its first page, so it must not be set below this.
PAGE_ROWS = 1000
# What fusion needs of a stored signal (Signal.from_row), and captured_at, when we first
# read it. evidence is left out: it is for audit, and fusion never reads it.
SIGNAL_COLUMNS = ("port_number,lane,direction,kind,source,state,minutes,low,high,"
                  "observed_at,independent,reporter_hash,content_hash,captured_at")


_FROM_SETTINGS = object()


class BorderStore:
    def __init__(self, client: Any = _FROM_SETTINGS) -> None:
        # By default the engine builds the client from SUPABASE_URL / SUPABASE_KEY, or
        # leaves it None when they are unset. client=None asks for no storage at all
        # (a dry run, the selfcheck); any other value is used as the client.
        self._client = Storage().client if client is _FROM_SETTINGS else client
        # Every call swallows its own failure so an answer is never lost to the database,
        # which also makes a broken database look exactly like a quiet one. The count is
        # what lets the poller fail its run instead (ADR-0011).
        self.failures = 0

    @property
    def enabled(self) -> bool:
        return self._client is not None

    async def _call(self, what: str, query: Callable[[Any], Any]) -> Any:
        """Run one query in a thread. None means storage is off or the call failed,
        which callers that care (typical_waits) can tell apart from an empty answer."""
        if not self.enabled:
            return None
        try:
            return await asyncio.to_thread(lambda: query(self._client).execute().data)
        except Exception as exc:  # noqa: BLE001 - never let storage break an answer
            self.failures += 1
            log.error("%s failed: %s: %s", what, type(exc).__name__, exc)
            return None

    # -- writes ------------------------------------------------------------
    async def upsert_ports(self) -> None:
        """Keep border_ports in step with the code registry. Safe to call every run."""
        rows = [{"port_number": n, "name": m["name"], "name_es": m["name_es"], "sort_order": m["order"]}
                for n, m in PORTS.items()]
        await self._call("upsert_ports", lambda c: c.table("border_ports").upsert(
            rows, on_conflict="port_number"))

    async def save_readings(self, snapshot: Snapshot) -> int:
        """Insert lane readings. content_hash is UNIQUE, so polling between CBP updates
        writes nothing — that is the point, not a failure. Returns rows actually new."""
        rows = [lane.to_row() for port in snapshot.ports for lane in port.lanes.values()]
        if not rows:
            return 0
        written = await self._call("save_readings", lambda c: c.table("border_readings").upsert(
            rows, on_conflict="content_hash", ignore_duplicates=True))
        return len(written) if isinstance(written, list) else 0

    async def upsert_current(self, rows: list[dict]) -> int:
        """Replace each (bridge, lane) row of border_current_waits with its latest state.
        Written every run, changed or not: checked_at is what shows the data is live."""
        if not rows:
            return 0
        written = await self._call("upsert_current", lambda c: c.table("border_current_waits").upsert(
            rows, on_conflict="port_number,lane"))
        return len(written) if isinstance(written, list) else 0

    async def log_run(self, tool: str, snapshot: Snapshot | None, written: int, status: str) -> None:
        # Per-port open-lane counts are what tell "CBP reported nothing for this bridge"
        # apart from "our poll broke".
        counts = ({p.port_number: sum(1 for lane in p.lanes.values() if lane.state == "open")
                   for p in snapshot.ports} if snapshot else {})
        row = {"tool": tool, "params": {}, "port_counts": counts, "readings_written": written,
               "status": status, "started_at": datetime.now(UTC).isoformat()}
        await self._call("log_run", lambda c: c.table("border_runs").insert(row))

    async def prune(self, keep_readings_days: int = 60, keep_runs_days: int = 14,
                    keep_alerts_days: int = 30) -> dict | None:
        """Rows removed per table (border_prune in 0015), or None when off or failed."""
        return await self._call("prune", lambda c: c.rpc("border_prune", {
            "keep_readings_days": keep_readings_days, "keep_runs_days": keep_runs_days,
            "keep_alerts_days": keep_alerts_days}))

    # -- the estimator (0021) ------------------------------------------------
    async def save_signals(self, rows: list[dict]) -> int:
        """Insert border_signals rows (Signal.to_row). content_hash is UNIQUE, so a claim
        read again, such as CBP's between its hourly updates, writes nothing, as in
        save_readings. Returns rows actually new."""
        if not rows:
            return 0
        written = await self._call("save_signals", lambda c: c.table("border_signals").upsert(
            rows, on_conflict="content_hash", ignore_duplicates=True))
        return len(written) if isinstance(written, list) else 0

    async def signals_since(self, since_iso: str) -> list[dict] | None:
        """Every signal observed at or after a moment, oldest first, each with captured_at.
        Paged: at the fixtures' 29 new signals a poll, the three hours fusion reads come
        to roughly 550 rows [A], within reach of the cap and growing with every source
        added. None when off or any page failed, so the caller can tell that from no
        signals: a short history would quietly change the estimate."""
        if not self.enabled:
            return None
        rows: list[dict] = []
        while True:
            start = len(rows)
            page = await self._call("signals_since", lambda c, start=start: c.table("border_signals")
                                    .select(SIGNAL_COLUMNS).gte("observed_at", since_iso)
                                    .order("observed_at").order("content_hash")
                                    .range(start, start + PAGE_ROWS - 1))
            if page is None:
                return None
            rows.extend(page)
            if len(page) < PAGE_ROWS:
                return rows

    async def source_trust(self, since_day: str) -> list[dict] | None:
        """border_source_trust rows from an El Paso day on, newest first. None when off or
        failed: weighing every source at its prior because a read failed would look just
        like sources no crossing has graded yet."""
        return await self._call("source_trust", lambda c: c.table("border_source_trust")
                                .select("source,direction,day,trust").gte("day", since_day)
                                .order("day", desc=True))

    async def save_estimates(self, rows: list[dict]) -> int:
        """Insert one fusion run's border_estimates rows (Estimate.to_row). Every run adds
        rows rather than replacing them: grading needs the estimate that stood when each
        timed crossing began (SPEC §4.6). Returns the rows written."""
        if not rows:
            return 0
        written = await self._call("save_estimates", lambda c: c.table("border_estimates")
                                   .insert(rows))
        return len(written) if isinstance(written, list) else 0

    async def prune_estimator(self, keep_days: int = 60, keep_reports_days: int = 30) -> dict | None:
        """Rows removed per table by border_prune_estimator (0021): signals and estimates
        after keep_days, crowd messages and the signals made from them after Q-14's 30.
        None when off or failed."""
        return await self._call("prune_estimator", lambda c: c.rpc("border_prune_estimator", {
            "keep_days": keep_days, "keep_reports_days": keep_reports_days}))

    # -- reads -------------------------------------------------------------
    async def current_waits(self) -> list[dict]:
        """Every row of border_current_waits, for the knowledge-base sheet."""
        return await self._call("current_waits", lambda c: c.table("border_current_waits")
                                .select("*").order("port_number").order("lane")) or []

    async def recent_readings(self, port_number: str, lane: str, limit: int = 3) -> list[dict]:
        """Newest first. Used to tell a genuine drop from a first sighting."""
        return await self._call("recent_readings", lambda c: c.table("border_readings").select("*")
                                .eq("port_number", port_number).eq("lane", lane)
                                .order("captured_at", desc=True).limit(limit)) or []

    async def readings_since(self, since_iso: str) -> list[dict]:
        """Open readings captured after a moment, oldest first. Seeds the trend window."""
        return await self._call("readings_since", lambda c: c.table("border_readings")
                                .select("port_number,lane,delay_minutes,captured_at")
                                .eq("state", "open").gte("captured_at", since_iso)
                                .order("captured_at")) or []

    async def typical_waits(self, weeks: int = 8, min_days: int = 3) -> list[dict] | None:
        """Median wait per (port, lane, weekday, hour); see border_typical_waits.
        None when the call failed, so the caller can tell that from "no history yet"."""
        return await self._call("typical_waits", lambda c: c.rpc(
            "border_typical_waits", {"weeks": weeks, "min_days": min_days}))

    # -- alerts ------------------------------------------------------------
    async def save_alert(self, row: dict) -> None:
        """id is deterministic, so a second writer of the same alert changes nothing."""
        await self._call("save_alert", lambda c: c.table("border_alerts").upsert(
            [row], on_conflict="id", ignore_duplicates=True))

    async def rearm_alert(self, alert_id: str, at_iso: str) -> None:
        await self._call("rearm_alert", lambda c: c.table("border_alerts")
                         .update({"rearmed_at": at_iso}).eq("id", alert_id))

    async def alerts_since(self, since_iso: str) -> list[dict]:
        """Alerts detected after a moment, plus any still waiting to re-arm, oldest first."""
        return await self._call("alerts_since", lambda c: c.table("border_alerts").select("*")
                                .or_(f"detected_at.gt.{since_iso},rearmed_at.is.null")
                                .order("detected_at")) or []

    # -- crossings ---------------------------------------------------------
    async def save_crossing(self, row: dict) -> None:
        await self._call("save_crossing", lambda c: c.table("border_crossings").insert(row))

    async def finish_crossing(self, crossing_id: str, finished_iso: str, actual_minutes: int) -> None:
        await self._call("finish_crossing", lambda c: c.table("border_crossings")
                         .update({"finished_at": finished_iso, "actual_minutes": actual_minutes})
                         .eq("id", crossing_id))

    async def get_crossing(self, crossing_id: str) -> dict | None:
        rows = await self._call("get_crossing", lambda c: c.table("border_crossings")
                                .select("*").eq("id", crossing_id))
        return rows[0] if rows else None

    async def crossings_since(self, since_iso: str) -> list[dict]:
        return await self._call("crossings_since", lambda c: c.table("border_crossings")
                                .select("*").gte("finished_at", since_iso)
                                .order("finished_at")) or []
