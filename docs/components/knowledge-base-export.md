# Component: Knowledge-Base Export

Writes upcoming events from Supabase into a Google Sheet that GoHighLevel syncs, grounding an
Instagram comment-to-DM chatbot. Owns `src/scraper/kb/`.

Small in code, high in consequence: **this sheet is the only grounded source of event facts the bot is
permitted to speak from.** Every rule below maps to a specific way the bot would otherwise say
something false to a customer.

Why it exists at all, and what it replaced: [ADR-0010](../architecture/adr/0010-sheet-export-over-crawler.md).

---

## At a glance

| | |
|---|---|
| **Entry point** | `python -m scraper.kb export` (also `scraper-kb`) |
| **Runs on** | GitHub Actions, after every scrape — **non-fatal** and skipped on trends-only runs |
| **Reads** | `events` + joined `venues`, approved and upcoming |
| **Writes** | One Google Sheet tab, rewritten in full |
| **Extra** | `pip install -e ".[kb]"` — `gspread` + `google-auth`, imported lazily |
| **Tests** | `tests/kb/test_rows.py` — 15 tests pinning the row contract |

```bash
python -m scraper.kb export                    # write the sheet
python -m scraper.kb export --dry-run          # print a summary, write nothing
python -m scraper.kb export --out events.csv   # also save a local copy
python -m scraper.kb export --days 90 --location "Ciudad Juárez"
```

---

## What it collects

The window is `[now, now + days)` — **from now, not from local midnight**, because *"an event that
started three hours ago is over, and leaving it in is the exact staleness the crawler was guilty of."*

`query_upcoming_events` selects approved events joined to venues, filtered by a location `ilike`,
ordered ascending, **paged 500 at a time with no ceiling** — it pages until exhausted, because *"a
`limit` that silently truncated would drop real events out of the knowledge base with no error
anywhere."*

If zero exportable rows are built it prints *"No upcoming events found — refusing to publish an empty
sheet"* and exits **1 before any write**.

---

## The sheet contract

Thirteen columns. **The column order is the header row and is append-only** — GHL's importer maps by
position for an existing import, so reordering silently rewrites every field.

| # | Header | Content |
|---|---|---|
| 0 | `content` | Bilingual prose — English, then `ES: ` and the Spanish version |
| 1 | `title` | Cleaned title |
| 2 | `starts_at` | Local ISO timestamp |
| 3 | `date` | `YYYY-MM-DD`, local |
| 4 | `time` | 12-hour clock, **omitted when implausible** |
| 5 | `weekday` | English weekday name |
| 6 | `venue` | Venue name, joined row preferred |
| 7 | `address` | Street address, joined row preferred |
| 8 | `categories` | Comma-joined |
| 9 | `tickets_url` | First ticket link, else the source URL |
| 10 | `more_info_url` | `{SITE_BASE_URL}/events/{id}` |
| 11 | `data_current_as_of` | The export date |
| 12 | `event_id` | The event uuid |

### Why column 0 carries prose

**GHL embeds rows, not columns.** A row of sparse structured cells retrieves badly — there is no
sentence for a question to match. So every row carries one prose cell that stands on its own, and the
structured columns are metadata rather than the payload.

Both languages live in **one** row rather than two: a Spanish question still retrieves the row on its
Spanish half, and it halves the row count and the import.

Shape: `"{title} — {date} at {time}, at {venue, address}."` plus optional category, a truncated
description (**English only**), tickets and more-info links. The Spanish half mirrors it with
`a las`, `en`, `Categoría:`, `Boletos:`, `Más información:`.

### The four content rules

| Rule | Reason |
|---|---|
| **No relative dates, in any language** | "This weekend" is true at export time and false when anyone asks. Rows carry absolute weekday + date + time, plus `data_current_as_of` so the bot can date itself. Pinned by a test asserting the absence of `tonight`, `this weekend`, `tomorrow`, `hoy`, `mañana` |
| **No cell is ever empty** | GHL rejects a row with any blank cell (*"Required field cannot be null or empty"*), so one blank breaks the whole import. Blanks become the literal `"Not listed"`, which reads as an honest answer if the bot quotes it — where `"N/A"` or `"-"` would not |
| **A time before 6am is omitted, not guessed** | Public events essentially never start that early, so it is a parse we cannot vouch for. The same call the carousel makes: an omitted time is unremarkable, a wrong one gets repeated back to a customer |
| **Boilerplate rows are dropped** | Titles like `"event"`, `"events"`, `"calendar"` come from listing pages whose own header got scraped |

Date formatting is hand-rolled in both languages (`"Friday, September 11, 2026"` /
`"viernes, 11 de septiembre de 2026"`) rather than using `%-d` or locale settings, because those are
**not portable to Windows, where this is often run by hand**.

---

## Authentication and writing

**A Google service account.** Create one, enable the Sheets API, and — the step everyone misses —
**share the target sheet with the service account's email as an Editor.** A service account has no
access to a sheet until it is shared like any other collaborator.

Credentials resolve in order:

1. `GOOGLE_SERVICE_ACCOUNT_JSON` — the **entire key file** as one env var. This form exists because
   GitHub Actions secrets are strings, so the key travels as one variable rather than as a path to a
   file that does not exist on the runner.
2. `GOOGLE_APPLICATION_CREDENTIALS` — a filesystem path, for local runs.
3. Neither → `SheetsUnavailable`.

`SheetsUnavailable` means *"gspread isn't installed, or no usable credentials were configured."* It is
raised for five conditions and the CLI catches it specifically, printing `Sheets export unavailable: …`
and returning 1 — a configuration problem is a clean non-zero exit, never a traceback.

### Grow, write, shrink — never clear first

The write strategy is the part to get right:

- **Not clear-then-write.** A clear leaves the sheet empty for the length of a round trip, and a GHL
  sync landing in that window would wipe the knowledge base. Writing first and shrinking after means
  the sheet is never emptier than it has to be; the worst case is a few stale trailing rows.
- **Grow before writing** — an update past the current grid bounds is an API error, so `resize` runs
  first when needed.
- **`value_input_option="RAW"`**, so Sheets never reinterprets a date or a URL.
- **Shrink after** to exactly the needed dimensions.

A missing worksheet tab is **created** rather than erroring.

---

## The CSV path

`--out PATH` is **additive, not a fallback** — it runs *in addition to* the Sheets write, and before
it. Two consequences:

- Combined with `--dry-run` it is a true offline mode: CSV written, Sheets untouched.
- Because the CSV is written first, **it survives a `SheetsUnavailable`** — which makes `--out` the
  recovery route when credentials break. Export to CSV and upload by hand.

---

## Configuration

| Var | Default | Notes |
|---|---|---|
| `KB_SHEET_ID` | none | The `/d/<THIS>/edit` segment of the sheet URL. Unset means the export is simply not wired up |
| `KB_SHEET_TAB` | `events` | |
| `KB_HORIZON_DAYS` | `60` | `--days` default |
| `KB_LOCATION` | `El Paso` | `--location` default → an `ilike` filter |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | none | Whole key file, one line |
| `SITE_BASE_URL` | `https://epchisme.com` | Builds `more_info_url`. **The code default is the bare apex, which 308-redirects** — the repo variable must be set to the `www` form |
| `EVENT_TIMEZONE` | → `IG_TIMEZONE` → `America/Denver` | The export window and all local formatting |

---

## The downstream consumer

`docs/ghl-ig-comment-bot-prompt.md` documents the GoHighLevel automation: a first-level Instagram
comment containing a keyword triggers a like, a rotating public reply, and a DM; email and first name
are captured through **plain workflow actions**; the site link is delivered; and only then is the
thread handed to a Conversation AI action.

The governing design decision there mirrors this component's: **capture deterministically, use AI only
after the link is delivered.** Anything that must be reliable is a plain workflow action, because GHL
support could not confirm the AI action writes back to contact fields.

Most relevant to us, the AI prompt is **forbidden from inventing event facts**:

> "Never invent a specific event, date, venue, price, or lineup. If they ask what is happening Friday,
> do not list anything from memory. Say the site is updated with the current week and point them to the
> site."

That is why the export refuses relative dates, refuses implausible times, never emits a blank cell, and
refuses to publish an empty sheet.

Also worth knowing operationally: Instagram allows **one** private reply to a comment, opening a
24-hour window. After 24 hours with no user reply you generally cannot DM again, so a delayed
follow-up step will likely fail.

---

## Gotchas

1. **A failed export does not fail the workflow.** The step is `continue-on-error: true`, so you will
   only see it in the step log, not in a red run.
2. **Exit 1 with "No upcoming events found" is not a bug in this component** — diagnose the *scrape*.
3. **`KB_LOCATION` defaults to `El Paso`**, so the sheet is El Paso-only in practice even though the
   product covers Juárez. Same shape as the carousel's hardcoded city. See
   [known-gaps](../known-gaps.md).
4. **The house rule:** any change to stored event data is followed by `python -m scraper.kb export`,
   so the bot and the site never disagree.
5. **Never reorder columns.** Append only.

---

*Verified against commit `9157646` (2026-09-06). Last updated 2026-09-10.*
