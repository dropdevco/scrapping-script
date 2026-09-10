# Component: Web App

The public site at `www.epchisme.com`, plus the moderation and Instagram-review admin surfaces, plus
the Telegram webhook. Owns `web/`.

> **Before writing any code here, read `web/AGENTS.md`.** It is five lines and says: *"This is NOT the
> Next.js you know… Read the relevant guide in `node_modules/next/dist/docs/` before writing any
> code."* Concretely in this codebase: `middleware.ts` is now `proxy.ts`, and `params`, `searchParams`
> and `cookies()` are all **Promises that must be awaited**.

---

## At a glance

| | |
|---|---|
| **Stack** | Next.js 16.2.10, React 19.2.4, Tailwind CSS 4 (no config file — `@theme` in `globals.css`), `@supabase/ssr` |
| **Deploy** | Vercel, auto-deploys on push to `main`. Root directory is `web` |
| **Rendering** | Every page is a Server Component and `force-dynamic` — they all read a language cookie |
| **Tests** | **None.** There is no test script at either level |
| **Scripts** | `dev`, `build`, `start`, `lint` |

---

## Routes

| Route | Kind | Renders | Data |
|---|---|---|---|
| `/` | Server | Hero (scratch-off + ransom-note type), filter rail, event grid | `fetchCategories`, `fetchEvents` |
| `/events/[id]` | Server | Detail page with microdata + injected schema.org JSON-LD, ticket buttons, Maps link | `fetchEvent` (twice — metadata and page) |
| `/map` | Server | Filter rail + dynamically-imported Google Map | `fetchCategories`, `fetchMappableEvents` |
| `/submit` | Server | Google-gated submission form (client component) | none |
| `/crawler/events` | Server | Untruncated machine-readable index, paginated by content size | `fetchCrawlerEvents` |
| `/admin` | Server | Event moderation queue | Session check + **service-role** read of pending rows |
| `/admin/ig` | Server | Carousel queue: 10 most recent posts, metrics, slide strip | `ig_posts`, `ig_post_metrics`, signed slide URLs |
| `/admin/ig/review/[token]` | Server | Single-post review from a magic link — **no session required** | HMAC token verify, then one post |
| `/sitemap.xml`, `/robots.txt` | Metadata routes | — | `fetchCrawlerEvents` for the sitemap |
| `/auth/callback` | Route handler | OAuth code exchange, then redirect | — |
| `/api/telegram/webhook` | Route handler | Telegram button and reply handling | Service role |

`web/src/proxy.ts` (Next 16's renamed middleware) refreshes the Supabase session on every matching
request. `next.config.ts` permanently redirects `/events` → `/#events` — an alias, not a second
listing page, because *"two URLs serving the same grid would split ranking signals and double the
maintenance."*

**There are no OG-image generation routes.** OG/Twitter metadata is text plus the remote `image_url`,
produced in `generateMetadata`.

---

## Data layer (`web/src/lib/`)

### Three Supabase clients — know which one you are using

| Client | Key | Used by |
|---|---|---|
| `supabaseBrowser()` | anon | `"use client"` components — auth button, submit form |
| `supabaseServer()` | anon | All server reads and `auth.getUser()`; cookie-bound |
| `supabaseAdmin()` | **service role** | `/admin`, `/admin/ig`, the review page, the Telegram webhook. **Bypasses RLS** |

> *"Server-only: `SUPABASE_SERVICE_ROLE_KEY` has no `NEXT_PUBLIC_` prefix, so Next.js never ships it to
> the browser bundle. Never import this from a `"use client"` component."*

`supabaseServer`'s `setAll` is wrapped in try/catch because Server Components cannot write cookies —
`proxy.ts` handles session refresh.

### Query functions (`events.ts`)

| Function | Constraints |
|---|---|
| `fetchEvents(filters, limit=500)` | `status='approved'`, ordered by start time |
| `fetchEvent(id)` | by id **plus the region filter** — see below |
| `fetchMappableEvents` | `venues!inner`, non-null `lat` |
| `fetchCrawlerEvents` | approved, upcoming, region-filtered |
| `fetchCategories()` | Returns the static canonical list; no DB round trip |

**The region filter is applied unconditionally to all of them** —
[ADR-0005](../architecture/adr/0005-region-restriction-everywhere.md). Two implementation details that
have each caused a bug:

- Patterns are **double-quoted** inside the PostgREST `or=(…)` filter, because its logic tree splits on
  bare commas and `"Juárez, CHH"` contains one.
- `CITY_PATTERNS.juarez` lists only **qualified** spellings. Never shorten it to a bare `Juárez`.

`applyEventFilters` is shared by the list and the map so both read URL params identically: time range
→ text search → region → categories (via `.overlaps`, so OR semantics).

### Categories

`CATEGORY_GROUPS` defines 14 canonical labels, each with aliases. Two directions matter:

- `canonicalCategory(raw)` maps a stored source string **to** a label — alias lookup first, then a
  dozen regex fallbacks, finally `"Other"`.
- `expandedCategoryAliases(selected)` goes the **other way**: a label the user clicked expands to the
  label plus every alias in its group, so `.overlaps()` matches the raw source-specific strings
  actually stored in `events.categories`.

Note the scraper has its own smaller taxonomy in `core/categorize.py`; the alias expansion is what
reconciles the two.

### Other lib modules

- **`datetime.ts`** — `EVENT_TZ` is hard-pinned to `America/Denver` and every formatter passes it. See
  [invariants §7](../architecture/invariants.md#all-date-formatting-is-pinned-to-el-paso--never-the-viewers-zone-never-the-servers).
  `eventLocalToIso` does a **two-pass offset sample** so DST-transition days convert correctly.
- **`site.ts`** — `siteOrigin()` with a loud production warning when `NEXT_PUBLIC_SITE_URL` is unset.
- **`event-schema.ts`** — JSON-LD builder; emits `Place` vs `VirtualLocation`, coordinates when
  available, and an `offers` array from ticket links. Escapes `<` before `dangerouslySetInnerHTML`.
- **`hash.ts`** — `venueAddressHash`, which **must stay byte-identical** to the Python and SQL versions.
- **`ig/`** — `moderate.ts` (shared CAS row operations), `reviewToken.ts`, `telegram.ts`,
  `githubDispatch.ts`.

---

## Auth

Supabase Auth with the Google provider. No NextAuth, no custom session store.

Google → **Supabase's own** `/auth/v1/callback` → the app's `/auth/callback` → code exchange → cookies.
Those two allowlists live in *different dashboards*, which is the usual cause of a redirect-URI
mismatch. In Google Cloud Console's newer "Google Auth Platform" UI, also check **Audience → Publishing
status is "In production"** — "Testing" silently blocks every non-allowlisted account.

**Admin is an email allowlist, not a role table.** `ADMIN_EMAILS`, comma-separated. Unset means nobody
is an admin and every admin action throws.

**Every Server Action re-checks authorisation itself:**

> *"Server Actions are directly callable (not just reachable through the page's own UI), so each one
> re-checks admin auth itself — never rely solely on the page component's gate."*

There are three parallel auth paths: session + allowlist (`/admin`), HMAC token (the review page), and
header-secret + chat-id allowlist (the Telegram webhook). `/admin` is not linked from the nav.

---

## Admin surfaces

### Event moderation — `/admin`

Lists `status='pending'` events, read with the **service-role** client because the anon client's
`events_select_own` policy can only see the signed-in user's own rows. Two actions: approve (sets
`approved`, revalidates `/` and `/admin`) and reject (sets `rejected`, keeps the row).

> *"Kept, not deleted — preserves an audit trail and keeps the submitter's own view of it consistent…
> `events_select_approved` already excludes anything that isn't `status='approved'`, so this never
> becomes publicly visible."*

### Instagram queue — `/admin/ig`

Shows drafts through published, ten most recent. `'published'` is included deliberately: *"the queue
doubles as the place you see how the last few actually did — otherwise the metrics have nowhere to
land."* Action buttons render only while the post is a `draft`.

All four row operations live in one shared module so the three callers (session actions, token actions,
Telegram webhook) cannot drift, and **each is a compare-and-swap**. Accepted status sets differ per
operation — see [invariants §4](../architecture/invariants.md#every-ig_posts-transition-is-a-compare-and-swap-and-losing-must-be-clean).

Why "publish now" is not the same as "approve":

> *"Same CAS as `approveIgPostRow`, but also pulls `scheduled_for` to right now — the publish sweep only
> ships rows whose `scheduled_for` has arrived, so without this an 'approve' and a 'publish now' would
> behave identically."*

And the division of labour with Python:

> *"Approval only flips a status. All publishing lives in Python so that turning on `IG_AUTOPOST` later
> reuses the exact same code path instead of needing a second implementation here."*

`triggerImmediatePublish()` POSTs a `workflow_dispatch` to the `ig_daily.yml` workflow. It is
best-effort by design — a failure alerts Telegram and logs, never throws. **The owner, repo and
workflow filename are hardcoded constants**, so a repo rename or fork silently breaks every "now"
action.

### Telegram webhook

Two independent checks before anything moves: the secret header, and the chat-id allowlist. Callback
data is `action:postId:arg`. Full behaviour in
[social-pipeline.md](social-pipeline.md#human-in-the-loop-approval).

`tomorrowAtPublishHour()` computes *today's date in the configured zone*, not the runner's UTC date,
*"which is already tomorrow for most of the El Paso evening"*.

---

## Submission flow

Unauthenticated visitors see a "Continue with Google" card. After sign-in the form writes **directly to
Supabase from the browser**, under RLS:

1. **Venue** (skipped when the event is online): compute `venueAddressHash`, look for an existing row,
   reuse or insert.
2. **Event**: insert with `source: "user_submission"`, `status: "pending"`, `submitted_by: user.id`,
   and a start time converted from `datetime-local` to an El Paso wall-clock instant.

`status='pending'` is enforced by **RLS, not just the client** — `events_insert_pending` has
`with check (status = 'pending' and submitted_by = auth.uid())`, so a user cannot self-approve or
impersonate.

Validation is **client-side only** — no zod, no server validation. Title and start are always required;
venue and address are required unless the city is "online".

> Note `venues_insert_authenticated` has `with check (true)`, so any signed-in user can insert an
> arbitrary venue row. Dedupe relies on `address_hash` uniqueness, not validation. Worth knowing as an
> abuse surface.

---

## Map

Google Maps via `@vis.gl/react-google-maps` — [ADR-0006](../architecture/adr/0006-leaflet-to-google-maps.md).
Loaded through `next/dynamic` with `ssr: false`, because Google Maps touches `window`.

- **Pins are grouped by rounded coordinate, not venue id**, because the same physical place often has
  several venue rows from different address spellings.
- **No clustering library.** Co-located events collapse into one marker that grows and carries a count.
- **`strictBounds`** caps panning and zoom-out to the border metro box — a handful of venues have bad
  geocodes clear across Mexico, and without a hard limit the map would happily scroll a visitor there.
- **Gesture handling** is `cooperative` on coarse pointers, which is the reason for the migration.
- Requires only `NEXT_PUBLIC_GOOGLE_MAPS_API_KEY`; without it the map area renders the missing-key text
  and the rest of the site is fine.

---

## Internationalization

Cookie-driven, server-resolved, two hard-coded dictionaries. No `next-intl`, no locale routes.

`LangToggle` writes the cookie and calls `router.refresh()` **from a `useEffect` keyed on state, not
from the click handler** — a deliberate fix; preserve it. Every server surface re-reads the cookie
independently, and anything other than the exact string `es` falls back to `en`.

Copy lives in `web/src/lib/i18n.ts`. The `Dict` type means **TypeScript errors if `es` is missing a key
that `en` has.** Admin and crawler surfaces are a deliberate English-only exception.

---

## Design system

**Light-first**, not dark. Warm cream "magazine paper" base, near-black ink, hot-pink signature.
`color-scheme: light`, no `dark:` variants anywhere, no theme toggle. Any doc describing a "dark-first
desert theme" is stale.

Tokens live in a Tailwind 4 `@theme` block in `globals.css` — **there is no `tailwind.config.ts`.**

Seven Google fonts: Fraunces (editorial headlines), Archivo (body), Oswald (condensed kickers), plus
four display faces for the ransom-note `CutoutText`. The four are loaded `subsets: ["latin"]` only —
**never pass accented text to `CutoutText`.**

Three components carry real complexity and each has extensive comments:

- **`LandmarkBackdrop`** — a fixed, parallaxed collage of nine local photos with a *provable*
  non-overlap invariant based on vertical lanes and shared parallax factors. Verified numerically at
  ~986k pair checks across 12 viewports. **Re-verify the maths if you change widths, rotation, stride,
  or add taller aspect ratios.** It also has two independent hero gates, both load-bearing.
- **`HeroScratch`** — a canvas scratch-off over the cover photo, with a firework completion animation.
  Deliberately **pointer-only**: on touch devices the layer is skipped entirely, because a finger drag
  over a 74dvh hero is a scroll gesture and forcing it would trap the user at the top of the page.
- **`CutoutText`** — per-character ransom-note lettering, **fully deterministic** via a seeded hash so
  server and client render identically with no hydration mismatch.

`EventImage` falls back to a branded placeholder when a source image is empty **or dead**, using two
detection paths because server-rendered `<img>` tags start loading before React hydrates. Note
`next/image` is used nowhere in the app.

Accessibility details that each fix a real problem: the search input is 16px on touch (iOS Safari
auto-zooms below that and never zooms back out), and mobile nav links carry extra padding because the
12px text alone gave an ~18px hit area.

---

## Gotchas

1. **`web/SETUP.md` is substantially stale** — wrong map library, wrong theme, wrong fonts, references a
   `tailwind.config.ts` that does not exist. Its environment-setup steps are still useful; nothing else
   is.
2. **The untyped Supabase client infers a to-one join as an array.** `/admin` defensively unwraps with
   `Array.isArray(e.venues) ? e.venues[0] : e.venues`; every public surface dots straight in on the
   strength of a type cast. Generating `Database` types would require auditing all of them.
3. **"Could not find a relationship between 'events' and 'venues'"** means migration `0002` has not been
   applied — not a code bug.
4. **`fetchEvent` applies the region filter**, so a valid id can still 404.
5. **`NEXT_PUBLIC_SITE_URL` unset in production poisons every absolute URL** — sitemap, robots, canonical,
   OG, JSON-LD. This shipped once; a crawler reported "no sitemap found". Verify with
   `www.epchisme.com/robots.txt`.
6. **`NEXT_PUBLIC_*` vars are inlined at build time** — changing one needs a **redeploy**, not a restart.
7. **Never add visual truncation to `/crawler/events`.**
8. **`IG_SLIDES_BUCKET` uses `??` where its siblings use `||`**, so an *empty-string* value yields bucket
   `""` and slide previews silently render nothing. Tracked in [known-gaps](../known-gaps.md).
9. **`"America/Denver"` is hardcoded in three places** on the web side plus read from an env var in a
   fourth. Changing the zone means touching four places, one of which is Python.
10. **`geist` is an unused dependency**, and `web/README.md` is untouched `create-next-app` boilerplate.
11. **Nine server-only env vars the app reads are missing from `web/.env.example`.** Use
    [configuration.md](../operations/configuration.md).

---

*Verified against commit `9157646` (2026-09-06). Last updated 2026-09-10.*
