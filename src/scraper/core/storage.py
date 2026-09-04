"""Supabase persistence + freshness cache.

If Supabase isn't configured the whole layer degrades to a no-op: tools still return
live results, they just aren't persisted and nothing is served from cache. This keeps
the server fully usable for local testing without a database.

Supabase's Python client is synchronous, so calls are pushed to a thread to avoid
blocking the event loop.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from .config import settings
from .dedupe import _TITLE_ONLY_THRESHOLD, _TOKEN_OVERLAP_THRESHOLD, _norm, _similar, _title_token_overlap
from .dedupe import merge_ticket_links as _merge_ticket_links
from .eventtime import event_tz, local_day, to_event_local
from .models import Event, TicketLink, Trend

log = logging.getLogger("scraper.storage")


def _iso(dt: Optional[datetime]) -> Optional[str]:
    """Serialize for a ``timestamptz`` column — always WITH an offset.

    ``to_event_local`` is applied here as well as in the orchestrator because
    this is the last line of defense: a naive value reaching Postgres is read as
    UTC, which is exactly the six-hour shift this fix removes.
    """
    dt = to_event_local(dt)
    return dt.isoformat() if dt else None


def _address_hash(address: Optional[str], venue_name: Optional[str]) -> str:
    """Stable venue natural key. MUST match the SQL rule in 0002_venues.sql:

    sha1( lower(trim(coalesce(address,''))) || '|' || lower(trim(coalesce(venue_name,''))) )
    """
    key = f"{(address or '').strip().lower()}|{(venue_name or '').strip().lower()}"
    return hashlib.sha1(key.encode("utf-8")).hexdigest()


def _venue_row(e: Event) -> dict[str, Any]:
    return {
        "name": e.venue,
        "address": e.location,
        "lat": e.lat,
        "lng": e.lng,
        "address_hash": _address_hash(e.location, e.venue),
    }


def _event_row(e: Event) -> dict[str, Any]:
    # first_seen/last_seen are managed by DB default + trigger (see migration).
    return {
        "source": e.source,
        "source_id": e.source_id,
        "title": e.title,
        "description": e.description,
        "start_time": _iso(e.start_time),
        "end_time": _iso(e.end_time),
        "venue": e.venue,
        "location": e.location,
        "url": e.url,
        "image_url": e.image_url,
        "categories": e.categories,
        "ticket_links": [tl.model_dump() for tl in e.ticket_links],
        "raw": e.raw,
        "content_hash": e.content_hash,
    }


def _is_same_event_title(a: str, b: str) -> bool:
    """Title check for the cross-run merge, where venue+day are already an
    exact match (same venue_id, same calendar day) — so unlike dedupe.py's
    in-batch check, no separate venue-similarity confirmation is needed here."""
    if _similar(_norm(a), _norm(b)) >= _TITLE_ONLY_THRESHOLD:
        return True
    return _title_token_overlap(a, b) >= _TOKEN_OVERLAP_THRESHOLD


def _trend_row(t: Trend) -> dict[str, Any]:
    return {
        "source": t.source,
        "platform": t.platform,
        "topic": t.topic,
        "title": t.title,
        "summary": t.summary,
        "url": t.url,
        "score": t.score,
        "engagement": t.engagement,
        "captured_for": t.captured_for,
        "raw": t.raw,
        "content_hash": t.content_hash,
    }


class Storage:
    def __init__(self) -> None:
        self._client = None
        if settings.storage_enabled:
            try:
                from supabase import create_client

                self._client = create_client(settings.supabase_url, settings.supabase_key)
            except Exception as exc:  # noqa: BLE001 - never let storage break a run
                log.error("Supabase init failed, running without storage: %s", exc)
                self._client = None

    @property
    def enabled(self) -> bool:
        return self._client is not None

    @property
    def client(self) -> Any:
        """The raw Supabase client, for callers that need an API this class
        doesn't wrap (e.g. Storage buckets in scraper.social). None when
        storage is unconfigured — check `enabled` first."""
        return self._client

    # ── writes ────────────────────────────────────────────────────────────────
    async def upsert_events(self, events: list[Event]) -> None:
        if not self.enabled or not events:
            return
        # Venues first, so event rows can reference them. A venue failure must never
        # break the event upsert — _upsert_venues returns {} and venue_id stays None.
        venue_ids = await asyncio.to_thread(self._upsert_venues, events)
        rows: list[dict[str, Any]] = []
        for e in events:
            row = _event_row(e)
            row["venue_id"] = (
                venue_ids.get(_address_hash(e.location, e.venue))
                if (e.venue or e.location)
                else None
            )
            rows.append(row)

        # dedupe.py already merges same-real-event duplicates that arrive in
        # THIS batch (e.g. Ticketmaster + Eventbrite for the same concert in
        # one scrape). This second pass catches the same real event showing up
        # in a LATER scrape via a source that didn't have it before — without
        # it, every new ticketing site a venue's event is later found on would
        # create a second card for the same happening instead of adding a
        # ticket link to the existing one.
        rows = await asyncio.to_thread(self._merge_with_existing, rows)
        if rows:
            await asyncio.to_thread(self._upsert, "events", rows, "content_hash")

    async def upsert_trends(self, trends: list[Trend]) -> None:
        if not self.enabled or not trends:
            return
        rows = [_trend_row(t) for t in trends]
        await asyncio.to_thread(self._upsert, "trends", rows, "content_hash")

    async def log_run(
        self,
        tool: str,
        params: dict[str, Any],
        source_counts: dict[str, Any],
        status: str,
        error: Optional[str] = None,
    ) -> None:
        if not self.enabled:
            return
        row = {
            "tool": tool,
            "params": params,
            "source_counts": source_counts,
            "status": status,
            "error": error,
        }
        try:
            await asyncio.to_thread(lambda: self._client.table("runs").insert(row).execute())
        except Exception as exc:  # noqa: BLE001
            log.error("run log failed: %s", exc)

    # ── reads (query_stored + freshness) ───────────────────────────────────────
    async def query_events(
        self,
        location: Optional[str] = None,
        since: Optional[str] = None,
        until: Optional[str] = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        if not self.enabled:
            return []

        def _q() -> list[dict[str, Any]]:
            q = self._client.table("events").select("*")
            if location:
                q = q.ilike("location", f"%{location}%")
            if since:
                q = q.gte("start_time", since)
            if until:
                q = q.lte("start_time", until)
            return q.order("start_time", desc=False).limit(limit).execute().data or []

        return await asyncio.to_thread(_q)

    async def query_trends(
        self,
        platform: Optional[str] = None,
        topic: Optional[str] = None,
        since: Optional[str] = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        if not self.enabled:
            return []

        def _q() -> list[dict[str, Any]]:
            q = self._client.table("trends").select("*")
            if platform:
                q = q.eq("platform", platform)
            if topic:
                q = q.ilike("captured_for", f"%{topic}%")
            if since:
                q = q.gte("captured_at", since)
            return q.order("score", desc=True).limit(limit).execute().data or []

        return await asyncio.to_thread(_q)

    async def fresh_events(self, location: Optional[str], limit: int) -> Optional[list[dict[str, Any]]]:
        """Return recently-captured events for this location, or None if cache is cold."""
        if not self.enabled:
            return None
        cutoff = self._cutoff()

        def _q() -> list[dict[str, Any]]:
            q = self._client.table("events").select("*").gte("last_seen", cutoff)
            if location:
                q = q.ilike("location", f"%{location}%")
            return q.limit(limit).execute().data or []

        rows = await asyncio.to_thread(_q)
        return rows or None

    async def fresh_trends(self, topic: Optional[str], limit: int) -> Optional[list[dict[str, Any]]]:
        if not self.enabled:
            return None
        cutoff = self._cutoff()

        def _q() -> list[dict[str, Any]]:
            q = self._client.table("trends").select("*").gte("captured_at", cutoff)
            if topic:
                q = q.ilike("captured_for", f"%{topic}%")
            return q.order("score", desc=True).limit(limit).execute().data or []

        rows = await asyncio.to_thread(_q)
        return rows or None

    # ── daily Instagram carousel (scraper.social) ──────────────────────────────
    async def query_events_for_day(
        self,
        location: str,
        start_iso: str,
        end_iso: str,
        limit: int = 400,
    ) -> list[dict[str, Any]]:
        """Approved events starting inside a HALF-OPEN [start, end) window.

        Deliberately a sibling of query_events() rather than a flag on it, for
        two reasons the difference matters here:

          * query_events() does NOT filter `status`, and this client is
            service_role (bypasses RLS) — reusing it would put unmoderated
            /submit rows straight onto the public Instagram feed.
          * query_events() uses `lte` on the upper bound, which includes an
            event starting at exactly 00:00 tomorrow in "today".

        query_events() is the MCP `query_stored` tool's contract, so its
        semantics are left alone.
        """
        if not self.enabled:
            return []

        def _q() -> list[dict[str, Any]]:
            return (
                self._client.table("events")
                .select("*, venues(*)")
                .eq("status", "approved")
                .ilike("location", f"%{location}%")
                .gte("start_time", start_iso)
                .lt("start_time", end_iso)
                .order("start_time", desc=False)
                .limit(limit)
                .execute()
                .data
                or []
            )

        return await asyncio.to_thread(_q)

    # ── knowledge-base export (scraper.kb) ────────────────────────────────────
    async def query_upcoming_events(
        self,
        location: str,
        start_iso: str,
        end_iso: str,
        page_size: int = 500,
    ) -> list[dict[str, Any]]:
        """Every approved event starting in [start, end), paginated.

        Unlike query_events_for_day this has no useful ceiling — the export
        covers a horizon of weeks, and a `limit` that silently truncated would
        drop real events out of the knowledge base with no error anywhere. So it
        pages until the source is exhausted rather than capping.
        """
        if not self.enabled:
            return []

        def _q(offset: int) -> list[dict[str, Any]]:
            return (
                self._client.table("events")
                .select("*, venues(*)")
                .eq("status", "approved")
                .ilike("location", f"%{location}%")
                .gte("start_time", start_iso)
                .lt("start_time", end_iso)
                .order("start_time", desc=False)
                .range(offset, offset + page_size - 1)
                .execute()
                .data
                or []
            )

        out: list[dict[str, Any]] = []
        offset = 0
        while True:
            page = await asyncio.to_thread(_q, offset)
            out.extend(page)
            if len(page) < page_size:
                return out
            offset += page_size

    async def recent_slide_keys(self, since_date: str) -> set[str]:
        """Every slide_key used by a post published on/after `since_date`.

        Drives recurrence suppression: a weekly event should not headline the
        carousel every single week.
        """
        if not self.enabled:
            return set()

        def _q() -> list[dict[str, Any]]:
            return (
                self._client.table("ig_posts")
                .select("slide_keys")
                .eq("status", "published")
                .gte("post_date", since_date)
                .execute()
                .data
                or []
            )

        rows = await asyncio.to_thread(_q)
        return {k for r in rows for k in (r.get("slide_keys") or [])}

    async def create_ig_draft(self, row: dict[str, Any]) -> Optional[dict[str, Any]]:
        """Insert a new ig_posts row. Returns None when the partial unique index
        rejects it (a live post already exists for that date) — a normal,
        expected outcome when a build is re-run, not an error."""
        if not self.enabled:
            return None

        def _q() -> Optional[dict[str, Any]]:
            try:
                res = self._client.table("ig_posts").insert(row).execute()
            except Exception as exc:  # noqa: BLE001
                log.warning("ig draft insert rejected (live post already exists?): %s", exc)
                return None
            data = res.data or []
            return data[0] if data else None

        return await asyncio.to_thread(_q)

    async def claim_ig_post(self, post_id: str, expect: str = "approved") -> bool:
        """Compare-and-swap a row into 'publishing'. True only for the winner.

        The publish sweep runs on a cron, and a slow run WILL eventually overlap
        the next one; without this both workers would build carousels from the
        same row and double-post. The conditional UPDATE takes a row lock, so
        the loser's filter matches nothing and it gets back zero rows.
        """
        if not self.enabled:
            return False
        now = datetime.now(timezone.utc).isoformat()

        def _q() -> bool:
            res = (
                self._client.table("ig_posts")
                .update({"status": "publishing", "claimed_at": now})
                .eq("id", post_id)
                .eq("status", expect)
                .execute()
            )
            return len(res.data or []) == 1

        return await asyncio.to_thread(_q)

    async def drafts_past_deadline(self, now_iso: Optional[str] = None) -> list[dict[str, Any]]:
        """Drafts whose auto_approve_at has arrived and that nobody touched.

        A NULL auto_approve_at is excluded, not treated as "immediately due":
        rows built before opt-out posting existed have no deadline, and reading
        NULL as "overdue" would publish a month of stale backlog on first run.
        """
        if not self.enabled:
            return []
        now = now_iso or datetime.now(timezone.utc).isoformat()

        def _q() -> list[dict[str, Any]]:
            return (
                self._client.table("ig_posts")
                .select("*")
                .eq("status", "draft")
                .not_.is_("auto_approve_at", "null")
                .lte("auto_approve_at", now)
                .order("post_date", desc=False)
                .execute()
                .data
                or []
            )

        return await asyncio.to_thread(_q)

    async def auto_approve_ig_post(self, post_id: str) -> bool:
        """CAS a draft into 'approved' because its deadline passed. True only
        for the winner — same idiom as claim_ig_post, and for the same reason:
        a human tapping Cancel at 16:59:59 must beat the sweep, and whichever
        of the two loses has to lose cleanly rather than overwrite the other."""
        if not self.enabled:
            return False

        def _q() -> bool:
            res = (
                self._client.table("ig_posts")
                .update({"status": "approved", "approved_by": "auto"})
                .eq("id", post_id)
                .eq("status", "draft")
                .execute()
            )
            return len(res.data or []) == 1

        return await asyncio.to_thread(_q)

    async def get_ig_post(self, post_id: str) -> Optional[dict[str, Any]]:
        if not self.enabled:
            return None

        def _q() -> Optional[dict[str, Any]]:
            rows = (
                self._client.table("ig_posts").select("*").eq("id", post_id).execute().data or []
            )
            return rows[0] if rows else None

        return await asyncio.to_thread(_q)

    async def pending_edits(self, post_id: Optional[str] = None) -> list[dict[str, Any]]:
        """Unapplied edit requests, oldest first — apply order is request order."""
        if not self.enabled:
            return []

        def _q() -> list[dict[str, Any]]:
            q = (
                self._client.table("ig_post_edits")
                .select("*")
                .is_("applied_at", "null")
            )
            if post_id:
                q = q.eq("post_id", post_id)
            return q.order("requested_at", desc=False).execute().data or []

        return await asyncio.to_thread(_q)

    async def has_unapplied_edits(self, post_id: str) -> bool:
        """Gate on the auto-approve sweep.

        A post with an edit still in flight must not ship: publishing a
        carousel that still contains the event someone explicitly asked to
        remove is worse than publishing late.
        """
        return bool(await self.pending_edits(post_id))

    async def mark_edits_applied(
        self, edit_ids: list[str], error: Optional[str] = None
    ) -> None:
        if not self.enabled or not edit_ids:
            return
        patch: dict[str, Any] = {"applied_at": datetime.now(timezone.utc).isoformat()}
        if error:
            patch["error"] = error[:500]

        def _q() -> None:
            self._client.table("ig_post_edits").update(patch).in_("id", edit_ids).execute()

        try:
            await asyncio.to_thread(_q)
        except Exception as exc:  # noqa: BLE001
            log.error("marking edits applied failed: %s", exc)

    async def request_ig_edit(
        self, post_id: str, op: str, payload: dict[str, Any]
    ) -> Optional[dict[str, Any]]:
        if not self.enabled:
            return None

        def _q() -> Optional[dict[str, Any]]:
            data = (
                self._client.table("ig_post_edits")
                .insert({"post_id": post_id, "op": op, "payload": payload})
                .execute()
                .data
                or []
            )
            return data[0] if data else None

        return await asyncio.to_thread(_q)

    async def events_by_ids(self, ids: list[str]) -> list[dict[str, Any]]:
        """Fetch events by id. Order is NOT preserved — callers that care
        (every rebuild does) must re-impose the post's own stored order."""
        if not self.enabled or not ids:
            return []

        def _q() -> list[dict[str, Any]]:
            return (
                self._client.table("events").select("*").in_("id", ids).execute().data or []
            )

        return await asyncio.to_thread(_q)

    async def apply_ig_post_edit_result(
        self, post_id: str, patch: dict[str, Any]
    ) -> bool:
        """Final CAS of a rebuild: write the new slides only if the post is
        still a draft.

        Zero rows means a human approved (or the sweep auto-approved) it while
        the rebuild was running — the carousel may already be at Meta, so the
        rebuild must abandon rather than overwrite what is being published.
        """
        if not self.enabled:
            return False

        def _q() -> bool:
            res = (
                self._client.table("ig_posts")
                .update(patch)
                .eq("id", post_id)
                .eq("status", "draft")
                .execute()
            )
            return len(res.data or []) == 1

        return await asyncio.to_thread(_q)

    async def due_for_metrics(self, window_hours: int, label: str) -> list[dict[str, Any]]:
        """Published posts old enough for this window and missing its snapshot.

        Deliberately "old enough AND not yet collected" rather than "published
        exactly N hours ago": that makes the collector idempotent and
        self-healing, so a skipped run backfills on the next sweep instead of
        losing the window forever. Bounded to 30 days so enabling a new window
        does not trigger a backfill over the account's whole history.
        """
        if not self.enabled:
            return []
        now = datetime.now(timezone.utc)
        older_than = (now - timedelta(hours=window_hours)).isoformat()
        floor = (now - timedelta(days=30)).isoformat()

        def _q() -> list[dict[str, Any]]:
            posts = (
                self._client.table("ig_posts")
                .select("id,post_date,ig_media_id,approved_by")
                .eq("status", "published")
                .not_.is_("ig_media_id", "null")
                .lte("published_at", older_than)
                .gte("published_at", floor)
                .execute()
                .data
                or []
            )
            if not posts:
                return []
            # Anti-join in a second round trip: PostgREST has no NOT IN
            # (subquery), and the candidate set here is at most ~30 rows.
            done = (
                self._client.table("ig_post_metrics")
                .select("post_id")
                .eq("window_label", label)
                .in_("post_id", [p["id"] for p in posts])
                .execute()
                .data
                or []
            )
            seen = {r["post_id"] for r in done}
            return [p for p in posts if p["id"] not in seen]

        return await asyncio.to_thread(_q)

    async def record_ig_metrics(self, post_id: str, label: str, row: dict[str, Any]) -> None:
        """Upsert one snapshot, keyed by the (post_id, window_label) index."""
        if not self.enabled:
            return
        payload = {**row, "post_id": post_id, "window_label": label}

        def _q() -> None:
            self._client.table("ig_post_metrics").upsert(
                payload, on_conflict="post_id,window_label"
            ).execute()

        try:
            await asyncio.to_thread(_q)
        except Exception as exc:  # noqa: BLE001
            log.error("ig metrics upsert failed for %s/%s: %s", post_id, label, exc)

    async def recent_metrics_baseline(
        self, label: str = "t24", limit: int = 7
    ) -> Optional[dict[str, float]]:
        """Mean reach over the most recent `limit` snapshots.

        Context for the digest: a bare reach number is unreadable to someone
        who does not already know what normal looks like for this account.
        """
        if not self.enabled:
            return None

        def _q() -> list[dict[str, Any]]:
            return (
                self._client.table("ig_post_metrics")
                .select("reach")
                .eq("window_label", label)
                .not_.is_("reach", "null")
                .order("fetched_at", desc=True)
                .limit(limit)
                .execute()
                .data
                or []
            )

        rows = await asyncio.to_thread(_q)
        values = [r["reach"] for r in rows if isinstance(r.get("reach"), int)]
        if not values:
            return None
        return {"reach": sum(values) / len(values), "n": float(len(values))}

    async def update_ig_post(self, post_id: str, patch: dict[str, Any]) -> None:
        if not self.enabled:
            return

        def _q() -> None:
            self._client.table("ig_posts").update(patch).eq("id", post_id).execute()

        try:
            await asyncio.to_thread(_q)
        except Exception as exc:  # noqa: BLE001
            log.error("ig post update failed for %s: %s", post_id, exc)

    async def ig_posts_by_status(
        self, status: str, post_date: Optional[str] = None
    ) -> list[dict[str, Any]]:
        if not self.enabled:
            return []

        def _q() -> list[dict[str, Any]]:
            q = self._client.table("ig_posts").select("*").eq("status", status)
            if post_date:
                q = q.eq("post_date", post_date)
            return q.order("post_date", desc=False).execute().data or []

        return await asyncio.to_thread(_q)

    async def approved_ready_to_publish(
        self, post_date: Optional[str] = None
    ) -> list[dict[str, Any]]:
        """Approved rows whose scheduled_for has arrived (or was never set —
        NULL means no restriction, so old rows and IG_AUTOPOST's immediate
        approve both publish on the very next sweep, same as before this
        existed)."""
        if not self.enabled:
            return []
        now = datetime.now(timezone.utc).isoformat()

        def _q() -> list[dict[str, Any]]:
            q = (
                self._client.table("ig_posts")
                .select("*")
                .eq("status", "approved")
                .or_(f"scheduled_for.is.null,scheduled_for.lte.{now}")
            )
            if post_date:
                q = q.eq("post_date", post_date)
            return q.order("post_date", desc=False).execute().data or []

        return await asyncio.to_thread(_q)

    # ── internals ───────────────────────────────────────────────────────────────
    def _merge_with_existing(self, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Fold rows that are the same real event as something already stored
        (same venue, same calendar day, near-identical title) into that
        existing row's ticket_links instead of inserting a second card for
        it. Returns the rows that should still go through the normal
        content_hash upsert — new events, plus any row with no resolved venue
        (a venue-less title-only cross-run match would be unreliable, so
        those are left to the plain upsert path unmerged).
        """
        candidates = [r for r in rows if r.get("venue_id") and r.get("start_time")]
        if not candidates:
            return rows

        venue_ids = list({r["venue_id"] for r in candidates})
        # Local calendar days, and a window whose edges are LOCAL midnights.
        # A 7pm show is 01:00Z the next day, so a UTC-midnight window would drop
        # every evening event on the last day of the range and the cross-run
        # merge would insert a duplicate card for it instead of merging.
        tz = event_tz()
        days = [local_day(r["start_time"]).date() for r in candidates]
        window_start = datetime.combine(min(days), datetime.min.time(), tzinfo=tz)
        window_end = datetime.combine(
            max(days) + timedelta(days=1), datetime.min.time(), tzinfo=tz
        )

        try:
            existing = (
                self._client.table("events")
                .select(
                    "id,title,venue_id,start_time,description,image_url,end_time,"
                    "categories,ticket_links"
                )
                .eq("status", "approved")
                .in_("venue_id", venue_ids)
                .gte("start_time", window_start.isoformat())
                .lt("start_time", window_end.isoformat())
                .execute()
                .data
                or []
            )
        except Exception as exc:  # noqa: BLE001 - a failed lookup must not break the upsert
            log.warning("existing-event lookup failed, skipping cross-run merge: %s", exc)
            return rows

        by_venue: dict[str, list[dict[str, Any]]] = {}
        for e in existing:
            by_venue.setdefault(e["venue_id"], []).append(e)

        kept: list[dict[str, Any]] = []
        merged_count = 0
        for row in rows:
            match = self._find_duplicate(row, by_venue.get(row.get("venue_id"), []))
            if match is None:
                kept.append(row)
            else:
                self._apply_merge(match, row)
                merged_count += 1
        if merged_count:
            log.info("merged %d event(s) into existing rows (new ticket link, not a new card)", merged_count)
        return kept

    @staticmethod
    def _find_duplicate(
        row: dict[str, Any], same_venue_existing: list[dict[str, Any]]
    ) -> Optional[dict[str, Any]]:
        if not same_venue_existing or not row.get("start_time"):
            return None
        # Both sides go through local_day: the incoming row carries a local
        # offset, the stored one comes back from Postgres in UTC, and comparing
        # those two .date() values directly filed the same evening event under
        # two different days.
        row_day = local_day(row["start_time"]).date()
        row_title = row.get("title") or ""
        for existing in same_venue_existing:
            existing_start = existing.get("start_time")
            if not existing_start:
                continue
            existing_local = local_day(existing_start)
            if existing_local is None or existing_local.date() != row_day:
                continue
            if _is_same_event_title(row_title, existing.get("title") or ""):
                return existing
        return None

    def _apply_merge(self, existing: dict[str, Any], row: dict[str, Any]) -> None:
        """Targeted UPDATE of an already-stored event: union ticket_links and
        categories, backfill fields the existing row is missing. Never
        overwrites a field the existing row already has, and never touches
        the row's id/content_hash, so links elsewhere (the /events/[id] URL,
        anything already pointing at this row) keep working."""
        existing_links = [TicketLink(**tl) for tl in (existing.get("ticket_links") or [])]
        new_links = [TicketLink(**tl) for tl in (row.get("ticket_links") or [])]
        patch: dict[str, Any] = {
            "ticket_links": [tl.model_dump() for tl in _merge_ticket_links(existing_links, new_links)]
        }

        existing_categories = existing.get("categories") or []
        merged_categories = list(dict.fromkeys([*existing_categories, *(row.get("categories") or [])]))
        if merged_categories != existing_categories:
            patch["categories"] = merged_categories

        for field in ("description", "image_url", "end_time"):
            if not existing.get(field) and row.get(field):
                patch[field] = row[field]

        try:
            self._client.table("events").update(patch).eq("id", existing["id"]).execute()
        except Exception as exc:  # noqa: BLE001
            log.warning("merge-update failed for event %s: %s", existing["id"], exc)

    def _upsert_venues(self, events: list[Event]) -> dict[str, str]:
        """Upsert one venue row per distinct address_hash; return hash -> venue id.

        Runs synchronously (called via asyncio.to_thread). Any failure is logged and
        yields {} so the caller stores events with venue_id = None instead of failing.
        """
        try:
            by_hash: dict[str, dict[str, Any]] = {}
            for e in events:
                if not (e.venue or e.location):
                    continue  # virtual / venue-less event
                row = _venue_row(e)
                kept = by_hash.get(row["address_hash"])
                # Prefer the duplicate that actually has coordinates.
                if kept is None or (kept.get("lat") is None and row.get("lat") is not None):
                    by_hash[row["address_hash"]] = row
            if not by_hash:
                return {}
            self._resolve_coords(by_hash)
            self._client.table("venues").upsert(
                list(by_hash.values()), on_conflict="address_hash"
            ).execute()
            res = (
                self._client.table("venues")
                .select("id,address_hash")
                .in_("address_hash", list(by_hash))
                .execute()
            )
            return {r["address_hash"]: r["id"] for r in (res.data or [])}
        except Exception as exc:  # noqa: BLE001 - venues must never break the event upsert
            log.error("venue upsert failed, storing events without venue_id: %s", exc)
            return {}

    def _resolve_coords(self, by_hash: dict[str, dict[str, Any]]) -> None:
        """Fill in venue lat/lng in place, before the upsert.

        Two jobs, both mattering for the map:

        1. PRESERVE. The upsert writes every column it is given, so a row carrying
           lat=None would blank out coordinates the venue already has in the DB
           (from a previous geocode or backfill). Existing values are copied into
           the outgoing row so a coordinate-less source can never erase them.
        2. GEOCODE. Venues still without coordinates are geocoded from their
           address, capped per run because Nominatim allows ~1 req/s. Anything
           left over is picked up next run or by `scraper.backfill_geocode`.

        Best-effort throughout: any failure leaves coordinates as-is.
        """
        missing = [h for h, r in by_hash.items() if r.get("lat") is None or r.get("lng") is None]
        if not missing:
            return

        # 1. preserve whatever the DB already knows
        try:
            res = (
                self._client.table("venues")
                .select("address_hash,lat,lng")
                .in_("address_hash", missing)
                .execute()
            )
            for r in res.data or []:
                if r.get("lat") is not None and r.get("lng") is not None:
                    row = by_hash.get(r["address_hash"])
                    if row is not None:
                        row["lat"], row["lng"] = r["lat"], r["lng"]
        except Exception as exc:  # noqa: BLE001
            log.warning("could not read existing venue coords (skipping preserve): %s", exc)

        if not settings.geocode_venues:
            return

        # 2. geocode what's still blank
        todo = [h for h in missing if by_hash[h].get("lat") is None][: settings.geocode_max_per_run]
        if not todo:
            return

        try:
            from .geocode import geocode
        except Exception as exc:  # noqa: BLE001
            log.warning("geocoding unavailable: %s", exc)
            return

        found = 0
        for h in todo:
            row = by_hash[h]
            try:
                hit = geocode(row.get("address"), row.get("name"))
            except Exception as exc:  # noqa: BLE001 - never break a scrape over a map pin
                log.warning("geocode failed for %r: %s", row.get("name"), exc)
                continue
            if hit:
                row["lat"], row["lng"] = hit
                found += 1
        log.info("geocoded %d/%d new venue(s)", found, len(todo))

    def _upsert(self, table: str, rows: list[dict[str, Any]], on_conflict: str) -> None:
        try:
            self._client.table(table).upsert(rows, on_conflict=on_conflict).execute()
        except Exception as exc:  # noqa: BLE001
            log.error("upsert into %s failed: %s", table, exc)

    @staticmethod
    def _cutoff() -> str:
        return (datetime.now(timezone.utc) - timedelta(hours=settings.freshness_hours)).isoformat()
