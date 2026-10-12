# Roadmap: Border Wait-Time Estimator

From today's code (`f5aa3b8`) to the final product, in build order. All work happens on branch
`chisme-border`. Decisions behind each step are in [QUESTIONS.md](QUESTIONS.md). Live progress is on
the [roadmap page](https://claude.ai/artifact/7h5SfBDqXBdmDQFFvBfUX6).

**Ordering principle.** Build what grades the signals before building more signals. Until timed
crossings flow, every trust weight is a guess (SPEC §4.2). So ground truth and the hidden estimate come
before cameras and Maps.

**Long-lead items to start early, in parallel:**

- Meta paperwork prep (3.0a);
- the GoHighLevel coexistence check (3.0b);
- the publisher investigation, including Facebook groups (4.0);
- the Google Maps legal review (5.0, owner is Q-17).

---

## The final product

1. All **six bridges**, **both directions**, **every lane type**. Every 10 minutes each open lane has
   today's number (CBP / freshest source, where one exists) and, once accurate enough, a **Chisme
   estimate** beside it.
2. The estimate fuses measured (YouTube cameras, Google Maps), published (every vetted site), crowd and
   position signals, weighted by **trust earned against real timed crossings**.
3. A bridge's estimate is shown, and drives the "fastest bridge" ranking, only once it reaches **≤ 5 min
   average error, ≥ 80 % within 10 min** over 30+ crossings in that direction.
4. Followers can report a wait, share their position in line, and time a crossing on **Telegram, the
   website, Instagram and WhatsApp**.
5. Closure conflicts are flagged openly, never resolved silently.
6. The estimate reaches the **GHL knowledge-base sheet**, **DM replies** and the **website**.
7. A visible trust ranking of sources, and a backtest that justifies every weight.

---

## Phase 0: Foundations (fix what is broken first)

| # | Step | Done when | Blocked by |
|---|---|---|---|
| 0.1 | **Fix Tornillo's port id**: `240221` → `240401` in `cbp.py:57`, `sources/borderswaittime.py:26`, `text.py:32`, the `border_ports` seed (new additive migration) and the test fixtures | Live CBP shows Tornillo with real numbers. **[V] 2026-10-12: `240401` "Marcelino Serna / Tornillo" read 30 min; `240221` read "Update Pending"** | — |
| 0.2 | **Fix lanes CBP flips to `N/A`**: `_lane()` returns `None`, so the row is never rewritten and stale minutes persist (per `origin/docs/handoff-2026-10-11`) | A lane going `N/A` is rewritten as `no_data` with a fresh `checked_at`; regression test added | — |
| 0.3 | **Restore the 10-minute clock.** Claude checks the live database through the Supabase connector (are `pg_cron` / `pg_net` present? is 0019 applied?), then applies 0019 **only with an explicit go-ahead**. A human creates the GitHub token and stores it in Vault | `border_runs` shows ~144 per day; `net._http_response` shows `204` | Go-ahead; the token (human) |
| 0.4 | Fold the `docs/handoff-2026-10-11` branch's findings into this branch's docs | Known gaps are consistent | — |

## Phase 1: The signal contract and storage

| # | Step | Done when | Blocked by |
|---|---|---|---|
| 1.1 | `border/signals.py`: the `Signal` dataclass (with `direction` and the `position` kind) and a `Reading → Signal` adapter (SPEC §3) | Unit tests pass; `Reading` untouched | — |
| 1.2 | Migration for `border_signals`, `border_estimates`, `border_source_trust` and `border_reports` (with `raw_text` and `position`), plus a 30-day prune of raw text and location (SPEC §6). Additive, RLS on, no policies | Applied to a non-production database [A: does a staging Supabase project exist?]; schema doc updated | — |
| 1.3 | The poller also writes every published reading as a `Signal` row | Rows accrue every poll; the existing answer is unchanged | 1.2 |

## Phase 2: The Chisme estimate, hidden

| # | Step | Done when | Blocked by |
|---|---|---|---|
| 2.1 | ~~Decide ADR-0013~~ **Decided 2026-10-12**: show both numbers; hidden until accurate; rank on the estimate once live; flag closure conflicts | ADR-0013 Accepted | — |
| 2.2 | `border/fusion.py`: weights, freshness decay, weighted median, range, confidence, crowd guards, `closure_conflict` flag (SPEC §4). All pure | Table-driven tests including adversarial cases (one wild scraper, crowd brigading, all-stale, southbound with no CBP) | — |
| 2.3 | `border/trust.py`: trust from `crossings.accuracy()` plus priors, per direction, stored daily | Trust rows per source per day | — |
| 2.4 | The poller computes the estimate every run and stores it with `shown=false` | Estimates accrue next to the live answer | 1.3, 2.2 |
| 2.5 | Replay harness: re-run fusion over stored signals, score against crossings per bridge and direction against the go-live target | One command prints error, bias and within-10 for both numbers, plus a pass/fail per bridge | 2.4, plus crossings |

## Phase 3: Crowd channels (the ground truth's front door)

Built in the approved order: **Telegram → website → Instagram → WhatsApp**.

| # | Step | Done when | Blocked by |
|---|---|---|---|
| 3.0a | **Prepare the Meta paperwork**: App Review screencast script, privacy-policy text, a Business Verification checklist. The owner is decided later (Q-12) | Drafts in `docs/` | — |
| 3.0b | **Investigate GoHighLevel coexistence**: can our webhook and GHL's integration both subscribe to `@elpasochisme`'s messages and comments, or must reports route through GHL? (Q-11) | A written finding with evidence | — |
| 3.1 | `requests.py`: a `report` intent: minutes, lower bound vs. measurement, bridge, lane, direction (SPEC §5.3) | Bilingual fixture tests incl. the alert-limit collisions | — |
| 3.2 | Inbound pipeline: channel adapter → hash the sender → classify → `border_reports` (raw text kept 30 days) → crowd `Signal` or `CrossingLog` | Works end to end against `devtools/fake_instagram.py` | 1.2 |
| 3.3 | **Channel 1: public Telegram bot** (a second bot; the admin bot stays locked down), webhook on Vercel, fail-loud path (ADR-0011) | A real message produces a `border_reports` row | — |
| 3.4 | Replies: thank the reporter, never promise their report changed the number, offer "voy a cruzar" timing in both directions | Copy reviewed in both languages | 3.2 |
| 3.5 | Abuse and privacy: rate limit, hashing, 30-day text retention (Q-14) | Tests; retention job | 1.2 |
| 3.6 | **Position reports** (SPEC §5.4): accept a shared location, check it against the approach corridor, compute distance to the booth, turn it into a wait with the live queue speed, and update the queue length from tail reports | Tests with synthetic positions | Q-19, a live queue speed (Phase 5 or 6) |
| 3.7 | **Channel 2: website form** on epchisme.com (report, timed crossing, optional location) | A real submission produces a row | 3.2 |
| 3.8 | **Channel 3: Instagram DMs + comments** | A real DM produces a row | 3.0a, 3.0b, Meta approval |
| 3.9 | **Channel 4: WhatsApp** (Business API) | A real message produces a row | A WhatsApp Business account [A] |

## Phase 4: Every published source, ranked

| # | Step | Done when | Blocked by |
|---|---|---|---|
| 4.0 | **Deep investigation** of every publisher of El Paso–Juárez waits: websites, apps, **Facebook groups**. For each, record where its number comes from, refresh rate, whether it is independent of CBP, terms, and access route (Q-9) | A publisher table in SPEC §5.2 with evidence per row | — |
| 4.1 | For each publisher: terms verdict and access route (scrape, API, licence for bordertraffic, admin partnership for Facebook groups, Q-18) | Every row has a verdict | 4.0 |
| 4.2 | One `Source` per approved publisher, with `independent` set from evidence and `expected_ports` set | Selfcheck covers it; mirror check if it copies CBP | 4.1 |
| 4.3 | Publish the trust ranking (sheet tab and/or `/health`) | Ranking visible | 2.3 + crossings |

## Phase 5: Google Maps queue signal (free tier first)

| # | Step | Done when | Blocked by |
|---|---|---|---|
| 5.0 | **Legal review** of the Google Maps terms on storing results and building datasets (Q-8b) | A written sign-off | Q-17 (who reviews) |
| 5.1 | Survey the approach queue segments per bridge and direction, plus the approach corridor polygons used by position reports. Surveyed, not guessed | Reviewed segment table | — |
| 5.2 | `sources/maps_queue.py`: traffic-aware route matrix per segment → `measured` Signal with low/high. **Free tier: ~1 poll per 52 min**; the paid 10-minute cadence is a config switch, documented | Tests with recorded responses; monthly call count logged against the free 5,000 | 5.0, a server-only API key |
| 5.3 | Grade Maps against crossings, within what the legal review allows | A trust grade for `maps` | 5.2 + crossings |

## Phase 6: Camera queue signal (YouTube, on our own machine)

| # | Step | Done when | Blocked by |
|---|---|---|---|
| 6.1 | Inventory the FPFCH YouTube streams (`youtube.com/@fpfch/streams`) and any other bridge streams: which bridge, direction and lanes each shows, and whether it is live 24/7 | Stream table with evidence | — |
| 6.2 | Set up the worker on **a machine we own**: resolve streams from the channel (not hard-coded ids), heartbeat to `/health`, Telegram alert on a dead or blocked stream | The heartbeat shows up; a killed stream triggers an alert | Machine access |
| 6.3 | Calibrate each camera: booth line, tail zone, lane polygons, car spacing | Calibration file per camera, checked against footage | 6.1 |
| 6.4 | Count cars crossing the booth line → `μ`; track the tail → `v_q`; produce a `measured` Signal (SPEC §5.1). Frames are analysed in memory and never stored | Validated against timed crossings | 6.2, 6.3 |

The detection library is **not chosen**. That needs a spike on real footage once 6.1 lands. **[A]** Do
not pick one blind.

## Phase 7: Go live, bridge by bridge

| # | Step | Done when | Blocked by |
|---|---|---|---|
| 7.1 | A bridge and direction meets the target: **≤ 5 min average error, ≥ 80 % within 10 min, ≥ 30 crossings** | Replay-harness numbers posted in the PR | Data |
| 7.2 | Show that bridge's estimate beside today's number, and rank on it (ADR-0013) | `shown=true` for that bridge | 7.1 |
| 7.3 | Surfaces: **GHL knowledge-base sheet** (`BORDER_KB_TAB`), **DM replies**, **website** border page | Each surface live | 7.2 |
| 7.4 | Update `border-wait-times.md`, `known-gaps.md` and the invariants (the two-number display, crowd and trust rules) | Docs verified-stamp bumped | — |

---

## What can start now

Phases **0.1, 0.2, 0.4**, all of **Phase 1**, **2.2–2.4**, **3.0a, 3.0b, 3.1–3.5, 3.7**, **4.0**, **5.1**
and **6.1**. Step **0.3** needs your go-ahead before anything touches the live database.

---

*Written 2026-10-12 against `f5aa3b8`. Updated with the product owner's answers the same day.*
