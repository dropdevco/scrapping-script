"""CBP, wrapped in the Source contract.

Authoritative for structure: the only source covering all six ports, every lane type,
hours and closures. Its weakness is freshness — it republishes about hourly.
BorderFeed reads CBP itself (it needs the whole snapshot, not just readings) and turns
it into readings with `readings_from`, the same function this source uses.
"""
from __future__ import annotations

from datetime import datetime

from ...core.http import HttpClient
from .. import cbp
from .base import Reading, Source


def readings_from(snapshot: cbp.Snapshot) -> list[Reading]:
    return [
        Reading(port_number=port.port_number, lane=lane_id, state=lane.state,
                minutes=lane.delay_minutes, lanes_open=lane.lanes_open,
                age_minutes=lane.known_age, observed_at=lane.cbp_updated_at,
                source=CbpSource.name, label=lane.cbp_update_label)
        for port in snapshot.ports for lane_id, lane in port.lanes.items()
    ]


class CbpSource(Source):
    name = "cbp"
    independent = True

    async def fetch(self, http: HttpClient, now: datetime) -> list[Reading]:
        return readings_from(cbp.build(await cbp.fetch(http), now))
