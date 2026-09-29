"""The pull surface: what the Chisme pipeline asks for, without HTTP in the way.

Built for someone who crosses every day, so every answer carries the three things
that decide "leave now or wait":

    minutes       what CBP says
    delta         better or worse than the previous reading
    crosses_at    the clock time they would actually get across

One CBP reading is reused for CACHE_SECONDS, so posts, replies, scheduled messages
and alert sweeps all share one fetch.

Previous readings come from memory first, then from border_readings. That is what
makes deltas and drop alerts survive a restart: without the database fallback, the
first sweep after a deploy has no "before" and silently reports nothing.

The pieces live next door: windows.py (how long each read is trusted), opinions.py
(the other sources), history.py (previous, trend, typical) and scoring.py (ranking).
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from functools import partial

from . import consensus, history, opinions, scoring, windows
from .alerts import AlertBook
from .core import cbp, text
from .core.cbp import LOCAL_TZ, Port, Snapshot
from .core.storage import Storage
from .crossings import CrossingError, CrossingLog
from .opinions import SourceStatus
from .sources import SOURCES, Reading, readings_from

log = logging.getLogger(__name__)
CACHE_SECONDS = 300
# How long a last-good reading may still be served after a refresh fails. Sending a
# 10-minute-old number marked as such beats sending nothing.
STALE_GRACE_SECONDS = 1800
CBP = "cbp"


class FeedError(RuntimeError):
    """CBP could not be read. The pipeline should send nothing rather than guess."""


@dataclass
class _View:
    """One reading and everything derived from it, swapped in as a unit.

    Readers outside the lock may still hold the previous snapshot; keeping its
    decisions and decorated rows together means they never see a mix of the two.
    """
    snap: Snapshot
    decisions: dict[tuple[str, str], consensus.Decision]
    sources: dict[str, SourceStatus]
    mirror_check: dict
    decorated: dict[tuple[str, str], dict] = field(default_factory=dict)


class BorderFeed:
    def __init__(self, storage: Storage | None = None, cache_seconds: int = CACHE_SECONDS,
                 fetcher=cbp.fetch, clock=lambda: datetime.now(timezone.utc),
                 extra_sources: list | None = None, enrich: bool = True):
        self.storage = storage if storage is not None else Storage()
        # CBP gives the skeleton (all six ports, hours, closures). Extra sources only
        # ever refine the minutes; none of them can add or remove a bridge.
        self.extra_sources = list(SOURCES if extra_sources is None else extra_sources)
        self.cache_seconds = cache_seconds
        # A poller only records readings; the history lookups that feed answers
        # (trend seed, previous-reading prefetch, typical waits) would be wasted there.
        self.enrich = enrich
        self._fetch = fetcher
        self._clock = clock
        # One upstream read at a time. Without this, several pipeline requests arriving
        # together each trigger their own fetch of CBP and the scraped site.
        self._lock = threading.Lock()
        self._view: _View | None = None
        self._old_view: _View | None = None
        self._fetched_monotonic: float | None = None
        self._source_cache: dict[str, dict] = {}   # name -> readings, at, signature, repeats
        self._last_error: str | None = None
        self._serving_stale = False
        self._fingerprint: str | None = None
        self._fingerprint_since: str | None = None
        self._ports_synced = False
        # Deltas compare against the previous *distinct* reading, not the previous fetch.
        # Rolling on every fetch made a 48 -> 20 drop read as "flat" five minutes later,
        # and an alert sweep that ran after any other refresh never saw the drop at all.
        # None in _previous means "looked, nothing there", so storage is asked once.
        self._current: dict[tuple[str, str], tuple[str, int]] = {}   # (hash, minutes)
        self._previous: dict[tuple[str, str], int | None] = {}
        # (port, lane) -> [(when, minutes)], trimmed to the trend window. This is today's
        # movement, not history: it answers "is the line growing right now?".
        self._recent: dict[tuple[str, str], list[history.Point]] = {}
        self._trend_seeded = False
        self._typical: dict[tuple[str, str, int, int], dict] = {}
        self._typical_due: float | None = None      # monotonic time of the next read
        self.alerts = AlertBook(self.storage, clock)
        self.crossings = CrossingLog(self.storage, clock)

    # -- state -------------------------------------------------------------
    @property
    def _snapshot(self) -> Snapshot | None:
        return self._view.snap if self._view else None

    @property
    def decisions(self) -> dict[tuple[str, str], consensus.Decision]:
        return self._view.decisions if self._view else {}

    _decisions = decisions

    @property
    def source_status(self) -> dict[str, SourceStatus]:
        return self._view.sources if self._view else {}

    @property
    def lanes_disputed(self) -> int:
        return sum(1 for d in self.decisions.values() if d.disputed)

    @property
    def serving_stale(self) -> bool:
        return self._serving_stale

    @property
    def last_error(self) -> str | None:
        return self._last_error

    @property
    def cache_age_seconds(self) -> float | None:
        if self._fetched_monotonic is None:
            return None
        return round(time.monotonic() - self._fetched_monotonic, 1)

    @property
    def frozen_hours(self) -> float:
        if not self._fingerprint_since or not self._view:
            return 0.0
        return (self._view.snap.at - datetime.fromisoformat(self._fingerprint_since)).total_seconds() / 3600

    def _view_for(self, snap: Snapshot) -> _View | None:
        return next((v for v in (self._view, self._old_view) if v and v.snap is snap), None)

    # -- snapshot ----------------------------------------------------------
    def _age(self) -> float | None:
        return None if self._fetched_monotonic is None else time.monotonic() - self._fetched_monotonic

    def _is_fresh(self) -> bool:
        age = self._age()
        return self._view is not None and age is not None and age < self.cache_seconds

    def snapshot(self, force: bool = False, allow_stale: bool = True) -> Snapshot:
        """The current reading. After a failed refresh the last good one is served for
        STALE_GRACE_SECONDS, flagged; allow_stale=False is for callers that need a
        genuinely new reading or an error, such as the poller."""
        if self._is_fresh() and not force:
            return self._view.snap
        with self._lock:
            # Another caller may have refreshed while we waited for the lock.
            if self._is_fresh() and not force:
                return self._view.snap
            try:
                return self._refresh()
            except FeedError:
                age = self._age()
                if allow_stale and self._view and age is not None and age < STALE_GRACE_SECONDS:
                    self._serving_stale = True
                    return self._view.snap
                raise

    def _refresh(self) -> Snapshot:
        now = self._clock()                     # once per refresh: every window agrees
        cached_payload = self._cached(CBP, now)
        readings, statuses, to_fetch = self._plan_sources(now)

        # CBP and the scraped sites are independent, so they are read at the same time.
        jobs = [(source, partial(source.fetch, now)) for source in to_fetch]
        if cached_payload is None:
            jobs.append((CBP, self._fetch))
        results = opinions.fetch_all(jobs)

        try:
            payload = cached_payload
            if payload is None:
                ok, payload = results[CBP]
                if not ok:
                    raise payload
            snap = cbp.build(payload, now)
        except Exception as exc:
            self._last_error = f"{type(exc).__name__}: {exc}"
            log.warning("CBP fetch failed: %s", self._last_error)
            raise FeedError(self._last_error) from exc

        readings = readings_from(snap) + readings
        for source in to_fetch:
            ok, value = results[source]
            if not ok:
                log.warning("source %s failed: %s", source.name, value)
                statuses[source.name] = SourceStatus("failed", type(value).__name__)
                continue
            fresh = [r for r in value if r.source]
            statuses[source.name] = opinions.judge(source, fresh)
            # An empty parse is not cached, so the next refresh tries again.
            if statuses[source.name].state != "empty":
                readings.extend(fresh)
                self._remember(source.name, fresh, opinions.signature(fresh))

        changed = self._track_fingerprint(snap)
        if cached_payload is None:
            # CBP gets the same widening as every other source: a repeated payload
            # stretches its window, a changed one snaps it back.
            self._remember(CBP, payload, self._fingerprint)
        if self.enrich:
            self._seed_trend(snap)
        self._record_trend(snap)
        self._roll_previous(snap)
        if self.enrich:
            self._prefetch_previous(snap)
            self._refresh_typical()

        self._old_view, self._view = self._view, _View(
            snap, consensus.reconcile_all(readings), statuses, opinions.compare_mirrors(readings))
        self._fetched_monotonic = time.monotonic()
        self._last_error = None
        self._serving_stale = False
        self._persist(snap, changed)
        return snap

    def _persist(self, snap: Snapshot, changed: bool) -> None:
        # Keep border_ports in step with the code registry, once per process. The
        # migration seeds it, but a renamed bridge here would otherwise never reach it.
        if self.storage.enabled and not self._ports_synced:
            self.storage.upsert_ports()
            self._ports_synced = True
        # An unchanged fingerprint means every row would collide on content_hash, so the
        # write is pure cost. Skip it and say so in the run log instead.
        if changed:
            self.storage.log_run("snapshot", snap, self.storage.save_readings(snap), "ok")
        else:
            self.storage.log_run("snapshot", snap, 0, "unchanged")

    def _track_fingerprint(self, snap: Snapshot) -> bool:
        """Remember when these exact numbers first appeared.

        CBP being unreachable is caught by the fetch. This catches the other failure:
        CBP answering happily with numbers that stopped moving hours ago.
        """
        fingerprint = history.fingerprint(snap)
        if fingerprint == self._fingerprint:
            return False
        self._fingerprint, self._fingerprint_since = fingerprint, snap.generated_at
        return True

    # -- source windows ----------------------------------------------------
    def is_rush_hour(self, now: datetime | None = None) -> bool:
        return windows.is_rush_hour(now or self._clock())

    def is_quiet_hour(self, now: datetime | None = None) -> bool:
        return windows.is_quiet_hour(now or self._clock())

    def _window(self, name: str, now: datetime | None = None) -> tuple[int, int]:
        return windows.window(name, now or self._clock())

    def _cached(self, name: str, now: datetime):
        # Per-source windows live inside the feed's own cache setting: with caching off
        # (cache_seconds=0) every read is fresh, which is what tests and --force expect.
        entry = self._source_cache.get(name) if self.cache_seconds else None
        if entry and windows.held_for(entry) < windows.ttl(name, entry, now):
            return entry["readings"]
        return None

    def _remember(self, name: str, value, signature) -> None:
        if self.cache_seconds:
            self._source_cache[name] = windows.next_entry(self._source_cache.get(name), value, signature)

    def _plan_sources(self, now: datetime) -> tuple[list[Reading], dict[str, SourceStatus], list]:
        """Readings still inside their window, a status per source, and what to fetch."""
        readings: list[Reading] = []
        statuses: dict[str, SourceStatus] = {CBP: SourceStatus("ok")}
        to_fetch = []
        for source in self.extra_sources:
            if not source.is_configured():
                statuses[source.name] = SourceStatus("not configured")
                continue
            cached = self._cached(source.name, now)
            if cached is None:
                to_fetch.append(source)
                continue
            held = windows.held_for(self._source_cache[source.name])
            readings.extend(opinions.aged(cached, held))
            statuses[source.name] = SourceStatus("cached", f"({len(cached)} readings, {int(held)}s old)")
        return readings, statuses, to_fetch

    def source_windows(self) -> dict[str, dict]:
        now = self._clock()
        return {name: {"ttl_seconds": windows.ttl(name, e, now),
                       "age_seconds": round(windows.held_for(e), 1),
                       "unchanged_reads": e["repeats"]}
                for name, e in self._source_cache.items()}

    # -- history -----------------------------------------------------------
    def _record_trend(self, snap: Snapshot) -> None:
        cutoff = history.trend_cutoff(snap.at)
        for port, lane_id, lane in cbp.open_lanes(snap):
            history.add_point(self._recent, (port.port_number, lane_id), (snap.at, lane.delay_minutes), cutoff)

    def _seed_trend(self, snap: Snapshot) -> None:
        """A fresh process has no movement to report; the last 90 minutes are in the database."""
        if self._trend_seeded or not self.storage.enabled:
            return
        self._trend_seeded = True
        cutoff = history.trend_cutoff(snap.at)
        for row in self.storage.readings_since(cutoff.isoformat()):
            if row.get("delay_minutes") is not None and row.get("captured_at"):
                point = (datetime.fromisoformat(row["captured_at"]), int(row["delay_minutes"]))
                history.add_point(self._recent, (row["port_number"], row["lane"]), point, cutoff)

    def trend(self, port_number: str, lane: str) -> dict | None:
        """How this lane has moved over the last hour and a half, if we have seen it."""
        return history.trend(self._recent.get((port_number, lane), []))

    def _refresh_typical(self) -> None:
        if not self.storage.enabled:
            return
        now = time.monotonic()
        if self._typical_due is not None and now < self._typical_due:
            return
        rows = self.storage.typical_waits()
        if rows is None:                       # the call failed: try again soon, keep what we had
            self._typical_due = now + history.TYPICAL_RETRY_SECONDS
            return
        self._typical = {(r["port_number"], r["lane"], int(r["dow"]), int(r["hour"])): r for r in rows}
        self._typical_due = now + history.TYPICAL_REFRESH_SECONDS

    def typical(self, snap: Snapshot, port_number: str, lane: str, minutes: int | None) -> dict | None:
        if minutes is None:
            return None
        local = snap.local_at
        return history.typical(self._typical.get(history.typical_key(port_number, lane, local)),
                               local, minutes)

    def _roll_previous(self, snap: Snapshot) -> None:
        """Advance "previous" only when a lane's reading actually changed.

        Same rule as border_readings, where content_hash makes one row per CBP update:
        memory and the database agree on what "the previous reading" means.
        """
        for port, lane_id, lane in cbp.open_lanes(snap):
            key = (port.port_number, lane_id)
            current = self._current.get(key)
            if current is not None and current[0] != lane.content_hash:
                self._previous[key] = current[1]
            self._current[key] = (lane.content_hash, lane.delay_minutes)

    def _prefetch_previous(self, snap: Snapshot) -> None:
        """Look up every lane memory cannot answer in one pass, in parallel.

        Only a fresh process needs this; afterwards memory answers and nothing is sent.
        """
        if not self.storage.enabled:
            return
        missing = [(port.port_number, lane_id, lane.content_hash)
                   for port, lane_id, lane in cbp.open_lanes(snap)
                   if (port.port_number, lane_id) not in self._previous]
        found = opinions.fetch_all([(m, partial(self._previous_from_storage, *m)) for m in missing])
        for (port_number, lane_id, _), (ok, value) in found.items():
            self._previous[(port_number, lane_id)] = value if ok else None

    def previous_minutes(self, port_number: str, lane: str, current_hash: str | None = None) -> int | None:
        """Last open reading for this lane: memory first, then the database.

        The database fallback is what a fresh process needs — otherwise every deploy
        loses one cycle of deltas and alerts.
        """
        key = (port_number, lane)
        if key not in self._previous:
            self._previous[key] = self._previous_from_storage(port_number, lane, current_hash)
        return self._previous[key]

    def _previous_from_storage(self, port_number: str, lane: str, current_hash: str | None) -> int | None:
        return history.previous_from_rows(self.storage.recent_readings(port_number, lane), current_hash)

    # -- one lane, decorated -----------------------------------------------
    _score = staticmethod(scoring.score)
    _rank_key = staticmethod(scoring.rank_key)

    def faster_lane_here(self, port: Port, lane_id: str) -> dict | None:
        return scoring.faster_lane_here(port, lane_id)

    def _decorate(self, snap: Snapshot, port: Port, lane_id: str) -> dict:
        """Derived view of one lane, computed once per snapshot.

        A single /waits asked for this 92 times before caching; with storage enabled
        each of those could mean a database round trip for the previous reading.
        """
        view = self._view_for(snap)
        if view is None:
            return self._decorate_uncached(snap, port, lane_id)
        key = (port.port_number, lane_id)
        if key not in view.decorated:
            view.decorated[key] = self._decorate_uncached(snap, port, lane_id)
        return view.decorated[key]

    def _decorate_uncached(self, snap: Snapshot, port: Port, lane_id: str) -> dict:
        lane = port.lanes[lane_id]
        view = self._view_for(snap)
        decision = view.decisions.get((port.port_number, lane_id)) if view else None
        # The reconciled value is what people are shown and what ranking uses; CBP's own
        # number stays alongside it so any difference is auditable.
        if decision:
            state, minutes = decision.state, decision.minutes
            age = decision.age_minutes if decision.age_minutes is not None else lane.age_minutes
            chosen_cbp = decision.chosen_source == CBP
        else:
            state, minutes, age, chosen_cbp = lane.state, lane.delay_minutes, lane.age_minutes, True
        is_open = state == "open"
        disputed = bool(decision and decision.disputed)
        local = snap.local_at
        crosses_at = local + timedelta(minutes=minutes) if is_open and minutes is not None else None
        closes_at = scoring.closes_at(local, port)
        # Sending someone to a bridge that shuts before they reach the booth is worse
        # than sending them to a slower one that is open.
        too_tight = bool(crosses_at and closes_at
                         and crosses_at + timedelta(minutes=scoring.CLOSING_MARGIN) > closes_at)
        was = self.previous_minutes(port.port_number, lane_id, lane.content_hash) if lane.has_minutes else None
        delta = history.delta(lane.delay_minutes, was) if lane.has_minutes else None

        # When sources disagree we plan for the worst of them. A headline that promises
        # 20 minutes while another site says 58 is the one mistake worth engineering
        # against: an undisputed bridge should win unless it is worse than the disputed
        # one's own worst case.
        opinion_minutes = [o["minutes"] for o in decision.opinions
                           if o.get("minutes") is not None] if disputed else []
        low, high = ((min(opinion_minutes), max(opinion_minutes)) if opinion_minutes
                     else (minutes, minutes))
        score = self._score(high if is_open else None, age, delta,
                            lane.stamp_ahead_of_clock and chosen_cbp)
        return {
            "port_number": port.port_number,
            "name": port.name,
            "name_es": port.name_es,
            "lane": lane_id,
            "state": state,
            "minutes": minutes,
            "planning_minutes": high,
            "optimistic_minutes": low,
            "cbp_minutes": lane.delay_minutes,
            "consensus": decision.to_dict() if decision else None,
            "effective_minutes": score["effective_minutes"] if score else None,
            "score": score,
            "confidence": (("low" if disputed
                            else scoring.confidence(age, delta, lane.stamp_ahead_of_clock))
                           if is_open else None),
            "age_minutes": age,
            "sources_disagree": disputed,
            "delta": delta,
            "trend": self.trend(port.port_number, lane_id),
            "typical": self.typical(snap, port.port_number, lane_id, minutes) if is_open else None,
            "faster_lane_here": self.faster_lane_here(port, lane_id),
            "crosses_at": crosses_at.isoformat() if crosses_at else None,
            "closes_at": closes_at.isoformat() if closes_at else None,
            "closes_before_you_cross": too_tight,
            "cbp_updated_at": lane.cbp_updated_at,
            "stale": lane.stale,
        }

    # -- what the pipeline asks for ---------------------------------------
    def ranked(self, lane: str = "car", snap: Snapshot | None = None) -> list[dict]:
        """Best bet first; bridges that close too soon sink below the usable ones."""
        snap = snap or self.snapshot()
        rows = [self._decorate(snap, p, lane) for p in snap.ports if lane in p.lanes]
        usable = sorted((r for r in rows if r["state"] == "open"),
                        key=lambda r: (r["closes_before_you_cross"], *self._rank_key(r)))
        return usable + [r for r in rows if r["state"] != "open"]

    def best(self, lane: str = "car", snap: Snapshot | None = None) -> dict | None:
        """The bridge we would actually send someone to for this lane."""
        rows = self.ranked(lane, snap)
        top = rows[0] if rows else None
        return top if top and top["state"] == "open" and not top["closes_before_you_cross"] else None

    def waits(self, lang: str = "es", lanes: tuple[str, ...] | None = None) -> dict:
        """Everything, or just the lanes asked for.

        A pipeline that only posts car waits does not need four ranked lists and every
        lane of every port, so `lanes` trims the response it has to parse.
        """
        snap = self.snapshot()
        wanted = tuple(lanes) if lanes else text.CAPTION_LANES
        return {
            **snap.to_dict(),
            "best": {l: self.best(l, snap) for l in wanted},
            "ranked": {l: self.ranked(l, snap) for l in wanted},
            "caption_es": text.caption(self, snap, "es"),
            "caption_en": text.caption(self, snap, "en"),
            "updated_label": text.last_honest_update(snap),
        }

    def bridge(self, port_key: str, lane: str = "car", lang: str = "es",
               snap: Snapshot | None = None) -> dict:
        snap = snap or self.snapshot()
        port = snap.port(port_key)
        if port is None:
            return {"found": False, "reply": text.unknown_bridge(lang), "options": text.menu(snap, lang)}
        if lane not in port.lanes:
            return {"found": True, "port_number": port.port_number, "name": port.name,
                    "name_es": port.name_es, "lane": lane, "state": "absent", "minutes": None,
                    "reply": text.no_such_lane(port, lane, lang)}

        body = dict(self._decorate(snap, port, lane))
        body["found"] = True
        body["alternative"] = self.alternative(port.port_number, lane, snap,
                                               any_open=body["closes_before_you_cross"])
        body["reply"] = text.bridge_reply(body, lang)
        return body

    def alternative(self, port_number: str, lane: str = "car", snap: Snapshot | None = None,
                    any_open: bool = False) -> dict | None:
        """A materially faster bridge for the same lane, or None.

        any_open drops the "must save 10 minutes" rule: when their bridge shuts before
        they would reach it, a slower bridge that is still open is the better answer.
        """
        snap = snap or self.snapshot()
        best = self.best(lane, snap)
        if not best or best["port_number"] == port_number:
            return None
        port = snap.port(port_number)
        current = port.lanes.get(lane) if port else None
        saved = None
        if current and current.has_minutes:
            saved = self._decorate(snap, port, lane)["effective_minutes"] - best["effective_minutes"]
        if not any_open and saved is not None and saved < scoring.WORTH_SWITCHING:
            return None
        return {**best, "saves_minutes": saved}

    def my_digest(self, ports: list[str], lane: str = "car", lang: str = "es") -> dict:
        """Only the bridges this person saved, in their lane. Their whole post."""
        snap = self.snapshot()
        found = [r for r in (self.bridge(p, lane, lang, snap) for p in ports) if r.get("found")]
        best = self.best(lane, snap)
        return {"lane": lane, "bridges": found, "best": best,
                "message": text.my_digest(found, best, lane, lang)}

    def drops(self, below: int, lane: str = "car", lang: str = "es",
              since: datetime | None = None) -> dict:
        """Lanes that crossed under `below`, as events any number of consumers can read.

        Without `since`: alerts that still describe the current reading, the same answer
        however often it is asked, so dedupe on `id`. With `since` (the `cursor` from the
        previous answer): everything detected after it, including drops that happened
        while the consumer was away. Reading never consumes, so two workers, or someone
        checking by hand, cannot take an alert from each other.

        A wait bouncing 28, 31, 29, 32 around a 30-minute limit fires once: the lane stays
        quiet until it climbs back to the limit plus a margin. That state is stored, so a
        restart does not resend the drop it already sent.
        """
        snap = self.snapshot()
        current: dict[str, str] = {}
        for port, lane_id, reading in cbp.open_lanes(snap):
            if lane_id != lane:
                continue
            current[port.port_number] = reading.content_hash
            was = self.previous_minutes(port.port_number, lane, reading.content_hash)
            self.alerts.check(port.port_number, lane, below, reading.delay_minutes, was,
                              reading.content_hash)

        events = self.alerts.events(lane, below, since, current if since is None else None)
        return {"generated_at": snap.generated_at, "lane": lane, "below": below,
                "quiet_hour": self.is_quiet_hour(),
                "cursor": self.alerts.cursor(lane, below) or (since.isoformat() if since else None),
                "drops": [self._drop_row(snap, event, lang) for event in events]}

    def _drop_row(self, snap: Snapshot, event: dict, lang: str) -> dict:
        port = snap.port(event["port_number"])
        detected = datetime.fromisoformat(event["detected_at"]).astimezone(LOCAL_TZ)
        crosses_at = (detected + timedelta(minutes=event["minutes"])).isoformat()
        return {
            "id": event["id"],
            "detected_at": event["detected_at"],
            "port_number": event["port_number"],
            "name": port.name if port else event["port_number"],
            "name_es": port.name_es if port else event["port_number"],
            "lane": event["lane"],
            "minutes": event["minutes"],
            "previous_minutes": event["previous_minutes"],
            "below": event["below"],
            "crosses_at": crosses_at,
            "message": (text.drop_alert(port, event["lane"], event["minutes"], event["below"],
                                        crosses_at, lang) if port else None),
        }

    # -- ground truth ------------------------------------------------------
    def start_crossing(self, port_key: str, lane: str = "car", lang: str = "es",
                       reporter: str | None = None) -> dict:
        """They joined the line. Freeze what every source says right now."""
        body = self.bridge(port_key, lane, lang)
        if not body.get("found"):
            raise CrossingError("no such bridge", "unknown_bridge")
        crossing = self.crossings.start(body, reporter)
        crossing["reply"] = text.crossing_started(body, lang)
        return crossing

    def finish_crossing(self, crossing_id: str, lang: str = "es") -> dict:
        result = self.crossings.finish(crossing_id)
        result["reply"] = text.crossing_finished(result, cbp.PORTS.get(result["port_number"], {}), lang)
        return result

    def accuracy(self, days: int = 30) -> dict:
        return self.crossings.accuracy(days)

    def health(self) -> dict:
        # A freshly started process has read nothing yet. Take one reading rather than
        # reporting "not ok", which would page someone over a server that just booted.
        if self._view is None and self._last_error is None:
            try:
                self.snapshot()
            except FeedError:
                pass

        frozen_hours = self.frozen_hours
        frozen = frozen_hours >= history.FROZEN_HOURS
        dark = self._view is not None and not any(p.has_live_data for p in self._view.snap.ports)
        statuses = self.source_status
        return {
            # A failed refresh sets last_error, which also covers serving a stale reading.
            "ok": self._last_error is None and self._view is not None and not frozen and not dark,
            "degraded": frozen or dark,
            "frozen_hours": round(frozen_hours, 2),
            "frozen": frozen,
            "all_bridges_dark": dark,
            "unchanged_since": self._fingerprint_since,
            "last_ok": self._view.snap.generated_at if self._view else None,
            "last_error": self._last_error,
            "cache_seconds": self.cache_seconds,
            "cache_age_seconds": self.cache_age_seconds,
            "serving_stale": self._serving_stale,
            "rush_hour": self.is_rush_hour(),
            "source_windows": self.source_windows(),
            "storage_enabled": self.storage.enabled,
            "ports_known": len(cbp.PORTS),
            "sources": {name: str(status) for name, status in statuses.items()},
            # A scraper that breaks quietly is still a broken scraper.
            "source_problems": {name: str(status) for name, status in statuses.items() if status.problem},
            "lanes_disputed": self.lanes_disputed,
            "parser_check": (self._view.mirror_check if self._view
                             else {"compared": 0, "skipped_other_update": 0, "mismatches": []}),
        }
