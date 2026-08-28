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


def _bool(name: str, default: bool) -> bool:
    raw = (os.getenv(name) or "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def _csv(name: str) -> set[str]:
    raw = os.getenv(name, "") or ""
    return {p.strip() for p in raw.split(",") if p.strip()}


class Settings:
    """Flat settings snapshot read once at import time."""

    def __init__(self) -> None:
        # Storage
        self.supabase_url = _clean(os.getenv("SUPABASE_URL"))
        self.supabase_key = _clean(os.getenv("SUPABASE_KEY"))
        # Direct Postgres connection (pooler recommended) — NOT used by the scraper
        # at runtime (that goes through the REST API above); only for admin/migration
        # scripts that need raw SQL, e.g. `python -m scraper.apply_migration`.
        self.supabase_db_url = _clean(os.getenv("SUPABASE_DB_URL"))

        # Behavior
        # Timezone the scraped region keeps its wall clocks in. Event listings
        # publish naive local times; core/eventtime.py resolves them against
        # this. Defaults to IG_TIMEZONE when only that is set, so the carousel's
        # "today" and the events it reads about can never drift apart.
        self.event_timezone = (
            _clean(os.getenv("EVENT_TIMEZONE"))
            or _clean(os.getenv("IG_TIMEZONE"))
            or "America/Denver"
        )
        self.freshness_hours = _int("FRESHNESS_HOURS", 24)
        self.http_max_concurrency = _int("HTTP_MAX_CONCURRENCY", 8)
        self.http_timeout_seconds = _int("HTTP_TIMEOUT_SECONDS", 20)
        self.user_agent = (
            _clean(os.getenv("USER_AGENT")) or "scraper-mcp/0.1 (+research bot)"
        )
        self.enabled_sources = _csv("ENABLED_SOURCES")    # allowlist (empty = all)
        self.disabled_sources = _csv("DISABLED_SOURCES")  # denylist

        # Venue geocoding. Sources rarely supply coordinates, and a venue without
        # them can never appear on the map, so new venues are geocoded at upsert
        # time. Nominatim is ~1 req/s, hence the per-run cap; leftovers are picked
        # up by the next run (or `python -m scraper.backfill_geocode`).
        self.geocode_venues = _bool("GEOCODE_VENUES", True)
        self.geocode_max_per_run = _int("GEOCODE_MAX_PER_RUN", 25)

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

        # Daily Instagram carousel (scraper.social)
        # Phase 1 leaves autopost off: the build job renders + drafts, a human
        # approves in /admin/ig, and the publish sweep ships it. Flipping this to
        # true makes the build publish immediately via the SAME code path.
        self.ig_autopost = _bool("IG_AUTOPOST", False)
        self.ig_slides_bucket = _clean(os.getenv("IG_SLIDES_BUCKET")) or "ig-slides"
        # El Paso is Mountain time. "Today" must be the local calendar day, not
        # the UTC one the CI runner happens to be in.
        self.ig_timezone = _clean(os.getenv("IG_TIMEZONE")) or "America/Denver"
        # Floor, not a target: a 3-slide "today in El Paso" reads worse than
        # posting nothing, and thin days are common (only ~half of events carry
        # a usable photo).
        self.ig_min_slides = _int("IG_MIN_SLIDES", 4)
        self.ig_max_slides = _int("IG_MAX_SLIDES", 9)  # +1 cover = Instagram's 10 cap
        self.ig_slide_retention_days = _int("IG_SLIDE_RETENTION_DAYS", 7)
        self.ig_handle = _clean(os.getenv("IG_HANDLE")) or "epchisme.com"
        # Default local hour the build job suggests for scheduled_for (see
        # __main__.py's _suggested_schedule). 5pm reads well for "things
        # happening tonight" without requiring real engagement data, which a
        # brand-new account doesn't have yet.
        self.ig_suggested_publish_hour = _int("IG_SUGGESTED_PUBLISH_HOUR", 17)
        # Empty means today's single-unnamed-digest behavior. Set to e.g.
        # "morning:11,evening:18" to run two independently-scheduled digests
        # a day — each gets its own name (stored as ig_posts.slot) and
        # suggested hour, and can coexist with the other on the same
        # post_date (see migration 0006_ig_kind_slot.sql).
        self.ig_digest_slots = _clean(os.getenv("IG_DIGEST_SLOTS")) or ""

        # Draft-ready notifications (scraper.social.notify). Each channel
        # self-disables when its own keys are missing, same idiom as everything
        # else here — Telegram and email are independent, either can be on alone.
        self.telegram_bot_token = _clean(os.getenv("TELEGRAM_BOT_TOKEN"))
        # Comma-separated. The FIRST entry is where notifications get SENT; the
        # Next.js webhook parses the same variable as an allowlist of chats
        # permitted to act on them. Keeping a second entry (an admin's DM)
        # alongside the group means button-bearing messages already sitting in
        # that DM keep working instead of silently dying on tap.
        self.telegram_chat_ids = [
            c.strip() for c in (os.getenv("TELEGRAM_CHAT_ID") or "").split(",") if c.strip()
        ]
        self.telegram_chat_id = self.telegram_chat_ids[0] if self.telegram_chat_ids else None
        self.resend_api_key = _clean(os.getenv("RESEND_API_KEY"))
        self.notify_admin_email = _clean(os.getenv("NOTIFY_ADMIN_EMAIL"))
        self.notify_email_from = (
            _clean(os.getenv("NOTIFY_EMAIL_FROM")) or "Chisme <onboarding@resend.dev>"
        )
        # Signs the token-gated /admin/ig/review/[token] page link (Next.js verifies
        # with the same secret) — unset means no review link is minted, email notify
        # no-ops, Telegram still works via its own inline approve/reject buttons.
        # ── GHL knowledge-base export (scraper.kb) ───────────────────────────
        # The bot's knowledge base is fed by a Google Sheet this repo rewrites
        # daily. Unset KB_SHEET_ID simply means the export is not wired up yet;
        # nothing else in the app depends on these.
        self.kb_sheet_id = _clean(os.getenv("KB_SHEET_ID"))
        self.kb_sheet_tab = _clean(os.getenv("KB_SHEET_TAB")) or "events"
        # How far ahead to publish. Long enough that "what's coming up next
        # month" works, short enough that the sheet stays a readable size.
        self.kb_horizon_days = _int("KB_HORIZON_DAYS", 60)
        self.kb_location = _clean(os.getenv("KB_LOCATION")) or "El Paso"
        self.google_service_account_json = _clean(os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON"))
        self.google_application_credentials = _clean(
            os.getenv("GOOGLE_APPLICATION_CREDENTIALS")
        )

        self.ig_notify_secret = _clean(os.getenv("IG_NOTIFY_SECRET"))
        self.ig_notify_ttl_hours = _int("IG_NOTIFY_TTL_HOURS", 36)
        self.site_base_url = _clean(os.getenv("SITE_BASE_URL")) or "https://epchisme.com"

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
