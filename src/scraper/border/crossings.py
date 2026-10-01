"""Real crossing times, logged against what each source claimed.

We can tell which source is fresher, never which is right. When CBP says 20 and
pasosfronterizos says 58, only someone who actually crossed can settle it. A follower
says "voy a cruzar" when they join the line and "ya crucé" past the booth; every
source's opinion is frozen at the first message, so each is judged on what it said
when it mattered, not on a later, luckier reading.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta

MIN_PLAUSIBLE = 1          # under a minute is a mistyped "ya crucé", not a crossing
MAX_PLAUSIBLE = 6 * 60     # past six hours they forgot to tell us; the number is noise
ENOUGH_TO_JUDGE = 10       # crossings per source before its error means anything
CLOSE_ENOUGH = 10          # minutes; "within 10" is the figure a crosser cares about


# Every reason a crossing can be refused, and the HTTP status it answers with. The
# follower-facing wording for each code lives in core.text.CROSSING_ERRORS.
ERROR_STATUS = {"unknown": 404, "unknown_bridge": 404, "not_open": 422,
                "finished": 422, "implausible": 422}


class CrossingError(ValueError):
    """The crossing cannot be started or finished. The message is for logs and the API;
    `code` is for writing the reply in the follower's language."""

    def __init__(self, message: str, code: str = "unknown"):
        super().__init__(message)
        self.code = code

    @property
    def http_status(self) -> int:
        return ERROR_STATUS.get(self.code, 422)


def _claims(row: dict) -> list[dict]:
    consensus = row.get("consensus") or {}
    opinions = consensus.get("opinions") or [{"source": "cbp", "minutes": row.get("cbp_minutes"),
                                              "age_minutes": row.get("age_minutes")}]
    return [{"source": o["source"], "minutes": o.get("minutes"), "age_minutes": o.get("age_minutes")}
            for o in opinions]


class CrossingLog:
    def __init__(self, storage, clock):
        self._storage = storage
        self._clock = clock
        self._crossings: dict[str, dict] = {}

    async def start(self, row: dict, reporter: str | None = None) -> dict:
        """row is a decorated lane (BorderFeed.bridge) at the moment they join the line."""
        if row.get("state") != "open":
            raise CrossingError("that lane is not open right now", "not_open")
        crossing = {
            "id": str(uuid.uuid4()),
            "port_number": row["port_number"],
            "lane": row["lane"],
            "started_at": self._clock().isoformat(),
            "finished_at": None,
            "actual_minutes": None,
            "shown_minutes": row.get("minutes"),
            "claims": _claims(row),
            "reporter": reporter,
        }
        self._crossings[crossing["id"]] = crossing
        await self._storage.save_crossing(dict(crossing))
        return dict(crossing)

    async def finish(self, crossing_id: str) -> dict:
        try:
            crossing_id = str(uuid.UUID(crossing_id))   # never put raw input in a query
        except (ValueError, AttributeError, TypeError):
            raise CrossingError("no such crossing", "unknown") from None
        crossing = self._crossings.get(crossing_id)
        if crossing is None:
            crossing = await self._storage.get_crossing(crossing_id)
            if crossing is None:
                raise CrossingError("no such crossing", "unknown")
        if crossing.get("finished_at"):
            raise CrossingError("that crossing is already finished", "finished")

        now = self._clock()
        actual = round((now - datetime.fromisoformat(crossing["started_at"])).total_seconds() / 60)
        if not MIN_PLAUSIBLE <= actual <= MAX_PLAUSIBLE:
            raise CrossingError(f"{actual} minutes is not a believable crossing; not recorded",
                                "implausible")

        crossing = {**crossing, "finished_at": now.isoformat(), "actual_minutes": actual}
        self._crossings[crossing_id] = crossing
        await self._storage.finish_crossing(crossing_id, crossing["finished_at"], actual)
        return {**crossing, "errors": {c["source"]: c["minutes"] - actual
                                       for c in crossing["claims"] if c.get("minutes") is not None}}

    async def accuracy(self, days: int = 30) -> dict:
        """How far each source was from what people actually waited.

        error = claimed − actual, so a positive bias means the source over-states the
        wait and a negative one means it promises less than people get.
        """
        since = self._clock() - timedelta(days=days)
        finished = {c["id"]: c for c in await self._storage.crossings_since(since.isoformat())}
        finished.update({i: c for i, c in self._crossings.items() if c.get("finished_at")})
        finished = {i: c for i, c in finished.items()
                    if c.get("actual_minutes") is not None
                    and datetime.fromisoformat(c["finished_at"]) >= since}

        errors: dict[str, list[int]] = {}
        for crossing in finished.values():
            actual = crossing["actual_minutes"]
            claims = list(crossing.get("claims") or [])
            if crossing.get("shown_minutes") is not None:
                claims.append({"source": "chisme", "minutes": crossing["shown_minutes"]})
            for claim in claims:
                if claim.get("minutes") is not None:
                    errors.setdefault(claim["source"], []).append(claim["minutes"] - actual)

        sources = {
            name: {
                "crossings": len(errs),
                "mean_abs_error": round(sum(abs(e) for e in errs) / len(errs), 1),
                "bias": round(sum(errs) / len(errs), 1),
                "within_10": round(sum(1 for e in errs if abs(e) <= CLOSE_ENOUGH) / len(errs), 2),
                "enough_data": len(errs) >= ENOUGH_TO_JUDGE,
            }
            for name, errs in errors.items()
        }
        ranked = sorted(sources, key=lambda n: sources[n]["mean_abs_error"])
        return {"days": days, "crossings": len(finished),
                "sources": {name: sources[name] for name in ranked}}
