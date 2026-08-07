"""Instagram caption for the daily carousel.

Instagram's hard limit is 2200 characters. We budget to 2100 — emoji and
non-ASCII count differently across implementations, and being 100 under is free
insurance against a post failing at publish time over a rounding difference.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Optional

MAX_CAPTION = 2100
# Not 30. A wall of 30 tags reads as spam and gets deprioritized; ~20 is the
# practical ceiling for a local account.
MAX_HASHTAGS = 20

_CORE_HASHTAGS = [
    "#elpaso",
    "#eptx",
    "#915",
    "#elpasotx",
    "#elpasoevents",
    "#thingstodoinelpaso",
    "#suncity",
    "#borderland",
    "#elpasotexas",
    "#chisme",
]

# Category -> extra tag. Only categories that actually read as a hashtag.
_CATEGORY_HASHTAGS = {
    "Music": "#elpasomusic",
    "Festivals": "#elpasofestivals",
    "Food & Drink": "#elpasofood",
    "Arts & Theatre": "#elpasoarts",
    "Sports": "#elpasosports",
    "Family": "#elpasofamily",
    "Tech": "#elpasotech",
}


def _time_label(start_local: Optional[Any]) -> str:
    # Suppressed rather than guessed when the stored time isn't credible —
    # see selection.has_plausible_time.
    from .selection import has_plausible_time

    if not has_plausible_time(start_local):
        return ""
    hour = start_local.strftime("%I").lstrip("0") or "12"
    minute = start_local.strftime("%M")
    suffix = start_local.strftime("%p")
    return f"{hour}:{minute}{suffix}" if minute != "00" else f"{hour}{suffix}"


def _hashtags(rows: list[dict[str, Any]]) -> list[str]:
    tags = list(_CORE_HASHTAGS)
    for row in rows:
        for cat in row.get("categories") or []:
            tag = _CATEGORY_HASHTAGS.get(cat)
            if tag and tag not in tags:
                tags.append(tag)
    return tags[:MAX_HASHTAGS]


def _venue_label(row: dict[str, Any]) -> str:
    venues = row.get("venues")
    if isinstance(venues, list):
        venues = venues[0] if venues else None
    if isinstance(venues, dict) and venues.get("name"):
        return str(venues["name"])
    return str(row.get("venue") or "")


def build_caption(
    day: date,
    picked: list[Any],
    *,
    site: str = "epchisme.com",
    max_caption: int = MAX_CAPTION,
) -> str:
    """Assemble the caption, degrading gracefully if it runs long.

    `picked` is a list of selection.Candidate. Slide numbering starts at 2
    because slide 1 is the cover.
    """
    header = f"TODAY IN EL PASO — {day.strftime('%a, %b %-d')}" if _supports_dash() else (
        f"TODAY IN EL PASO — {day.strftime('%a, %b ')}{day.day}"
    )
    footer = f"\n\nFull list, map and details at {site} (link in bio)."
    tags = _hashtags([c.row for c in picked])

    # Progressive degradation, cheapest loss first: venue suffixes, then title
    # length, then whole lines, then optional hashtags.
    for drop_venue, title_cap, tag_count in _degradations(len(tags)):
        lines = []
        for idx, cand in enumerate(picked, start=2):
            title = str(cand.row.get("title") or "").strip()
            if title_cap and len(title) > title_cap:
                title = title[: title_cap - 1].rstrip() + "…"
            when = _time_label(cand.start_local)
            venue = "" if drop_venue else _venue_label(cand.row)
            parts = [f"{idx}."]
            if when:
                parts.append(f"{when} ·")
            parts.append(title)
            line = " ".join(parts)
            if venue:
                line += f" — {venue}"
            lines.append(line)

        tag_block = "\n\n" + " ".join(tags[:tag_count]) if tag_count else ""
        body = header + "\n\n" + "\n".join(lines) + footer + tag_block
        if len(body) <= max_caption:
            return body
        # Still too long even fully degraded: drop trailing event lines.
        while lines and len(body) > max_caption:
            lines.pop()
            body = header + "\n\n" + "\n".join(lines) + footer + tag_block
        if len(body) <= max_caption:
            return body

    return (header + footer)[:max_caption]


def _degradations(tag_total: int) -> list[tuple[bool, int, int]]:
    """(drop_venue, title_cap, tag_count) tried in order — least lossy first."""
    return [
        (False, 0, tag_total),
        (False, 0, min(tag_total, 12)),
        (True, 0, min(tag_total, 12)),
        (True, 48, min(tag_total, 10)),
        (True, 32, len(_CORE_HASHTAGS)),
    ]


def _supports_dash() -> bool:
    """`%-d` (no zero padding) is glibc-only; Windows strftime rejects it."""
    try:
        date(2026, 8, 5).strftime("%-d")
        return True
    except ValueError:
        return False
