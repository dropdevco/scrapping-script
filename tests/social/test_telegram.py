"""The Telegram Bot API wrapper: ok=false detection and webhook health.

Both behaviors here exist because their absence is invisible. A 200 carrying
``ok: false`` and a webhook registered at a redirecting URL both look exactly
like success from the caller's side, and the only symptom either produces is
that the bot quietly stops working.
"""

from __future__ import annotations

import httpx
import pytest

from scraper.core.config import settings
from scraper.social import telegram


class FakeHttp:
    """Stands in for HttpClient. `post_json` mirrors the real contract: it
    raises on >=400 rather than returning the body."""

    def __init__(self, body=None, *, status: int = 200, redirect_to: str | None = None):
        self.body = body if body is not None else {"ok": True, "result": {}}
        self.status = status
        self.redirect_to = redirect_to
        self.calls: list[tuple[str, dict]] = []

    async def post_json(self, url, **kwargs):
        self.calls.append((url, kwargs.get("json") or {}))
        if self.status >= 400:
            raise httpx.HTTPStatusError(f"{self.status} from {url}", request=None, response=None)
        return self.body

    async def request(self, method, url, **kwargs):
        if self.redirect_to:
            return httpx.Response(308, headers={"location": self.redirect_to})
        return httpx.Response(200)


@pytest.fixture(autouse=True)
def _token(monkeypatch):
    monkeypatch.setattr(settings, "telegram_bot_token", "123:ABC")


async def test_call_returns_result_on_ok():
    http = FakeHttp({"ok": True, "result": {"username": "epchisme_bot"}})
    assert await telegram.call(http, "getMe") == {"username": "epchisme_bot"}


async def test_call_raises_on_ok_false_despite_http_200():
    """The case a status-only check misses entirely: Telegram reports
    'chat not found' and 'bot was blocked' as a 200."""
    http = FakeHttp({"ok": False, "description": "chat not found"})
    with pytest.raises(telegram.TelegramError, match="chat not found"):
        await telegram.call(http, "sendMessage", {"chat_id": "1"})


async def test_call_raises_on_http_error():
    http = FakeHttp(status=401)
    with pytest.raises(telegram.TelegramError):
        await telegram.call(http, "getMe")


async def test_call_never_leaks_the_bot_token_into_the_error():
    """Errors from here reach CI logs and the email fallback."""
    http = FakeHttp(status=401)
    with pytest.raises(telegram.TelegramError) as exc:
        await telegram.call(http, "getMe")
    assert "123:ABC" not in str(exc.value)


async def test_set_webhook_refuses_a_redirecting_url_and_names_the_target():
    """A bare apex that 308s to www: setWebhook accepts it, then every
    delivery fails because Telegram does not follow redirects."""
    http = FakeHttp(redirect_to="https://www.epchisme.com/api/telegram/webhook")
    with pytest.raises(telegram.TelegramError) as exc:
        await telegram.set_webhook(http, "https://epchisme.com/api/telegram/webhook", "s3cret")
    assert "www.epchisme.com" in str(exc.value)
    assert http.calls == [], "must not register a URL it already knows is broken"


async def test_set_webhook_registers_when_the_url_is_direct():
    http = FakeHttp({"ok": True, "result": {"url": "https://www.epchisme.com/x"}})
    await telegram.set_webhook(http, "https://www.epchisme.com/x", "s3cret")
    methods = [url.rsplit("/", 1)[-1] for url, _ in http.calls]
    assert methods == ["setWebhook", "getWebhookInfo"]
    assert http.calls[0][1]["secret_token"] == "s3cret"
    assert http.calls[0][1]["allowed_updates"] == telegram.ALLOWED_UPDATES


EXPECTED = "https://www.epchisme.com/api/telegram/webhook"


def test_health_clean_webhook_has_no_problems():
    assert telegram.health_problems({"url": EXPECTED, "pending_update_count": 0}, EXPECTED) == []


def test_health_flags_a_last_error_message():
    problems = telegram.health_problems(
        {"url": EXPECTED, "last_error_message": "Wrong response from the webhook: 308"},
        EXPECTED,
    )
    assert len(problems) == 1 and "308" in problems[0]


def test_health_flags_a_url_that_drifted_from_site_base_url():
    problems = telegram.health_problems({"url": "https://epchisme.com/api/telegram/webhook"}, EXPECTED)
    assert len(problems) == 1 and "expected" in problems[0]


def test_health_flags_an_undelivered_backlog():
    """Deliveries can fail without Telegram having recorded an error yet;
    a growing queue is the earlier signal."""
    problems = telegram.health_problems(
        {"url": EXPECTED, "pending_update_count": telegram.MAX_PENDING_UPDATES}, EXPECTED
    )
    assert len(problems) == 1 and "queued" in problems[0]


def test_health_flags_no_registration_at_all():
    assert telegram.health_problems({"url": ""}, EXPECTED) == [
        "no webhook is registered (url is empty)"
    ]
