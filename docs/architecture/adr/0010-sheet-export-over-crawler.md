# ADR-0010: A Google Sheet export replaces crawling our own site

- **Status:** Accepted
- **Date:** 2026-08-27 (`1ae43cc`)
- **References:** `src/scraper/kb/`, `docs/ghl-ig-comment-bot-prompt.md`

## Context

A GoHighLevel automation answers Instagram comments and DMs about what is happening in El Paso. Its
knowledge base was fed by a **crawler pointed at our own public events page.**

Two problems with that:

1. It re-derived, badly, data we already hold in structured form — parsing our own rendered HTML back
   into facts.
2. **It never forgot.** Past events stayed indexed, so the bot answered customers with shows that had
   already happened.

The second is the serious one: a bot confidently recommending a concert from three weeks ago is worse
than a bot that says "check the site".

## Decision

Write events **straight out of Supabase into a Google Sheet** that GoHighLevel syncs from, and
**rewrite the sheet in full on every run** — so anything that has already started simply stops
existing. Wired into the scrape workflow, so the sheet is republished after every scrape.

Five rules fall out of how GHL actually consumes the sheet, and each one maps to a specific way the bot
would otherwise say something false:

| Rule | Reason |
|---|---|
| **One row is one retrievable chunk.** Every row carries a prose `content` cell (English, then Spanish) that stands on its own; the structured columns are metadata, not the payload. | GHL embeds **rows, not columns**, so sparse cells retrieve badly — there is no sentence for a question to match. |
| **No relative dates, in any language.** Absolute weekday + date + time, plus a `data_current_as_of` column. | *"This weekend" is true at export time and false by the time anyone asks.* |
| **No cell is ever empty** — blanks become the literal string `"Not listed"`. | GHL's importer rejects a row with **any** empty cell, so one blank breaks the whole knowledge base rather than degrading one field. `"Not listed"` also reads as an honest answer if the bot quotes it verbatim, which `"N/A"` or `"-"` would not. |
| **A time before 6am is omitted, not printed.** | Public events essentially never start that early, so it is a parse we cannot vouch for. A missing time is unremarkable; a wrong one gets repeated back to a customer. Same call the carousel makes. |
| **An empty result refuses to publish** and exits 1. | *"An export that finds nothing is a scrape problem, not a signal to wipe the knowledge base."* Otherwise the bot tells customers nothing is happening in El Paso. |

And one rule about the write itself:

**Grow, write, then shrink — never clear first.** A clear leaves the sheet empty for the length of a
round trip, and a GHL sync landing in that window would wipe the knowledge base. Under the current
order the worst case is a few stale trailing rows for one round trip (`kb/sheets.py:81-88`).

Verified end-to-end at the time: *"the table dropped 40 → 38 on sync."*

## Alternatives rejected

| Rejected | Why |
|---|---|
| Keep crawling the public site | Re-derives structured data from HTML, and never forgets past events. |
| An API endpoint for GHL to poll | GHL syncs from Sheets natively; an endpoint would need auth, hosting and a GHL-side integration that does not exist out of the box. |
| Incremental sheet updates | Full rewrite is what makes forgetting automatic. Incremental updates would need deletion logic and could leave orphans — exactly the crawler's failure. |
| Clear-then-write | A GHL sync in the empty window wipes the knowledge base. |
| One row per language | Doubles the row count and the import for no retrieval gain — an ES question still matches the row on its ES half. |
| Letting the AI answer event questions from memory | Explicitly forbidden in the bot prompt: *"Never invent a specific event, date, venue, price, or lineup."* The sheet is the only grounded source it may speak from. |

## Consequences

**The export is a contract, not a dump.** Column order **is** the header row and is **append-only**,
because GHL's importer maps by position for an existing import — reordering silently rewrites every
field. Tests index headers by name precisely so this stays safe.

**A standing operational rule:** any change to stored event data is followed by
`python -m scraper.kb export`, so the bot and the site never disagree about what is on this week. It
runs automatically after every scrape, with `continue-on-error: true` — *"a Sheets outage must not fail
the scrape that already succeeded"* — and is skipped on trends-only runs.

**Because the step is non-fatal, a failed export is invisible in the workflow's status.** You will
only see it in the step log. See [runbooks](../../operations/runbooks.md#the-google-sheets-export-failed).

**Auth is a Google service account**, and the step everyone misses is that **the sheet must be shared
with the service account's email as an Editor** — it has no access until you do. The key travels as
`GOOGLE_SERVICE_ACCOUNT_JSON`, the entire key file as one env var, because GitHub Actions secrets are
strings and there is no file on the runner to point at.

**`--out` is a real recovery route.** It writes the same rows to CSV *before* attempting the Sheets
write, so the CSV survives a credentials failure and can be uploaded by hand.

**The downstream bot's design constrains ours.** From `docs/ghl-ig-comment-bot-prompt.md`: anything
that must be reliable — email capture, first name, the link — is built from plain workflow actions,
and AI handles only the open-ended tail after the link is delivered. Instagram also allows **one**
private reply to a comment, opening a 24-hour window, after which a delayed follow-up will likely
fail.
