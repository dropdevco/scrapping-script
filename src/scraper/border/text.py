"""Captions and replies, written for someone who crosses every day.

Every message leads with the answer — the fastest bridge, or their bridge's number —
before any list. Three rules hold everywhere:

  - Minutes appear only when state is 'open'. Closed says closed, no data says so.
    A missing number is never rendered as 0.
  - "Updated HH:MM" never quotes a CBP stamp that is ahead of the clock.
  - A change is only worth showing when it is big enough to act on (5+ minutes).
"""
from __future__ import annotations

from datetime import datetime

from .cbp import LANE_LABELS, Port, Snapshot

CAPTION_LANES = ("car", "car_sentri", "car_ready", "walk")
OLD_AFTER_MINUTES = 95   # only call a reading old when it really is old
# Instagram rejects captions past 2,200 characters; keep room for the pipeline to add
# its own hashtags without the post failing at publish time.
CAPTION_LIMIT = 2000
HEADLINE_LANE = "car"
# The compact one-line lanes, in either language: what _fit drops first.
COMPACT_PREFIXES = tuple(f"{label}:" for lane in CAPTION_LANES if lane != HEADLINE_LANE
                         for label in LANE_LABELS[lane])
SHORT_NAMES = {   # what people call them in a one-line list
    "240202": ("Paso del Norte", "Paso del Norte"),
    "240203": ("Ysleta", "Zaragoza"),
    "240201": ("BOTA", "Puente Libre"),
    "240204": ("Stanton", "Lerdo"),
    "240801": ("Santa Teresa", "Santa Teresa"),
    "240221": ("Tornillo", "Tornillo"),
}


def _clock(iso: str | None, lang: str) -> str:
    """'9:40 p. m.' / '9:40 pm'. Hand-rolled rather than strftime("%-I"): that flag is
    glibc-only and this must render identically on CI (Linux) and local Windows."""
    if not iso:
        return "?"
    at = datetime.fromisoformat(iso)
    afternoon = at.hour >= 12
    meridiem = ("p. m." if afternoon else "a. m.") if lang == "es" else ("pm" if afternoon else "am")
    return f"{at.hour % 12 or 12}:{at.minute:02d} {meridiem}"


def _pick(en: str, es: str, lang: str) -> str:
    return es if lang == "es" else en


def _label(lane_id: str, lang: str) -> str:
    return _pick(*LANE_LABELS[lane_id], lang)


def _name(port: Port, lang: str) -> str:
    return _pick(port.name, port.name_es, lang)


def _row_name(row: dict, lang: str) -> str:
    """The same, for a decorated lane or any dict carrying name / name_es."""
    return _pick(row["name"], row["name_es"], lang)


def _short(port_number: str, name: str, lang: str) -> str:
    return _pick(*SHORT_NAMES.get(port_number, (name, name)), lang)


def _crosses(iso: str | None, lang: str) -> str:
    """' · cruzas 2:05 p. m.', the suffix every wait carries when we know it."""
    if not iso:
        return ""
    return _pick(" · across by ", " · cruzas ", lang) + _clock(iso, lang)


def opinions_text(opinions: list[dict]) -> str:
    """'cbp 20, pasosfronterizos 58' — each source that gave a number."""
    return ", ".join(f"{o['source']} {o['minutes']}" for o in opinions if o.get("minutes") is not None)


def duration(minutes: int | None) -> str:
    """75 minutes reads as "1 h 15", which people parse faster than a raw count."""
    if minutes is None:
        return "?"
    if minutes < 60:
        return f"{minutes} min"
    hours, rest = divmod(minutes, 60)
    if rest == 0:
        return f"{hours} h"
    return f"{hours} h {rest:02d}"


# Postgres dow numbering, which is what border_typical_waits returns: 0 = Sunday.
WEEKDAYS = {"es": ("domingo", "lunes", "martes", "miércoles", "jueves", "viernes", "sábado"),
            "en": ("Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday")}


def typical_phrase(typical: dict | None, lang: str) -> str | None:
    """'45 min' means little alone; 'worse than a usual Tuesday' tells them what to do."""
    if not typical or typical["compared"] == "usual":
        return None
    day = WEEKDAYS["es" if lang == "es" else "en"][typical["weekday"]]
    usual, diff = duration(typical["minutes"]), abs(typical["difference"])
    if lang == "es":
        how = "más" if typical["compared"] == "worse" else "menos"
        return f"Un {day} a esta hora suele estar en {usual}: hoy {diff} min {how}."
    how = "more" if typical["compared"] == "worse" else "less"
    return f"A usual {day} at this hour is {usual}: {diff} min {how} today."


def trend_phrase(trend: dict | None, lang: str) -> str | None:
    """"It has grown 15 minutes in the last hour" changes what someone does next."""
    if not trend or not trend["worth_saying"]:
        return None
    amount, over = abs(trend["change"]), trend["over_minutes"]
    if lang == "es":
        verb = "subió" if trend["direction"] == "up" else "bajó"
        return f"{verb} {amount} min en la última hora" if over >= 45 else f"{verb} {amount} min hace poco"
    verb = "up" if trend["direction"] == "up" else "down"
    return f"{verb} {amount} min in the last hour" if over >= 45 else f"{verb} {amount} min just now"


def lane_tip(faster: dict | None, lang: str) -> str | None:
    """Point out a much quicker lane at the same bridge."""
    if not faster:
        return None
    label = _label(faster["lane"], lang)
    minutes, saved = duration(faster["minutes"]), faster["saves_minutes"]
    if faster["lane"] == "walk":
        # Anyone can park and walk, so this one needs no condition.
        return (f"A pie son {minutes} — {saved} menos que en carro." if lang == "es"
                else f"On foot it is {minutes} — {saved} less than driving.")
    # SENTRI and Ready Lane need a card, so offer them as a conditional, not an instruction.
    return (f"Si tienes {label}: {minutes} — {saved} menos." if lang == "es"
            else f"If you have {label}: {minutes} — {saved} less.")


def _arrow(delta: dict | None) -> str:
    """↓10 means ten minutes better than the last reading."""
    if not delta or not delta["meaningful"]:
        return ""
    return f" {'↑' if delta['direction'] == 'up' else '↓'}{abs(delta['change'])}"


def last_honest_update(snapshot: Snapshot) -> str | None:
    stamps = [l.cbp_updated_at for p in snapshot.ports for l in p.lanes.values()
              if l.state == "open" and l.cbp_updated_at and not l.stamp_ahead_of_clock]
    return max(stamps) if stamps else None


# ------------------------------------------------------------------ posts
def sources_footer(feed, lang: str = "es") -> str:
    """Which sites this post was built from, and whether any of them argued."""
    names = [name for name, status in feed.source_status.items() if status.contributed]
    if not names:
        return ""
    joined = ", ".join(names)
    disputed = feed.lanes_disputed
    line = (f"Fuentes: {joined}" if lang == "es" else f"Sources: {joined}")
    if disputed:
        line += (f" · {disputed} dato(s) en disputa, mostramos el más reciente"
                 if lang == "es" else f" · {disputed} reading(s) disputed, newest shown")
    return line


def caption(feed, snapshot: Snapshot, lang: str = "es") -> str:
    """Headline first: the fastest bridge, then the cars list, then the rest compact."""
    es = lang == "es"
    lines = [("Puentes Juárez–El Paso" if es else "Juárez–El Paso bridges")
             + f" · {_clock(snapshot.local_at.isoformat(), lang)}"]

    best = feed.best_in(snapshot, HEADLINE_LANE)
    if best:
        name = _row_name(best, lang)
        # A disputed headline shows the range, so nobody is promised the optimistic end.
        shown = (f"{best['optimistic_minutes']}–{best['planning_minutes']} min"
                 if best.get("sources_disagree") else f"{best['minutes']} min")
        lines.append((f"Más rápido ahora: {name} {shown}" if es else f"Fastest now: {name} {shown}")
                     + _crosses(best["crosses_at"], lang))
    lines.append("")

    lines.append(_label(HEADLINE_LANE, lang))
    for row in feed.ranked_in(snapshot, HEADLINE_LANE):
        short = _short(row["port_number"], _row_name(row, lang), lang)
        if row["state"] == "open" and row.get("sources_disagree"):
            lines.append(f"{short} {row['optimistic_minutes']}–{row['planning_minutes']} min")
        elif row["state"] == "open" and row.get("closes_before_you_cross"):
            shuts = _clock(row["closes_at"], lang)
            lines.append(f"{short} {row['minutes']} min · " + (f"cierra {shuts}" if es else f"closes {shuts}"))
        elif row["state"] == "open":
            lines.append(f"{short} {duration(row['minutes'])}{_arrow(row['delta'])}")
        elif row["state"] == "closed":
            lines.append(f"{short} " + ("cerrado" if es else "closed"))
    lines.append("")

    for lane_id in CAPTION_LANES:
        if lane_id == HEADLINE_LANE:
            continue
        open_rows = [r for r in feed.ranked_in(snapshot, lane_id) if r["state"] == "open"]
        if not open_rows:
            continue
        parts = [f"{_short(r['port_number'], _row_name(r, lang), lang)} {r['minutes']}" for r in open_rows]
        lines.append(f"{_label(lane_id, lang)}: " + " · ".join(parts))

    dark = [_short(p.port_number, _name(p, lang), lang) for p in snapshot.ports if not p.has_live_data]
    if dark:
        lines += ["", ("Sin datos de CBP: " if es else "No CBP data: ") + ", ".join(dark)]
    stamp = last_honest_update(snapshot)
    if stamp:
        lines.append(("Datos de CBP, actualizados " if es else "CBP data, updated ") + _clock(stamp, lang))
    footer = sources_footer(feed, lang)
    if footer:
        lines.append(footer)
    return _fit(lines, lang)


def _fit(lines: list[str], lang: str) -> str:
    """Keep the caption inside Instagram's limit, dropping detail before essentials.

    The headline, the cars list and the footer are what the post is for; the compact
    lane lines in the middle go first if anything has to.
    """
    caption = "\n".join(lines).strip()
    if len(caption) <= CAPTION_LIMIT:
        return caption

    keep, dropped = [], 0
    for line in lines:
        if line.startswith(COMPACT_PREFIXES) and len(caption) > CAPTION_LIMIT:
            dropped += 1
            caption = caption.replace(line + "\n", "", 1)
            continue
        keep.append(line)
    if dropped:
        keep.append("…")
    caption = "\n".join(keep).strip()
    return caption[:CAPTION_LIMIT].rsplit("\n", 1)[0] if len(caption) > CAPTION_LIMIT else caption


# --------------------------------------------------------------- messages
def unknown_bridge(lang: str = "es") -> str:
    return ("No encuentro ese puente. Escribe \"puentes\" para ver todos."
            if lang == "es" else "I don't know that bridge. Send \"bridges\" to see them all.")


def no_such_lane(port: Port, lane_id: str, lang: str = "es") -> str:
    name, label = _name(port, lang), _label(lane_id, lang)
    return (f"{name} no tiene carril {label}." if lang == "es" else f"{name} has no {label} lane.")


def _sources_line(body: dict, lang: str) -> str | None:
    """Say plainly when the sites disagree, instead of picking a winner quietly."""
    if not body.get("sources_disagree"):
        return None
    opinions = (body.get("consensus") or {}).get("opinions", [])
    if sum(1 for o in opinions if o.get("minutes") is not None) < 2:
        return None
    joined = opinions_text(opinions)
    return (f"Las fuentes no coinciden ({joined}); usamos la más reciente."
            if lang == "es" else f"Sources disagree ({joined}); we use the newest.")


def bridge_reply(body: dict, lang: str = "es") -> str:
    """One bridge, one lane: minutes, movement, when they'd cross, better option."""
    es = lang == "es"
    name = _row_name(body, lang)
    label = _label(body["lane"], lang)

    if body["state"] == "closed":
        return f"{name} · {label}: " + ("cerrado ahora." if es else "closed right now.")
    if body["state"] != "open":
        return (f"{name} · {label}: CBP no está reportando datos ahora."
                if es else f"{name} · {label}: CBP is not reporting data right now.")

    line = f"{name} · {label}: {duration(body['minutes'])}{_arrow(body.get('delta'))}"
    if body.get("closes_before_you_cross") and body.get("closes_at"):
        shuts = _clock(body["closes_at"], lang)
        line += (f" — cierra a las {shuts}, no alcanzas" if es
                 else f" — closes at {shuts}, you would not make it")
        alt = body.get("alternative")
        if alt:
            alt_name = _row_name(alt, lang)
            line += (f"\nMejor {alt_name}: {alt['minutes']} min."
                     if es else f"\nBetter: {alt_name} at {alt['minutes']} min.")
        return line
    line += _crosses(body.get("crosses_at"), lang)
    # Cite whoever the number actually came from, not whoever we usually quote.
    consensus = body.get("consensus") or {}
    chosen = consensus.get("chosen_source", "cbp")
    if chosen != "cbp" and body.get("age_minutes") is not None:
        line += (f"\n{chosen}, hace {body['age_minutes']} min"
                 if es else f"\n{chosen}, {body['age_minutes']} min ago")
    elif body.get("cbp_updated_at"):
        line += (f"\nCBP actualizó a las {_clock(body['cbp_updated_at'], lang)}"
                 if es else f"\nCBP updated at {_clock(body['cbp_updated_at'], lang)}")
    # Low confidence can come from a disagreement, which the sources line explains.
    # Only say "old" when the number genuinely is old.
    age = body.get("age_minutes")
    if age is not None and age > OLD_AFTER_MINUTES:
        line += (" — dato viejo, puede haber cambiado" if es
                 else " — old reading, it may have moved")

    alt = body.get("alternative")
    if alt and alt.get("saves_minutes"):
        alt_name = _row_name(alt, lang)
        line += (f"\n{alt_name} está en {alt['minutes']} min — {alt['saves_minutes']} menos."
                 if es else f"\n{alt_name} is at {alt['minutes']} min — {alt['saves_minutes']} less.")
    usual = typical_phrase(body.get("typical"), lang)
    if usual:
        line += "\n" + usual
    movement = trend_phrase(body.get("trend"), lang)
    if movement:
        line += f"\n{'La fila ' if es else 'The line is '}{movement}."
    tip = lane_tip(body.get("faster_lane_here"), lang)
    if tip:
        line += "\n" + tip
    disagreement = _sources_line(body, lang)
    if disagreement:
        line += "\n" + disagreement
    return line


def my_digest(rows: list[dict], best: dict | None, lane_id: str, lang: str = "es") -> str:
    """Their saved bridges, their lane. Two or three lines, not thirty."""
    es = lang == "es"
    if not rows:
        return ("Todavía no guardas puentes. Escribe \"puentes\" para elegir."
                if es else "You haven't saved any bridges yet. Send \"bridges\" to pick.")

    label = _label(lane_id, lang)
    lines = [f"{'Tus puentes' if es else 'Your bridges'} · {label}"]
    for row in rows:
        name = _short(row["port_number"], _row_name(row, lang), lang)
        if row["state"] == "open":
            line = f"{name}: {row['minutes']} min{_arrow(row.get('delta'))}" + _crosses(row.get("crosses_at"), lang)
        elif row["state"] == "closed":
            line = f"{name}: " + ("cerrado" if es else "closed")
        else:
            line = f"{name}: " + ("sin datos" if es else "no data")
        lines.append(line)

    if best and best["port_number"] not in {r["port_number"] for r in rows}:
        # Compare expected waits, not raw ones: a fresher reading is the better bet.
        mine = [r["effective_minutes"] for r in rows
                if r["state"] == "open" and r.get("effective_minutes") is not None]
        if mine and min(mine) - best["effective_minutes"] >= 10:
            name = _row_name(best, lang)
            lines.append(f"Más rápido: {name} {best['minutes']} min."
                          if es else f"Faster: {name} at {best['minutes']} min.")
    return "\n".join(lines)


def drop_alert(port: Port, lane_id: str, minutes: int, below: int,
               crosses_at: str | None = None, lang: str = "es") -> str:
    name, label = _name(port, lang), _label(lane_id, lang)
    if lang == "es":
        line = f"Bajó la fila: {name} · {label} está en {minutes} min (menos de {below})."
        # The Spanish clock already ends in a period ("2:00 p. m."), so no full stop here.
        return line + (f" Cruzas alrededor de {_clock(crosses_at, lang)}" if crosses_at else "")
    line = f"The line dropped: {name} · {label} is at {minutes} min (under {below})."
    return line + (f" Across by about {_clock(crosses_at, lang)}." if crosses_at else "")


def menu(snapshot: Snapshot, lang: str = "es") -> list[str]:
    return [_short(p.port_number, _name(p, lang), lang) for p in snapshot.ports]


# -------------------------------------------------------------- crossings
def crossing_started(body: dict, lang: str = "es") -> str:
    name = _row_name(body, lang)
    if lang == "es":
        return (f"Va: {name}, {duration(body['minutes'])} según nosotros. "
                "Cuando pases la caseta escribe \"ya crucé\" — nos ayuda a saber qué fuente acierta.")
    return (f"Got it: {name}, {duration(body['minutes'])} by our numbers. "
            "Send \"crossed\" once you are past the booth — it tells us which source is right.")


def crossing_finished(result: dict, port: dict, lang: str = "es") -> str:
    es = lang == "es"
    name = port.get("name_es" if es else "name", result["port_number"])
    said = opinions_text(result["claims"])
    line = (f"Tardaste {duration(result['actual_minutes'])} en {name}." if es
            else f"You took {duration(result['actual_minutes'])} at {name}.")
    if said:
        line += (f" Las fuentes decían: {said}." if es else f" The sources said: {said}.")
    return line + (" ¡Gracias!" if es else " Thank you!")


CROSSING_ERRORS = {
    "not_open": ("Ese carril no está abierto ahora; no hay nada que medir.",
                 "That lane is not open right now, so there is nothing to measure."),
    "unknown_bridge": ("No encuentro ese puente. Escribe \"voy a cruzar\" y el nombre.",
                       "I don't know that bridge. Send \"crossing\" and its name."),
    "implausible": ("Ese tiempo no parece un cruce real, así que no lo anoté.",
                    "That time does not look like a real crossing, so I did not log it."),
    "finished": ("Ese cruce ya estaba anotado.", "That crossing was already logged."),
}


def crossing_error(code: str, lang: str = "es") -> str:
    es, en = CROSSING_ERRORS.get(code, ("No pude anotar ese cruce.", "I could not log that crossing."))
    return _pick(en, es, lang)
