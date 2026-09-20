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
_FENCED = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)


class LLMUnavailable(Exception):
    """We did not get a usable answer, for any reason. Carry on without one."""


def _json_candidates(text: str) -> list[str]:
    """Every plausible JSON object inside a reply, best first.

    Asking for JSON does not reliably get you only JSON. Observed from real
    replies: a bare object; an object inside a ```json fence; and — the one
    that actually broke the curator — a fenced object FOLLOWED BY several
    sentences of prose explaining the choice. Stripping a fence only when the
    reply both starts and ends with one misses that last shape entirely, so
    match the fence wherever it appears and fall back to the widest brace span.
    """
    t = text.strip()
    out: list[str] = []
    fenced = _FENCED.search(t)
    if fenced:
        out.append(fenced.group(1))
    if t.startswith("{"):
        out.append(t)
    first, last = t.find("{"), t.rfind("}")
    if first != -1 and last > first:
        out.append(t[first : last + 1])
    return out


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

    for candidate in _json_candidates(content):
        try:
            parsed = json.loads(candidate)
        except (ValueError, TypeError):
            continue
        if isinstance(parsed, dict):
            return parsed
    raise LLMUnavailable(f"no JSON object in content: {content[:200]!r}")
