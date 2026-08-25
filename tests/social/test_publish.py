"""token_expires_in_days must propagate an introspection failure, not
swallow it to None.

Regression coverage for a real incident: a blocked IG_ACCESS_TOKEN made the
/debug_token call itself fail with a 400 (same as every other Graph call
using that token), and the old code caught that exception and returned
None — which check_token() then reported as "expiry unknown, non-expiring".
The token check ran green for two days while the account had actually
stopped being able to post at all.
"""

from __future__ import annotations

import httpx
import pytest

from scraper.social.publish import token_expires_in_days


class _RaisingHttp:
    async def get_json(self, url, **kwargs):
        request = httpx.Request("GET", url)
        response = httpx.Response(400, request=request, json={"error": {"message": "blocked"}})
        raise httpx.HTTPStatusError("400 from debug_token", request=request, response=response)


class _WorkingHttp:
    def __init__(self, payload):
        self._payload = payload

    async def get_json(self, url, **kwargs):
        return self._payload


async def test_introspection_failure_raises_instead_of_returning_none():
    with pytest.raises(httpx.HTTPStatusError):
        await token_expires_in_days(_RaisingHttp(), "token", "app|secret")


async def test_successful_response_with_no_expiry_field_returns_none():
    # A real "this token type doesn't expire" response — still valid.
    http = _WorkingHttp({"data": {}})
    assert await token_expires_in_days(http, "token", "app|secret") is None


async def test_successful_response_with_expiry_returns_days():
    import time

    future = int(time.time()) + 5 * 86400
    http = _WorkingHttp({"data": {"expires_at": future}})
    days = await token_expires_in_days(http, "token", "app|secret")
    assert days in (4, 5)  # allow for the second or two the test itself takes


class _RecordingHttp:
    def __init__(self, payload=None):
        self.calls = []
        self._payload = payload or {"id": "1", "username": "elpasochisme"}

    async def get_json(self, url, **kwargs):
        self.calls.append((url, kwargs.get("params") or {}))
        return self._payload


async def test_token_identity_probes_me_not_debug_token():
    """graph.instagram.com has no /debug_token — it 500s on every call, healthy
    token or not — so liveness is checked against /me instead."""
    from scraper.social.publish import token_identity

    http = _RecordingHttp()
    identity = await token_identity(http, "IGAAtoken")
    assert identity["username"] == "elpasochisme"
    url, params = http.calls[0]
    assert url.endswith("/me")
    assert params["access_token"] == "IGAAtoken"


async def test_token_identity_propagates_a_failure():
    from scraper.social.publish import token_identity

    with pytest.raises(httpx.HTTPStatusError):
        await token_identity(_RaisingHttp(), "token")


def test_debug_token_is_gated_off_for_the_instagram_login_host():
    from scraper.social.publish import GRAPH, TOKEN_INTROSPECTION_SUPPORTED

    assert TOKEN_INTROSPECTION_SUPPORTED == GRAPH.startswith("https://graph.facebook.com")
