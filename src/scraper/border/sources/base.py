"""The contract every source implements."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from ...core.config import settings
from ...core.http import HttpClient


@dataclass(frozen=True)
class Reading:
    """One source's opinion about one lane at one port."""
    port_number: str
    lane: str
    state: str                    # open | closed | no_data
    minutes: int | None
    lanes_open: int | None = None
    age_minutes: int | None = None     # how old the source says the number is
    observed_at: str | None = None
    source: str = ""
    independent: bool = True           # False when it re-publishes CBP
    label: str | None = None           # the CBP update it describes, e.g. "At 7:00 pm MDT"


class Source:
    """A place to read wait times from.

    name:           stored on every Reading, and the key used to enable or disable it
    independent:    False for sites that mirror CBP — they never add corroboration
    expected_ports: ports a healthy page always shows. A scraper whose page changed
                    shape parses to nothing rather than raising, so this is how that
                    breakage becomes visible instead of reading as "ok (0 readings)".
    """
    name: str = ""
    independent: bool = True
    expected_ports: frozenset[str] = frozenset()

    def is_configured(self) -> bool:
        """No network here. ENABLED_SOURCES / DISABLED_SOURCES apply by name, the same
        switch the engine's registry honours."""
        return settings.source_allowed(self.name)

    async def fetch(self, http: HttpClient, now: datetime) -> list[Reading]:
        """Raise on hard failure; the feed records it and answers from CBP alone."""
        raise NotImplementedError
