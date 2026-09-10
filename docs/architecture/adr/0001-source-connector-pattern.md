# ADR-0001: Sources are independent connectors behind a tiny interface

- **Status:** Accepted
- **Date:** 2026-07-16 (initial), reinforced continuously
- **References:** `src/scraper/sources/base.py`, `src/scraper/sources/registry.py`, `README.md:82-84`

## Context

Chisme's value is coverage: ~30 public sources across two countries, in two languages, with wildly
different shapes — a keyed REST API, public pages carrying `schema.org/Event` JSON-LD, Wix event
widgets, Spanish-language municipal calendars with no machine-readable markup at all, and RSS feeds.

Those sources also fail constantly and independently. A site redesigns its templates. A ticketing
portal starts bot-blocking. A provider's free tier rate-limits. A DNS record disappears. Any design
where one source's failure can affect another, or where adding a source means touching shared
orchestration code, would make the system unmaintainable at this level of churn.

## Decision

Every source is a class implementing a four-member interface (`sources/base.py:16-28`):

```
name: str                  # unique; the allow/deny-list key and the stored Event.source
kind: Kind                 # EVENTS | TRENDS | WEB — selects when it runs
def is_configured() -> bool                     # False if keys/deps missing → skipped
async def fetch(params, http) -> list[Event|Trend|Document]
```

It exports a module-level `SOURCE` (or `SOURCES` for several), and its module name is added to
`_MODULES` in `sources/registry.py`. **Nothing in the orchestrator changes.**

Three supporting rules make it work:

1. **Heavy or optional third-party imports go inside `fetch()`**, so the registry can be built
   without them installed (`base.py:1-6`).
2. **`fetch()` raises on hard failure and the orchestrator isolates it** — `_fetch_one` catches,
   logs, and returns `([], SourceResult(ok=False, …))` with the comment *"one dead source must not
   fail the run"* (`orchestrator.py:63-70`).
3. **Sources receive the shared `HttpClient`** rather than creating one, so the global concurrency
   semaphore, retry policy, robots.txt cache and connection pool are shared across the whole fan-out.

A source is typically about 40 lines.

## Alternatives rejected

| Rejected | Why |
|---|---|
| Per-source logic in the orchestrator | Every new source would be a change to the one file every other source depends on. |
| Eager imports at registry level | `pytrends` pulls in pandas; Pillow is a separate extra. Eager imports would make the base install carry every optional dependency, and a single missing package would break the registry for all sources. |
| One "scrape everything" function | No way to run events without trends, and no way to disable a misbehaving source. |
| Failing the run on any source error | With ~30 third-party dependencies this would mean near-permanent failure. A partial result is the correct outcome. |

## Consequences

**Good.** "Every API key is optional" is literally true — `.env.example:2-3` can promise it because
`is_configured()` enforces it. A developer with only a Supabase URL still gets working keyless
sources. The 2026-09-06 work added two bespoke Juárez parsers without touching any shared code.
`source_status()` gives a one-call answer to "what is actually running right now".

**Costs and obligations.**

- `_MODULES` is **hand-maintained**. A new file that is never registered silently does nothing.
- `registry._cache` memoises the module list per process, so a newly added module needs a restart —
  though allow/deny lists and `is_configured()` are re-evaluated on every `sources_for()` call.
- The interface's looseness is real: `SearchParams.categories` is accepted and logged but **no source
  reads it**. Nothing structurally prevents that kind of drift.
- A source that is configured but silently returning zero results looks identical to one that is
  correctly finding nothing. Only the `runs` table's per-source counts distinguish them.

## Adding a source

1. Create `src/scraper/sources/<name>.py`; subclass `Source`; set `name` (unique) and `kind`.
2. Implement `is_configured()` — `True` for keyless, else `bool(settings.<key>)`.
3. Implement `async def fetch(self, params, http)`. Use the passed `http`. Import optional deps inside.
4. For events: set `categories` via `categorize.guess_categories`, format addresses with
   `address.format_address`, sanitise images with `media.clean_image_url`, and emit **naive
   wall-clock** datetimes unless the provider gives a real instant ([ADR-0002](0002-local-timestamp-invariant.md)).
5. Export `SOURCE = MySource()`.
6. Add the bare module name to `registry._MODULES`.
7. Verify it self-disables cleanly with its keys removed — this is part of the
   [verification contract](../../engineering/conventions.md#the-verification-contract).
8. Confirm the target is static HTML and allowed by `robots.txt` before wiring it in. This is house
   practice, stated in the commit that added the most recent two sources.
