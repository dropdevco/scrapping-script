"""Environment-driven configuration.

Everything is optional. A source checks ``settings`` in its ``is_configured()`` and
self-disables when its required keys are missing, so the app runs with whatever the
user has provided.
"""

from __future__ import annotations

import os
from functools import lru_cache

from dotenv import load_dotenv

load_dotenv()  # load .env from CWD if present; real env vars still win


def _clean(val: str | None) -> str | None:
    """Treat empty / placeholder values as unset."""
    if val is None:
        return None
    val = val.strip()
    return val or None


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "").strip() or default)
    except ValueError:
        return default


def _csv(name: str) -> set[str]:
    raw = os.getenv(name, "") or ""
    return {p.strip() for p in raw.split(",") if p.strip()}


class Settings:
    """Flat settings snapshot read once at import time."""

    def __init__(self) -> None:
        # Storage
        self.supabase_url = _clean(os.getenv("SUPABASE_URL"))
        self.supabase_key = _clean(os.getenv("SUPABASE_KEY"))

        # Behavior
        self.freshness_hours = _int("FRESHNESS_HOURS", 24)
        self.http_max_concurrency = _int("HTTP_MAX_CONCURRENCY", 8)
        self.http_timeout_seconds = _int("HTTP_TIMEOUT_SECONDS", 20)
        self.user_agent = (
            _clean(os.getenv("USER_AGENT")) or "scraper-mcp/0.1 (+research bot)"
        )
        self.enabled_sources = _csv("ENABLED_SOURCES")    # allowlist (empty = all)
        self.disabled_sources = _csv("DISABLED_SOURCES")  # denylist

        # Events
        self.ticketmaster_api_key = _clean(os.getenv("TICKETMASTER_API_KEY"))

        # Web search
        self.tavily_api_key = _clean(os.getenv("TAVILY_API_KEY"))
        self.brave_api_key = _clean(os.getenv("BRAVE_API_KEY"))

        # Trends
        self.reddit_client_id = _clean(os.getenv("REDDIT_CLIENT_ID"))
        self.reddit_client_secret = _clean(os.getenv("REDDIT_CLIENT_SECRET"))
        self.reddit_user_agent = (
            _clean(os.getenv("REDDIT_USER_AGENT")) or "scraper-mcp/0.1"
        )
        self.youtube_api_key = _clean(os.getenv("YOUTUBE_API_KEY"))

        # Meta (own-account scope)
        self.meta_app_id = _clean(os.getenv("META_APP_ID"))
        self.meta_app_secret = _clean(os.getenv("META_APP_SECRET"))
        self.ig_access_token = _clean(os.getenv("IG_ACCESS_TOKEN"))
        self.ig_business_account_id = _clean(os.getenv("IG_BUSINESS_ACCOUNT_ID"))
        self.threads_access_token = _clean(os.getenv("THREADS_ACCESS_TOKEN"))
        self.threads_user_id = _clean(os.getenv("THREADS_USER_ID"))

    @property
    def storage_enabled(self) -> bool:
        return bool(self.supabase_url and self.supabase_key)

    def source_allowed(self, name: str) -> bool:
        """Apply the ENABLED/DISABLED allow/deny lists to a source name."""
        if name in self.disabled_sources:
            return False
        if self.enabled_sources and name not in self.enabled_sources:
            return False
        return True


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
