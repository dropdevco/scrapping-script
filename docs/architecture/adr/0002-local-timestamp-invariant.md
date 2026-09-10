# ADR-0002: Event timestamps are aware and event-local, normalised centrally

- **Status:** Accepted
- **Date:** 2026-08-24 (`47bc62c`), refined 2026-09-01 (`da3a513`)
- **References:** `src/scraper/core/eventtime.py`, `src/scraper/core/orchestrator.py:50-60`
- **Related:** [invariants §1](../invariants.md#1-time)

The most consequential correctness decision in the repo. If you read one ADR, read this one.

## Context

Two incompatible conventions arrive from the sources, and the system silently mixed them.

**Naive wall clock — most of the open web.** Eventbrite, Meetup, municipal calendars and venue sites
publish `"startDate": "2026-08-25T18:30"` with no offset. Parsed with `fromisoformat` this yields a
**naive** datetime, and a naive datetime written to a `timestamptz` column is read by Postgres as
UTC. An 8:00 PM show was therefore stored as 20:00Z and rendered back as 2:00 PM — six hours early,
every time. Measured consequence: **343 stored events claimed to start between 1am and 5am**, and the
carousel had an implausible cluster of morning slides.

**Genuine UTC instants — keyed APIs.** Ticketmaster hands over a real absolute instant. So
`start_time.date()` on one of its evening shows returns *tomorrow's* date.

Mixing the two was worse than either alone: **the same concert scraped from both kinds of source
looked like two different events on two different days**, which defeated dedupe and put both copies on
the same carousel. One real carousel carried Thee Sacred Souls twice, at two different times.

By the time this was diagnosed, the bug had leaked into five further places: Ticketmaster's date-only
fallback, the cross-run merge, `backfill_merge_duplicates`'s grouping, the web app's timezone-less
formatting (which made SSR print UTC and then silently change after hydration), and the submission
form reading `datetime-local` in the browser's zone.

## Decision

**One invariant, stated once:** `Event.start_time` and `end_time` are timezone-**aware** and
expressed in the event's **local** zone.

- *Aware* means the absolute instant is right — Postgres stores it correctly and the offset travels
  with the value.
- *Local* means `.date()` and `.hour` are the local calendar day and local wall-clock hour — what
  dedupe keys on, what the day-bounds query filters by, and what the flyer prints.

**Applied centrally** in `orchestrator.run` via `_localize_times`, with the reason stated in the code:
*"Done here rather than in each connector so a new source cannot reintroduce the naive-local-stored-as-UTC
bug, and so dedupe below keys on local calendar days for sources that report UTC (Ticketmaster) and
sources that report wall-clock (everything else) alike."*

`to_event_local` (`eventtime.py:49-61`) reads **naive input as local wall-clock** —
the right reading for a scraped listing, which quotes the time a person standing at the venue would
see — and converts aware input.

Three enforcement points: `orchestrator._localize_times`, `storage._iso` as the last line of defence
(*"a naive value reaching Postgres is read as UTC, which is exactly the six-hour shift this fix
removes"*), and `eventtime.local_day` wherever a persisted time must be compared on same-calendar-day
terms.

One timezone covers the whole product: El Paso and Ciudad Juárez are both Mountain and observe the
same transitions, since Mexico dropped nationwide DST in 2022 with Juárez explicitly exempted to stay
aligned with El Paso (`eventtime.py:39-46`).

### The 2026-09-01 refinement

A scraped **offset** is only trusted when it matches what the region was actually observing at that
instant. An Eventbrite show on Dyer St in El Paso is published as `-05:00` with
`timezone: America/Chicago`, because the offset follows the *organiser's account setting* rather than
the building — honouring it moves a customer-facing 8:00 PM to 7:00 PM, an hour out of step with the
ticket page we link to.

**`+00:00` is exempt.** A UTC stamp is a deliberate normalisation, not a local-time claim: Meetup
publishes `"2026-08-30T13:00:00.000Z"` for a 7am El Paso hike, and re-reading that as a wall clock
would move the hike to 1pm. *Only an offset that asserts a local zone can be asserting the wrong
one.*

This lives in `events_web._dt`, **not** in `to_event_local`, because it must not touch API sources —
discarding a non-Mountain offset from Ticketmaster would corrupt every one of its timestamps.

## Alternatives rejected

| Rejected | Why |
|---|---|
| Fix it per connector | It had already leaked into six places. A per-connector fix leaves the next source free to reintroduce it, with no test that would catch it. |
| Store everything as UTC and convert on read | Every consumer — dedupe, day bounds, the renderer, the sheet, the web app — would need to get the conversion right independently. Storing local-aware means `.date()` is simply correct everywhere. |
| A blind offset shift to repair existing rows | Would have corrupted the rows that were already correct. |
| Trust every offset a source publishes | This is exactly what produced the Chicago-offset bug. |
| Discard every non-UTC offset | Would corrupt Ticketmaster, whose offsets are genuine. |

## Consequences

**The repair.** `backfill_event_timezones.py` re-derives each row's true time by running the *fixed*
parser over the row's own preserved `raw` payload — it does not guess an offset, so rows that were
always correct come out untouched. It skips rows whose re-parse disagrees by more than a day.
**Applied: 1,014 corrected, 219 already correct, 0 skipped.**

**`raw` became load-bearing.** Every `backfill_*` script re-derives from `events.raw`. Dropping or
trimming that column would remove the only repair mechanism the system has.

**Config coupling, deliberately.** `EVENT_TIMEZONE` falls back to `IG_TIMEZONE` before
`America/Denver`, *"so the carousel's 'today' and the events it reads about can never drift apart"*
(`config.py:59-66`).

**A known wiring gap.** `EVENT_TIMEZONE` is passed to the knowledge-base step of the scrape workflow
but **not to the scrape step itself** (`scheduled_scrape.yml:33-46` vs `:56-65`), so the scrape runs
on the hardcoded default and setting the repo variable would have no effect on it. Tracked in
[known-gaps.md](../../known-gaps.md).

**Watch for regressions.** `HANDOFF.md` named this a "timezone bug class already fixed once". The
`%-d`/`%-I` portability rule, the half-open local-midnight day bounds, and the 6am plausibility
guard are all downstream of this decision.
