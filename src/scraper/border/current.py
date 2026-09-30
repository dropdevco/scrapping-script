"""The crossing times as they stand now: one row per bridge and lane, for
border_current_waits (migration 0016) and through it the Google Sheet the GoHighLevel
knowledge base syncs from.

Every lane CBP lists for a bridge gets a row on every run, open or not, so a lane
never silently drops out of the knowledge base: a shut lane reads "cerrado". The
minutes are the reconciled ones — what a follower would be told — with CBP's own
figure beside them, exactly as a bridge reply does.
"""

from __future__ import annotations

from datetime import datetime

from . import text
from .cbp import LANE_LABELS, LOCAL_TZ, Snapshot

# How a source is named in a sentence a follower may read; the source column keeps the id.
SOURCE_NAMES = {"cbp": "CBP", "pasosfronterizos": "pasosfronterizos.com"}


def rows(feed, snap: Snapshot, checked_at: datetime) -> list[dict]:
    """feed supplies the reconciled decisions for snap (BorderFeed.decisions)."""
    decisions = feed.decisions
    checked_local = checked_at.astimezone(LOCAL_TZ).isoformat()
    out = []
    for port in snap.ports:
        for lane_id, lane in port.lanes.items():
            decision = decisions.get((port.port_number, lane_id))
            state = decision.state if decision else lane.state
            minutes = (decision.minutes if decision else lane.delay_minutes) if state == "open" else None
            source = (decision.chosen_source if decision else "cbp") if state == "open" else "cbp"
            lane_en, lane_es = LANE_LABELS[lane_id]
            row = {
                "port_number": port.port_number,
                "lane": lane_id,
                "bridge_es": port.name_es,
                "bridge_en": port.name,
                "lane_es": lane_es,
                "lane_en": lane_en,
                "state": state,
                "minutes": minutes,
                "cbp_minutes": lane.delay_minutes,
                "source": source,
                "lanes_open": lane.lanes_open,
                "cbp_updated_at": lane.cbp_updated_at,
                "checked_at": checked_at.isoformat(),
            }
            for lang, bridge, label in (("es", port.name_es, lane_es), ("en", port.name, lane_en)):
                wait = text.wait_words(state, minutes, lang)
                row[f"wait_{lang}"] = wait
                row[f"summary_{lang}"] = text.current_summary(
                    bridge, label, wait, SOURCE_NAMES.get(source, source) if state == "open" else None,
                    checked_local, lang)
            out.append(row)
    return out
