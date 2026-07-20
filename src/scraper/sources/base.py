"""Source interface every connector implements.

A source is intentionally tiny: declare a ``name`` + ``kind``, say whether it's
configured, and implement ``fetch``. Heavy / optional third-party libs must be imported
*inside* ``fetch`` (or guarded) so the registry can be built without them installed.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from ..core.http import HttpClient
from ..core.models import Document, Event, SearchParams, Kind, Trend


class Source(ABC):
    name: str
    kind: Kind

    @abstractmethod
    def is_configured(self) -> bool:
        """False if required keys/deps are missing -> the source is skipped."""

    @abstractmethod
    async def fetch(
        self, params: SearchParams, http: HttpClient
    ) -> list[Event] | list[Trend] | list[Document]:
        """Return normalized items. Raise on hard failure; the orchestrator isolates it."""
