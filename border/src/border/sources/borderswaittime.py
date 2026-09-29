"""borderswaittime.com — a mirror of CBP, kept deliberately.

It republishes CBP verbatim: at 6:45 pm on 22 Sep it returned "At 6:00 pm MDT, 43 min
delay" for Paso del Norte, exactly CBP's own values. That makes it worthless as a
second opinion, so it carries independent=False and never counts toward agreement.

It earns its place as a check on *us*: when a mirror of CBP disagrees with our reading
of CBP, the bug is usually in our parsing, or CBP changed something.
"""
from __future__ import annotations

import re
from datetime import datetime

from ..core import http
from .base import Reading, Source

URL = "https://borderswaittime.com/united-states-mexico/el-paso/"

PORTS = {
    "paso del norte": "240202",
    "ysleta": "240203",
    "bridge of the americas": "240201",
    "stanton": "240204",
    "santa teresa": "240801",
    "tornillo": "240221",
}
# "General Lanes: At 6:00 pm MDT 43 min delay" — the CBP phrasing, passed straight
# through. The segment must stop at the next lane label: a passenger card is followed
# by Sentri, Ready and Pedestrian rows, any of which may say "Lanes Closed".
GENERAL_RE = re.compile(r"General Lanes:\s*(.*?)(?=\b(?:Sentri|Ready|Pedestrian|Commercial)\b|$)",
                        re.I | re.S)
DELAY_RE = re.compile(r"(\d+)\s*min\s*delay", re.I)
# Which CBP update the numbers belong to. Comparing only readings of the same update is
# what keeps a mirror cached for 20 minutes from "disagreeing" with a newer CBP reading.
LABEL_RE = re.compile(r"At\s+(?:\d{1,2}:\d{2}\s*[ap]m|noon|midnight)\s*[A-Z]{3}", re.I)
# The four bridges with a passenger card; Santa Teresa and Tornillo have none.
EXPECTED_PORTS = frozenset({"240202", "240203", "240201", "240204"})
CLOSED_RE = re.compile(r"lanes?\s*closed", re.I)


def parse(page: str, now: datetime) -> list[Reading]:
    text = re.sub(r"<[^>]+>", " ", page)
    text = " ".join(text.split())
    readings: list[Reading] = []

    lowered = text.lower()
    for name, port in PORTS.items():
        # The bridge name also appears in the page's navigation, where the text that
        # follows belongs to some other bridge. Take the occurrence that is actually a
        # data card: the one followed by the port's own hours and lane table.
        block = ""
        position = lowered.find(name)
        while position != -1:
            candidate = text[position:position + 700]
            if "Hours:" in candidate[:120] and "Passenger Vehicles" in candidate:
                block = candidate
                break
            position = lowered.find(name, position + 1)
        if not block:
            continue

        passenger = block.find("Passenger Vehicles")
        general = GENERAL_RE.search(block[passenger:passenger + 320])
        if not general:
            continue
        segment = general.group(1)

        if CLOSED_RE.search(segment):
            readings.append(Reading(port, "car", "closed", None, source="borderswaittime",
                                    independent=False))
            continue
        found = DELAY_RE.search(segment)
        if found:
            label = LABEL_RE.search(segment)
            readings.append(Reading(port, "car", "open", int(found.group(1)),
                                    source="borderswaittime", independent=False,
                                    label=label.group() if label else None))
    return readings


class BordersWaitTimeSource(Source):
    name = "borderswaittime"
    independent = False          # mirrors CBP; never counts as corroboration
    expected_ports = EXPECTED_PORTS

    def __init__(self, url: str = URL, timeout: int = 20, opener=None):
        self.url, self.timeout, self._opener = url, timeout, opener

    def fetch(self, now: datetime) -> list[Reading]:
        return parse(http.get_text(self.url, self.timeout, self._opener), now)
