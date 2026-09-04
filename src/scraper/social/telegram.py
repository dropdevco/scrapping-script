"""The one place Python talks to the Telegram Bot API.

Two failure modes motivate this module existing at all:

1. **Telegram answers HTTP 200 with ``{"ok": false}``** for a whole class of
   real errors — "chat not found", "bot was blocked by the user", "bot was
   kicked from the supergroup". ``HttpClient.post_json`` only raises on
   ``>=400``, so every one of those reads as success to a status-only check.
   ``call()`` checks both.

2. **A silent channel cannot report that it is silent.** Every send in this
   codebase is deliberately best-effort — a failed ping must never fail the
   build that produced a perfectly good draft. That is right, but it means the
   only evidence of a broken bot is a line in a CI log nobody reads. So the
   health of the channel is checked explicitly (``check-telegram``) rather than
   inferred from the absence of complaints.

The webhook registration helpers live here too, for a reason worth stating:
``setWebhook`` accepts a URL that redirects, and Telegram then refuses to
follow the redirect when delivering. The result is a registration that looks
successful and delivers nothing. ``set_webhook`` preflights for that.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from ..core.config import settings
from ..core.http import HttpClient

log = logging.getLogger("scraper.social.telegram")

API = "https://api.telegram.org"

# Updates we actually handle (see web/src/app/api/telegram/webhook/route.ts).
# Narrowing this is not cosmetic: Telegram queues every allowed update type,
# so subscribing to chatter we ignore inflates pending_update_count and makes
# the health check's backlog signal meaningless.
ALLOWED_UPDATES = ["message", "callback_query"]

# A healthy webhook drains continuously. A backlog this size means deliveries
# are failing (bad secret, 5xx, cold start timeouts) even when Telegram has
# not yet recorded a last_error_message.
MAX_PENDING_UPDATES = 20


class TelegramError(RuntimeError):
    """A Bot API call that did not succeed, including a 200 with ok=false."""


def configured() -> bool:
    return bool(settings.telegram_bot_token)


def webhook_url() -> str:
    return f"{settings.site_base_url.rstrip('/')}/api/telegram/webhook"


async def call(http: HttpClient, method: str, payload: Optional[dict] = None) -> Any:
    """POST a Bot API method and return its ``result``.

    Raises TelegramError on transport failure, on a non-2xx (post_json already
    raises there), and on a 200 body carrying ok=false — see this module's
    docstring for why that last case is the one that actually bites.
    """
    if not settings.telegram_bot_token:
        raise TelegramError("TELEGRAM_BOT_TOKEN is not set")
    url = f"{API}/bot{settings.telegram_bot_token}/{method}"
    try:
        body = await http.post_json(url, json=payload or {})
    except Exception as exc:  # noqa: BLE001
        # Never let the token reach a log line or an exception message that
        # might be forwarded to email or Telegram itself.
        raise TelegramError(f"{method} failed: {str(exc).replace(settings.telegram_bot_token, '***')}") from exc
    if not isinstance(body, dict) or not body.get("ok"):
        desc = (body or {}).get("description") if isinstance(body, dict) else None
        raise TelegramError(f"{method} returned ok=false: {desc or body!r}")
    return body.get("result")


async def get_me(http: HttpClient) -> dict:
    return await call(http, "getMe")


async def get_webhook_info(http: HttpClient) -> dict:
    return await call(http, "getWebhookInfo")


async def _redirects(http: HttpClient, url: str) -> Optional[str]:
    """Return the redirect target if `url` answers a 3xx, else None.

    Telegram does not follow redirects when delivering an update, but
    setWebhook happily accepts a redirecting URL. The classic instance is a
    bare apex domain that 308s to www — the registration succeeds, every
    delivery then fails, and getWebhookInfo is the only place that says so.
    """
    try:
        resp = await http.request("POST", url, follow_redirects=False)
    except Exception as exc:  # noqa: BLE001
        log.warning("webhook preflight could not reach %s: %s", url, exc)
        return None
    if 300 <= resp.status_code < 400:
        return resp.headers.get("location") or "(no Location header)"
    return None


async def set_webhook(http: HttpClient, url: str, secret: str) -> dict:
    """Register `url`, refusing to do so if it redirects."""
    target = await _redirects(http, url)
    if target:
        raise TelegramError(
            f"{url} redirects to {target}. Telegram does not follow redirects when "
            "delivering updates, so registering this URL would silently deliver "
            "nothing. Set SITE_BASE_URL to the final host and retry."
        )
    await call(
        http,
        "setWebhook",
        {
            "url": url,
            "secret_token": secret,
            "allowed_updates": ALLOWED_UPDATES,
        },
    )
    return await get_webhook_info(http)


def health_problems(info: dict, expected_url: str) -> list[str]:
    """Read a getWebhookInfo payload and return everything wrong with it.

    Split out from the CLI so it is testable without a network round-trip.
    """
    problems: list[str] = []
    url = (info or {}).get("url") or ""
    if not url:
        problems.append("no webhook is registered (url is empty)")
    elif url != expected_url:
        problems.append(f"registered url is {url}, expected {expected_url}")
    if info.get("last_error_message"):
        problems.append(
            f"last delivery failed: {info['last_error_message']} "
            f"(at {info.get('last_error_date')})"
        )
    pending = int(info.get("pending_update_count") or 0)
    if pending >= MAX_PENDING_UPDATES:
        problems.append(f"{pending} updates are queued undelivered")
    return problems
