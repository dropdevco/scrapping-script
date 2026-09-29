"""Persistence for border_ports, border_readings and border_runs.

Follows storage.py in the scraper engine: degrades to a complete no-op when Supabase
is unconfigured, and write failures are logged, never raised — a broken database must
never stop the pipeline answering with live numbers.

Talks to PostgREST over urllib so the package stays dependency-free. Needs the
service-role key, which bypasses RLS; every border_ table is deny-all for anon.
"""
from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
from datetime import datetime, timezone
from urllib.parse import quote

from .cbp import PORTS, Snapshot

log = logging.getLogger(__name__)


class Storage:
    def __init__(self, url: str | None = None, key: str | None = None):
        self.url = (url or os.environ.get("SUPABASE_URL") or "").rstrip("/")
        self.key = key or os.environ.get("SUPABASE_SERVICE_KEY") or ""
        self.enabled = bool(self.url and self.key)
        if not self.enabled:
            log.info("Supabase unconfigured — storage disabled, live answers still work")

    # -- plumbing ----------------------------------------------------------
    def _request(self, method: str, path: str, body: list | dict | None = None,
                 prefer: str | None = None, timeout: int = 20):
        req = urllib.request.Request(
            f"{self.url}/rest/v1/{path}",
            method=method,
            data=json.dumps(body).encode() if body is not None else None,
            headers={
                "apikey": self.key,
                "Authorization": f"Bearer {self.key}",
                "Content-Type": "application/json",
                **({"Prefer": prefer} if prefer else {}),
            },
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            return json.loads(raw) if raw else []

    def _safe(self, what: str, method: str, path: str, **kwargs):
        if not self.enabled:
            return None
        try:
            return self._request(method, path, **kwargs)
        except urllib.error.HTTPError as exc:
            log.warning("%s failed: HTTP %s %s", what, exc.code, exc.read()[:300])
        except Exception as exc:  # never let storage break a run
            log.warning("%s failed: %s: %s", what, type(exc).__name__, exc)
        return None

    # -- writes ------------------------------------------------------------
    def upsert_ports(self) -> None:
        """Keep border_ports in step with the code registry. Safe to call every run."""
        rows = [{"port_number": n, "name": m["name"], "name_es": m["name_es"], "sort_order": m["order"]}
                for n, m in PORTS.items()]
        self._safe("upsert_ports", "POST", "border_ports?on_conflict=port_number",
                   body=rows, prefer="resolution=merge-duplicates,return=minimal")

    def save_readings(self, snapshot: Snapshot) -> int:
        """Insert lane readings. content_hash is UNIQUE, so polling between CBP
        updates writes nothing — that is the point, not a failure."""
        rows = [lane.to_row() for port in snapshot.ports for lane in port.lanes.values()]
        if not rows:
            return 0
        written = self._safe("save_readings", "POST", "border_readings?on_conflict=content_hash",
                             body=rows, prefer="resolution=ignore-duplicates,return=representation")
        return len(written) if isinstance(written, list) else 0

    def log_run(self, tool: str, snapshot: Snapshot | None, written: int,
                status: str, error: str | None = None, params: dict | None = None) -> None:
        counts = {}
        if snapshot:
            counts = {p.port_number: sum(1 for l in p.lanes.values() if l.state == "open")
                      for p in snapshot.ports}
        self._safe("log_run", "POST", "border_runs", body=[{
            "tool": tool,
            "params": params or {},
            "port_counts": counts,
            "readings_written": written,
            "status": status,
            "error": error,
            "started_at": datetime.now(timezone.utc).isoformat(),
        }], prefer="return=minimal")

    # -- reads -------------------------------------------------------------
    def recent_readings(self, port_number: str, lane: str, limit: int = 3) -> list[dict]:
        """Newest first. Used to tell a genuine drop from a first sighting."""
        path = (f"border_readings?port_number=eq.{port_number}&lane=eq.{lane}"
                f"&order=captured_at.desc&limit={limit}")
        return self._safe("recent_readings", "GET", path) or []

    def readings_since(self, since_iso: str) -> list[dict]:
        """Open readings captured after a moment, oldest first. Seeds the trend window."""
        path = ("border_readings?select=port_number,lane,delay_minutes,captured_at"
                f"&state=eq.open&captured_at=gte.{quote(since_iso)}&order=captured_at.asc")
        return self._safe("readings_since", "GET", path) or []

    def typical_waits(self, weeks: int = 8, min_days: int = 3) -> list[dict] | None:
        """Median wait per (port, lane, weekday, hour); see border_typical_waits.
        None when the call failed, so the caller can tell that from "no history yet"."""
        return self._safe("typical_waits", "POST", "rpc/border_typical_waits",
                          body={"weeks": weeks, "min_days": min_days})

    def prune(self, keep_readings_days: int = 60, keep_runs_days: int = 14,
              keep_alerts_days: int = 30) -> dict | None:
        """Rows removed per table, or None when storage is off or the call failed."""
        return self._safe("prune", "POST", "rpc/border_prune", body={
            "keep_readings_days": keep_readings_days, "keep_runs_days": keep_runs_days,
            "keep_alerts_days": keep_alerts_days})

    # -- alerts ------------------------------------------------------------
    def save_alert(self, row: dict) -> None:
        """id is deterministic, so a second writer of the same alert changes nothing."""
        self._safe("save_alert", "POST", "border_alerts?on_conflict=id", body=[row],
                   prefer="resolution=ignore-duplicates,return=minimal")

    def rearm_alert(self, alert_id: str, at_iso: str) -> None:
        self._safe("rearm_alert", "PATCH", f"border_alerts?id=eq.{quote(alert_id)}",
                   body={"rearmed_at": at_iso}, prefer="return=minimal")

    def alerts_since(self, since_iso: str) -> list[dict]:
        """Alerts detected after a moment, plus any still waiting to re-arm, oldest first."""
        path = (f"border_alerts?or=(detected_at.gt.{quote(since_iso)},rearmed_at.is.null)"
                "&order=detected_at.asc")
        return self._safe("alerts_since", "GET", path) or []

    # -- crossings ---------------------------------------------------------
    def save_crossing(self, row: dict) -> None:
        self._safe("save_crossing", "POST", "border_crossings", body=[row], prefer="return=minimal")

    def finish_crossing(self, crossing_id: str, finished_iso: str, actual_minutes: int) -> None:
        self._safe("finish_crossing", "PATCH", f"border_crossings?id=eq.{quote(crossing_id)}",
                   body={"finished_at": finished_iso, "actual_minutes": actual_minutes},
                   prefer="return=minimal")

    def get_crossing(self, crossing_id: str) -> dict | None:
        rows = self._safe("get_crossing", "GET", f"border_crossings?id=eq.{quote(crossing_id)}")
        return rows[0] if rows else None

    def crossings_since(self, since_iso: str) -> list[dict]:
        path = (f"border_crossings?finished_at=gte.{quote(since_iso)}"
                "&order=finished_at.asc")
        return self._safe("crossings_since", "GET", path) or []
