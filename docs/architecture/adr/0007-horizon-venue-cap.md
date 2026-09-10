# ADR-0007: The horizon format keeps loose diversity caps

- **Status:** Accepted
- **Date:** 2026-09-03 (`33e3796`)
- **References:** `src/scraper/social/selection.py:116-142`, `tests/social/test_post_kinds.py:225-280`

A small, narrow decision recorded as an ADR because **the failure mode generalises**: two independent
quality filters composed multiplicatively on a small pool, and the redundant one was the binding
constraint.

## Context

The `horizon` post format ("SAVE THE DATE") draws from a 60-day window starting about six months out.
Two quality filters apply to it:

1. `require_ticket_links = True` — horizon-only. Six months out, an event without tickets on sale is
   almost always a recurring fixture scheduled far ahead rather than something to plan around, and
   nothing else in the schema distinguishes the two.
2. The generic diversity caps: `max_per_venue`, `max_per_category`. Weekend and monthly tighten
   `max_per_venue` to 1 as an editorial gain.

Horizon had been written with the same tightened cap, by analogy.

Measured on 2026-09-03: **all 30 events in the horizon window were ticketed, but spread across just
four venues.** So `max_per_venue=1` capped the post at four slides — exactly `ig_min_slides`, and one
quiet week away from skipping entirely.

## Decision

**Do not tighten diversity caps for `horizon`.** Leave `max_per_venue` at the default 2 and
`max_per_category` at 3. The reasoning is recorded in place (`selection.py:127-135`):

> "Diversity caps are deliberately NOT tightened here, unlike weekend and monthly. Six months out,
> the only events on sale are at the handful of venues big enough to sell that far ahead… so
> `max_per_venue=1` capped the post at four slides — the bare minimum, one bad day away from skipping
> entirely. `require_ticket_links` is already doing the quality filtering; a venue cap on top only
> starves it."

Result: the same window went from **4 slides to 6**.

## Alternatives rejected

| Rejected | Why |
|---|---|
| Loosen the caps globally | *"Weekend and monthly keep their tightened caps: they draw on a much broader pool, where spreading across venues is a real editorial gain rather than a constraint on an already-small set."* |
| Lower `ig_min_slides` for horizon | Treats the symptom. A 3-slide carousel reads worse than none — that is why the floor exists. |
| Drop `require_ticket_links` instead | It is the filter doing the actual quality work for this format. The venue cap was the redundant one. |
| Widen the horizon window | Would dilute the editorial premise ("things to book now") rather than fix the filter interaction. |

## Consequences

**Three regression tests lock this in**, and the third is the interesting one
(`tests/social/test_post_kinds.py:225-280`):

- `test_horizon_does_not_tighten_venue_caps`
- `test_horizon_fills_a_carousel_from_a_few_big_venues`
- `test_horizon_would_have_starved_under_a_one_per_venue_cap` — a test that asserts the *old*
  behaviour would have failed. That is how you stop a "cleanup" commit reintroducing the bug.

**This was the third fix in a chain**, which is the real story and worth internalising:

1. `ca30cac` added the horizon format but deliberately left it **off the cron calendar**, because the
   30-day scrape window meant it had nothing to draw from. *"Enabling it needs `SCHEDULE_DAYS` raised
   first."*
2. Raising `SCHEDULE_DAYS` to 240 then exposed `ffc6cf5`: **Ticketmaster caps `size` at 200, sorts
   date-ascending, and `fetch()` made a single request.** Invisible at 30 days (El Paso has ~61
   events); at 240 days it returns 215 across two pages, **and the dropped page is the
   furthest-out events — exactly what the horizon format is made of.** With paging, the 180–240 day
   band went from 7 to 35 events.
3. Only then did the venue cap become the binding constraint.

A related instance of the same class, `e09320f`: `SCHEDULE_DAYS` was raised to 30 via a repo variable
while the `SearchParams` limit stayed hardcoded at 100, silently truncating >150 El Paso candidates
*after* the chronological sort. Raised to 400.

**The generalisable lesson.** When you widen an input, audit every cap downstream of it. In this
system the caps are `_MAX_PAGES`, `max_details`, `_MAX_TIME_LOOKUPS`, `geocode_max_per_run`,
`SearchParams.limit`, and the selection diversity caps — all politeness or editorial limits, none of
them correctness limits, and each capable of starving a consumer downstream.
