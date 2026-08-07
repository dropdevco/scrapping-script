"""make_review_token: the cross-language HMAC contract.

Python signs, web/src/lib/ig/reviewToken.ts verifies — get the payload shape
and base64url encoding pinned exactly here, since a mismatch on either side
breaks every emailed review link silently.
"""

from __future__ import annotations

import base64
import hashlib
import hmac

from scraper.core.config import settings
from scraper.social.notify import make_review_token


def _b64url_decode(s: str) -> bytes:
    padded = s + "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(padded)


def test_no_token_without_secret(monkeypatch):
    monkeypatch.setattr(settings, "ig_notify_secret", None)
    assert make_review_token("post-1") is None


def test_token_payload_and_signature_roundtrip(monkeypatch):
    monkeypatch.setattr(settings, "ig_notify_secret", "test-secret")
    monkeypatch.setattr(settings, "ig_notify_ttl_hours", 36)

    token = make_review_token("abc-123")
    assert token is not None

    payload_b64, sig_b64 = token.split(".")
    payload = _b64url_decode(payload_b64)
    post_id, expiry = payload.decode().split(".")
    assert post_id == "abc-123"
    assert expiry.isdigit()

    expected_sig = hmac.new(b"test-secret", payload, hashlib.sha256).digest()
    assert _b64url_decode(sig_b64) == expected_sig


def test_token_has_no_padding_characters(monkeypatch):
    # Node's Buffer.from(str, "base64url") tolerates missing padding but not
    # stray '=' characters in the input — confirm we never emit any.
    monkeypatch.setattr(settings, "ig_notify_secret", "another-secret")
    token = make_review_token("post-xyz")
    assert token is not None
    assert "=" not in token


def test_different_post_ids_produce_different_tokens(monkeypatch):
    monkeypatch.setattr(settings, "ig_notify_secret", "s")
    a = make_review_token("post-a")
    b = make_review_token("post-b")
    assert a != b
