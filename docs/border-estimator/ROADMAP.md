# Roadmap: Border Wait-Time Estimator

From today's code (`f5aa3b8`) to the final product, in build order. All work happens on branch
`chisme-border`.

**Ordering principle.** Build what grades the signals before building more signals. Until timed
crossings flow, every trust weight is a guess (SPEC §4.2). So ground truth and a shadow-mode
fusion engine come before cameras and Maps, which are also the slowest to unblock.

**Start the long-lead paperwork on day one, in parallel with Phase 0.** Meta App Review and Business
Verification (Q-12), the camera-feed permission (Q-7) and the Maps budget and legal sign-off (Q-8) take
weeks, and none of them needs code.

---

## The final product

1. Every **10 minutes**, each open bridge × lane (× direction, if Q-4 says so) has one estimated wait
   with a range and a confidence.
2. That estimate fuses measured, published and crowd signals, weighted by **trust earned against real
   crossings**.
3. Followers can **report a wait** and **time a crossing** from at least one public channel.
4. Each report gets an honest acknowledgement, and none moves the number on its own.
5. The estimate reaches the surfaces chosen in Q-15: the KB sheet for the GHL bot, DM replies,
   Instagram and/or the website.
6. A public-facing trust ranking of sources, and a backtest that justifies every weight.

---

## Phase 0: Foundations (fix what is broken first)

| # | Step | Done when | Blocked by |
|---|---|---|---|
| 0.1 | **Fix Tornillo's port id**: `240221` → `240401` in `cbp.py:57`, `sources/borderswaittime.py:26`, `text.py:32`, the `border_ports` seed (new additive migration) and the test fixtures | Live CBP shows Tornillo with real numbers. **[V] 2026-10-12: `240401` "Marcelino Serna / Tornillo" read 30 min; `240221` read "Update Pending"** | — |
| 0.2 | **Fix lanes CBP flips to `N/A`**: `_lane()` returns `None`, so the row is never rewritten and stale minutes persist (per `origin/docs/handoff-2026-10-11`) | A lane going `N/A` is rewritten as `no_data` with a fresh `checked_at`; a regression test is added | — |
| 0.3 | **Apply `0019` and store `github_dispatch_token` in Vault** (ops) | `border_runs` shows ~144 per day; `net._http_response` shows `204` | Q-16 (console access) |
| 0.4 | Merge or rebase the `docs/handoff-2026-10-11` branch knowledge into this branch's docs | Known-gaps is consistent | — |

## Phase 1: The signal contract and storage

| # | Step | Done when |
|---|---|---|
| 1.1 | `border/signals.py`: the `Signal` dataclass and a `Reading → Signal` adapter (SPEC §3) | Unit tests; `Reading` untouched |
| 1.2 | Migration: `border_signals`, `border_estimates`, `border_source_trust`, `border_reports` (SPEC §6), additive, RLS on, no policies | Applied to a non-prod DB **[A: does a staging Supabase exist? ask]**; schema doc updated |
| 1.3 | The poller also writes every published reading as a `Signal` row | Rows accrue every poll; the existing answer is unchanged |

## Phase 2: Fusion engine, in shadow mode

| # | Step | Done when | Blocked by |
|---|---|---|---|
| 2.1 | Decide ADR-0013 (**Q-1, Q-2**) | ADR status is Accepted or Rejected | **You** |
| 2.2 | `border/fusion.py`: weights, freshness decay, weighted median, range, confidence, crowd guards (SPEC §4). All pure | Table-driven tests including adversarial cases (one wild scraper, crowd brigading, all-stale) | 2.1 |
| 2.3 | `border/trust.py`: trust from `crossings.accuracy()` plus priors, stored daily | Trust rows per source per day | — |
| 2.4 | The poller computes `fusion_v1` every run and stores it with `shown=false` | Estimates accrue next to the live answer | 1.3 |
| 2.5 | Replay harness: re-run fusion over stored signals, score against crossings | One command prints MAE / bias / within-10 for consensus vs. fusion | 2.4, plus some crossings |

## Phase 3: Crowd channel (the ground truth's front door)

The first channel chosen in **Q-10**. If Meta review is still pending, start with the quickest one
available (likely a public Telegram bot or a website form [A]) so crossings start flowing.

| # | Step | Done when | Blocked by |
|---|---|---|---|
| 3.1 | `requests.py`: a `report` intent: minutes, lower bound vs. measurement, bridge, lane (SPEC §5.3) | Bilingual fixture tests incl. the alert-limit collisions | — |
| 3.2 | Inbound pipeline: channel adapter → hash reporter → classify → `border_reports` → `Signal` (crowd) or `CrossingLog` | Works end-to-end against `devtools/fake_instagram.py` | 1.2 |
| 3.3 | Channel #1 webhook (on Vercel, beside `api/telegram/webhook`), with signature verification and a "fail loud" path (ADR-0011) | A real message produces a `border_reports` row | Q-10, Q-11, Q-12 |
| 3.4 | Replies: thank the reporter, never promise their report changed the number, and offer "voy a cruzar" timing | Reply copy reviewed in both languages | 3.2 |
| 3.5 | Abuse and privacy: rate limit, hashing, retention (SPEC §4.3) | Tests; retention job | Q-14 |
| 3.6 | Further channels from Q-10, one at a time | — | Meta review, etc. |

## Phase 4: More published sources, ranked

| # | Step | Done when | Blocked by |
|---|---|---|---|
| 4.1 | Read each candidate site's terms and record the verdict in SPEC §5.2 | Every row has a terms verdict | Q-9 |
| 4.2 | One `Source` per approved site, with `independent` set from evidence and `expected_ports` set | Selfcheck covers it; mirror check if it copies CBP | 4.1 |
| 4.3 | Publish the trust ranking (sheet tab and/or `/health`) | Ranking visible | 2.3 + crossings |

## Phase 5: Google Maps queue signal

| # | Step | Done when | Blocked by |
|---|---|---|---|
| 5.1 | Map the approach queue segments per bridge (origin → booth coordinates). These are **surveyed, not guessed**: check against cameras or crossings | A segment table, reviewed | Q-5, Q-8 |
| 5.2 | `sources/maps_queue.py`: Routes `computeRouteMatrix` (traffic-aware) per segment → `measured` Signal with low/high | Unit tests with recorded responses; cost per poll logged | Q-8 (key, budget, terms) |
| 5.3 | Calibration: compare against crossings **without storing Maps content past what the terms allow** (Q-8 legal) | A grade in `border_source_trust` | 5.2 + crossings |

## Phase 6: Camera queue signal

| # | Step | Done when | Blocked by |
|---|---|---|---|
| 6.1 | Obtain a permitted feed (FPFCH / City of El Paso), **not** YouTube frame capture | Written permission or a direct stream URL | Q-7 |
| 6.2 | Stand up the always-on worker | Host chosen; heartbeat reaches `/health` | Q-3 |
| 6.3 | Per-camera calibration: booth line, tail zone, lane polygons, `spacing` | Calibration file per camera, reviewed against footage | 6.1 |
| 6.4 | Counting (vehicles crossing a line) → `μ`; tail tracking → `v_q`; → `measured` Signal (SPEC §5.1) | Validated against timed crossings | 6.2, 6.3 |

The detection library is **not chosen**. That needs a spike on real footage once 6.1 lands. **[A]** Do
not pick one blind.

## Phase 7: Go live

| # | Step | Done when | Blocked by |
|---|---|---|---|
| 7.1 | Fusion beats consensus on the Q-6 target over the replay harness | Numbers posted in the PR | Q-6, data |
| 7.2 | Flip `shown=true`. Captions say what the estimate is built from | ADR-0013 accepted; docs updated | 7.1 |
| 7.3 | Surfaces from Q-15: `BORDER_KB_TAB` sheet, DM replies, Instagram and/or website | Each surface live | Q-15 |
| 7.4 | Update `border-wait-times.md`, `known-gaps.md`, the invariants (the new crowd and trust rules) | Docs verified-stamp bumped | — |

---

## What can start today with no answers

Phases **0.1, 0.2, 0.4, 1.1, 1.3, 2.3, 3.1, 3.2** (against the fake Instagram), and the
**paperwork** for Q-7, Q-8 and Q-12.

---

*Written 2026-10-12 against `f5aa3b8`.*
