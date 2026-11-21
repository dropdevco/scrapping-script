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
| 6.1 | ~~Inventory the FPFCH YouTube streams~~ **Done 2026-10-19**: 16 streams, 9 live and 7 down ("technical difficulties"); none at Santa Teresa. Table in [border-camera-reader.md](../components/border-camera-reader.md#stream-inventory-roadmap-step-61) | Stream table with evidence | — |
| 6.2 | Set up the worker on **a machine we own**: resolve streams from the channel (not hard-coded ids), heartbeat to `/health`, Telegram alert on a dead or blocked stream | The heartbeat shows up; a killed stream triggers an alert | Machine access |
| 6.3 | Calibrate each camera: booth line, tail zone, lane polygons, car spacing. **In progress 2026-10-19**: 5 cameras, 12 lanes across PDN, Lerdo and BOTA; booth distances, lane types, directions and scales not yet verified | Calibration file per camera, checked against footage | 6.1 |
| 6.4 | Measure queue length and queue speed → `measured` Signal (SPEC §5.1). Frames are analysed in memory and never stored. **Built 2026-10-19** (`src/scraper/border/camera/`, commit `19035e9`) as queue occupancy + time-averaged speed (Little's law) rather than booth-line counting, because no camera sees a CBP booth; validated on rendered queues, not yet on real ones | Validated against timed crossings | 6.3 |
| 6.5 | **`camera estimate`** ([ADR-0015](../architecture/adr/0015-camera-wait-from-cars-ahead.md)): the queue reader and both crossing counters in one process. It gives PDN northbound's camera wait per lane-type group (SENTRI, normal) and per method (`loop`, `yolox`), side by side and never averaged, on the owner's page and terminal only. **Built 2026-10-26** (`combine.py`, `estimate.py`, `pairs.json`), tested on simulated queues; run live by the owner on 2026-10-27 (12:58-14:01 local, daylight); the night run of 6.8 made no minutes: all 209 camera records are floors or "not measured" (no K_eff, no anchored count), and `--replay` matched 194 of its 209 records, the other 15 taking queue readings from the tick's own minute, which the live tick had not yet seen. Every total is a floor until line B to the booths is timed | Each method shows minutes for a sure count with every stretch known; `--replay` of a live run equals its records; graded against GPS-timed crossings | K_eff timings and the booth count (Q-23), B occupancy in the runner, hand counts (Q-24); voting needs ADR-0015 decisions 3 and 4 (Q-26) |
| 6.6 | **Verify the frozen rule and the no-motion gap** ([ADR-0016](../architecture/adr/0016-frozen-when-the-picture-repeats.md), Proposed). **Built 2026-10-27**, tested on scripted frames only: the City picture counts as frozen only when it repeats exactly, a line where nothing moved for more than 300 s is cut, and a line counts as seen only up to its last moving frame. It replaces a rule that fired 50.4 times an hour on a live picture at night and made every cut at line B (the evening's cuts are attributed to it by their size). Runs at dusk across sunset, deep night, peak, and under `camera estimate`, logging numbers only (Q-32) | No `StreamFrozen` at B on a live picture; the most exact repeats in any 40 pairs well under 14; "nothing moved" gaps and the hold's lag logged per run; the owner settles `QUIET_S` and the trade-offs (Q-28) | The owner's go-ahead for the runs (Q-32) |
| 6.7 | **Decide how a count first anchors** (Q-29): an uncut run of at least 3.5 h away from sunset, or a lower `hi_min` for PDN through a new ADR. No run so far has anchored. The 3.5-h night run of 2026-10-28 (6.8) ran about 207-209 min uncut and still did not: the runner keeps crossings 3 h, so no measured span passes 180 min, and at `hi_min` 90 a trip of T min can then be enclosed only for A crossings in a band at most T min wide (about 6 min that night). A longer run slides that band; it does not widen it (Q-29). **Since 2026-10-28** each record's `anchoring`, and bars on the status and the page, show how far a run has come | One run whose records show source "anchors" with 3 or more enclosed anchors; whether "now" stays flagged is recorded with it | 6.6; a yolox "normal" hand count at A and B (Q-24) first, because "now" stays flagged unless capture agrees to about 1 % |
| 6.8 | **The 3.5-hour night run of 2026-10-28.** `camera estimate` from the `Chisme-test` checkout at `59aaa95` (its reflog: there from 01:30:59 to 05:04:08 local), 01:31:22-05:01:22 local (07:31-11:01 UTC), YOLOX on the CPU (its launcher set `CHISME_YOLOX_DEVICE=cpu`), `hi_min` 90, in a new state folder (`camera-3h5-2026-10-28`) so the road was learnt that night, page on 8090. It ran on the `Chisme` checkout's `.venv` Python with `PYTHONPATH` set to `Chisme-test`'s `src`. ASSUMPTION: no uncommitted change to that `src` during the run. **Results** (its records, log and replays, each checked by a second reader, 2026-10-28). **Cuts:** one gap in 627 transit records: line B, loop only, 5 s at 07:33 UTC, "loop zones learning the empty road". Only the gap's 5-s pad shows: the runner starts the span at the gap's end, so how long the loop learnt is not recorded, and line A's learning not at all. Line A had no gap, YOLOX none at either line; 0 reopens at A and B; no expired segment, no frames more than 5 s apart, no `StreamFrozen`, no "nothing moved" gap. The queue reader's `pdn-south-street` (not a counting line) lost one cycle's 2 readings to a `TimelineCut` at about 07:54 UTC; its log line has no time and the terminal never showed it. **Longest uncut stretch:** about 209 min for YOLOX (07:31-11:00 UTC) and 207 min for the loop (07:33-11:00). No measured span passed 180 min: from 10:32 UTC the 3-h signature limit trimmed each span from behind, so the final record's "08:00-11:01, gaps 0" is the last 3 h of the same stretch, not a new one. **No count anchored:** sources only "unknown" and "few-anchors"; at most 1 enclosed anchor agreed at any tick (loop 09:38-10:12 UTC, YOLOX 10:44-10:51), against 3 needed, and no epoch had an `offset_sd`. The anchors found behaved like look-alikes (INFERRED: at the end 6 of 8 and 6 of 7 lay more than 3 cars from their median, and offsets jumped 89-147 cars between ticks), and the counts disagreed: over 08:00-11:01 UTC B counted 15 % more than A for the loop (783 against 681) and 22 % more for YOLOX normal (574 against 469). SENTRI's col-e counted 3 against 292 at A. Every "now" written was flagged. Why length was not the limit: Q-29. **Camera wait:** all 209 records are floors or "not measured", with no minutes (no K_eff in `pairs.json`, no anchored count). **False lines:** none before 03:57 local. All 488 readings from 07:37 UTC on the night-learnt reference read no line, and lane-5 never held a queued metre. From 09:57 UTC lanes 1-3 read lines that start at the first visible metre and grow toward the camera (INFERRED: real early traffic). Lane-4 read a line in 29 readings from 10:03 UTC with its head 46-54 m from the booth end, clear road ahead of it, lane-5 empty and its speed tracking lane-3's: not shown to be a SENTRI line (Q-40). The `59aaa95` cards put minutes on 159 of 712 readings, 52 of them "under ~N" on lanes with no line; replayed through `6d8249c`, 0 cards carry minutes, and the 18 m head gap drops all 29 lane-4 lines and none on lanes 1-3. **Replay:** `--replay` matched 194 of 209 camera records (6.5). **Load:** 2.1-3.6 CPU-s/s of 16; each line 4-10 s behind live. ASSUMPTION: the run ended before dawn. | Results recorded here and in Q-29 / Q-32 | — |
| 6.9 | **YOLOX on the GPU** ([ADR-0017](../architecture/adr/0017-yolox-on-the-gpu.md), Accepted in part 2026-10-27). A daylight run on the CPU was cut 40 times in an hour at line A by expired segments. **Built 2026-10-27/28** (`gpu.py`, the `gpu` extra, `--device`): DirectML on the RTX 4070 by default, only while its start-up self-check and its running CPU re-check agree, with one lock, the adapter chosen by name, no CPU fallback, and a gap (never the CPU) if it stops mid-run. Records name the device; a replay grades one. Same boxes and crossings as the CPU on live frames (31 = 31 by day, 15 = 15 at night), not bit-exact. **1-h check at night, 2026-10-28** (05:04-06:04 local, `6d8249c`, a new state folder): YOLOX on the RTX 4070 at both cameras throughout, no `GpuFailed`, the running CPU re-check 238 and 227 frames with 0 differing, 0 cuts and 0 reopens for YOLOX at A and B, each line 3-9 s behind live, process CPU 0.2-0.6 CPU-s/s (median 0.3) against 2.1-3.6 on the CPU the night before (6.8, lighter traffic). The loop counter at B could not learn its road for about 56 min, as traffic stood on it from the start, and once it had, its record showed only the gap's 5-s pad (Q-39) | A run of at least 3.5 h on the GPU with no `GpuFailed`, its `cpu_differed` logged, and no cut at A from expired segments | The owner's view of the check limits (Q-35) |
| 6.10 | **Queue cards with the booth out of view** ([ADR-0018](../architecture/adr/0018-queue-card-with-the-booth-out-of-view.md), Proposed). On 2026-10-27 at 13:45 local the page showed minutes on an empty street. **Built 2026-10-28:** an empty lane reads "no line" with no minutes (the "under ~N" ceiling is gone), PDN's cards show a line's length and speed with no minutes (the camera wait times their road), and a per-camera head gap (18 m on `pdn-north` and `pdn-south-street`) drops still blobs far into the view (read by `calibration.py` since 2026-10-28; combine counts a dropped run as a floor, Q-34(vii)). Replayed on the logged hour: cards with minutes 35 → 0; on the night run of 6.8, 159 → 0 | A daytime run with no minutes on an empty PDN card and no line starting more than 18 m into the view; the owner settles Q-34 | — |
| 6.11 | **Day and night references for the queue reader** (the deferred F3 of the free-flow review, Q-34 v). A reference learnt at midday never refreshed at night (false 75 m and 50 m+ lines in 16 of 16 windows); absorbing still pixels after about 12 min hid real stop-and-go lines on renders, so it was not landed. A night-learnt reference read no false line in 488 readings at night (6.8); dawn was not reached | No false line at night on a day-learnt reference, and none at midday on a night-learnt one, with stop-and-go lines still read (renders and one live day and night) | How long PDN's lanes stand fully still outside closures (Q-34 v) |
| 6.12 | **Zaragoza as the next pair (conditional).** Line A on FPFCH `KX-bm-Lhy3w` (live 2026-10-27, column D SENTRI per Q-22), line B only if City `BridgeZaragoza3`, labelled "Norte a Sur", shows northbound cars near the CBP booths. Finish PDN first (6.5-6.7). Cheaper meanwhile: queue cards for `KX-bm-Lhy3w` and Tornillo's `0gT7jvaLCkg` (`cameras.json` only, Q-38) | A new ADR, lines drawn and hand-counted, as PDN's | The owner's look at `BridgeZaragoza3` on the City's page (Q-38); PDN validated |

The queue reader uses no detection library or model: it is plain OpenCV image arithmetic, chosen so
every reading is reproducible (see the component doc). The crossing counters' YOLOX-S is ADR-0014's
pinned model, on the CPU or, by ADR-0017, on the GPU.

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

*Written 2026-10-12 against `f5aa3b8`. Updated with the product owner's answers the same day. Steps
6.6 and 6.7 added 2026-10-27 (ADR-0016, Q-28 to Q-32). Steps 6.8 to 6.12 added 2026-10-28 (ADR-0017,
ADR-0018, Q-34 to Q-38); 6.8's results recorded the same day.*
