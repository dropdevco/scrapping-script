"""CBP Border Wait Times: fetch, normalise, identify.

The feed is the only source of live numbers. It has four traps, all handled here:

1. delay_minutes arrives as a string, and missing values are "" or "N/A", never null.
2. operational_status casing is inconsistent: "no delay" vs "Lanes Closed".
3. crossing_name is EMPTY for port 240221 (Tornillo), so names come from the registry.
4. update_time is prose ("At 10:00 pm MDT") and is sometimes ahead of the clock.

Sources never compute their own content_hash in the Chisme engine; here the fetch and
the hash live together because there is exactly one source.
"""
from __future__ import annotations

import difflib
import hashlib
import re
from collections.abc import Iterator
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta

from ..core.eventtime import event_tz
from ..core.http import HttpClient

FEED_URL = "https://bwt.cbp.gov/api/waittimes"
# The engine's zone, not a second copy of it: El Paso and Juárez share one clock, and a
# wait-time "cruzas 9:40" must agree with the event times on the same account.
LOCAL_TZ = event_tz()
FRESH_MINUTES = 90  # CBP posts roughly hourly; older than this reads as stale
DAY_MINUTES = 24 * 60
# A stamp CBP dated in the future hides when the reading was really taken. Reporting it
# as age 0 would make the least trustworthy number win every freshness comparison.
UNKNOWN_AGE = 45

# known_as: the names people actually call a bridge, as the meeting of 2026-09-29 did
# ("Centro", "Puente Libre", "Lerdo", "Zaragoza"). Curated and correctly spelled — they
# are written into the knowledge base — unlike `aliases`, which also carries the
# misspellings the lookup has to tolerate.
# Registry mirrors border_ports in 0013_border_waits.sql. Kept in code as well so the
# feed works with no database, the way Storage degrades to a no-op.
PORTS: dict[str, dict] = {
    "240202": {"name": "Paso del Norte", "name_es": "Paso del Norte (Santa Fe)", "slug": "paso-del-norte",
               "aliases": ["pdn", "santa fe", "puente santa fe", "centro"], "order": 10,
               "known_as": ["Centro", "Santa Fe", "PDN"]},
    "240203": {"name": "Ysleta–Zaragoza", "name_es": "Zaragoza–Ysleta", "slug": "ysleta",
               "aliases": ["zaragoza", "zaragosa", "ysleta", "zaragoza-ysleta"], "order": 20,
               "known_as": ["Zaragoza", "Ysleta"]},
    "240201": {"name": "Bridge of the Americas", "name_es": "Puente Libre (Córdova–Américas)", "slug": "bota",
               "aliases": ["bota", "libre", "puente libre", "cordova", "córdova", "free bridge", "americas"], "order": 30,
               "known_as": ["Puente Libre", "Córdova", "Américas", "BOTA"]},
    "240204": {"name": "Stanton–Lerdo", "name_es": "Lerdo–Stanton", "slug": "stanton",
               "aliases": ["stanton", "lerdo", "good neighbor"], "order": 40,
               "known_as": ["Lerdo", "Stanton"]},
    "240801": {"name": "Santa Teresa", "name_es": "Jerónimo–Santa Teresa", "slug": "santa-teresa",
               "aliases": ["santa teresa", "jeronimo", "jerónimo", "san jeronimo"], "order": 50,
               "known_as": ["Jerónimo", "San Jerónimo"]},
    "240221": {"name": "Tornillo–Guadalupe", "name_es": "Guadalupe–Tornillo", "slug": "tornillo",
               "aliases": ["tornillo", "guadalupe", "marcelino serna"], "order": 60,
               "known_as": ["Guadalupe", "Marcelino Serna"]},
}

# feed group -> feed lane key -> our lane id
LANES = {
    "passenger_vehicle_lanes": {"standard_lanes": "car", "NEXUS_SENTRI_lanes": "car_sentri", "ready_lanes": "car_ready"},
    "pedestrian_lanes": {"standard_lanes": "walk", "ready_lanes": "walk_ready"},
    "commercial_vehicle_lanes": {"standard_lanes": "truck", "FAST_lanes": "truck_fast"},
}
LANE_LABELS = {
    "car": ("Cars", "Autos"),
    "car_sentri": ("SENTRI", "SENTRI"),
    "car_ready": ("Ready Lane", "Ready Lane"),
    "walk": ("Walking", "Peatones"),
    "walk_ready": ("Walking Ready Lane", "Peatones Ready Lane"),
    "truck": ("Trucks", "Carga"),
    "truck_fast": ("FAST", "FAST"),
}
# Words that appear in several bridge names and so identify none of them.
LOOKUP_STOPWORDS = {"puente", "puentes", "bridge", "international", "internacional",
                    "cruce", "the", "del", "las", "los", "por"}

STATES = {"no delay": "open", "delay": "open", "lanes closed": "closed", "update pending": "no_data"}
NULLISH = {"", "n/a", "-", "none"}
STAMP_RE = re.compile(r"(\d{1,2}):(\d{2})\s*(am|pm)", re.IGNORECASE)
# Hours arrive as "24 hrs/day", "6 am-Midnight", "6 am-10 pm".
HOURS_RE = re.compile(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)?|noon|midnight", re.IGNORECASE)
# CBP writes the two turning points as words: "At Noon MDT", "At Midnight MDT".
WORD_TIMES = {"noon": (12, 0), "midnight": (0, 0)}


@dataclass
class Lane:
    port_number: str
    lane: str
    state: str                      # open | closed | no_data
    delay_minutes: int | None
    lanes_open: int | None
    cbp_updated_at: str | None      # ISO, local time
    cbp_update_label: str | None
    age_minutes: int | None
    stale: bool
    stamp_ahead_of_clock: bool

    @property
    def has_minutes(self) -> bool:
        """Open with a number: the only lanes a wait, a delta or an alert can use."""
        return self.state == "open" and self.delay_minutes is not None

    @property
    def known_age(self) -> int | None:
        """Age for freshness comparisons; a future stamp counts as UNKNOWN_AGE old."""
        return UNKNOWN_AGE if self.stamp_ahead_of_clock else self.age_minutes

    @property
    def content_hash(self) -> str:
        key = f"{self.port_number}|{self.lane}|{self.cbp_updated_at}|{self.state}|{self.delay_minutes}"
        return hashlib.sha1(key.encode()).hexdigest()

    def to_row(self) -> dict:
        """A border_readings row."""
        return {
            "port_number": self.port_number,
            "lane": self.lane,
            "state": self.state,
            "delay_minutes": self.delay_minutes,
            "lanes_open": self.lanes_open,
            "cbp_updated_at": self.cbp_updated_at,
            "cbp_update_label": self.cbp_update_label,
            "stamp_ahead_of_clock": self.stamp_ahead_of_clock,
            "content_hash": self.content_hash,
        }


def _minutes_of_day(token: str) -> int | None:
    token = token.strip().lower()
    if "midnight" in token:
        return 0
    if "noon" in token:
        return 12 * 60
    match = re.search(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", token)
    if not match:
        return None
    hour, minute, meridiem = int(match.group(1)), int(match.group(2) or 0), (match.group(3) or "").lower()
    if meridiem == "pm" and hour != 12:
        hour += 12
    elif meridiem == "am" and hour == 12:
        hour = 0
    return hour * 60 + minute


def parse_hours(text: str | None) -> tuple[int, int] | None:
    """'6 am-10 pm' -> (360, 1320) minutes from local midnight. None = open 24 hours.

    A closing time at or before the opening time means it runs past midnight, so the
    close is pushed into the next day (Stanton's 'Midnight' becomes 1440, not 0).
    """
    if not text or "24" in text:
        return None
    parts = re.split(r"\s*[-–—]\s*", text, maxsplit=1)
    if len(parts) != 2:
        return None
    opens, closes = _minutes_of_day(parts[0]), _minutes_of_day(parts[1])
    if opens is None or closes is None:
        return None
    if closes <= opens:
        closes += DAY_MINUTES
    return opens, closes


@dataclass
class Port:
    port_number: str
    name: str
    name_es: str
    slug: str
    aliases: list[str]
    port_status: str | None
    hours: str | None
    notice: str | None
    lanes: dict[str, Lane] = field(default_factory=dict)

    @property
    def has_live_data(self) -> bool:
        return any(l.state == "open" for l in self.lanes.values())

    @property
    def hours_window(self) -> tuple[int, int] | None:
        """(opens, closes) in minutes from local midnight, or None when 24 hours."""
        return parse_hours(self.hours)


@dataclass
class Snapshot:
    generated_at: str
    generated_at_local: str
    ports: list[Port]
    missing_ports: list[str]

    @property
    def at(self) -> datetime:
        return datetime.fromisoformat(self.generated_at)

    @property
    def local_at(self) -> datetime:
        return self.at.astimezone(LOCAL_TZ)

    def port(self, key: str) -> Port | None:
        """Port number, slug, alias, or a loose name match.

        People type what they call the bridge — "libre", "zaragoza", "santa fe" —
        so aliases are checked before falling back to substring matching.
        """
        key = " ".join(key.strip().lower().split())
        if not key:
            return None
        for p in self.ports:
            if key in (p.port_number, p.slug, p.name.lower(), p.name_es.lower()) or key in p.aliases:
                return p
        words = [w for w in key.split() if len(w) > 2 and w not in LOOKUP_STOPWORDS]
        scored: list[tuple[int, Port]] = []
        for p in self.ports:
            haystack = f"{p.name.lower()} {p.name_es.lower()} {' '.join(p.aliases)}"
            score = (5 if key in haystack else 0) + sum(1 for w in words if w in haystack)
            if score:
                scored.append((score, p))
        if scored:
            top = max(s for s, _ in scored)
            winners = [p for s, p in scored if s == top]
            if len(winners) == 1:
                return winners[0]
            # A tie ("santa fee" hits both Santa Fe and Santa Teresa) falls through to
            # spelling distance, which separates them; if that is also unsure, we ask.

        # Phone typing: "sarragoza", "santa fee". Close spellings only, never a guess
        # loose enough to send someone to the wrong bridge.
        candidates: dict[str, Port] = {}
        for p in self.ports:
            for phrase in [p.name.lower(), p.name_es.lower(), p.slug, *p.aliases]:
                candidates.setdefault(phrase, p)
        for token in (key, *words):
            near = difflib.get_close_matches(token, list(candidates), n=2, cutoff=0.8)
            if near and (len(near) == 1 or candidates[near[0]] is candidates[near[1]]):
                return candidates[near[0]]
        return None

    def to_dict(self) -> dict:
        return {
            "generated_at": self.generated_at,
            "generated_at_local": self.generated_at_local,
            "source": FEED_URL,
            "region": "El Paso – Ciudad Juárez",
            "ports": [
                {**{k: v for k, v in asdict(p).items() if k != "lanes"},
                 "has_live_data": p.has_live_data,
                 "lanes": {lane_id: {k: v for k, v in asdict(l).items() if k not in ("port_number", "lane")}
                           for lane_id, l in p.lanes.items()}}
                for p in self.ports
            ],
            "missing_ports": self.missing_ports,
        }


def open_lanes(snapshot: Snapshot) -> Iterator[tuple[Port, str, Lane]]:
    """(port, lane id, lane) for every lane that is open with a number."""
    for port in snapshot.ports:
        for lane_id, lane in port.lanes.items():
            if lane.has_minutes:
                yield port, lane_id, lane


def _clean(value) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return None if text.lower() in NULLISH else text


def _int(value) -> int | None:
    text = _clean(value)
    match = re.search(r"-?\d+", text) if text else None
    return int(match.group()) if match else None


def parse_stamp(label: str | None, now: datetime) -> datetime | None:
    """'At 10:00 pm MDT' -> aware local datetime on the most recent matching day."""
    if not label:
        return None
    lowered = label.lower()
    word = next((v for k, v in WORD_TIMES.items() if k in lowered), None)
    match = STAMP_RE.search(label)
    if word:
        hour, minute = word
    elif match:
        hour = int(match.group(1)) % 12 + (12 if match.group(3).lower() == "pm" else 0)
        minute = int(match.group(2))
    else:
        return None
    local_now = now.astimezone(LOCAL_TZ)
    stamp = local_now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    # Hours ahead means yesterday's stamp. A small lead is CBP labelling the next hour
    # early — kept as-is, reported as age 0, and never quoted in a caption.
    if stamp - local_now > timedelta(hours=2):
        stamp -= timedelta(days=1)
    return stamp


def _lane(port_number: str, lane_id: str, raw: dict, now: datetime) -> Lane | None:
    status = _clean(raw.get("operational_status"))
    if status is None:
        return None  # this lane type does not exist at this port
    state = STATES.get(status.lower(), "no_data")
    minutes = _int(raw.get("delay_minutes"))
    if state == "open" and minutes is None and status.lower() == "no delay":
        minutes = 0
    if state == "open" and minutes is None:
        state = "no_data"
    stamp = parse_stamp(_clean(raw.get("update_time")), now)
    age = None if stamp is None else max(0, int((now - stamp).total_seconds() // 60))
    return Lane(
        port_number=port_number,
        lane=lane_id,
        state=state,
        delay_minutes=minutes if state == "open" else None,
        lanes_open=_int(raw.get("lanes_open")),
        cbp_updated_at=stamp.isoformat() if stamp else None,
        cbp_update_label=_clean(raw.get("update_time")),
        age_minutes=age,
        stale=state == "open" and (age is None or age > FRESH_MINUTES),
        stamp_ahead_of_clock=bool(stamp and stamp > now),
    )


def _port(raw: dict, now: datetime) -> Port:
    meta = PORTS[raw["port_number"]]
    lanes: dict[str, Lane] = {}
    for group, mapping in LANES.items():
        for feed_key, lane_id in mapping.items():
            parsed = _lane(raw["port_number"], lane_id, raw.get(group, {}).get(feed_key, {}), now)
            if parsed:
                lanes[lane_id] = parsed
    return Port(
        port_number=raw["port_number"],
        name=meta["name"],
        name_es=meta["name_es"],
        slug=meta["slug"],
        aliases=meta.get("aliases", []),
        port_status=_clean(raw.get("port_status")),
        hours=_clean(raw.get("hours")),
        notice=_clean(raw.get("construction_notice")),
        lanes=lanes,
    )


def build(feed: list[dict], now: datetime | None = None) -> Snapshot:
    now = now or datetime.now(UTC)
    by_number = {p["port_number"]: p for p in feed if p.get("port_number") in PORTS}
    ports = [_port(by_number[n], now) for n in sorted(PORTS, key=lambda n: PORTS[n]["order"]) if n in by_number]
    return Snapshot(
        generated_at=now.isoformat(timespec="seconds"),
        generated_at_local=now.astimezone(LOCAL_TZ).strftime("%Y-%m-%d %H:%M %Z"),
        ports=ports,
        missing_ports=[n for n in PORTS if n not in by_number],
    )


async def fetch(http: HttpClient, url: str = FEED_URL) -> list[dict]:
    """Read the feed through the engine's client, which already retries 429 and 5xx
    with backoff, honours Retry-After, and negotiates gzip (93 KB -> 9 KB). The feed
    sends no ETag or Last-Modified, so conditional GET is not available."""
    payload = await http.get_json(url)
    if not isinstance(payload, list):
        raise TypeError(f"CBP feed returned {type(payload).__name__}, expected a list of ports")
    return payload
