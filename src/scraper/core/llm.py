"""Minimal OpenRouter client — the only model-calling code in the project.

Deliberately not a vendor SDK. OpenRouter is an OpenAI-shaped JSON POST, which
means core/http.py already provides everything that actually matters here:
bounded concurrency, three retries with exponential backoff (429s are the
common failure), and error bodies surfaced rather than swallowed. The existing
Resend call in social/notify.py has the identical shape. Adding an SDK would
buy nothing and cost a dependency.

ONE exception type. Every caller does the same thing with every failure mode —
no key, network, rate limit, timeout, refusal, truncation, malformed JSON —
which is to carry on without the model. Distinguishing them at the call site
would be ceremony, so they all arrive as LLMUnavailable.

The defensive parsing is not paranoia; each branch is a real response observed
while choosing a model on 2026-09-20:
  - claude-haiku-4.5 wraps its JSON in a ```json fence despite response_format
  - gpt-5-nano spends its whole token budget on reasoning and returns content
    of None with finish_reason "length"
  - several models overrun an explicit character limit in the prompt, so
    length is enforced by the caller, never trusted from the model
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Optional

from .config import settings
from .http import HttpClient

log = logging.getLogger("scraper.llm")

_ENDPOINT = "https://openrouter.ai/api/v1/chat/completions"
_FENCE_OPEN = re.compile(r"^```(?:json)?\s*")
_FENCE_CLOSE = re.compile(r"\s*```$")


class LLMUnavailable(Exception):
    """We did not get a usable answer, for any reason. Carry on without one."""


def _unfence(text: str) -> str:
    t = text.strip()
    if t.startswith("```"):
        t = _FENCE_CLOSE.sub("", _FENCE_OPEN.sub("", t))
    return t.strip()


async def complete_json(
    http: HttpClient,
    *,
    system: str,
    user: str,
    model: Optional[str] = None,
    max_tokens: int = 700,
) -> dict[str, Any]:
    """One turn in, parsed JSON object out. Raises LLMUnavailable otherwise.

    Never returns a partial or empty result: a caller must not be able to read
    "the model said nothing" as "the model said no".
    """
    if not settings.openrouter_api_key:
        raise LLMUnavailable("OPENROUTER_API_KEY is not set")

    payload = {
        "model": model or settings.council_model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        # Honoured by most models and harmless on the rest; _unfence and the
        # json.loads below are what actually guarantee the shape.
        "response_format": {"type": "json_object"},
        "max_tokens": max_tokens,
    }

    try:
        body = await http.post_json(
            _ENDPOINT,
            json=payload,
            headers={"Authorization": f"Bearer {settings.openrouter_api_key}"},
            timeout=settings.council_timeout_seconds,
        )
    except Exception as exc:  # noqa: BLE001 - transport, status, timeout all mean the same thing
        raise LLMUnavailable(f"request failed: {exc}") from exc

    if not isinstance(body, dict):
        raise LLMUnavailable("response was not a JSON object")

    choices = body.get("choices") or []
    if not choices:
        raise LLMUnavailable(f"no choices in response: {str(body)[:200]}")

    choice = choices[0] or {}
    content = (choice.get("message") or {}).get("content")
    if not content:
        # A reasoning model that burned its budget before emitting anything.
        raise LLMUnavailable(f"empty content (finish_reason={choice.get('finish_reason')!r})")

    try:
        parsed = json.loads(_unfence(content))
    except (ValueError, TypeError) as exc:
        raise LLMUnavailable(f"content was not JSON: {content[:200]!r}") from exc

    if not isinstance(parsed, dict):
        raise LLMUnavailable(f"expected a JSON object, got {type(parsed).__name__}")
    return parsed
