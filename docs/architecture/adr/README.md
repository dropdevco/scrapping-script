# Architecture Decision Records

Each ADR records one significant decision: the context it was made in, what was decided, **what was
rejected and why**, and what the decision cost. The rejected alternatives are usually the most
valuable part — they are what stops the same proposal coming back around.

These were reconstructed from git commit bodies, code comments, and `PROJECT_HANDOFF.md`. The commit
bodies in this repo are unusually detailed and are cited throughout; when an ADR and a commit body
disagree, the commit body is the primary source.

## Index

| ADR | Decision | Status |
|---|---|---|
| [0001](0001-source-connector-pattern.md) | Sources are independent connectors behind a tiny interface | Accepted |
| [0002](0002-local-timestamp-invariant.md) | Event timestamps are aware and event-local, normalised centrally | Accepted |
| [0003](0003-human-in-the-loop-then-opt-out.md) | Human approval for posting, inverted to opt-out | Accepted |
| [0004](0004-crash-safety-over-retry.md) | Crash safety and idempotency over retry-by-default | Accepted |
| [0005](0005-region-restriction-everywhere.md) | Every event surface is restricted to El Paso / Juárez | Accepted |
| [0006](0006-leaflet-to-google-maps.md) | Google Maps replaces Leaflet | Accepted |
| [0007](0007-horizon-venue-cap.md) | The horizon format keeps loose diversity caps | Accepted |
| [0008](0008-one-parameterised-renderer.md) | Five post formats share one parameterised renderer | Accepted |
| [0009](0009-edit-intents-as-rows.md) | Telegram edits record intent rows; a Python job rebuilds | Accepted |
| [0010](0010-sheet-export-over-crawler.md) | A Google Sheet export replaces crawling our own site | Accepted |
| [0011](0011-make-swallowed-failures-loud.md) | Swallowed failures are made loud, everywhere | Accepted |
| [0012](0012-python-rendering-not-satori.md) | Slides render in Python/Pillow, not `next/og` | Accepted |

## Writing a new one

Copy the shape of an existing file. Number sequentially. Statuses are `Proposed`, `Accepted`,
`Superseded by ADR-NNNN`, or `Deprecated`.

**Never edit an accepted ADR to reflect a changed mind.** Write a new one that supersedes it and mark
the old one. An ADR is a record of what was true at a point in time; rewriting it destroys the
reasoning trail, which is the only thing it exists to preserve.

Good candidates for an ADR: anything where a reader six months from now would reasonably ask "why
didn't they just…?". If the answer is non-obvious, write it down.

---

*Verified against commit `9157646` (2026-09-06). Last updated 2026-09-10.*
