# ADR-0015: The camera wait is counted from the cars ahead, one method at a time, and shown only to the owner

- **Status:** Accepted in part. Decision 2 (display on the owner's PC) was accepted on 2026-10-26 by the
  product owner. Decisions 1, 3 and 4 are **Proposed**. Decision 1 is what the code computes today,
  but the owner has not signed the formula off.
- **Date:** 2026-10-26
- **Builds on:** [ADR-0014](0014-crossing-time-from-two-cameras.md). Its decisions 2 (two methods, never
  mixed) and 6 (transits become a live signal only through a later decision) stand; decision 3 here is
  that later decision, still open. Fusion ([ADR-0013](0013-weighted-fusion-of-wait-signals.md)) is
  unchanged, because no camera Signal is built.
- **Changes:** the component doc's "for grading and calibration, not for display", which now reads that
  the owner sees the numbers on this PC (decision 2).
- **References:** QUESTIONS.md Q-21, Q-22, Q-23 to Q-26 and Q-33; `docs/components/border-camera-reader.md`,
  section "Combined estimate"; SPEC §5.1. How a line's gaps are found, and how far its count is
  "seen", is [ADR-0016](0016-frozen-when-the-picture-repeats.md) (Proposed, 2026-10-27).

## Context

On 2026-10-26 the owner asked for "the combined camera estimator that goes along with the viewer".
Until then two commands ran apart, and nothing combined their numbers:

| Command | What it runs | Lock |
|---|---|---|
| `camera view` / `camera run` | the queue reader: queue length and speed on each strip | `worker.lock` |
| `camera transit` | both counters (`loop`, `yolox`) at line A (`pdn-north`) and line B (`city-pdn-north`) | `transit.lock` |

The queue reader times a wait as (map distance + visible line) ÷ visible speed. At PDN north it read
99-128 min against CBP's 47 (ADR-0014). Metres are not conserved between the street and the booths:
2 street columns become 5 bridge lanes, then 5-6 columns on the US half, then the booths. Cars are
conserved. So the cars between the lines, divided by the rate they leave line B, is a better measure
than metres over speed.

Four facts shaped the decision:
- **Units.** A counter that misses half the cars at B also halves the rate it counts there, so
  N ÷ μ holds. A count of **real** cars divided by a **counted** rate does not hold: it reads 1/p_B too
  high. That is 2× when half the cars are counted (simulation in `test_border_camera_combine.py`).
- **ADR-0014 decision 2.** The two counting methods are "logged separately, compared before either is
  trusted".
- **Lane types (Q-22, 2026-10-26).** SENTRI is lane 5 at A and column E at B. "Normal" is the other
  lanes and columns. Normal covers both of CBP's standard and Ready car lanes, which are not told apart
  (ASSUMPTION). Pairing the k-th car at A with the (offset + k)-th at B holds only within one type.
- **Nothing measures line B to the booths.** No City view shows a booth up close.

## Decision

1. **The formula (Proposed).** For each lane-type group and each counting method m:

   ```
   W_m = tail_to_A + N_AB,m / μ_B,m + q_B · K_eff,m / μ_B,m
   ```

   - **tail_to_A:** from the back of the line to line A, while the line reaches back over A. It comes
     from the queue reader on `pdn-north` and `pdn-south-street`.
   - **N_AB,m:** the cars between the lines now, as counted by method m.
   - **μ_B,m:** the rate method m counts cars crossing B, over the last 10 min.
   - **K_eff,m:** t_Bb × μ_B,m, where t_Bb is one car's time from B to the canopy front, timed by the
     owner, and μ_B,m is method m's counted rate at that minute.
   - **q_B:** 1 when B sits inside the line, from B's own occupancy.

   Every constant is in method m's counted units, never in real cars.

   **The methods sit side by side and are never averaged.** There is no mean, no envelope and no
   combined minutes. `agree` says whether they agree, within max(3 min, 20 %) (ASSUMPTION). Agreement
   is necessary but not sufficient: where both counters measure a group, they see the same frames and
   zones, and the same cars hide from both. One method per condition (light, speed band) is chosen
   later from hand counts.

   If any segment is not known, the result is a **floor**: the sum of the known lows, with no minutes
   and no high. B→booth stays unknown, so every total is a floor, until all three of these exist:
   - a K_eff timed by the owner (Q-23);
   - B's occupancy, which the runner does not record yet;
   - the number of primary car booths.

   The A→B range is 2σ, with σ² = sd² + (3 + |drift| × max(T, 15)/60)², plus a rate term of
   2/√n_B. The range is an ASSUMPTION until `combine.self_check` has 30 windows.

   CBP is never an input to any of it.
2. **Display on the owner's PC (ACCEPTED, 2026-10-26, by the product owner).**
   - **Where:** each method's camera wait for PDN northbound is shown on the terminal status line and on
     the local page (127.0.0.1:8090), per lane-type group and per method, side by side.
   - **Never public.** The City picture is never shown: display:false cameras stay out of the cards,
     the overlays and the Worker.
   - **One rule decides what is shown:** `combine.display`. Minutes are shown only when:
     - the count between the lines is sure: its epoch's source is `anchors`, `continued` or `given`,
       and its `offset_sd` is known;
     - and every segment is known.
   - **Otherwise:**
     - a floor is shown as "≥ N min", with the reason each segment is unknown;
     - an unsure count is shown as "—, count not anchored (source)";
     - with a line down at the live edge, where there is no count at all, it is shown as "—, no count
       of the cars between the lines".
   - **As built, the rule is one step stricter than the owner's words.** A count that transit flags
     (no anchors have checked it within `CHECK_MAX_MIN`, 60 min) also shows "—", as "count not checked
     by anchors in the last 60 min" (asked in Q-26).
   - **As built, a method the runner does not measure for a group** (a zone's `"methods"` in
     `cameras.json` leaves it out, as yolox-only col-e does for the loop's SENTRI at PDN) shows "—, not
     measured: <why>", and is listed among that group's `would_vote` reasons. It shows no minutes, so
     the rule above is unchanged (added 2026-10-27).
   - **`show_minutes`** in `pairs.json` is true. Set to false, the page and terminal say only "recorded,
     not shown".
   - **The page shows each stretch** through the same rule. The A→B stretch is gated by its count. The
     tail and B→booth do not rest on that count, so the count does not gate them. A measured stretch
     can therefore show minutes beside a floor total (asked in Q-26).
   - **CBP's published wait** sits beside each group, for comparison only.
   - **`camera transit`'s own status line** still prints its ungated "now ~N min" and median transit.
     Decision 2 allows these on this PC. Whether they must follow the same rule is asked in Q-26.
     `camera estimate` does not print them.
3. **The vote (Proposed; the owner chooses).** Once a pair is validated:
   - **(a)** it votes in the hidden estimate only while no validation or transient flag is up, and it
     is graded by replay like any source. This is recommended.
   - **(b)** it is recorded and shown only, until GPS-timed crossings grade it.
   - **(c)** it is used for grading only.

   Today all three give the same behaviour: nothing passes the validation flags, no Signal is built,
   `pairs.json` has `camera_votes` false, and every group records `would_vote: false` with its reasons.
4. **Only a pair with a CBP lane id votes (Proposed).** At PDN that is the SENTRI group (`car_sentri`).
   The normal group pools standard and Ready, so it has no lane id and never votes, unless the owner
   confirms that Ready is physically separate before line B (Q-25). A pooled "mixed" group never votes.
   While col-e is counted by yolox only (Q-24), SENTRI has one method. Its `agree` stays None, and
   "loop not measured" alone keeps its `would_vote` false, until a 15-min col-e hand count lifts that.

## Alternatives considered

| Alternative | Why not |
|---|---|
| Keep `(D + L) / v` with map distances | It reads 0.5-8× away from CBP (ADR-0014). Metres are not conserved through the fan-out from 2 street columns to 5 bridge lanes and 5-6 US columns; cars are |
| Average the two methods, or take their envelope | ADR-0014 decision 2 keeps them apart until compared. They share frames, zones and occlusion, so a mean hides which one is right and an envelope widens on the worse one. One method per condition is chosen from hand counts instead |
| A count of real cars over a counted rate (for B→booth, or a given offset in real cars) | Mixes units. It reads 1/p_B too high (2× at half capture, simulation); a given offset of 0 cars at p_B 0.5 gave 199.5 min in the design panel's run (script not kept). K_eff, timed with the same zones, cancels capture, occlusion and column use together |
| Infer q_B (B inside the line) from the A→B count | It would switch the largest term on a noisy, often flagged number. q_B comes from B's own occupancy, which is not built yet |
| File the pooled or normal number under `car` | Neither has a CBP lane id. A pooled offset follows one type's trip (ADR-0014, unflagged in simulation), and normal mixes standard and Ready |
| A separate vote for each method | Two votes from one camera pair would double its weight. One method speaks per condition |
| Show nothing until the vote is decided (the design panel's default, `show_minutes` false) | The owner asked to see the estimator with the viewer, on this PC only. The display rule keeps unsure counts and floors from looking like waits |

## Consequences

- **A new command, `camera estimate`.** One process holds both locks, so `camera view`, `run` and
  `transit` refuse while it runs. It runs:
  - the same `TransitRunner` that `camera transit` runs;
  - a `Worker` on `pdn-north` and `pdn-south-street`;
  - the existing page.

  `pdn-north` is pulled twice, once by the runner and once by the queue reader (the owner's OK,
  2026-10-26). The command calls `cv2.setNumThreads(4)`, and `--methods loop` is the fallback if the
  CPU falls behind. That fallback gives no SENTRI wait while col-e is counted by yolox only (Q-24).
- **Files.**
  - New: `queue-<date>.jsonl`, `camera-<date>.jsonl` and `camera-replay-<name>.jsonl`.
  - They hold derived numbers only, with every time cut to the minute. No frame is written.
  - Per-vehicle data stays in the runner's memory for at most 3 h, as ADR-0014 decision 4 requires.
  - `--replay` recombines the logged files under the live rule and prints the self-check. It gives
    back the live records, except where a reading reached the log after its tick, after a restart,
    across a UTC midnight, or, for a log that does not carry its run's `not_measured`, where
    `cameras.json` `"methods"` or `--methods` are not the live run's (component doc).
- **Tonight every total is a floor,** because B→booth is not timed. After dusk the count is unsure:
  no colour anchors form in the dark.
- **Lane types are measured apart.** Zones are typed per Q-22, and the runner measures each type that
  both lines carry on its own: one record per method and group. The exception is a group with a zone
  that names the counters allowed there (`cameras.json` `"methods"`). Any other method leaves that
  group "not measured", with no record. At PDN the groups are
  `normal` (A lanes 1-4 ↔ B col-1..col-4) and `sentri` (A lane-5 ↔ B col-e). col-4's cars cover col-e,
  so col-e is counted by yolox only (Q-24), and a tick gives 3 records: loop normal, yolox normal and
  yolox sentri. A pair pools its lanes
  as "mixed" only when its lines share no type. ADR-0014's consequence "One lane type per pair"
  describes the state before this. It is not edited here, because it is an accepted record.
- **A method that is not measured (2026-10-27, after review).**
  - `camera estimate` writes the run's `not_measured` into every transit record, and `--replay` uses
    it. Rejected: logging only `--methods`, which still drifts when `cameras.json` `"methods"` change
    (as when col-e is lifted after its hand count); documenting the difference only.
  - In `combine`, `not_measured` wins over a record of the same method and group, which then feeds
    neither `agree` nor the vote's reasons. Rejected: letting the record win, which would show a wait
    that today's configuration says does not hold. The live path cannot reach this; only a replay of a
    log without `not_measured` under a changed `cameras.json` can (Q-33).
  - A run in which no method measures any group writes no camera record, and the page says why.
    Rejected: an empty camera record, which a replay could not make again. Refusing to start is left
    to the owner (Q-33).
  - The page's agreement line names the method that is not measured, instead of "both methods need
    minutes".
- **Line B was redrawn on 2026-10-26** from two daylight City frames, by the owner's leave. That voids
  any K_eff timed before, though none existed. Every timing must use these zones.
- **City frames.** `snapshot` and `read --debug` refuse a display:false camera unless `--calibration`
  is passed. With it, they print the rule: draw a counting line only, keep the frame out of the
  repository (a path inside it is refused), never commit or publish it, and delete it after. The design panel had rejected any flag,
  as a standing way to write City frames; the owner chose the flag. ADR-0014 decision 5 (never shown
  on our pages) is unchanged.
- **Grading stays like with like** (ADR-0014). The camera wait uses the queue reader's tail, so it never
  grades the queue reader. Only GPS-timed crossings grade the total. `self_check` grades the A→B live
  edge against later by-entry windows.
- **Fusion is untouched by this ADR.** In the same change set, fusion raises a closure conflict only for
  a measured claim with minutes above zero, so a floor (minutes None) never raises one.
- **Owner work:**
  - K_eff timings and the primary booth count (Q-23);
  - hand counts per group at A and B (Q-24);
  - whether Ready is separate before line B (Q-25);
  - whether SENTRI cars queue with the others on the street before lane-5 splits off (Q-27);
  - decisions 3 and 4 (Q-26);
  - three choices about methods that are not measured (Q-33).
