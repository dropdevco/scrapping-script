"""Draft-ready notifications — Telegram (inline approve) and email (magic link).

Telegram is the auth boundary for its own approve/reject buttons (only the
admin's chat receives them, and the webhook checks a Telegram-supplied secret
plus the sender's chat id — see web/src/app/api/telegram/webhook/route.ts), so
no token is needed there. Email has no such channel-level guarantee, so its
link carries an HMAC-signed, expiring token that the Next.js review page
verifies independently (web/src/lib/ig/reviewToken.ts) — same secret,
same encoding, on both sides; get the format wrong here and every emailed
link breaks silently until someone reads the logs.

Neither channel's failure should ever fail the build — the draft still sits
in /admin/ig regardless of whether a ping went out, so each channel function
swallows and logs its own exceptions.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional

from ..core.config import settings
from ..core.http import HttpClient
from . import slides_store

log = logging.getLogger("scraper.social.notify")

TELEGRAM_MAX_MEDIA_GROUP = 10


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def make_review_token(post_id: str) -> Optional[str]:
    """Sign `{post_id}.{expiry}` for the token-gated review page.

    None when IG_NOTIFY_SECRET is unset — email notify no-ops in that case,
    Telegram's own inline buttons are unaffected.
    """
    if not settings.ig_notify_secret:
        return None
    expiry = int(
        (datetime.now(timezone.utc) + timedelta(hours=settings.ig_notify_ttl_hours)).timestamp()
    )
    payload = f"{post_id}.{expiry}".encode()
    sig = hmac.new(settings.ig_notify_secret.encode(), payload, hashlib.sha256).digest()
    return f"{_b64url(payload)}.{_b64url(sig)}"


async def notify_draft_ready(
    http: HttpClient,
    *,
    storage_client: Any,
    post_id: str,
    day: date,
    slide_paths: list[str],
    caption: str,
) -> None:
    review_url: Optional[str] = None
    token = make_review_token(post_id)
    if token:
        review_url = f"{settings.site_base_url}/admin/ig/review/{token}"

    await _notify_telegram(http, storage_client, post_id, day, slide_paths, caption, review_url)
    await _notify_email(http, day, review_url)


async def _notify_telegram(
    http: HttpClient,
    storage_client: Any,
    post_id: str,
    day: date,
    slide_paths: list[str],
    caption: str,
    review_url: Optional[str],
) -> None:
    if not (settings.telegram_bot_token and settings.telegram_chat_id):
        return
    base = f"https://api.telegram.org/bot{settings.telegram_bot_token}"
    try:
        urls = slides_store.signed_urls(
            storage_client, settings.ig_slides_bucket, slide_paths[:TELEGRAM_MAX_MEDIA_GROUP]
        )
        media = [{"type": "photo", "media": u} for u in urls]
        await http.post_json(
            f"{base}/sendMediaGroup", json={"chat_id": settings.telegram_chat_id, "media": media}
        )

        buttons = [
            [
                {"text": "✅ Approve & publish", "callback_data": f"apv:{post_id}"},
                {"text": "❌ Reject", "callback_data": f"rej:{post_id}"},
            ]
        ]
        if review_url:
            buttons.append([{"text": "\U0001f50d Full preview", "url": review_url}])

        # Plain text, no parse_mode: event titles routinely contain _ * [ ] —
        # any of which breaks Telegram's Markdown parser and drops the message.
        text = f"Today in El Paso — {day.isoformat()}\n\n{caption[:3500]}"
        await http.post_json(
            f"{base}/sendMessage",
            json={
                "chat_id": settings.telegram_chat_id,
                "text": text,
                "reply_markup": {"inline_keyboard": buttons},
            },
        )
    except Exception as exc:  # noqa: BLE001
        log.error("telegram notify failed: %s", exc)


async def _notify_email(http: HttpClient, day: date, review_url: Optional[str]) -> None:
    if not (settings.resend_api_key and settings.notify_admin_email):
        return
    if not review_url:
        log.warning(
            "IG_NOTIFY_SECRET or SITE_BASE_URL unset — skipping email, no review link to send"
        )
        return
    try:
        html = (
            f"<p>Today's Instagram carousel is ready to review.</p>"
            f'<p><a href="{review_url}">Review &amp; approve — {day.isoformat()}</a></p>'
            f"<p>Link expires in {settings.ig_notify_ttl_hours}h.</p>"
        )
        await http.post_json(
            "https://api.resend.com/emails",
            json={
                "from": settings.notify_email_from,
                "to": [settings.notify_admin_email],
                "subject": f"Chisme IG post ready — {day.isoformat()}",
                "html": html,
            },
            headers={"Authorization": f"Bearer {settings.resend_api_key}"},
        )
    except Exception as exc:  # noqa: BLE001
        log.error("email notify failed: %s", exc)
