"""Normalized domain models shared by every source and the storage layer.

Sources emit ``Event`` / ``Trend`` / ``Document`` instances. The orchestrator assigns
``content_hash`` (via :mod:`scraper.core.dedupe`) before persisting, so sources don't
have to compute it themselves.
"""

from __future__ import annotations

from datetime import date, datetime
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field


class Kind(str, Enum):
    """What a request/source is about. Drives which sources the orchestrator picks."""

    EVENTS = "events"
    TRENDS = "trends"
    WEB = "web"


class SearchParams(BaseModel):
    """One flexible request object. Each source reads only the fields it cares about."""

    kind: Kind
    query: Optional[str] = None            # free-text topic / keywords
    location: Optional[str] = None         # e.g. "El Paso, TX"
    start_date: Optional[date] = None
    end_date: Optional[date] = None
    categories: list[str] = Field(default_factory=list)
    platforms: list[str] = Field(default_factory=list)   # subset filter for trends sources
    timeframe: str = "week"                # "day" | "week" | "month" (source-interpreted)
    limit: int = 50
    force_refresh: bool = False            # bypass the freshness cache


class Event(BaseModel):
    source: str
    source_id: Optional[str] = None        # stable id from the provider, if any
    title: str
    description: Optional[str] = None
    start_time: Optional[datetime] = None
    end_time: Optional[datetime] = None
    venue: Optional[str] = None
    location: Optional[str] = None
    url: Optional[str] = None
    image_url: Optional[str] = None
    categories: list[str] = Field(default_factory=list)
    raw: dict[str, Any] = Field(default_factory=dict)
    content_hash: Optional[str] = None     # filled in by the dedupe step


class Trend(BaseModel):
    source: str
    platform: str                          # "reddit" | "hackernews" | "youtube" | "google_trends" | ...
    topic: Optional[str] = None            # the query/topic this was captured for
    title: str
    summary: Optional[str] = None
    url: Optional[str] = None
    score: Optional[float] = None          # normalized popularity signal (upvotes, points, views…)
    engagement: dict[str, Any] = Field(default_factory=dict)   # raw counts (comments, likes…)
    captured_for: Optional[str] = None     # original request topic, for grouping
    raw: dict[str, Any] = Field(default_factory=dict)
    content_hash: Optional[str] = None


class Document(BaseModel):
    """A web-research result (not persisted to the typed tables; returned to the agent)."""

    source: str
    title: Optional[str] = None
    url: str
    snippet: Optional[str] = None
    text: Optional[str] = None             # extracted main content, when fetched
    published: Optional[str] = None


class SourceResult(BaseModel):
    """Per-source outcome, aggregated by the orchestrator into a run summary."""

    source: str
    ok: bool
    count: int = 0
    error: Optional[str] = None
