"""pasosfronterizos.com — a second opinion, refreshed every few minutes.

Worth reading because it disagrees. On 22 Sep at 6:45 pm CBP published Paso del Norte
at 43 min stamped "At 6:00 pm"; this site showed 50 min "actualizado hace 8 minutos".
Neither is provably right, but one of them is 45 minutes newer.

Covers four bridges — Paso del Norte, Stanton, BOTA and Ysleta. Santa Teresa and
Tornillo appear only in CBP.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

from .. import http
from .base import Reading, Source

URL = "https://pasosfronterizos.com/puentes-el-paso-juarez.php"

# Their card titles -> our port numbers.
PORTS = {
    "paso del norte": "240202",
    "stanton": "240204",
    "américas": "240201",
    "americas": "240201",
    "ysleta": "240203",
}
LANES = {"estandar": "car", "sentri": "car_sentri", "ready": "car_ready"}

CARD_RE = re.compile(r'<li class="header">(.*?)</li>(.*?)(?=<li class="header">|$)', re.S)
NUMBER_RE = re.compile(r"<span class='linenumber'>(\d+)</span>\s*([a-záé ]+)", re.I)
CLOSED_RE = re.compile(r"cerrad", re.I)
AGE_RE = re.compile(r"Actualizado hace (\d+)\s*(minuto|hora)", re.I)


def _port_of(title: str) -> str | None:
    lowered = title.lower()
    return next((port for key, port in PORTS.items() if key in lowered), None)


def _lane_of(title: str) -> str | None:
    lowered = title.lower()
    return next((lane for key, lane in LANES.items() if key in lowered), None)


def parse(page: str, now: datetime) -> list[Reading]:
    """Pull (port, lane, minutes) out of the page. Unreadable cards are skipped."""
    age_match = AGE_RE.search(page)
    age = None
    if age_match:
        age = int(age_match.group(1)) * (60 if age_match.group(2).lower().startswith("hora") else 1)
    observed = (now - timedelta(minutes=age)).isoformat() if age is not None else None

    readings: list[Reading] = []
    for title, body in CARD_RE.findall(page):
        port, lane = _port_of(title), _lane_of(title)
        if not port or not lane:
            continue

        # Each card lists "N líneas abiertas" then "M mins de espera", cars first and
        # pedestrians after a walking icon. Pair them in document order.
        pairs, pending = [], None
        for value, label in NUMBER_RE.findall(body):
            if "l" in label.lower()[:2]:          # "líneas abiertas"
                pending = int(value)
            elif "min" in label.lower():
                pairs.append((pending, int(value)))
                pending = None

        if not pairs:
            continue
        walk_at = body.find("directions_walk")
        closed = bool(CLOSED_RE.search(body))

        found = [(lane, pairs[0])]
        # The second pair is the pedestrian line, and only the standard card has one.
        if lane == "car" and len(pairs) > 1 and walk_at != -1:
            found.append(("walk", pairs[1]))
        for lane_id, (lanes_open, minutes) in found:
            readings.append(Reading(
                port_number=port, lane=lane_id,
                state="closed" if closed else "open",
                minutes=None if closed else minutes,
                lanes_open=lanes_open, age_minutes=age, observed_at=observed,
                source="pasosfronterizos",
            ))
    return readings


class PasosFronterizosSource(Source):
    name = "pasosfronterizos"
    independent = True
    expected_ports = frozenset(PORTS.values())

    def __init__(self, url: str = URL, timeout: int = 20, opener=None, obey_robots: bool = True):
        self.url, self.timeout, self._opener = url, timeout, opener
        self.obey_robots = obey_robots
        self._allowed: bool | None = None

    def is_configured(self) -> bool:
        """Ask robots.txt once. Unreachable robots.txt means allowed, as crawlers do."""
        if self._opener or not self.obey_robots:
            return True
        if self._allowed is None:
            self._allowed = self._robots_allows()
        return self._allowed

    def _robots_allows(self) -> bool:
        base = "{0.scheme}://{0.netloc}".format(urlparse(self.url))
        parser = RobotFileParser()
        try:
            robots = http.get(urljoin(base, "/robots.txt"), timeout=10)
            parser.parse(robots.decode(errors="replace").splitlines())
        except Exception:
            return True
        try:
            return parser.can_fetch(http.BROWSER_USER_AGENT, self.url)
        except Exception:
            return True

    def fetch(self, now: datetime) -> list[Reading]:
        return parse(http.get_text(self.url, self.timeout, self._opener), now)
