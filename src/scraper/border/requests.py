"""What a follower asked for, read out of what they typed.

"avísame cuando zaragoza baje de 20", "alert me when santa teresa is under 10",
"avisame cuando sentri en el puente libre este en menos de 15 min". Until this existed
the alert handler kept only the number: every request was filed as Paso del Norte, car
lane, whatever bridge was named, and answered in Spanish.

Pure functions over the message text, so any DM handler can use them. The bridge itself
is resolved by Snapshot.port — aliases ("libre", "pdn"), words, then close spellings
("sarragoza") — once the words that are clearly not a bridge name are taken out.
"""

from __future__ import annotations

import re

DEFAULT_LIMIT = 30
# A limit above this is not a wait anyone would ask about; it is a port number
# ("240203") or another stray figure, never "tell me when it drops under 4 hours".
MAX_LIMIT = 300

ES_TRIGGERS = ("avísame", "avisame", "aviso", "alerta", "dime cuando", "notifícame", "notificame")
EN_TRIGGERS = ("alert me", "alert", "let me know", "notify me", "tell me when")

# Lane words, most specific first: "ready a pie" is the walking Ready Lane, not the
# car one, and "sentri" never means anything but the car SENTRI lane.
WALK_WORDS = ("a pie", "caminando", "peatonal", "peatones", "peaton", "peatón", "walking", "on foot", "walk")
LANE_PATTERNS = (
    ("car_sentri", ("sentri", "nexus")),
    ("walk_ready", tuple(f"ready {w}" for w in WALK_WORDS) + tuple(f"{w} ready" for w in WALK_WORDS)),
    ("car_ready", ("ready",)),
    ("walk", WALK_WORDS),
    ("truck_fast", ("fast",)),
    ("truck", ("camión", "camion", "carga", "trailer", "tráiler", "truck")),
)

# Words that appear in an alert request but in no bridge name. Taking them out before
# the lookup keeps the typo matcher from comparing "cuando" or "under" with an alias.
FILLER = {
    # es
    "cuando", "baje", "bajé", "bajar", "de", "del", "a", "al", "menos", "que", "este", "esté",
    "está", "esta", "en", "la", "el", "los", "las", "fila", "espera", "tiempo", "minutos",
    "minuto", "min", "mins", "por", "favor", "porfa", "me", "y", "sea", "haya", "carril",
    "carro", "auto", "coche", "si", "hay",
    # en
    "when", "the", "is", "it", "its", "gets", "get", "drops", "drop", "below", "under", "less",
    "than", "wait", "line", "minutes", "minute", "please", "at", "goes", "to", "falls", "lane",
    "car", "for", "on",
}


def is_alert_request(message: str) -> bool:
    text = _normalise(message)
    return text.startswith(ES_TRIGGERS + EN_TRIGGERS)


def language_of(message: str, default: str = "es") -> str:
    """The language of the request itself, so a follower who writes in English is
    answered in English whatever the account's default. The longest matching trigger
    decides: "alerta" is Spanish even though it starts with "alert"."""
    text = _normalise(message)
    matches = [(len(t), lang) for lang, triggers in (("es", ES_TRIGGERS), ("en", EN_TRIGGERS))
               for t in triggers if text.startswith(t)]
    return max(matches)[1] if matches else default


def lane_in(message: str) -> str | None:
    """The lane named in the message, or None when it names none (the caller then uses
    the person's saved lane, or the car lane)."""
    text = _normalise(message)
    for lane, words in LANE_PATTERNS:
        if any(re.search(rf"\b{re.escape(w)}\b", text) for w in words):
            return lane
    return None


def limit_in(message: str, default: int = DEFAULT_LIMIT) -> int:
    for figure in re.findall(r"\d+", message):
        value = int(figure)
        if 0 < value <= MAX_LIMIT:
            return value
    return default


def bridge_text(message: str) -> str:
    """What is left once the trigger, the numbers, the lane words and the filler are
    taken out — the part that can only be a bridge name. Empty when none was named."""
    text = _normalise(message)
    for trigger in sorted(ES_TRIGGERS + EN_TRIGGERS, key=len, reverse=True):
        if text.startswith(trigger):
            text = text[len(trigger):]
            break
    for _, words in LANE_PATTERNS:
        for w in sorted(words, key=len, reverse=True):
            text = re.sub(rf"\b{re.escape(w)}\b", " ", text)
    text = re.sub(r"\d+", " ", text)
    words = [w for w in re.split(r"[\s,.;:!?¿¡]+", text) if w and w not in FILLER]
    return " ".join(words)


def _normalise(message: str) -> str:
    return " ".join(message.strip().lower().split())
