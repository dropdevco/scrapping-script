# Spec: Border Wait-Time Estimator

Status: **approved direction** (Q-1 to Q-16 answered 2026-10-12, see [QUESTIONS.md](QUESTIONS.md)). Every
number marked **[A]** is a starting value to be tuned against real crossings, not a claim. Items marked
**[Q-n]** cite the answer that decided them, or wait on an open question (Q-17 onward).

**Scope (Q-4, Q-5):** all six bridges, both directions, all lane types (car, SENTRI, Ready Lane,
pedestrian, commercial).

---

## 1. Goal

For each **port × lane type × direction**, publish one estimated wait in minutes, with:

- a range (low–high);
- a confidence word (`high` / `medium` / `low`);
- the list of signals that produced it and how much each counted.

Every number must be explainable to a person, matching the existing rule in `scoring.py:3-5`.

**No machine learning.** The estimate is a deterministic function of its inputs. Two runs over the same
inputs give the same answer. That keeps it testable and arguable, and it matches how the existing code
is written.

### Non-goals (for now)

- Predicting future waits (forecasting). `history.py`'s "typical for this hour" stays as it is.
- Adding or removing bridges. CBP remains the authority on structure (`border-wait-times.md:268-269`).
- Holiday and payday effects. These are explicitly deferred in `border-wait-times.md:697-698`.

---

## 2. What exists and is reused

| Piece | File | Role in the estimator |
|---|---|---|
| `Reading` + `Source` contract | `border/sources/base.py` | Published signals keep using it; wrapped into `Signal` (§3) |
| CBP, pasosfronterizos, borderswaittime | `border/cbp.py`, `border/sources/` | The first three published signals |
| Source health (`ok/partial/empty/failed`) | `border/opinions.py:38-46` | Reused for every signal kind |
| Mirror parity check | `border/opinions.py:68-98` | Kept: a CBP mirror still never votes |
| Freshness windows, rush hours | `border/windows.py` | Reused for poll cadence per signal |
| Staleness and trend adjustments | `border/scoring.py:32-54` | Applied **after** fusion, unchanged |
| **Timed crossings (ground truth)** | `border/crossings.py`, table `border_crossings` | Grades every signal, which produces the trust weights (§4.2) |
| DM intent parser | `border/requests.py:153` `classify()` | Extended with a `report` intent (§5.3) |
| Fake Instagram | `border/devtools/fake_instagram.py` | Test harness for the DM channel before Meta access exists |

---

## 3. The `Signal` contract

A new dataclass in `border/signals.py`. `Reading` is **not** modified, because the current pipeline and
its 16 test files depend on it. An adapter turns each `Reading` into a `Signal`.

| Field | Type | Meaning |
|---|---|---|
| `port_number` | str | CBP port id, e.g. `240202` |
| `lane` | str | `car`, `car_sentri`, `car_ready`, `walk`, `commercial`, … (existing ids) |
| `direction` | str | `north` \| `south`. Both are in scope (Q-4); CBP covers only `north` |
| `kind` | str | `published` \| `measured` \| `crowd` \| `position` (§5.4) |
| `source` | str | e.g. `cbp`, `pasosfronterizos`, `maps`, `cam:pdn-north`, `ig_dm` |
| `state` | str | `open` \| `closed` \| `no_data` |
| `minutes` | int \| None | the signal's own claim |
| `low`, `high` | int \| None | its own uncertainty, if it has one (`measured` signals always do) |
| `observed_at` | aware datetime | when the claim was true (not when we read it) |
| `independent` | bool | `False` for CBP mirrors. They never vote (existing rule 3) |
| `reporter` | str \| None | opaque hash, crowd only **[Q-14]** |
| `evidence` | dict | raw inputs (queue length, speed, the parsed message, …) for audit |

---

## 4. Fusion

### 4.1 Per-signal weight

```
w = trust(source) × freshness(age, kind) × kind_factor(kind)
```

**`freshness`** is exponential decay with a half-life per kind:

| Kind | Half-life [A] | Why |
|---|---|---|
| `measured` | 10 min | Computed from live traffic; goes stale fastest |
| `published` | 30 min | CBP republishes about hourly (`windows.py:3-4`) |
| `crowd` | 20 min | A person's report describes the line they just left |

**`kind_factor`** [A]: measured 1.0, published 1.0, crowd 0.3. That is the "taken less seriously" in the
brief, and it was approved as proposed (Q-13). A `position` report is a measured-style signal computed from
a crowd input. It uses the crowd guards but a kind factor of 0.6 [A], because its wait comes from our own
queue-speed measurement rather than the person's guess (§5.4).

### 4.2 Trust, earned against ground truth

Trust is **measured, not declared**. `crossings.accuracy()` (`crossings.py:97-132`) already computes, per
source over the last 30 days, the mean absolute error, the bias and the share within 10 minutes.

```
trust_empirical = 1 / (1 + MAE / 10)              # MAE 0 → 1.0, MAE 10 → 0.5, MAE 30 → 0.25
trust           = (n·trust_empirical + k·prior) / (n + k)      # k = 10  [A]
```

- **`prior`** is the hand-set starting trust [A]: CBP 0.6, other published sites 0.4, measured 0.5,
  crowd 0.3. It applies fully until a source has graded crossings, then fades as `n` grows.
- `k = 10` matches `ENOUGH_TO_JUDGE` (`crossings.py:16`): at 10 crossings the evidence and the prior
  count equally.
- Recomputed daily and stored in `border_source_trust` (§6), so every estimate can cite the trust it used.

This **is** the "rank websites on how trustworthy they are" requirement. The ranking is
`border_source_trust` ordered by `trust`.

> Dependency: trust stays at the prior until timed crossings flow. That makes the crowd channel
> (Phase 3) a prerequisite for *every* signal's ranking, not just crowd reports.

### 4.3 Crowd guards

The same idea as the brief's "less seriously", made concrete. Approved as proposed (Q-13); the thresholds are [A]:

1. **Corroboration.** A crowd report counts only if a second distinct reporter agrees within tolerance
   (`consensus.tolerance_for`) in the same 20 minutes. **Exception:** a lane with no other live signal may
   show crowd-only, labelled as such.
2. **Share cap.** The total crowd weight is capped at 25 % of the total non-crowd weight.
3. **Rate limit.** One counted report per reporter per lane per 30 minutes.
4. **Plausibility.** 1–360 minutes, the same bounds as `crossings.py:14-15`.
5. **No votes on closures.** A crowd report never opens or closes a lane.

### 4.4 Combining

- **State.** Closures follow CBP for the state field. When a measured signal says traffic is moving on
  a lane CBP calls closed, the lane carries a `closure_conflict` flag. The reply then says so ("CBP marca
  cerrado, pero se ve movimiento") instead of picking a side (Q-2: flag it, don't decide).
- **Minutes.** The **weighted median** of the voting signals, not the weighted mean. One broken scraper
  saying 58 against three signals saying about 20 must not drag the answer to 30. The median also always
  lands on a value some real signal claimed.
- **Range.** The weighted 10th–90th percentile, combined with the signals' own `low` / `high`.
- **Two numbers (Q-1).** Today's freshest-reading number (`consensus.reconcile`) is kept and shown as
  is. The Chisme estimate is shown beside it once live (§4.6). Southbound has no CBP number, so there
  the estimate is the only number.
- **Ranking (Q-2b).** Once the estimate is live, bridges rank on the **trust-weighted estimate**. Until
  then, ranking keeps today's worst-case rule (`border-wait-times.md:245-249`).
- **Then** `scoring.score()` applies the existing staleness and trend adjustments, unchanged.

### 4.5 Confidence

| Word | Rule [A] |
|---|---|
| `high` | total weight ≥ 1.0, at least 2 independent kinds, range ≤ 15 min |
| `medium` | total weight ≥ 0.5, or a single high-trust fresh signal |
| `low` | everything else, including crowd-only lanes |

### 4.6 Hidden until it meets the target

The estimate is **computed and stored on every poll but not shown** (`border_estimates.shown = false`)
until it meets the Q-6 target for a bridge (§8). Each timed crossing grades both numbers. The switch is
**per bridge and direction**, because a busy bridge will reach 30 crossings long before Santa Teresa.
Until a bridge passes, its replies, ranking and sheet rows are exactly what they are today.

---

## 5. Signals

### 5.1 Measured: queue length ÷ queue speed (deterministic)

```
wait ≈ L / v_q
```

- `L` is the **queue length** in metres, from the tail of the line to the booth.
- `v_q` is the **queue advance speed** in metres per minute, the rate the tail moves forward.

The brief says "moving speed of cars". Note that the speed of a car in stop-and-go traffic is **not**
`v_q`: it reads 0 one moment and 15 km/h the next. What we need is how fast the line as a whole
advances, which is observed in one of two ways:

- **Throughput form** (camera at the booth end): `wait ≈ N / μ`, where `N = L × lanes_open / spacing`
  and `μ` is the number of vehicles per minute passing the inspection line. `spacing` is about 7.5 m per
  car [A, to be calibrated].
- **Tail-tracking form** (camera at the tail): `v_q` is how far the tail moved, divided by the time taken.

**Inputs by source:**

| Input | Camera (FPFCH / City) | Google Maps |
|---|---|---|
| `L` | Visible queue extent only, since a camera sees a few hundred metres | Congested extent of the approach: segments whose live duration ≫ free-flow |
| `v_q` / `μ` | Counting vehicles across a line | Live duration over a segment that covers the queue ≈ the queue's own L / v |
| Lane separation | Possibly, if the lanes are visible | **No.** Maps blends SENTRI, Ready and general **[A]** |
| Inspection time | Included in `μ` | **Not included**: Maps routes see road time, not booth time **[A]** |

Each measured signal must carry `low` / `high`. The uncertainty comes from `spacing`, the count noise
and the edge of the visible queue.

**Cameras (Q-7, Q-3).** The product owner chose to read the bridges' public **YouTube live streams**
(FPFCH's channel, `youtube.com/@fpfch/streams`), accepting that YouTube's ToS and API policies forbid
automated capture. The worker runs on **a machine we own**. Consequences the worker must handle:

- **Streams disappear or change id without notice.** Resolve the stream from the channel, not a
  hard-coded video id, and treat "no frames for N minutes" as a source failure.
- **An IP block is possible.** It would hit that machine's connection. A blocked or dead stream must be
  **loud** (ADR-0011): a heartbeat to `/health` and a Telegram alert, never a silent gap.
- Frames are analysed in memory and **not stored**. Only the derived counts are kept, which limits
  what is retained.

**Google Maps (Q-8).** Start on the **free tier**. 5,000 calls a month across ~6 segments [A] allows
≈ 833 polls a month, which is **about one poll every 52 minutes**. At that cadence Maps is a slow,
coarse signal, and its freshness half-life keeps its weight modest.

- **Paid option, documented, not enabled:** polling every 10 minutes is ≈ 25,920 calls a month, which
  is **≈ $209/month** at $10 per 1,000 after the free 5,000 (pricing verified 2026-10-08). Enabling it
  is a config change (poll interval), not a code change.
- **Terms (Q-8b).** The risk is accepted, but a **legal review comes first**, so Phase 5 waits on it
  (Q-17). Until then the design keeps Maps results only as long as grading needs them.

### 5.2 Published signals

Existing: `cbp` (authority, ~1 h), `pasosfronterizos` (independent, ~8 min), `borderswaittime` (mirror,
never votes).

**Q-9: all of them, after a deep investigation** of every publisher of El Paso–Juárez border waits,
**including Facebook groups**. The investigation (Roadmap 4.0) records for each publisher where its
number comes from, how often it updates, whether it is independent of CBP, and its terms. Notes:

- **bordertraffic.com** still needs a licence for commercial use; the investigation covers getting one.
- **Facebook groups** cannot be scraped within Meta's terms, and the Groups API is effectively gone.
  They become a source through a legitimate route instead (Q-18).

Candidates found so far (2026-10-12), all **[V]** from each site's own page unless noted:

| Site | Number comes from | Independent? | Notes |
|---|---|---|---|
| puentesjuarez.com | "Datos oficiales de CBP y Fideicomiso de Puentes" | Partly. The FPFCH share could be independent | No API, no terms found |
| cruceya.com | CBP plus its own labelled "CruceYa" estimates | Its own estimates only | Footer credits Google Maps |
| filadepuentes.com | Estimates plus "Report My Time" crowd | Unknown method | Has terms, **not read** |
| alpuente.mx/juarez | Crowd reports, each with a photo | Yes | No terms seen |
| bordertraffic.com | Own video analytics ("AccuWait", premium) | Yes | **Personal use only; commercial use needs a licence** |
| bordertime.us | Crowd plus cameras plus Google traffic [V, via search summary only] | Unknown | Unverified |

**Rules for adding one** (in addition to `border-wait-times.md:262-269`): set `independent` from
evidence, not hope. A site that copies CBP must be `False`, and it is only useful as a parser check.

### 5.3 Crowd signals

**Channels (Q-10): all four, built in this order:** a public **Telegram bot** (a second bot, separate from the
admin one) → a **website form** → **Instagram DMs and comments** (after the GHL coexistence check, Q-11,
and Meta review, Q-12) → **WhatsApp**. Nothing reads follower messages today **[V]**.

**Parsing.** Add an intent `report` to `requests.classify()`, for messages like "tardé 40 min en santa
fe", "llevo una hora en la fila del libre" and "40 min zaragoza sentri". Hard cases that need tests
first:

- **Collision with alert limits.** "avísame cuando baje de 30" is an `alert` with limit 30, not a
  report. `_stated_limit` (`requests.py:256`) already reads numbers for alerts.
- **Elapsed versus total.** "llevo 40 min" is still in line, so it is a *lower bound*. "tardé 40" is
  finished, so it is a *measurement*. They are stored differently: `low = 40, high = None` for the first.
- Hours and words: "hora y media", "1h", "una hora".
- No bridge named means no signal. Ask, do not guess. This is the same posture as
  `border-wait-times.md:349-354`.

**Timed crossings** ("voy a cruzar" → "ya crucé") already parse and are stored. These are the **ground
truth** of §4.2, not ordinary crowd signals, so they are graded rather than weighted. Southbound
crossings ("voy a cruzar a Juárez") are the only ground truth for southbound lanes (Q-20).

### 5.4 Position reports (from Q-13)

A person tells us **where they are in the line**, not how long they think it will take. We turn that
into a wait with our own measurement:

```
their_wait ≈ distance_to_booth / v_q        # v_q from cameras or Maps (§5.1)
```

- It is deterministic, and as accurate as `v_q` and the position.
- **A report from near the tail measures the queue length `L` itself.** That updates every lane
  estimate built from `L / v_q`, not just this person's. It is the "there's a user almost at the end of
  the line" case.
- It needs a live `v_q`. Without one, the report is stored and used only as a queue-length observation.
- **Position capture is unverified [A].** Instagram and most chat apps strip GPS from photos, so a
  shared **live location** (Telegram and WhatsApp support it) or the website form's location permission
  is the likely route. A photo can be placed by landmark manually, which does not scale (Q-19).
- Positions are checked against a per-bridge **approach corridor polygon**. A location outside it is
  not a queue position.

---

## 6. Storage (new migrations, additive only per invariants §8)

Following the existing `border_*` posture: **RLS on, no policies, service-role only** (`0013`–`0017`).

| Table | One row per | Key columns |
|---|---|---|
| `border_signals` | signal observation | port, lane, direction, kind, source, state, minutes, low, high, observed_at, independent, reporter_hash, evidence jsonb, `content_hash` unique |
| `border_estimates` | port × lane × direction × computation | minutes, low, high, confidence, method (`fusion_v1`), inputs jsonb (each signal's weight), `shown boolean`, computed_at |
| `border_source_trust` | source × day | n, mae, bias, within_10, prior, trust |
| `border_reports` | inbound crowd message | channel, reporter_hash, received_at, raw_text, position (lat/lng, nullable), parsed jsonb, outcome (`counted` / `uncorroborated` / `rate_limited` / `implausible` / `unparsed`) |

**Privacy (Q-14):** the sender is stored only as a hash. The **message text is kept for 30 days**, to
debug the parser, then pruned by the same mechanism as `border_prune` (`0014`). Parsed values and
outcomes are kept long enough to recompute trust (≥ 30 days [A]). Location coordinates follow the same
30-day limit as raw text [A].

---

## 7. Runtime

| Job | Cadence | Host |
|---|---|---|
| Published-site polling, plus fusion | Every 10 min (existing `border-poll`) | GitHub Actions, dispatched by pg_cron (**needs migration 0019 applied**; Claude via the Supabase connector, Q-16) |
| Trust recomputation | Daily (the existing `job=daily` run) | GitHub Actions |
| Instagram / Telegram webhooks | On event | Vercel (next to `api/telegram/webhook`) |
| Google Maps polling | ~Every 52 min on the free tier (Q-8); every 10 min if the paid option is enabled | GitHub Actions |
| Camera analysis | Continuous | **A machine we own** (Q-3), reporting a heartbeat |

---

## 8. Success metrics

Graded against `border_crossings`, per bridge and direction, rolling 30 days.

**Go-live target (Q-6):** mean absolute error **≤ 5 min** and **≥ 80 %** of crossings within 10 min,
over **≥ 30** timed crossings for that bridge and direction. A bridge that passes starts showing the
estimate (§4.6).

- MAE, bias and within-10 for `fusion_v1` against the current consensus answer, side by side.
- Coverage: the share of open lanes with an estimate fresher than 15 min.
- Crowd health: reports received, the share counted, and the share flagged.
- Each signal's trust over time, which is the "ranking".

---

## 9. Testing

- Fusion, weights, trust and crowd guards are pure functions, so they get table-driven tests in
  `tests/border/` with no network. This matches the existing border test style.
- Report parsing gets fixture messages in both languages, including the alert-limit collision cases.
- A **replay harness** re-runs fusion over stored `border_signals` and scores it against stored
  crossings. That is how any weight change is justified.
- `ci.yml` already runs `pytest` and border-scoped `ruff` on every push.

---

*Written 2026-10-12 against `f5aa3b8`.*
