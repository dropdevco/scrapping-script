"""Instagram caption for the daily carousel.

Instagram's hard limit is 2200 characters. We budget to 2100 — emoji and
non-ASCII count differently across implementations, and being 100 under is free
insurance against a post failing at publish time over a rounding difference.

Tone is deliberately voice-y — reads like a plugged-in local texting you what's
happening, not a press release. The opener rotates through a small fixed set,
picked deterministically from the date (not true randomness), so a given day's
caption is reproducible across re-renders and easy to test. Considered and
dropped: a per-event parenthetical hook like "(free!)" — there's no reliable
price/free signal in the event data today, and inventing one would be guessing,
not copywriting.
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
_PILLAR_HASHTAGS = {
    "Live Music": "#elpasomusic",
    "Arts & Culture": "#elpasoarts",
    "Sports": "#elpasosports",
    "Fitness & Activities": "#elpasofitness",
    "Family": "#elpasofamily",
    "Food & Drink": "#elpasofood",
}
_PILLAR_EMOJI = {
    "Live Music": "🎶",
    "Arts & Culture": "🎨",
    "Sports": "🏟️",
    "Fitness & Activities": "🏃",
    "Family": "👨‍👩‍👧",
    "Food & Drink": "🍽️",
}

_CATEGORY_HASHTAGS = {
    "Music": "#elpasomusic",
    "Festivals": "#elpasofestivals",
    "Food & Drink": "#elpasofood",
    "Arts & Theatre": "#elpasoarts",
    "Sports": "#elpasosports",
    "Family": "#elpasofamily",
    "Tech": "#elpasotech",
}

# Category -> the emoji that opens its caption line. Mirrors _CATEGORY_HASHTAGS'
# keys so the two stay in sync; anything unmapped (including "Community", the
# no-match fallback category) gets _DEFAULT_EMOJI.
_CATEGORY_EMOJI = {
    "Music": "🎶",
    "Festivals": "🎉",
    "Food & Drink": "🍽️",
    "Arts & Theatre": "🎨",
    "Sports": "🏟️",
    "Family": "👨‍👩‍👧",
    "Tech": "💻",
    "Nightlife": "🌙",
}
_DEFAULT_EMOJI = "✨"

# (opener text, flourish emoji) — day.toordinal() % len(_OPENERS) picks one,
# so it varies day to day without needing real randomness. The digest ships
# the evening before the day it covers (see build()'s event_day), so these
# read as advance notice, not same-day hype.
_OPENERS = [
    ("TOMORROW'S THE DAY", "🌙"),
    ("EL PASO, LISTEN UP", "📣"),
    ("HAPPENING TOMORROW", "⚡"),
    ("DON'T SLEEP ON TOMORROW", "👀"),
    ("EL PASO'S GOT PLANS", "🔥"),
]

# Per-format openers. The daily list is unchanged and still keyed the same
# way, so a digest caption is identical to what it was before formats existed.
_OPENERS_BY_KIND: dict[str, list[tuple[str, str]]] = {
    "digest": _OPENERS,
    "breaking": [("JUST ANNOUNCED", "🚨"), ("HEADS UP, EL PASO", "📣")],
    "weekend": [
        ("YOUR WEEKEND, SORTED", "🎉"),
        ("EL PASO THIS WEEKEND", "🌵"),
        ("NO EXCUSES THIS WEEKEND", "🔥"),
    ],
    "monthly": [
        ("THE MONTH AHEAD", "🗓"),
        ("EL PASO, THIS MONTH", "📅"),
        ("MARK YOUR CALENDAR", "✍️"),
    ],
    "horizon": [
        ("SAVE THE DATE", "🎟"),
        ("TICKETS ARE OUT", "🎫"),
        ("PLAN AHEAD, EL PASO", "🔭"),
    ],
}

# The line under the header. "worth leaving the couch for" reads wrong on a
# post about a concert six months out.
_SUBHEADS: dict[str, str] = {
    "digest": "El Paso's not sleeping on this one — {n} thing{s} worth leaving the couch for:",
    "breaking": "{n} thing{s} just landed:",
    "weekend": "{n} thing{s} to get you out of the house this weekend:",
    "monthly": "{n} thing{s} worth planning around this month:",
    "horizon": "{n} thing{s} already on sale — get in early:",
}

_KIND_HASHTAGS: dict[str, list[str]] = {
    "weekend": ["#elpasoweekend", "#weekendplans"],
    "monthly": ["#elpasothismonth", "#elpasoevents"],
    "horizon": ["#savethedate", "#elpasotickets"],
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


def _labels(row: dict[str, Any]) -> list[str]:
    """Pillars when the event has them, else its raw categories — the same
    preference selection ranks by, so a slide's chip, its hashtag and its
    weighting all describe the event the same way."""
    return [c for c in (row.get("content_tags") or row.get("categories") or []) if c]


def _hashtags(rows: list[dict[str, Any]], kind: str = "digest") -> list[str]:
    tags = list(_CORE_HASHTAGS) + _KIND_HASHTAGS.get(kind, [])
    for row in rows:
        for label in _labels(row):
            tag = _PILLAR_HASHTAGS.get(label) or _CATEGORY_HASHTAGS.get(label)
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


def _emoji_for(row: dict[str, Any]) -> str:
    for label in _labels(row):
        emoji = _PILLAR_EMOJI.get(label) or _CATEGORY_EMOJI.get(label)
        if emoji:
            return emoji
    return _DEFAULT_EMOJI


def _run_label(row: dict[str, Any], start_local: Optional[Any]) -> str:
    """"through Sep 20" for an event that runs past the day it starts.

    Reads end_time, which the social pipeline had never looked at once, despite
    72% of stored rows carrying one. Only a genuinely later DAY counts — most
    end_times are just a closing hour, and "through Sep 18" on a Sep 18 event
    is noise.
    """
    from ..core.eventtime import local_day

    if start_local is None or not row.get("end_time"):
        return ""
    end_local = local_day(row["end_time"])
    if end_local is None or end_local.date() <= start_local.date():
        return ""
    end = end_local.date()
    day_num = end.strftime("%-d") if _supports_dash() else str(end.day)
    return f"through {end.strftime('%b')} {day_num}"


def _date_label(day: date) -> str:
    if _supports_dash():
        return day.strftime("%a, %b %-d")
    return f"{day.strftime('%a, %b')} {day.day}"


def build_caption(
    day: date,
    picked: list[Any],
    *,
    site: str = "epchisme.com",
    max_caption: int = MAX_CAPTION,
    kind: str = "digest",
    period_label: Optional[str] = None,
) -> str:
    """Assemble the caption, degrading gracefully if it runs long.

    `picked` is a list of selection.Candidate, already in chronological order.
    `kind` defaults to "digest", so every existing call site produces exactly
    the caption it did before formats existed.
    """
    openers = _OPENERS_BY_KIND.get(kind, _OPENERS)
    # Keyed on the ordinal rather than randomised, so re-rendering the same
    # post yields the same caption — which is what lets a rebuild after a
    # dropped event stay stable instead of rewriting the opener too.
    opener, flourish = openers[day.toordinal() % len(openers)]
    header = f"{opener} {flourish} — {period_label or _date_label(day)}"
    n = len(picked)
    if n == 0:
        subhead = "Nothing on the radar right now — check back later."
    else:
        template = _SUBHEADS.get(kind, _SUBHEADS["digest"])
        subhead = template.format(n=n, s="s" if n != 1 else "")
    footer = f"\n\nFull list + map → {site}"
    tags = _hashtags([c.row for c in picked], kind=kind)

    # Progressive degradation, cheapest loss first: venue suffixes, then title
    # length, then whole lines, then optional hashtags.
    for drop_blurbs, drop_venue, title_cap, tag_count in _degradations(len(tags)):
        lines = []
        for cand in picked:
            title = str(cand.row.get("title") or "").strip()
            if title_cap and len(title) > title_cap:
                title = title[: title_cap - 1].rstrip() + "…"
            when = _time_label(cand.start_local)
            venue = "" if drop_venue else _venue_label(cand.row)
            parts = [_emoji_for(cand.row)]
            if when:
                parts.append(f"{when} ·")
            parts.append(title)
            line = " ".join(parts)
            if venue:
                line += f" — {venue}"
            # An exhibit or a festival that runs for days reads as a one-night
            # thing without this, which is the opposite of useful: the whole
            # point of a multi-day listing is that you have not missed it.
            run = _run_label(cand.row, cand.start_local)
            if run:
                line += f" ({run})"
            # The generated one-liner for a title that does not explain itself.
            # Dropped first when the caption runs long: it is the most useful
            # thing to have and the least damaging thing to lose.
            if not drop_blurbs:
                blurb = str(cand.row.get("blurb") or "").strip()
                if blurb:
                    line += f"\n   {blurb}"
            lines.append(line)

        tag_block = "\n\n" + " ".join(tags[:tag_count]) if tag_count else ""
        body = header + "\n\n" + subhead + "\n\n" + "\n".join(lines) + footer + tag_block
        if len(body) <= max_caption:
            return body
        # Still too long even fully degraded: drop trailing event lines.
        while lines and len(body) > max_caption:
            lines.pop()
            body = header + "\n\n" + subhead + "\n\n" + "\n".join(lines) + footer + tag_block
        if len(body) <= max_caption:
            return body

    return (header + footer)[:max_caption]


def _degradations(tag_total: int) -> list[tuple[bool, bool, int, int]]:
    """(drop_blurbs, drop_venue, title_cap, tag_count), least lossy first.

    Blurbs go first: nine of them can add ~900 characters, and losing them
    returns the caption to what it said before they existed. Everything after
    that starts removing facts.
    """
    return [
        (False, False, 0, tag_total),
        (False, False, 0, min(tag_total, 12)),
        (True, False, 0, min(tag_total, 12)),
        (True, True, 0, min(tag_total, 12)),
        (True, True, 48, min(tag_total, 10)),
        (True, True, 32, len(_CORE_HASHTAGS)),
    ]


def _supports_dash() -> bool:
    """`%-d` (no zero padding) is glibc-only; Windows strftime rejects it."""
    try:
        date(2026, 8, 5).strftime("%-d")
        return True
    except ValueError:
        return False
