"""Drop alerts as events: fired once per crossing, remembered across restarts, and
readable by any number of consumers.

Two things went wrong when "already sent" lived only in a set in memory:

1. A restart forgot what had been sent, while the previous wait survived through
   border_readings, so the same drop fired again after every deploy.
2. Asking was consuming: whoever called /drops first got the alert and every later
   caller got nothing — a second pipeline worker, or someone checking by hand.

Now an alert is a row with a deterministic id and a detected_at. Detecting it is
idempotent (same crossing, same id), and reading it is not consuming: a consumer passes
back the `cursor` from its last answer and receives only what is newer.
"""
from __future__ import annotations

import asyncio
import hashlib
from datetime import datetime, timedelta

MEMORY_HOURS = 48          # events older than this are served from nowhere; nobody asks
REARM_MARGIN = 5           # a lane re-arms only after climbing back to limit + margin

Key = tuple[str, str, int]   # (port_number, lane, below)


def _at(event: dict) -> datetime:
    # ISO strings with and without microseconds do not sort as text; compare as times.
    return datetime.fromisoformat(event["detected_at"])


def alert_id(port_number: str, lane: str, below: int, reading_hash: str) -> str:
    return hashlib.sha1(f"{port_number}|{lane}|{below}|{reading_hash}".encode()).hexdigest()


class AlertBook:
    def __init__(self, storage, clock):
        self._storage = storage
        self._clock = clock
        self._lock = asyncio.Lock()
        self._events: dict[str, dict] = {}     # id -> event, oldest first
        self._latest: dict[Key, dict] = {}     # the newest alert per key: its arm state
        self._loaded = False

    # -- state -------------------------------------------------------------
    async def _load(self) -> None:
        """Pick up what earlier processes sent. Once per process, on first use."""
        if self._loaded:
            return
        self._loaded = True
        if not self._storage.enabled:
            return
        since = self._clock() - timedelta(hours=MEMORY_HOURS)
        for row in sorted(await self._storage.alerts_since(since.isoformat()), key=_at):
            self._remember(dict(row))

    def _remember(self, event: dict) -> None:
        """Events arrive in time order: sorted on load, strictly increasing after."""
        self._events[event["id"]] = event
        self._latest[(event["port_number"], event["lane"], int(event["below"]))] = event

    def _next_time(self) -> datetime:
        """Strictly increasing, so a cursor never hides an event detected in the same
        instant as the one before it."""
        now = self._clock()
        if self._events:
            last = _at(next(reversed(self._events.values())))
            if now <= last:
                now = last + timedelta(microseconds=1)
        return now

    def _trim(self) -> None:
        cutoff = self._clock() - timedelta(hours=MEMORY_HOURS)
        self._events = {i: e for i, e in self._events.items()
                        if _at(e) >= cutoff or e.get("rearmed_at") is None}

    def _mine(self, lane: str, below: int) -> list[dict]:
        return [e for e in self._events.values() if e["lane"] == lane and int(e["below"]) == below]

    # -- detection ---------------------------------------------------------
    async def check(self, port_number: str, lane: str, below: int, minutes: int,
                    previous: int | None, reading_hash: str) -> dict | None:
        """Record an alert if this reading is a fresh crossing under `below`."""
        async with self._lock:
            await self._load()
            key = (port_number, lane, below)
            latest = self._latest.get(key)
            armed = latest is None or latest.get("rearmed_at") is not None

            if not armed and minutes >= below + REARM_MARGIN:
                latest["rearmed_at"] = self._clock().isoformat()
                await self._storage.rearm_alert(latest["id"], latest["rearmed_at"])
                armed = True

            if previous is None or previous < below or minutes >= below or not armed:
                return None
            event_id = alert_id(port_number, lane, below, reading_hash)
            if event_id in self._events:
                return None
            event = {"id": event_id, "port_number": port_number, "lane": lane, "below": below,
                     "minutes": minutes, "previous_minutes": previous,
                     "reading_hash": reading_hash,
                     "detected_at": self._next_time().isoformat(), "rearmed_at": None}
            self._remember(event)
            await self._storage.save_alert(dict(event))
            self._trim()
            return event

    # -- reading -----------------------------------------------------------
    async def events(self, lane: str, below: int, since: datetime | None = None,
                     current: dict[str, str] | None = None) -> list[dict]:
        """With `since`, everything newer. Without it, alerts still describing the
        current reading — the same answer however many times it is asked."""
        async with self._lock:
            await self._load()
            mine = self._mine(lane, below)
            if since is not None:
                return [dict(e) for e in mine if _at(e) > since]
            current = current or {}
            return [dict(e) for e in mine if current.get(e["port_number"]) == e["reading_hash"]]

    def cursor(self, lane: str, below: int) -> str | None:
        mine = self._mine(lane, below)
        return mine[-1]["detected_at"] if mine else None
