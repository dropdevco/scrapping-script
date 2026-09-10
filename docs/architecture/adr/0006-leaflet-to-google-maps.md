# ADR-0006: Google Maps replaces Leaflet

- **Status:** Accepted
- **Date:** 2026-09-05 (`859b582`)
- **References:** `web/src/components/event-map.tsx`, `web/package.json`

## Context

The events map is a 62dvh card — most of a phone screen. With Leaflet, a one-finger drag over it
panned the map instead of scrolling the page, which left a mobile visitor **unable to scroll past the
map at all**.

The fix at the time was a hand-rolled gesture gate: a two-finger requirement plus a "Use two fingers
to move the map" hint overlay. It worked, but it was custom touch-event code maintained by us,
sitting in the path of every mobile visitor, with no test coverage and several browser-specific
behaviours to keep straight.

Leaflet's draw was that it is free and tile-provider-agnostic (the app used free CARTO tiles).

## Decision

Replace Leaflet and `react-leaflet` with the Google Maps JavaScript API via
`@vis.gl/react-google-maps`, styled with a custom array to match the site's cream/ink/cosmo palette.

**The non-obvious reason, and the actual point of the change:** touch panning now uses **Google's
built-in `gestureHandling: "cooperative"` instead of a custom touch gate.** The decision traded a
free tile provider for **deleting hand-maintained mobile-gesture code.**

`useGestureHandling()` (`event-map.tsx:72-81`) returns `"cooperative"` on `(pointer: coarse)` and
`"greedy"` otherwise, with a live `matchMedia` listener. Cooperative mode keeps one-finger swipes as
page scroll with no custom gate.

## Alternatives rejected

| Rejected | Why |
|---|---|
| Keep Leaflet, keep maintaining the gesture gate | The gate was the cost being paid; removing it was the goal. |
| Keep Leaflet, find a plugin for cooperative gestures | Another dependency in the same place, still not the platform's own behaviour. |
| Mapbox | Not evaluated in the commit record. Google was already in the stack for OAuth, so the account and billing relationship existed. |
| Marker clustering library | Not adopted. Co-located events collapse into one marker whose icon grows (22px → 30px) and carries a numeric label. At ~100 mapped events this is sufficient and is one fewer dependency. |

## Consequences

**A new required, restricted credential.** `NEXT_PUBLIC_GOOGLE_MAPS_API_KEY`, with **Maps JavaScript
API** enabled and HTTP-referrer restrictions listing both the production domain and `localhost`. When
it is empty the component renders the literal text `Missing NEXT_PUBLIC_GOOGLE_MAPS_API_KEY` in place
of the map; the rest of the site is unaffected.

**Markers are grouped by rounded coordinate, not venue id** (`event-map.tsx:89-110`):

> "Grouped by COORDINATE, not venue id: the same physical place often has several venue rows
> (different address spellings hash to different venues), which would otherwise stack identical pins
> on one spot."

This is the read-side defence for the same duplicate-venue problem that `selection.venue_key`
addresses on the carousel side. The underlying fix — an address normaliser — is still outstanding.

**`ssr: false` is mandatory.** Google Maps touches `window`, so `EventMap` is loaded through
`next/dynamic` inside a client `MapShell` wrapper with a pulsing-dot fallback.

**Size/anchor objects are passed as plain objects, not `new google.maps.Size(...)`**, because markers
can render before the Maps script has finished loading and the API only ever reads these as data
(`event-map.tsx:50-52`).

**One brittle spot.** The InfoWindow's chrome is restyled with `!important` overrides on Google's
internal class names (`.gm-style-iw-c`, `-iw-d`, `-iw-t::after`, `-iw-tc::after`) in
`globals.css:136-146`. That will break if Google changes its internal DOM.

**Legacy `Marker`, not `AdvancedMarker`.** No map id / vector map is configured, so the legacy marker
API is in play. Migrating would require a cloud-configured map style.

**Documentation debt this created.** `web/SETUP.md:83,87,136-140`, `PROJECT_HANDOFF.md:123,129,167,207`
and `HANDOFF.md:45` all still describe Leaflet. Leaflet is completely absent from `web/package.json`.
Those files are marked [superseded](../../README.md#superseded-documents).
