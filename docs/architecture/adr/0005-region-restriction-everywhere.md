# ADR-0005: Every event surface is restricted to El Paso / Juárez

- **Status:** Accepted
- **Date:** 2026-09-05 (`761a89e`, then `04316e2`), hardened 2026-09-06 (`9157646`)
- **References:** `web/src/lib/events.ts:12-34`, `web/src/components/event-map.tsx:23-28`, `src/scraper/sources/events_directories.py:188-249`

## Context

An optional city-tab filter existed but was never mandatory, so scraped junk kept surfacing
sitewide: remote-work webinars with no venue at all, and venues geocoded clear across Mexico —
Mexico City, Guadalajara. On the map specifically, a visitor could pan or zoom out and find them.

Chisme's entire proposition is *this* border region. An event outside it is not a lower-priority
result; it is wrong.

## Decision

In two steps, on the same day — and the sequence is the lesson.

**Step 1 (`761a89e`): the map.** `restriction={{ latLngBounds: REGION_BOUNDS, strictBounds: true }}`
caps both panning and zooming out to a box covering El Paso, Juárez, Las Cruces and Alamogordo
(`event-map.tsx:21-28`).

**Step 2 (`04316e2`): every query.** The region check — `location` or `venue` matching one of the
approved patterns — is applied **unconditionally** in `fetchEvents`, `fetchMappableEvents`,
`fetchEvent` **and** `fetchCrawlerEvents`. From the code: *"Always on, not just when a city tab is
selected — the site never shows anything outside El Paso / Juárez, regardless of what a source
scraped"* (`web/src/lib/events.ts:78-81`).

Source-side, `events_directories._event_matches_directory_region` filters individual events for the
Juárez directories, because a statewide calendar mixes Chihuahua-capital and border events freely.

## Alternatives rejected

| Rejected | Why |
|---|---|
| Fixing only the map | Step 1 did exactly this and was superseded the same day. **A presentation-layer clamp is not a data-scope guarantee** — the list, the detail pages and the crawler index were still serving out-of-region events. |
| Relying on the optional city filter | It existed and was never mandatory. Optional scoping is not scoping. |
| Filtering only at ingest | A source can mislabel an event, and historical rows are already stored. Read-side filtering is what protects the user today. |
| Filtering only at read | Leaves junk accumulating in the database, reaching the carousel and the KB sheet, which do not go through the web layer. |

Both layers, independently, is the accepted answer.

## The Juárez false positive — fixed three times

This single pattern needed narrowing in three separate places, and it is the most instructive part of
the decision.

**A bare "Juárez" is not Ciudad Juárez.** Benito Juárez was a Mexican president, so it is a Mexico
City borough and a street, avenue and plaza name across the entire country.

| # | Where | What went wrong |
|---|---|---|
| 1 | `geocode.py` | The venue-name-only fallback **unconditionally injected a city** into the Nominatim query. Because Nominatim is called with `bounded=1`, forcing a wrong city still returns *some* in-bbox match — **a confident, wrong, in-region pin.** `_fallback_anchor_city()` now only injects a city when there is real signal. |
| 2 | `web/src/lib/events.ts` | A bare `Juárez` ILIKE matched the Mexico City borough. `CITY_PATTERNS.juarez` now lists only qualified forms: `Ciudad Juárez`, `Juárez, CHH`, `Juárez, Chih` and their unaccented variants. |
| 3 | `events_directories.py` (2026-09-06) | The **generic branch** still matched bare `juarez` anywhere in an event's text, so a museum on "Av. Benito Juárez" in the Chihuahua *state capital* passed as a Cd. Juárez event. Tightened to the qualified forms the ticketing-portal branch already used. |

Two supporting rules came out of this:

- **Place fields only — never the description.** National touring-show listings mention Ciudad Juárez
  in an "also playing in…" blurb even when the event on the page is elsewhere, so a hard negative on
  the place fields always wins regardless of the free text (`events_directories.py:170-195`).
- **The failure mode does not throw.** From the handoff: *"the failure mode here doesn't throw, it
  just quietly geocodes into the wrong city."* After touching region matching, re-run a live pass and
  **eyeball the `location` output.**

## Consequences

**A surprising but intended behaviour:** `fetchEvent(id)` applies the region filter too, so an
approved event whose `location`/`venue` do not match a pattern **404s on its own detail page** even
with a valid id — and `generateMetadata` returns `robots: { index: false }` for it. Expect to hit
this while debugging a "missing" event.

**PostgREST quoting matters.** The filter double-quotes each pattern because PostgREST's `or=(…)`
logic tree splits on bare commas, and `"Juárez, CHH"` contains one (`events.ts:31-33`).

**The map's bounds are wider than the query's patterns**, on purpose: Las Cruces and Alamogordo
legitimately appear in the data, and `strictBounds` exists to stop a visitor scrolling to a
bad geocode clear across Mexico.

**Known inconsistency.** The social pipeline hardcodes `CITY = "El Paso"`
(`social/__main__.py:37`) and filters with `location ILIKE '%El Paso%'`, so a Juárez event whose
`location` lacks "El Paso" **can never reach a carousel** — even though the rest of the product now
covers Juárez. The KB export has the same shape via `KB_LOCATION`. Nothing in the code justifies the
restriction; it looks like an oversight from before Juárez coverage landed. Tracked in
[known-gaps.md](../../known-gaps.md).
