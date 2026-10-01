"""What a follower asked for, read out of what they typed.

A DM arrives as whatever someone thumbs into a phone at a red light: "avísame cuando
zaragoza baje de 20", "cual puente esta mas rapido?", "cuanto esta el libre a pie",
"es mejor zaragoza o lerdo", "ya no me avises de zaragoza", "gracias!!", "hola, como
esta pdn". Until this existed only a handful of exact phrasings were understood: the
alert handler kept just the number and filed every request as Paso del Norte, "gracias"
was answered with "no encuentro ese puente", and a question about a bridge ignored the
lane it named.

classify() turns a message into an Intent: what they want, which bridges, which lane,
what limit, which language. It is pure text work — no network, no snapshot — so any DM
handler can use it; the bridge names it returns are resolved by Snapshot.port (aliases,
words, close spellings) the same way every other bridge question is.

Matching is done on a folded copy of the message: lower case, accents removed, emoji and
punctuation dropped. "Avísame", "avisame" and "AVISAME!!" are the same request.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

DEFAULT_LIMIT = 30
# A limit above this is not a wait anyone would ask about; it is a port number
# ("240203") or another stray figure, never "tell me when it drops under 6 hours".
MAX_LIMIT = 300

# ── what they want ───────────────────────────────────────────────────────────────
# Checked in this order, first match wins. Cancel comes before alert because "no me
# avises" contains "avises"; crossing-done before crossing-start because "ya cruce"
# must not read as someone about to cross.
CANCEL = ("ya no me avises", "no me avises", "deja de avisarme", "dejar de avisarme",
          "ya no quiero avisos", "cancela", "cancelar", "quita la alerta", "quitar alerta",
          "borra la alerta", "borrar alerta", "desuscribir", "alto", "stop alerts", "stop",
          "unsubscribe", "cancel", "no more alerts", "turn off alerts")
CROSSING_DONE = ("ya cruce", "ya cruzamos", "ya pase", "ya pasamos", "ya llegue",
                 "ya estoy del otro lado", "i crossed", "we crossed", "made it across",
                 "just crossed", "done crossing")
CROSSING_DONE_ALONE = ("cruce", "crossed")        # only as the whole message: "el cruce de…"
CROSSING_START = ("voy a cruzar", "vamos a cruzar", "estoy cruzando", "estamos cruzando",
                  "ya estoy en la fila", "estoy en la fila", "estamos en la fila", "me forme",
                  "entrando a la fila", "crossing now", "im crossing", "i am crossing",
                  "im in line", "i am in line", "in line at", "getting in line")
CROSSING_START_LEAD = ("cruzando", "crossing")    # only at the start: "the crossing at…"
ALERT = ("avisame", "avisarme", "me avisas", "me avises", "avisas", "aviso", "alertame",
         "alerta", "notificame", "notifica", "dime cuando", "mandame un mensaje",
         "mandame mensaje", "escribeme cuando", "let me know", "notify me", "notify",
         "tell me when", "ping me", "text me", "message me", "remind me", "alert me", "alert")
ALERT_LEAD = ("cuando", "when", "si baja", "if")  # "cuando zaragoza baje de 20" + a number
SAVE = ("guardar", "guarda", "guardame", "mi puente es", "mi puente", "agrega", "agregar",
        "my bridge is", "my bridge", "save")
BEST = ("cual puente", "que puente", "cual es el mejor", "cual esta mas rapido",
        "cual es mas rapido", "el mas rapido", "mas rapido", "el mejor puente",
        "mejor puente", "por donde cruzo", "por cual cruzo", "por cual me voy",
        "cual me conviene", "which bridge", "best bridge", "fastest", "quickest",
        "shortest", "where should i cross", "which one is faster", "which is faster")
HELP = ("ayuda", "que puedes hacer", "que haces", "como funciona", "como te uso",
        "instrucciones", "opciones", "help", "what can you do", "how does this work",
        "commands", "options")
MENU = ("todos los puentes", "como estan los puentes", "tiempos de espera", "tiempos",
        "puentes", "todos", "menu", "all bridges", "wait times", "bridges", "waits", "all")
GREETING = ("buenos dias", "buenas tardes", "buenas noches", "buenas", "que onda", "hola",
            "good morning", "good evening", "hello", "hey", "hi")
THANKS = ("muchas gracias", "mil gracias", "gracias", "perfecto", "excelente", "orale",
          "sale", "vale", "listo", "thank you", "thanks", "thx", "great", "cool", "ok",
          "okay", "ty")
THANKS_EMOJI = ("👍", "🙏", "❤", "👌", "🙌", "💯")

# ── which lane ───────────────────────────────────────────────────────────────────
# Most specific first: "ready a pie" is the walking Ready Lane, not the car one.
WALK_WORDS = ("a pie", "caminando", "peatonal", "peatones", "peaton", "walking", "on foot",
              "pedestrian", "walkers", "walk")
LANE_PATTERNS = (
    ("car_sentri", ("sentri", "nexus")),
    ("walk_ready", tuple(f"ready {w}" for w in WALK_WORDS) + tuple(f"{w} ready" for w in WALK_WORDS)),
    ("car_ready", ("ready lane", "ready")),
    ("walk", WALK_WORDS),
    ("truck_fast", ("fast lane", "carril fast")),
    ("truck", ("camiones", "camion", "carga", "trailer", "comercial", "commercial", "trucks", "truck")),
    ("car", ("carro", "carros", "auto", "autos", "coche", "vehicular", "vehiculo", "car", "cars")),
)

# ── which limit ──────────────────────────────────────────────────────────────────
NUMBER_WORDS = {
    "cinco": 5, "diez": 10, "quince": 15, "veinte": 20, "veinticinco": 25, "treinta": 30,
    "cuarenta": 40, "cincuenta": 50, "sesenta": 60, "noventa": 90,
    "five": 5, "ten": 10, "fifteen": 15, "twenty": 20, "thirty": 30, "forty": 40,
    "fifty": 50, "sixty": 60, "ninety": 90,
}
PHRASE_LIMITS = (   # longest first, so "hora y media" is not read as "hora"
    ("hora y media", 90), ("hour and a half", 90), ("an hour and a half", 90),
    ("cuarto de hora", 15), ("quarter of an hour", 15), ("quarter hour", 15),
    ("media hora", 30), ("half an hour", 30), ("half hour", 30),
    ("cuarenta y cinco", 45), ("forty five", 45), ("twenty five", 25), ("veinte y cinco", 25),
    ("una hora", 60), ("an hour", 60), ("one hour", 60), ("dos horas", 120), ("two hours", 120),
)

# ── words that can be no bridge ──────────────────────────────────────────────────
# Taken out before the lookup so the typo matcher never compares "cuando" or "under"
# with an alias. Separators ("o", "y", "or", "and", "vs") are kept aside: they split
# "zaragoza o lerdo" into two bridges.
SEPARATORS = {"o", "y", "or", "and", "vs", "versus", "contra", "u", "e"}
FILLER = {
    # es
    "cuando", "baje", "bajar", "baja", "de", "del", "a", "al", "menos", "que", "este", "esta",
    "en", "la", "el", "los", "las", "lo", "fila", "espera", "tiempo", "minutos", "minuto",
    "min", "mins", "m", "por", "favor", "porfa", "me", "sea", "haya", "carril", "si", "hay",
    "cuanto", "cuantos", "como", "estan", "va", "anda", "linea", "hoy", "ahora",
    "ahorita", "mas", "o", "para", "con", "es", "son", "mi", "tu", "un", "una",
    "quiero", "saber", "dime", "puedes", "podrias", "oye", "porfavor", "pls", "plis", "ya",
    "cual", "mejor", "rapido", "hora", "horas", "h", "hr", "hrs", "tarda", "tardan", "cola",
    # "puente libre" still resolves through its alias once "puente" is gone; left in,
    # "cual es el puente mas rapido" would be taken for a bridge called "puente".
    "puente", "puentes", "bridge", "bridges", "border", "frontera", "garita", "garitas",
    "alertas", "avisos", "notificaciones", "alerts", "notifications", "todas", "everything",
    # en
    "when", "the", "is", "it", "its", "gets", "get", "drops", "drop", "below", "under", "less",
    "than", "wait", "line", "minutes", "minute", "please", "at", "goes", "to", "falls", "lane",
    "for", "on", "how", "long", "much", "whats", "what", "time", "now", "today", "right",
    "there", "an", "of", "in", "my", "your", "can", "you", "i", "want", "know", "tell",
    "hour", "hours", "better", "faster", "or", "and", "which", "one", "does", "do", "take",
}
# Every phrase used to recognise an intent is also taken out of the bridge text.
_INTENT_PHRASES = (CANCEL + CROSSING_DONE + CROSSING_DONE_ALONE + CROSSING_START
                   + CROSSING_START_LEAD + ALERT + SAVE + BEST + HELP + MENU + GREETING + THANKS)

# ── which language ───────────────────────────────────────────────────────────────
ES_MARKERS = {"avisame", "avisarme", "avises", "cuando", "baje", "baja", "cuanto", "esta",
              "puente", "puentes", "hola", "gracias", "fila", "cual", "mas", "rapido",
              "guardar", "guarda", "cruzar", "cruce", "menos", "ayuda", "que", "como", "alto",
              "voy", "ya", "de", "mi", "alerta", "buenas", "dime", "mejor", "pie", "tiempo"}
EN_MARKERS = {"when", "alert", "let", "know", "under", "below", "how", "long", "is", "the",
              "bridge", "bridges", "which", "fastest", "best", "save", "crossing", "crossed",
              "help", "thanks", "hi", "hello", "stop", "my", "notify", "tell", "wait", "at",
              "what", "whats", "quickest", "walking", "foot", "thank", "hey"}


@dataclass(frozen=True)
class Intent:
    """kind: alert | cancel | crossing_done | crossing_start | save | best | bridge |
    menu | help | thanks | unknown. bridges are name texts to resolve, possibly several
    ("es mejor zaragoza o lerdo"); lane is None when none was named; limit only for alerts."""
    kind: str
    lang: str
    lane: str | None = None
    limit: int | None = None
    bridges: tuple[str, ...] = ()


def classify(message: str, default_lang: str = "es") -> Intent:
    text = fold(message)
    lang = language_of(message, default_lang)
    lane = lane_in(message)
    bridges = bridge_texts(message)

    def has(phrases) -> bool:
        return any(_contains(text, p) for p in phrases)

    if has(CANCEL):
        return Intent("cancel", lang, lane, bridges=bridges)
    if has(CROSSING_DONE) or text in CROSSING_DONE_ALONE:
        return Intent("crossing_done", lang)
    if has(CROSSING_START) or text.startswith(tuple(f"{p} " for p in CROSSING_START_LEAD)) \
            or text in CROSSING_START_LEAD:
        return Intent("crossing_start", lang, lane, bridges=bridges)
    if has(ALERT) or (text.startswith(ALERT_LEAD) and _stated_limit(text) is not None):
        return Intent("alert", lang, lane, limit_in(message), bridges)
    if has(SAVE):
        return Intent("save", lang, lane, bridges=bridges)
    if bridges:
        return Intent("bridge", lang, lane, bridges=bridges)
    if has(BEST):
        return Intent("best", lang, lane)
    if _only_emoji_thanks(message):
        return Intent("thanks", lang)
    if has(HELP) or not text:
        return Intent("help", lang)
    if has(MENU) or has(GREETING):
        return Intent("menu", lang, lane)
    if has(THANKS):
        return Intent("thanks", lang)
    return Intent("unknown", lang)


# ── the pieces, usable on their own ──────────────────────────────────────────────
def is_alert_request(message: str) -> bool:
    return classify(message).kind == "alert"


def language_of(message: str, default: str = "es") -> str:
    """The language the message is written in, by counting words only one language
    uses. A tie keeps the default, so a bare "zaragoza" is answered in the account's."""
    words = set(fold(message).split())
    es, en = len(words & ES_MARKERS), len(words & EN_MARKERS)
    if es == en:
        return default
    return "es" if es > en else "en"


def lane_in(message: str) -> str | None:
    """The lane named in the message, or None when it names none (the caller then uses
    the person's saved lane, or the car lane)."""
    text = fold(message)
    for lane, words in LANE_PATTERNS:
        if any(_contains(text, w) for w in words):
            return lane
    return None


def limit_in(message: str, default: int = DEFAULT_LIMIT) -> int:
    limit = _stated_limit(fold(message))
    return default if limit is None else limit


def bridge_texts(message: str) -> tuple[str, ...]:
    """Every bridge name in the message, split on "o" / "y" / "or" / "and" / "vs" —
    what is left once intent words, lane words, numbers and filler are taken out."""
    text = fold(message)
    for phrase in sorted(_INTENT_PHRASES + tuple(w for _, ws in LANE_PATTERNS for w in ws)
                         + tuple(p for p, _ in PHRASE_LIMITS), key=len, reverse=True):
        text = re.sub(rf"(?<!\w){re.escape(phrase)}(?!\w)", " ", text)
    text = re.sub(r"\d+", " ", text)
    parts, current = [], []
    for word in text.split():
        if word in SEPARATORS:
            parts.append(current)
            current = []
        elif word not in FILLER and word not in NUMBER_WORDS:
            current.append(word)
    parts.append(current)
    return tuple(" ".join(p) for p in parts if p)


def bridge_text(message: str) -> str:
    """The single bridge a message names, or "" — for callers that expect one."""
    found = bridge_texts(message)
    return found[0] if len(found) == 1 else " ".join(found)


def fold(message: str) -> str:
    """Lower case, accents off, punctuation and emoji out, whitespace collapsed."""
    text = unicodedata.normalize("NFKD", message.lower())
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = re.sub(r"[^\w\s<]", " ", text)
    return " ".join(text.split())


# ── internals ────────────────────────────────────────────────────────────────────
def _contains(text: str, phrase: str) -> bool:
    return re.search(rf"(?<!\w){re.escape(phrase)}(?!\w)", text) is not None


def _stated_limit(text: str) -> int | None:
    """A wait in minutes, as people write it: "20", "20min", "<20", "1h30", "1 hora",
    "media hora", "veinte", "an hour and a half". None when no limit is stated."""
    for phrase, minutes in PHRASE_LIMITS:
        if _contains(text, phrase):
            return minutes
    hm = re.search(r"(\d+)\s*(?:hours|hour|horas|hora|hrs|hr|h)(?![a-z])\s*(\d+)?", text)
    if hm:
        minutes = int(hm.group(1)) * 60 + int(hm.group(2) or 0)
        if 0 < minutes <= MAX_LIMIT:
            return minutes
    for figure in re.findall(r"\d+", text):
        value = int(figure)
        if 0 < value <= MAX_LIMIT:
            return value
    for word in text.split():
        if word in NUMBER_WORDS:
            return NUMBER_WORDS[word]
    return None


def _only_emoji_thanks(message: str) -> bool:
    return not fold(message) and any(e in message for e in THANKS_EMOJI)
