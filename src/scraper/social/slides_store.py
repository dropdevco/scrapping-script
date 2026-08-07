"""Rendered slides in Supabase Storage.

The bucket is PRIVATE and slides are handed out as signed URLs. Signed URLs are
still plain unauthenticated HTTPS GETs, so Meta's server-side cURL fetches them
fine — a public bucket would buy nothing and leave a permanently enumerable
hotlink surface over other people's event photos.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Any

log = logging.getLogger("scraper.social.slides_store")

# Signed-URL lifetime. Comfortably longer than the ~24h a media container lives,
# short enough that a leaked link goes stale on its own.
SIGN_TTL_SECONDS = 7 * 24 * 3600


def object_path(post_date: date, post_id: str, index: int) -> str:
    """Date-first so retention pruning is a cheap prefix sweep."""
    return f"{post_date.isoformat()}/{post_id}/{index:02d}.jpg"


def upload_slides(client: Any, bucket: str, paths: list[str], jpegs: list[bytes]) -> None:
    store = client.storage.from_(bucket)
    for path, blob in zip(paths, jpegs, strict=True):
        store.upload(
            path=path,
            file=blob,
            file_options={"content-type": "image/jpeg", "upsert": "true"},
        )


def signed_urls(
    client: Any, bucket: str, paths: list[str], expires_in: int = SIGN_TTL_SECONDS
) -> list[str]:
    """Signed URL per path, in the same order. Raises if any can't be signed —
    a carousel missing a slide is not something to paper over."""
    store = client.storage.from_(bucket)
    res = store.create_signed_urls(paths, expires_in)
    by_path = {
        (r.get("path") or "").lstrip("/"): (r.get("signedURL") or r.get("signedUrl"))
        for r in (res or [])
    }
    out: list[str] = []
    for path in paths:
        url = by_path.get(path.lstrip("/"))
        if not url:
            raise RuntimeError(f"could not sign slide {path}")
        out.append(url)
    return out


def prune_before(client: Any, bucket: str, cutoff: date) -> int:
    """Delete every slide folder dated before `cutoff`.

    Pruning on a lag rather than deleting at publish time is deliberate: one
    prefix sweep cleans up published, rejected, expired AND orphaned uploads
    (upload succeeded, row insert failed) with no per-status bookkeeping — and
    it leaves a half-failed publish something to retry against.
    """
    store = client.storage.from_(bucket)
    removed = 0
    try:
        day_dirs = store.list()
    except Exception as exc:  # noqa: BLE001
        log.warning("slide prune: could not list bucket %s: %s", bucket, exc)
        return 0

    for entry in day_dirs or []:
        name = entry.get("name") or ""
        try:
            day = date.fromisoformat(name)
        except ValueError:
            continue  # not a date folder; leave it alone
        if day >= cutoff:
            continue
        try:
            for post_dir in store.list(name) or []:
                sub = post_dir.get("name") or ""
                files = store.list(f"{name}/{sub}") or []
                paths = [f"{name}/{sub}/{f.get('name')}" for f in files if f.get("name")]
                if paths:
                    store.remove(paths)
                    removed += len(paths)
        except Exception as exc:  # noqa: BLE001
            log.warning("slide prune failed under %s: %s", name, exc)
    return removed


def retention_cutoff(today: date, days: int) -> date:
    return today - timedelta(days=days)
