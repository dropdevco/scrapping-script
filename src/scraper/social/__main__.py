"""CLI for the daily Instagram carousel.

    python -m scraper.social build   [--date YYYY-MM-DD] [--dry-run] [--out DIR]
    python -m scraper.social autoapprove [--date YYYY-MM-DD] [--dry-run]
    python -m scraper.social publish [--date YYYY-MM-DD] [--dry-run]
    python -m scraper.social prune
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo

from ..core.config import settings
from ..core.http import HttpClient
from ..core.storage import Storage
from ..sources import auth_meta
from . import caption as caption_mod
from . import notify as notify_mod
from . import publish as publish_mod
from . import render, selection, slides_store
from . import telegram as telegram_mod
from .imaging import fetch_photo

log = logging.getLogger("scraper.social")

CITY = "El Paso"

# Publish attempts before a post is given up on. Meta intermittently
# refuses to fetch slide URLs, and one bad sweep should not cost the day.
MAX_PUBLISH_ATTEMPTS = 3


def _today(tz_name: str) -> date:
    return datetime.now(ZoneInfo(tz_name)).date()


def _suggested_schedule(day: date, tz_name: str, hour: int) -> str:
    """A best-effort publish time: `hour` local on the post's own day, or
    right now if that's already in the past (a same-day post can't wait for
    tomorrow's slot — the staleness guard would just expire it)."""
    tz = ZoneInfo(tz_name)
    now = datetime.now(tz)
    suggested = datetime.combine(day, time(hour, 0), tzinfo=tz)
    if suggested <= now:
        suggested = now
    return suggested.astimezone(timezone.utc).isoformat()


def _parse_slots(raw: str, default_hour: int) -> list[tuple[Optional[str], int]]:
    """`"morning:11,evening:18"` -> `[("morning", 11), ("evening", 18)]`.

    Empty input is the pre-multi-slot default: one unnamed digest at
    `default_hour` — byte-identical to how `build()` behaved before slots
    existed, so leaving IG_DIGEST_SLOTS unset changes nothing.
    """
    raw = raw.strip()
    if not raw:
        return [(None, default_hour)]
    slots: list[tuple[Optional[str], int]] = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        name, _, hour_str = part.partition(":")
        try:
            hour = int(hour_str)
        except ValueError:
            hour = default_hour
        slots.append((name.strip() or None, hour))
    return slots or [(None, default_hour)]


# ── build ──────────────────────────────────────────────────────────────────────
async def build(day: Optional[date], dry_run: bool, out_dir: Optional[str]) -> int:
    tz_name = settings.ig_timezone
    day = day or _today(tz_name)
    storage = Storage()
    if not storage.enabled and not dry_run:
        log.error("Supabase is not configured; nothing to do.")
        return 1

    slots = _parse_slots(settings.ig_digest_slots, settings.ig_suggested_publish_hour)
    rc = 0
    for slot_name, slot_hour in slots:
        rc |= await _build_one(storage, day, tz_name, dry_run, out_dir, slot_name, slot_hour)
    return rc


async def _build_one(
    storage: Storage,
    day: date,
    tz_name: str,
    dry_run: bool,
    out_dir: Optional[str],
    slot_name: Optional[str],
    slot_hour: int,
) -> int:
    label = f" [{slot_name}]" if slot_name else ""
    start_iso, end_iso = selection.day_bounds(day, tz_name)
    log.info("building carousel for %s%s (%s .. %s)", day, label, start_iso, end_iso)

    rows = await storage.query_events_for_day(CITY, start_iso, end_iso)
    log.info("%d approved El Paso event(s) today%s", len(rows), label)
    if not rows:
        return await _skip(storage, day, "no events for this date", dry_run, slot_name)

    # Recurrence suppression spans this window regardless of slot, so a later
    # slot the same day naturally down-ranks whatever an earlier slot already
    # published — no slot-aware selection logic needed.
    recent_keys = await storage.recent_slide_keys((day - timedelta(days=14)).isoformat())

    # Rank first, then fetch photos in rank order — so we only pay for
    # downloads we're likely to use. A dead/missing photo no longer drops the
    # event: render_event_slide has a text-only layout for exactly this case,
    # so the event keeps its slot with photo=None rather than losing it to a
    # lower-ranked candidate purely for lacking a source image.
    ranked = selection.choose(
        rows, tz_name=tz_name, recent_keys=recent_keys, max_slides=len(rows)
    )

    picked: list[selection.Candidate] = []
    photos: list[Any] = []
    async with HttpClient() as http:
        for cand in ranked:
            if len(picked) >= settings.ig_max_slides:
                break
            photo = await fetch_photo(http, cand.row.get("image_url"))
            picked.append(cand)
            photos.append(photo)

    with_photo = sum(1 for p in photos if p is not None)
    log.info("%d event(s) picked, %d with a photo%s", len(picked), with_photo, label)
    if len(picked) < settings.ig_min_slides:
        return await _skip(
            storage,
            day,
            f"only {len(picked)} usable slide(s), minimum is {settings.ig_min_slides}",
            dry_run,
            slot_name,
        )

    # Re-order to chronological for the reader, keeping each photo with its
    # event (they were gathered in rank order).
    paired = sorted(
        zip(picked, photos, strict=True),
        key=lambda pair: pair[0].start_local or datetime.max.replace(tzinfo=ZoneInfo(tz_name)),
    )
    picked = [c for c, _ in paired]
    photos = [p for _, p in paired]

    total = len(picked) + 1
    jpegs = [render.render_cover(day, len(picked), settings.ig_handle)]
    for i, (cand, photo) in enumerate(zip(picked, photos, strict=True), start=2):
        jpegs.append(render.render_event_slide(i, total, cand.row, photo, cand.start_local))

    text = caption_mod.build_caption(day, picked, site=settings.ig_handle)
    log.info("rendered %d slide(s), caption %d chars%s", len(jpegs), len(text), label)

    if out_dir:
        target = Path(out_dir) / (slot_name or ".")
        target.mkdir(parents=True, exist_ok=True)
        for i, blob in enumerate(jpegs):
            (target / f"{i:02d}.jpg").write_bytes(blob)
        (target / "caption.txt").write_text(text, encoding="utf-8")
        log.info("wrote preview to %s", target)

    if dry_run:
        log.info("dry run: not uploading or inserting a draft")
        return 0

    scheduled_for = _suggested_schedule(day, tz_name, slot_hour)
    # Reusing _suggested_schedule gives the deadline the same never-in-the-past
    # clamp: a draft built late (a re-run, a slow scrape) gets a deadline of
    # "now" and ships on the next sweep, rather than sitting until tomorrow
    # when the staleness guard would expire it unpublished.
    auto_approve_at = _suggested_schedule(day, tz_name, settings.ig_auto_approve_hour)
    draft = await storage.create_ig_draft(
        {
            "post_date": day.isoformat(),
            "status": "draft",
            "kind": "digest",
            "slot": slot_name,
            "event_ids": [str(c.row["id"]) for c in picked],
            "slide_keys": [c.key for c in picked],
            "caption": text,
            "scheduled_for": scheduled_for,
            "auto_approve_at": auto_approve_at,
            "window_start": start_iso,
            "window_end": end_iso,
        }
    )
    if draft is None:
        log.info("a live post already exists for %s%s; nothing inserted", day, label)
        return 0

    post_id = str(draft["id"])
    paths = [
        slides_store.object_path(day, post_id, i) for i in range(len(jpegs))
    ]
    try:
        slides_store.upload_slides(storage.client, settings.ig_slides_bucket, paths, jpegs)
    except Exception as exc:  # noqa: BLE001
        log.error("slide upload failed: %s", exc)
        await storage.update_ig_post(post_id, {"status": "failed", "error": f"upload: {exc}"})
        return 1

    await storage.update_ig_post(post_id, {"slide_paths": paths})
    log.info("draft %s ready for review (%d slides)%s", post_id, len(paths), label)

    if not settings.ig_autopost:
        # No point pinging for approval on a post that's about to auto-publish
        # itself in the next block.
        async with HttpClient() as http:
            await notify_mod.notify_draft_ready(
                http,
                storage_client=storage.client,
                post_id=post_id,
                day=day,
                slide_paths=paths,
                caption=text,
                scheduled_for=auto_approve_at if settings.ig_auto_approve else scheduled_for,
                slot=slot_name,
            )

    # Phase 2: same code path, no separate flag plumbing.
    if settings.ig_autopost:
        log.info("IG_AUTOPOST is on — approving and publishing immediately")
        await storage.update_ig_post(post_id, {"status": "approved"})
        return await publish(day, dry_run=False)
    return 0


async def _skip(
    storage: Storage, day: date, reason: str, dry_run: bool, slot_name: Optional[str] = None
) -> int:
    label = f" [{slot_name}]" if slot_name else ""
    log.info("skipping %s%s: %s", day, label, reason)
    if not dry_run and storage.enabled:
        await storage.create_ig_draft(
            {
                "post_date": day.isoformat(),
                "status": "skipped",
                "kind": "digest",
                "slot": slot_name,
                "error": reason,
            }
        )
    return 0


# ── autoapprove ────────────────────────────────────────────────────────────────
async def autoapprove(day: Optional[date] = None, dry_run: bool = False) -> int:
    """Flip untouched drafts whose deadline has passed to 'approved'.

    This is the whole of opt-out posting. The morning build files a draft and
    pings Telegram; the human has all day to cancel, postpone or edit it; and
    if the deadline arrives with the row still sitting in 'draft', silence is
    read as consent.

    Runs as a step immediately before `publish` in the same sweep, rather than
    on its own 17:00 cron. Two reasons: a post crossing its deadline then ships
    in that same run instead of up to 30 minutes later, and there is no second
    schedule to keep DST-correct — the deadline is a stored timestamptz, so the
    comparison is right in both halves of the year no matter when the sweep
    happens to fire.
    """
    if not settings.ig_auto_approve:
        log.info("IG_AUTO_APPROVE is off — leaving drafts for a human to approve")
        return 0
    if settings.ig_autopost:
        # Nothing to do: IG_AUTOPOST already published at build time, so there
        # is no draft to age out. Say so rather than looking like a no-op.
        log.warning("IG_AUTOPOST is on, so posts never reach 'draft' — autoapprove is inert")
        return 0

    tz_name = settings.ig_timezone
    today = _today(tz_name)
    storage = Storage()
    if not storage.enabled:
        log.error("Supabase is not configured; nothing to do.")
        return 1

    pending = await storage.drafts_past_deadline()
    if day:
        pending = [r for r in pending if str(r.get("post_date")) == day.isoformat()]
    if not pending:
        log.info("no drafts past their deadline")
        return 0

    approved = 0
    for row in pending:
        post_id = str(row["id"])
        post_date = date.fromisoformat(str(row["post_date"]))

        if post_date < today:
            # Never resurrect a stale draft. _publish_one would only expire it
            # a moment later anyway ("TODAY IN EL PASO — Sep 1" published on
            # Sep 3, every event already over), and leaving it in 'draft'
            # means the sweep re-examines it forever.
            log.info("draft %s is dated %s, today is %s — expiring", post_id, post_date, today)
            if not dry_run:
                await storage.update_ig_post(
                    post_id,
                    {"status": "expired", "error": f"deadline passed on {post_date}, never approved"},
                )
            continue
        if post_date > today:
            continue  # built ahead of time; its own day has not arrived

        if dry_run:
            log.info("dry run: would auto-approve %s (%s)", post_id, post_date)
            approved += 1
            continue

        if await storage.auto_approve_ig_post(post_id):
            log.info("auto-approved %s (deadline passed, no human action)", post_id)
            approved += 1
        else:
            # Not an error: a human cancelled or approved it between the query
            # above and this CAS. They win.
            log.info("draft %s changed status before the sweep reached it", post_id)

    log.info("auto-approved %d post(s)", approved)
    return 0


# ── publish ────────────────────────────────────────────────────────────────────
async def publish(day: Optional[date], dry_run: bool) -> int:
    tz_name = settings.ig_timezone
    today = _today(tz_name)
    storage = Storage()
    if not storage.enabled:
        log.error("Supabase is not configured; nothing to do.")
        return 1

    ig_id, token = publish_mod.account_credentials()
    if not (ig_id and token):
        log.error("IG_BUSINESS_ACCOUNT_ID / IG_ACCESS_TOKEN are not set.")
        return 1

    pending = await storage.approved_ready_to_publish(day.isoformat() if day else None)
    if not pending:
        log.info("nothing approved to publish")
        return 0

    rc = 0
    async with HttpClient() as http:
        for row in pending:
            rc |= await _publish_one(storage, http, row, today, ig_id, token, dry_run)
    return rc


async def _publish_one(
    storage: Storage,
    http: HttpClient,
    row: dict[str, Any],
    today: date,
    ig_id: str,
    token: str,
    dry_run: bool,
) -> int:
    post_id = str(row["id"])
    post_date = date.fromisoformat(str(row["post_date"]))

    # Staleness guard, before anything touches the network. A post approved late
    # that only succeeds the next morning would otherwise publish "TODAY IN EL
    # PASO — Aug 5" on Aug 6, with every event already over.
    if post_date != today:
        log.warning("post %s is dated %s, today is %s — expiring", post_id, post_date, today)
        await storage.update_ig_post(
            post_id, {"status": "expired", "error": f"post_date {post_date} != {today}"}
        )
        return 0

    if int(row.get("attempts") or 0) >= MAX_PUBLISH_ATTEMPTS:
        await storage.update_ig_post(
            post_id, {"status": "failed", "error": "attempt limit reached"}
        )
        return 1

    if not await storage.claim_ig_post(post_id):
        log.info("post %s was claimed by another run", post_id)
        return 0

    attempts = int(row.get("attempts") or 0) + 1
    paths = list(row.get("slide_paths") or [])
    if not paths:
        await storage.update_ig_post(
            post_id, {"status": "failed", "attempts": attempts, "error": "no slide_paths"}
        )
        return 1

    async def remember_container(container_id: str) -> None:
        # Persisted BEFORE media_publish — see publish_carousel's docstring.
        await storage.update_ig_post(post_id, {"ig_creation_id": container_id})

    try:
        urls = slides_store.signed_urls(storage.client, settings.ig_slides_bucket, paths)
        media_id = await publish_mod.publish_carousel(
            http,
            ig_id=ig_id,
            token=token,
            image_urls=urls,
            caption=str(row.get("caption") or ""),
            min_children=settings.ig_min_slides,
            on_container=remember_container,
            dry_run=dry_run,
        )
    except Exception as exc:  # noqa: BLE001
        log.error("publish failed for %s: %s", post_id, exc)

        # Hand the row back for another sweep instead of killing the day.
        #
        # 'failed' is terminal — approved_ready_to_publish only selects
        # 'approved' — so writing it here on the FIRST error made the
        # attempts<3 cap above dead code, and one transient hiccup (Meta
        # refusing to fetch the signed slide URLs is the common one; it took
        # out 3 of 25 days in one sample month) permanently lost that day's
        # post. Under opt-in nobody noticed, because most days were never
        # published anyway. Under opt-out posting this is the dominant
        # failure mode, so a retryable error now goes back to 'approved'.
        #
        # NOT retried once ig_creation_id exists: that id is persisted before
        # media_publish precisely so recovery knows the carousel reached Meta.
        # Retrying past that point risks a duplicate public post, which costs
        # far more than a missed one (see publish_carousel's docstring).
        reached_meta = bool(row.get("ig_creation_id"))
        retryable = attempts < MAX_PUBLISH_ATTEMPTS and not reached_meta
        patch = {"attempts": attempts, "error": str(exc)[:500]}
        patch["status"] = "approved" if retryable else "failed"
        await storage.update_ig_post(post_id, patch)

        if retryable:
            log.warning(
                "publish attempt %d/%d failed for %s — retrying on the next sweep",
                attempts,
                MAX_PUBLISH_ATTEMPTS,
                post_id,
            )
            return 0

        # Otherwise this is invisible on the Telegram side — the row is
        # correctly marked failed, but nobody watching the chat has any way
        # to tell "tried and failed" apart from "never ran".
        reason = "container already reached Meta" if reached_meta else "no attempts left"
        await notify_mod.notify_alert(
            http,
            f"Publish failed for {post_date.isoformat()} ({reason}, "
            f"attempt {attempts}/{MAX_PUBLISH_ATTEMPTS}): {str(exc)[:300]}",
        )
        return 1

    if dry_run:
        # Hand the row back so a real run can still publish it.
        await storage.update_ig_post(post_id, {"status": "approved", "attempts": attempts})
        log.info("dry run: containers validated for %s, not published", post_id)
        return 0

    await storage.update_ig_post(
        post_id,
        {
            "status": "published",
            "attempts": attempts,
            "ig_media_id": media_id,
            "published_at": datetime.now(timezone.utc).isoformat(),
            "error": None,
        },
    )
    log.info("published %s as media %s", post_id, media_id)
    return 0


# ── check-token ────────────────────────────────────────────────────────────────
async def check_token(min_days: int = 14) -> int:
    """Fail loudly while a token can still be rotated calmly.

    Long-lived Meta tokens last ~60 days, and a GitHub Actions workflow cannot
    rewrite its own secrets. Without this the first symptom of expiry is a
    silent run of 400s that nobody notices for a week.
    """
    _, token = publish_mod.account_credentials()
    if not token:
        log.error("IG_ACCESS_TOKEN is not set.")
        return 1

    # One client for the whole check, including any alert send below — a
    # closed HttpClient can't make further calls, so the alert has to happen
    # before this `with` exits, not after.
    async with HttpClient() as http:
        # Liveness first, and unconditionally. This is the check that works on
        # every token type: an expired, revoked, or blocked token fails /me
        # exactly like it fails a real publish. It is also the only check
        # available on graph.instagram.com, which has no /debug_token.
        try:
            identity = await publish_mod.token_identity(http, token)
        except Exception as exc:  # noqa: BLE001
            log.error("IG_ACCESS_TOKEN is not usable: %s", exc)
            await notify_mod.notify_alert(
                http, f"IG_ACCESS_TOKEN looks dead (/me failed): {str(exc)[:300]}"
            )
            return 1
        log.info("token is live for @%s", (identity or {}).get("username") or "?")

        if not publish_mod.TOKEN_INTROSPECTION_SUPPORTED:
            # Not a silent pass: say plainly that the days-remaining half of
            # this check cannot run here, so nobody reads a green check as
            # "expiry verified". The token stays refreshable — see the
            # refresh-token command — and liveness above still catches an
            # actual expiry the day it happens.
            log.info(
                "expiry not introspectable for an Instagram-Login token; "
                "long-lived tokens last ~60 days — run `python -m scraper.social "
                "refresh-token` to extend the window."
            )
            return 0

        if not (settings.meta_app_id and settings.meta_app_secret):
            log.warning("META_APP_ID/SECRET unset — cannot introspect token expiry.")
            return 0

        app_token = f"{settings.meta_app_id}|{settings.meta_app_secret}"
        try:
            days = await publish_mod.token_expires_in_days(http, token, app_token)
        except Exception as exc:  # noqa: BLE001
            # Introspection itself failing IS the finding — a dead/blocked
            # token fails this exact call the same way it fails a real
            # publish. Reporting that as "fine, must not expire" is the bug
            # that let a blocked token run silently for two days before
            # anyone noticed the account had stopped posting.
            log.error("IG_ACCESS_TOKEN introspection failed — token is likely dead: %s", exc)
            await notify_mod.notify_alert(
                http, f"IG_ACCESS_TOKEN looks dead (introspection failed): {str(exc)[:300]}"
            )
            return 1

        if days is None:
            log.info("token expiry unknown (this token type doesn't report one)")
            return 0
        if days < min_days:
            log.error("IG_ACCESS_TOKEN expires in %d day(s) — regenerate it now.", days)
            await notify_mod.notify_alert(
                http, f"IG_ACCESS_TOKEN expires in {days} day(s) — regenerate it soon."
            )
            return 1
        log.info("IG_ACCESS_TOKEN expires in %d day(s)", days)
    return 0


# ── check-telegram / telegram-webhook ──────────────────────────────────────────
async def check_telegram() -> int:
    """Fail loudly when the notification channel is broken.

    Every send in this codebase is best-effort by design — a failed ping must
    not fail a build that produced a perfectly good draft. The cost of that
    choice is that a dead bot is indistinguishable from a quiet week. This is
    the check that tells them apart, and it deliberately exits non-zero: with
    opt-out posting, a broken channel means posts ship with nobody able to
    stop them, which is worse than a noisy CI failure.
    """
    if not telegram_mod.configured():
        log.error("TELEGRAM_BOT_TOKEN is not set.")
        return 1

    expected = telegram_mod.webhook_url()
    async with HttpClient() as http:
        try:
            me = await telegram_mod.get_me(http)
            info = await telegram_mod.get_webhook_info(http)
        except Exception as exc:  # noqa: BLE001
            log.error("Telegram API is unreachable: %s", exc)
            await _alert_offline_channel(http, f"Telegram API unreachable: {str(exc)[:300]}")
            return 1

        log.info("bot is live as @%s", (me or {}).get("username") or "?")
        problems = telegram_mod.health_problems(info, expected)
        if not problems:
            log.info("webhook healthy at %s (0 pending)", info.get("url"))
            return 0

        for p in problems:
            log.error("telegram webhook: %s", p)
        await _alert_offline_channel(
            http, "Telegram webhook is unhealthy:\n" + "\n".join(f"- {p}" for p in problems)
        )
        return 1


async def _alert_offline_channel(http: HttpClient, text: str) -> None:
    """Report a Telegram problem somewhere that isn't Telegram.

    Email if Resend is configured; the non-zero exit that follows makes GitHub
    email the repo owner regardless. Both, because either alone gets missed.
    """
    sent = await notify_mod.send_email(
        http, "Chisme: Telegram notifications are broken", f"<pre>{text}</pre>"
    )
    if not sent:
        log.error("no email fallback configured — this job's failure is the only alert")


async def telegram_webhook(set_it: bool) -> int:
    if not telegram_mod.configured():
        log.error("TELEGRAM_BOT_TOKEN is not set.")
        return 1
    url = telegram_mod.webhook_url()
    async with HttpClient() as http:
        try:
            if set_it:
                if not settings.telegram_webhook_secret:
                    log.error(
                        "TELEGRAM_WEBHOOK_SECRET is not set. It must match the value the "
                        "webhook host (Vercel) checks, or every delivery will 401."
                    )
                    return 1
                info = await telegram_mod.set_webhook(
                    http, url, settings.telegram_webhook_secret
                )
                log.info("registered %s", url)
            else:
                info = await telegram_mod.get_webhook_info(http)
        except Exception as exc:  # noqa: BLE001
            log.error("%s", exc)
            return 1

    for key in (
        "url",
        "pending_update_count",
        "last_error_date",
        "last_error_message",
        "max_connections",
    ):
        if info.get(key) not in (None, ""):
            log.info("%-22s %s", key, info[key])
    problems = telegram_mod.health_problems(info, url)
    for p in problems:
        log.warning("problem: %s", p)
    return 1 if problems else 0


# ── refresh-token ──────────────────────────────────────────────────────────────
async def refresh_token() -> int:
    """Extend the long-lived token's window and print the replacement.

    Prints rather than writes: the token lives in `.env` locally and in a GitHub
    Actions secret in CI, and a workflow cannot rewrite its own secrets. So this
    is a deliberate, human-run rotation — run it, then paste the new value into
    both places. Meta will only extend a token that is still valid, so this has
    to be run inside the ~60-day window, not after it lapses.
    """
    _, token = publish_mod.account_credentials()
    if not token:
        log.error("IG_ACCESS_TOKEN is not set.")
        return 1

    async with HttpClient() as http:
        result = await auth_meta.refresh_long_lived(http, token)

    if result is None:
        log.error("token refresh failed — see the warning above. A token that has already "
                  "expired cannot be refreshed and must be re-minted through the app.")
        return 1

    fresh, expires_in = result
    days = expires_in // 86400
    if fresh == token:
        log.info("token unchanged; window now %d day(s)", days)
    else:
        log.info(
            "new token valid %d day(s) — update IG_ACCESS_TOKEN in .env AND in the "
            "repo's Actions secrets:",
            days,
        )
        log.info("%s", fresh)
    return 0


# ── prune ──────────────────────────────────────────────────────────────────────
async def prune() -> int:
    storage = Storage()
    if not storage.enabled:
        return 1
    cutoff = slides_store.retention_cutoff(
        _today(settings.ig_timezone), settings.ig_slide_retention_days
    )
    removed = slides_store.prune_before(storage.client, settings.ig_slides_bucket, cutoff)
    log.info("pruned %d slide object(s) dated before %s", removed, cutoff)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(prog="scraper.social", description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    b = sub.add_parser("build", help="render slides and create a draft")
    b.add_argument("--date")
    b.add_argument("--dry-run", action="store_true")
    b.add_argument("--out", help="also write slides to this directory")

    p = sub.add_parser("publish", help="publish approved posts")
    p.add_argument("--date")
    p.add_argument("--dry-run", action="store_true", help="build containers but do not publish")

    aa = sub.add_parser(
        "autoapprove", help="approve drafts whose auto-approve deadline has passed"
    )
    aa.add_argument("--date")
    aa.add_argument("--dry-run", action="store_true", help="report what would be approved")

    sub.add_parser("prune", help="delete slides past the retention window")

    sub.add_parser("refresh-token", help="extend the long-lived IG token and print the new one")
    sub.add_parser("check-telegram", help="fail if the Telegram webhook is unhealthy")
    tw = sub.add_parser("telegram-webhook", help="inspect or (re)register the Telegram webhook")
    tw.add_argument(
        "--set",
        dest="set_webhook",
        action="store_true",
        help="register SITE_BASE_URL/api/telegram/webhook (refuses a redirecting URL)",
    )
    t = sub.add_parser("check-token", help="fail if the IG token is close to expiry")
    t.add_argument("--min-days", type=int, default=14)

    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    day = date.fromisoformat(args.date) if getattr(args, "date", None) else None

    if args.cmd == "build":
        return asyncio.run(build(day, args.dry_run, args.out))
    if args.cmd == "publish":
        return asyncio.run(publish(day, args.dry_run))
    if args.cmd == "autoapprove":
        return asyncio.run(autoapprove(day, args.dry_run))
    if args.cmd == "refresh-token":
        return asyncio.run(refresh_token())
    if args.cmd == "check-token":
        return asyncio.run(check_token(args.min_days))
    if args.cmd == "check-telegram":
        return asyncio.run(check_telegram())
    if args.cmd == "telegram-webhook":
        return asyncio.run(telegram_webhook(args.set_webhook))
    return asyncio.run(prune())


if __name__ == "__main__":
    sys.exit(main())
