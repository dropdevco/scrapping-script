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
from zoneinfo import ZoneInfo

from ..core.config import settings
from ..core.http import HttpClient
from . import slides_store, telegram

log = logging.getLogger("scraper.social.notify")

TELEGRAM_MAX_MEDIA_GROUP = 10


async def notify_alert(http: HttpClient, text: str) -> None:
    """A plain-text ping for things that need a human's attention but aren't
    a draft to review — a dead access token, a publish attempt that failed
    after a real Instagram round-trip. Without this, a failure that the code
    already caught and handled correctly (logged, written to the DB) is
    still invisible to whoever's waiting on Telegram: the bot went quiet, and
    "went quiet" and "is fine" look identical unless something says
    otherwise. Same best-effort contract as the rest of this module — a
    failure to alert must never raise into the caller."""
    if not (settings.telegram_bot_token and settings.telegram_chat_id):
        return
    try:
        await telegram.call(
            http, "sendMessage", {"chat_id": settings.telegram_chat_id, "text": text}
        )
    except Exception as exc:  # noqa: BLE001
        log.error("alert notify failed: %s", exc)


def _format_local_time(iso: str, tz_name: str) -> str:
    """12-hour clock without a leading zero, e.g. '5:00 PM'. Deliberately not
    strftime's %-I / %#I — those are platform-specific (glibc vs MSVC) and
    this needs to run identically in CI (Linux) and local dev (Windows)."""
    dt = datetime.fromisoformat(iso).astimezone(ZoneInfo(tz_name))
    hour12 = dt.hour % 12 or 12
    ampm = "AM" if dt.hour < 12 else "PM"
    return f"{hour12}:{dt.minute:02d} {ampm}"


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
    scheduled_for: Optional[str] = None,
    slot: Optional[str] = None,
) -> None:
    review_url: Optional[str] = None
    token = make_review_token(post_id)
    if token:
        review_url = f"{settings.site_base_url}/admin/ig/review/{token}"

    await _notify_telegram(
        http, storage_client, post_id, day, slide_paths, caption, review_url, scheduled_for, slot
    )
    await _notify_email(http, day, review_url, scheduled_for, slot)


async def _notify_telegram(
    http: HttpClient,
    storage_client: Any,
    post_id: str,
    day: date,
    slide_paths: list[str],
    caption: str,
    review_url: Optional[str],
    scheduled_for: Optional[str],
    slot: Optional[str],
) -> None:
    if not (settings.telegram_bot_token and settings.telegram_chat_id):
        return

    # Two INDEPENDENT sends, and the order matters. The media group is a
    # preview; the message below it carries the buttons that are the only way
    # to act on this draft from a phone. They used to share one try block,
    # which meant a single unreachable image URL — Meta and Telegram both
    # fetch these server-side, so any Supabase hiccup does it — swallowed the
    # buttons too, and the failure looked identical to "the bot went quiet".
    preview_failed = False
    try:
        urls = slides_store.signed_urls(
            storage_client, settings.ig_slides_bucket, slide_paths[:TELEGRAM_MAX_MEDIA_GROUP]
        )
        media = [{"type": "photo", "media": u} for u in urls]
        await telegram.call(
            http, "sendMediaGroup", {"chat_id": settings.telegram_chat_id, "media": media}
        )
    except Exception as exc:  # noqa: BLE001
        preview_failed = True
        log.error("telegram slide preview failed (buttons still sent): %s", exc)

    try:
        when = _format_local_time(scheduled_for, settings.ig_timezone) if scheduled_for else None
        buttons = [
            [
                {"text": "✅ Post now", "callback_data": f"now:{post_id}"},
                {"text": "🕗 Tomorrow", "callback_data": f"pos:{post_id}"},
            ],
            [
                {"text": "✏️ Caption", "callback_data": f"cap:{post_id}"},
                {"text": "🗑 Cancel", "callback_data": f"rej:{post_id}"},
            ],
        ]
        if review_url:
            buttons.append([{"text": "\U0001f50d Full preview", "url": review_url}])

        slot_label = f" — {slot.title()}" if slot else ""
        # State the opt-out contract in the message itself. A post that ships
        # unless you stop it, announced by a message whose main button says
        # "Approve", trains exactly the wrong habit.
        if settings.ig_auto_approve and when:
            lead = f"Goes out automatically at {when} unless you cancel."
        elif when:
            lead = f"Waiting for approval — suggested for {when}."
        else:
            lead = "Waiting for approval."
        head = f"Today in El Paso{slot_label} — {day.isoformat()}\n{lead}"
        if preview_failed:
            head += "\n⚠️ Slide preview failed to send; open the full preview to see them."
        # Plain text, no parse_mode: event titles routinely contain _ * [ ] —
        # any of which breaks Telegram's Markdown parser and drops the message.
        await telegram.call(
            http,
            "sendMessage",
            {
                "chat_id": settings.telegram_chat_id,
                "text": f"{head}\n\n{caption[:3500]}",
                "reply_markup": {"inline_keyboard": buttons},
            },
        )
    except Exception as exc:  # noqa: BLE001
        log.error("telegram notify failed: %s", exc)


async def send_email(http: HttpClient, subject: str, html: str) -> bool:
    """Best-effort Resend send. Returns whether it went out.

    Extracted so the "Telegram is broken" alert has somewhere to go that isn't
    Telegram — an alert delivered over the channel it is reporting on is not
    an alert.
    """
    if not (settings.resend_api_key and settings.notify_admin_email):
        return False
    try:
        await http.post_json(
            "https://api.resend.com/emails",
            json={
                "from": settings.notify_email_from,
                "to": [settings.notify_admin_email],
                "subject": subject,
                "html": html,
            },
            headers={"Authorization": f"Bearer {settings.resend_api_key}"},
        )
        return True
    except Exception as exc:  # noqa: BLE001
        log.error("email notify failed: %s", exc)
        return False


async def _notify_email(
    http: HttpClient,
    day: date,
    review_url: Optional[str],
    scheduled_for: Optional[str],
    slot: Optional[str],
) -> None:
    if not (settings.resend_api_key and settings.notify_admin_email):
        return
    if not review_url:
        log.warning(
            "IG_NOTIFY_SECRET or SITE_BASE_URL unset — skipping email, no review link to send"
        )
        return
    slot_label = f" — {slot.title()}" if slot else ""
    when = (
        f" Suggested to go out around {_format_local_time(scheduled_for, settings.ig_timezone)}."
        if scheduled_for
        else ""
    )
    html = (
        f"<p>Today's Instagram carousel{slot_label} is ready to review.{when}</p>"
        f'<p><a href="{review_url}">Review &amp; approve — {day.isoformat()}</a></p>'
        f"<p>Link expires in {settings.ig_notify_ttl_hours}h.</p>"
    )
    await send_email(http, f"Chisme IG post ready{slot_label} — {day.isoformat()}", html)
