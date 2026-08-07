"""CLI for the daily Instagram carousel.

    python -m scraper.social build   [--date YYYY-MM-DD] [--dry-run] [--out DIR]
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
from . import caption as caption_mod
from . import notify as notify_mod
from . import publish as publish_mod
from . import render, selection, slides_store
from .imaging import fetch_photo

log = logging.getLogger("scraper.social")

CITY = "El Paso"


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

    # Rank first, then verify photos in rank order — so we only pay for
    # downloads we're likely to use, but still backfill from the next-best
    # candidate whenever a source image turns out to be dead.
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
            if photo is None:
                continue  # dead CDN link / hotlink-protected / too small
            picked.append(cand)
            photos.append(photo)

    log.info("%d event(s) have a usable photo%s", len(picked), label)
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
                scheduled_for=scheduled_for,
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

    if int(row.get("attempts") or 0) >= 3:
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
        await storage.update_ig_post(
            post_id, {"status": "failed", "attempts": attempts, "error": str(exc)[:500]}
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
    if not (settings.meta_app_id and settings.meta_app_secret):
        log.warning("META_APP_ID/SECRET unset — cannot introspect token expiry.")
        return 0

    app_token = f"{settings.meta_app_id}|{settings.meta_app_secret}"
    async with HttpClient() as http:
        days = await publish_mod.token_expires_in_days(http, token, app_token)
    if days is None:
        log.info("token expiry unknown (long-lived or non-expiring)")
        return 0
    if days < min_days:
        log.error("IG_ACCESS_TOKEN expires in %d day(s) — regenerate it now.", days)
        return 1
    log.info("IG_ACCESS_TOKEN expires in %d day(s)", days)
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

    sub.add_parser("prune", help="delete slides past the retention window")

    t = sub.add_parser("check-token", help="fail if the IG token is close to expiry")
    t.add_argument("--min-days", type=int, default=14)

    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    day = date.fromisoformat(args.date) if getattr(args, "date", None) else None

    if args.cmd == "build":
        return asyncio.run(build(day, args.dry_run, args.out))
    if args.cmd == "publish":
        return asyncio.run(publish(day, args.dry_run))
    if args.cmd == "check-token":
        return asyncio.run(check_token(args.min_days))
    return asyncio.run(prune())


if __name__ == "__main__":
    sys.exit(main())
