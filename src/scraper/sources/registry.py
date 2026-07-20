"""Source registry.

Collects every source instance, then exposes the ones that are (a) allowed by config and
(b) configured, grouped by kind. Adding a source = add its module name here and export a
``SOURCE`` (or ``SOURCES``) from it. The orchestrator asks this module and nothing else.
"""

from __future__ import annotations

import importlib
import logging

from ..core.config import settings
from ..core.models import Kind
from .base import Source

log = logging.getLogger("scraper.registry")

_MODULES = [
    "events_ticketmaster",
    "events_web",
    "trends_reddit",
    "trends_hackernews",
    "trends_google",
    "trends_youtube",
    "web_search",
    "social_instagram",
    "social_threads",
]

_cache: list[Source] | None = None


def _load_all() -> list[Source]:
    global _cache
    if _cache is not None:
        return _cache
    found: list[Source] = []
    for mod_name in _MODULES:
        try:
            mod = importlib.import_module(f"{__package__}.{mod_name}")
        except Exception as exc:  # noqa: BLE001 - a broken/optional module shouldn't kill the app
            log.warning("could not import source module %s: %s", mod_name, exc)
            continue
        if hasattr(mod, "SOURCES"):
            found.extend(mod.SOURCES)
        elif hasattr(mod, "SOURCE"):
            found.append(mod.SOURCE)
    _cache = found
    return found


def sources_for(kind: Kind) -> list[Source]:
    """Active sources for a kind: right kind, allowed by config, and configured."""
    active: list[Source] = []
    for s in _load_all():
        if s.kind is not kind:
            continue
        if not settings.source_allowed(s.name):
            continue
        try:
            if not s.is_configured():
                continue
        except Exception as exc:  # noqa: BLE001
            log.warning("is_configured() failed for %s: %s", s.name, exc)
            continue
        active.append(s)
    return active


def status() -> list[dict[str, object]]:
    """Diagnostic snapshot: every source, its kind, and whether it's active."""
    rows: list[dict[str, object]] = []
    for s in _load_all():
        try:
            configured = s.is_configured()
        except Exception:  # noqa: BLE001
            configured = False
        rows.append(
            {
                "name": s.name,
                "kind": s.kind.value,
                "configured": configured,
                "allowed": settings.source_allowed(s.name),
                "active": configured and settings.source_allowed(s.name),
            }
        )
    return rows
