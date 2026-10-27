# Questions and Decisions

Every original question (Q-1 to Q-16) was **answered by the product owner on 2026-10-12**. The answers
are recorded verbatim in spirit below, with what each one changes. New questions that came out of the
answers are at the bottom (Q-17 onward) and are **open**.

When a question is answered, record it here and update [SPEC.md](SPEC.md) and [ROADMAP.md](ROADMAP.md)
in the same commit.

---

## Decisions (answered 2026-10-12)

| # | Question | Answer | What it changes |
|---|---|---|---|
| Q-1 | Weighted fusion vs. "newest reading wins" | **Show both numbers.** Today's answer (CBP / freshest source) stays, and the Chisme estimate is shown beside it | [ADR-0013](../architecture/adr/0013-weighted-fusion-of-wait-signals.md) is Accepted in this form. `consensus.py` rule 2 is **kept** for the first number, not superseded |
| Go-live gate | When does the estimate appear and drive ranking? | **Hidden until it meets the Q-6 target.** Ranking keeps today's method until then | SPEC §4.6 |
| Q-2 | CBP says closed, another signal says moving | **Flag it, don't decide.** Show "CBP says closed, traffic appears to be moving" | SPEC §4.4: a closure conflict is surfaced, never resolved silently |
| Q-2b | How to rank and show when sources disagree | **Rank on the trust-weighted estimate** (once live), not the worst case | **Supersedes** the "disputed lane is ranked on its worst case" rule (`border-wait-times.md:245-249`) once the estimate is live. Recorded in ADR-0013 |
| Q-3 | Where the always-on video worker runs | **A machine we own** | Phase 6.2. The worker must report a heartbeat (ADR-0011: loud failures) |
| Q-4 | Directions | **Both: northbound and southbound** | `direction` is required on every signal. Southbound has **no CBP number**, so it is estimated from cameras, Maps and the crowd only, and graded by southbound timed crossings |
| Q-5 | Lane types | **All of them**: car, SENTRI, Ready Lane, pedestrian, commercial | Each lane is estimated separately where a signal can tell lanes apart. Maps cannot (SPEC §5.1), so it contributes only where lanes share one approach |
| Q-5b | Bridges | **All six**: Paso del Norte, BOTA, Ysleta, Stanton, Tornillo, Santa Teresa | Tornillo and Santa Teresa have no known camera, so they rely on CBP, sites, Maps and the crowd |
| Q-6 | Accuracy target | **Stricter: average error ≤ 5 min, ≥ 80 % within 10 min**, over ≥ 30 timed crossings per bridge | Gate for showing the estimate (SPEC §8) |
| Q-7 | Camera frames from YouTube | **Use the YouTube streams anyway**, accepting the risk | Product-owner decision, recorded with its risk: YouTube's ToS and API policies forbid automated capture. The likely failures are an IP block of the worker's connection and streams that disappear. The worker must detect a dead stream and alert (SPEC §5.1) |
| Q-8 | Google Maps budget | **Free tier now; document the paid option** | 5,000 free calls a month ÷ 6 segments ≈ 833 polls a month ≈ **one poll every ~52 min** [A: 6 segments]. The paid option (every 10 min ≈ $209/month) is documented in SPEC §5.1 |
| Q-8b | Maps terms on storing data | **Accept the risk, but get a legal review first** | Phase 5 waits on the review. Who does it is Q-17 |
| Q-9 | Which websites | **All of them, after a deep investigation** of who publishes border waits, **including Facebook groups** | New Phase 4.0 (investigation). bordertraffic.com still needs a licence for commercial use. Facebook groups cannot be scraped within Meta's terms, so the investigation looks for a legitimate route (Q-18) |
| Q-10 | Crowd channels | **All four**, in order: **Telegram bot → website form → Instagram DMs + comments → WhatsApp** | Phase 3 builds them in that order |
| Q-11 | Instagram inbox shared with GoHighLevel | **Investigate first** whether our webhook and GHL can both subscribe | New step 3.0b |
| Q-12 | Meta App Review / Business Verification owner | **Prepare everything now; decide the owner later** | New step 3.0a: draft the screencast script and privacy-policy text |
| Q-13 | Crowd weight | **As proposed** (~30 % of a published site, corroboration, 25 % cap), **plus position reports**: a photo or location tells us where someone is in line, and with our queue speed that gives a deterministic wait for them. A report from near the end of the line also tells us the queue length | New signal kind in SPEC §5.4. **[A, unverified]** Instagram and most chat apps strip photo GPS, so a shared live location (Telegram, WhatsApp) is the likely reliable route (Q-19) |
| Q-14 | Privacy | **Also keep the message text**: hashed sender id, parsed values and raw text, kept **30 days** | `border_reports.raw_text`, with a 30-day prune |
| Q-15 | Surfaces | **GHL knowledge-base sheet, DM replies, website.** Instagram posts are not chosen | Phase 7.3 |
| Q-16 | Who applies migration 0019 | **Claude, via the Supabase connector**: check the live database first, then apply 0019 with an explicit go-ahead. Creating the GitHub token and storing it in Vault stays with a human | Step 0.3 |

### Notes on the original options

- Q-1's chosen option, "show both", was listed in the first draft of ADR-0013 as a rejected alternative.
  ADR-0013 was still *Proposed* at that point, so it was rewritten to record the actual decision, with
  the original proposal kept as a rejected alternative.

---

## Open questions (raised by the answers)

### Q-17. Who does the legal review of Google Maps terms?

Phase 5 waits on it (Q-8b). Is there a lawyer or advisor to send it to, or should I prepare a
one-page summary of the relevant clauses for whoever does it?

### Q-18. Facebook groups: which route?

Scraping groups breaks Meta's terms and the Groups API is effectively gone. The legitimate routes are:

- the group's admins agree to share or forward reports;
- members tag or forward reports to our Telegram bot or website;
- partnering with a group to cross-post our estimate in exchange.

Which groups matter most (for example "Reporte de Puentes")? Is there a relationship with any admin?

### Q-19. Position reports: how do people send their position?

**[A]** Photos sent through Instagram and most chat apps lose their GPS data. The options:

- shared live location (Telegram and WhatsApp support it);
- the website form asking for location permission;
- a photo that we place by landmark, which is manual and does not scale.

Which do you want? Is asking for location acceptable to the audience?

### Q-20. Southbound ground truth

With no CBP number southbound, the southbound estimate is graded only by southbound timed crossings.
Should replies invite people heading into Juárez to time their crossing too ("voy a cruzar a Juárez")?

### Q-21. Crossing time from a Mexican-side and a US-side camera: which feeds, which methods, which data?

**Answered 2026-10-24 by the product owner** ([ADR-0014](../architecture/adr/0014-crossing-time-from-two-cameras.md)):

- **The US-side feed** is the City of El Paso's Bridge Cameras page
  (https://www2.elpasotexas.gov/misc/externally_linked/bridges/cameras.html). CBP publishes no cameras.
- **Methods:** two, side by side on the same frames. One is deterministic (no model); the other uses a
  vision model (YOLOX-S, pinned, Apache-2.0).
- **Licence plates:** never read, never matched.
- **City feeds:** the risk of reading them automatically is accepted. They are **never displayed** on our
  pages.
- **GPS-logged team crossings:** later.

Per-vehicle data (boxes, colour signatures) stays in memory for at most 3 h. Only counts and transits
per window are written.

**Exception, owner, 2026-10-26: City frames may be written only to draw a counting line.**
- **What it was used for:** the owner asked for labelled City frames to answer Q-22, and line B was
  redrawn from two daylight City frames of that afternoon.
- **What the commands do:** `camera snapshot` and `camera read --debug` refuse a display:false camera
  unless `--calibration` is passed. With the flag, they print the rule:
  - keep the frame outside the repository (enforced: a path inside it is refused before any stream
    opens);
  - never commit or publish it;
  - delete it once the line is drawn;
  - never describe the plates or people in it.
- **What has not changed:** City frames are still never displayed on our pages.

### Q-22. Which bridges have SENTRI lanes northbound, and which lanes in each camera are they?

**Partly answered 2026-10-26 by the product owner ("for now"):**

| Bridge (northbound, Juárez → El Paso) | Car lanes |
|---|---|
| Paso del Norte (Santa Fe) | SENTRI **and** normal |
| Zaragoza (Ysleta) | SENTRI **and** normal |
| Lerdo (Stanton) | **SENTRI only** |
| Córdova de las Américas (BOTA) | **normal only** |

CBP's live feed agreed on 2026-10-26 at about 22:55 UTC. PDN: standard 1 open, SENTRI 1, Ready 4. Ysleta:
standard closed, SENTRI 3, Ready 3. Stanton DCL: SENTRI 2, standard closed, Ready N/A. BOTA: standard 2,
Ready 7, SENTRI N/A.

- **ASSUMPTION:** "normal" covers both of CBP's non-SENTRI car lanes, standard and Ready. CBP publishes them
  separately.
- Tornillo and Santa Teresa were not part of the answer.
**Which lanes in each camera view are SENTRI** (owner, 2026-10-26, from labelled frames of that afternoon):

| View | SENTRI | Normal |
|---|---|---|
| PDN, Mexican half (`pdn-north`, line A) | lane 5, the roadway past the orange barrels | lanes 1–4 |
| PDN, US half (`city-pdn-north`, line B) | column E, to the right of the white posts along the right railing | the columns left of the posts |
| Zaragoza bridge, Mexican side (FPFCH "Zaragoza (Vista Norte)") | column D, to the right of the white posts along the barrier | columns A–C |
| Zaragoza, US booths (City `BridgeZaragoza3`) | not sure; "normally the far left", left of booth lane 1 | — |

At both PDN lines the SENTRI lane is physically separated (barrels at A, posts at B), so SENTRI and normal
cars can be counted and paired separately there. The Zaragoza booth answer is the owner's best recollection,
not a confirmed one.

**What changed with this answer (2026-10-26):**
- **Zones are typed.**
  - `pdn-north`: lane-5 is `sentri`, lanes 1-4 `normal`.
  - `city-pdn-north`: line B was redrawn as col-1..col-4 (`normal`, left of the posts) and col-e
    (`sentri`, column E).
  - The queue reader's `pdn-north` strips all keep `cbp_lane` null, lane-5 included: the lane type
    lives on the count zones. A strip's number is the queue reader's (D+L)/v, which ADR-0015 rejects
    as a wait, so it is not labelled SENTRI or filed as `car_sentri` until it is checked against
    timed crossings.
- **SENTRI and normal are measured apart.** The runner gives one record per counting method and lane
  type. The exception is the loop's SENTRI at PDN, which is not measured: col-e is counted by yolox
  only (Q-24).
- **Two rulings from the redraw, for the owner to confirm:**
  - The band along the left wall has no zone. It was empty in both frames while every other column
    was queued, has cones at its far end, and is about 1-2 m wide by a rough model of the camera.
  - No car stood in column E in either frame, so col-e's place still needs a frame with a SENTRI car
    in it.

### Q-23. Line B to the booths: time cars for K_eff, and give the booth count

Every camera wait is a floor ("≥ N min") until the stretch from line B to the booths is timed
([ADR-0015](../architecture/adr/0015-camera-wait-from-cars-ahead.md)). Three things are needed.

**1. Timed cars, for K_eff.**
- **How:** watch the City's own public page, never ours and never a written frame. Note the minute a
  car of distinctive colour crosses line B, and the minute it reaches the canopy front. Colour only,
  never a plate.
- **How many:** at least 3 cars, at fully queued moments.
- **Kept apart:** by light (daylight first, night later) and by lane type (column E for SENTRI, the
  columns left of the posts for normal).
- **How a timing becomes K_eff:** K_eff = the timed minutes × that method's counted rate at line B at
  that minute, read from `camera-<date>.jsonl`.
- **What is stored:** `pairs.json` keeps only `{lane_type, light, hours_utc, low, high, n, date}`,
  never one car's timing.
- **Fewer than 3 timings:** a K_eff is used, but never votes.
- **Redraws:** timings must use line B as redrawn on 2026-10-26. Any later redraw voids them.
- **Hours:** entries are selected by UTC hours (`hours_utc`). Is UTC acceptable?

**2. The total number of CBP primary car booths at PDN northbound.** This is a site fact, never CBP's
live `lanes_open`. Without it, nothing bounds the cars under the canopy, so the total stays a floor
even with K_eff.

**3. B's occupancy, recorded by the runner.** This is code, not yet built.

### Q-24. Hand counts at line A and line B, per lane type

Neither counter has been checked against a hand count, so no number can be trusted or vote.

**Where:** at each line, per lane type:

| Lane type | Line A | Line B |
|---|---|---|
| SENTRI | lane 5 | column E |
| Normal | lanes 1-4 | the columns left of the posts |

**How long and when:** 15 min each, in free flow, in stop-and-go and at night.

**What is stored:** each method's capture in `pairs.json` `hand_counts`, as `{method, lane_type, line,
capture, hours_utc, n, date}`. A method is checked for a group when its capture is within 5 % at both
lines.

**Column E first.** Seen from this low camera, col-4's cars cover col-e, and the loop counter does
count them as SENTRI: in a rendered check (2026-10-26, synthetic frames, script not kept), a loop
over col-e alone counted 2 crossings for 6 col-4 cars passing in front of it with the zones'
`metres` (3 per car before the zones had them). So `cameras.json` names col-e as counted by yolox only
(`"methods": ["yolox"]`), and the loop does not measure SENTRI: its SENTRI wait reads "not
measured". A 15-min hand count at col-e lifts that.

**The normal columns too.** col-1..col-3 cars cover the next column in the same way, so the loop's
normal count at B also counts them again in col-2..col-4 (by how much is not measured; ASSUMPTION).
That over-count stays inside the group, so it reads as drift, not as another group's cars. The
loop's normal stays measured but cannot vote: hand-count the normal columns at B for 15 min with
col-1 queued.
- **Open (2026-10-27):** take the loop off col-2..col-4 now (`"methods": ["yolox"]`), or only if that
  count is not within 5 %? Either way the loop then measures nothing at PDN. **Default, as built:**
  only if the count fails.

### Q-25. Is Ready physically separate from standard before line B?

"Normal" pools CBP's standard and Ready lanes (Q-22, ASSUMPTION).

**If they are separate** (their own lanes at A and their own columns at B), normal can be split, and its
numbers filed under `car` and `car_ready`.

**If not:**
- the cameras can only ever vote for `car_sentri`;
- if standard and Ready cars reorder between A and B, for example by queueing in different columns
  at different speeds, the normal group's pairing is off, in the same way as a pooled pair but by
  less.

### Q-26. ADR-0015: the vote, which lanes vote, and three display details

**1. Decision 3, the vote.** Once a pair is validated, does the camera wait:
- **(a)** vote in the hidden estimate while no flag is up (recommended);
- **(b)** only get recorded and shown, until GPS-timed crossings grade it;
- **(c)** only grade?

**2. Decision 4.** Only a pair with a CBP lane id votes. At PDN that is SENTRI, unless Q-25 says Ready
is separate. While col-e is counted by yolox only, SENTRI has one method and cannot vote (Q-24).

**3. Display details.** Decision 2 (display on this PC) is accepted. As built:
- **(i) A flagged count.** A count that anchors have not checked within 60 min shows "—". That is
  stricter than "anchored, continued or given, with its sd known". Keep it?
- **(ii) A stretch beside a floor.** The page shows the A→B stretch's own minutes beside a floor total
  (for example "14 min" beside "≥ 10 min"). Acceptable, or should a stretch show minutes only when the
  total does?
- **(iii) `camera transit`'s status line.** It still prints an ungated "now ~N min" and median
  transit. Should it follow the same rule?

### Q-27. Do SENTRI cars queue with the other cars before lane-5 splits off?

At `pdn-north`, SENTRI is lane-5, past the orange barrels (Q-22). Sometimes lanes 1-4 back up past the
camera's near edge onto the Juárez street while lane-5 does not reach line A. The camera wait then
cannot tell whether a SENTRI car joining now waits in that street line first.
- **If SENTRI has its own approach** from the street, its tail before A is 0 then, and the street line
  is the normal lanes' alone.
- **If it shares the street approach**, its tail includes the street line, the gap, and the run-up to
  where lane-5 splits off.

Until this is answered, that tail is not known (a floor of 0). The same holds for the normal group in
the mirror case.

**A follow-up.** The tail is made unknown only when the street is queued. Two cases keep a tail of 0,
known, for a group whose own lanes are short of A:
- another type's line runs past the near edge but ends between the views (the street is empty);
- the street cannot be read.

Should those be unknown too until this is answered? Today it changes nothing shown, because every total
is a floor until K_eff is timed (Q-23).

### Q-28. ADR-0016: how long may nothing move at a line, and what comes with it

The City camera (line B) has no burned-in clock. Its picture now counts as frozen only when it repeats
exactly. A freeze re-encoded with noise is caught by the runner instead: once nothing has moved at a line
for more than `QUIET_S`, that stretch is cut, and until then the line counts as seen only up to its last
moving frame ([ADR-0016](../architecture/adr/0016-frozen-when-the-picture-repeats.md), Proposed). Each
choice below is built as its default, which stays an ASSUMPTION until you confirm it.

**1. `QUIET_S`.** **Default: 300 s.** The figures come from one 20-min night at line B (2026-10-26,
21:32-21:52 local), with its motion bursts thinned to stand for lighter traffic (ASSUMPTION):

| `QUIET_S` | Gaps an hour at full / half / a quarter of that traffic | A noisy freeze shorter than this reads as |
|---|---|---|
| 120 s | 0 / 0.8 / 5.8 | a quiet road, for up to 2 min |
| **300 s (built)** | 0 / 0 / 0.13 | a quiet road, for up to 5 min |
| 600 s | 0 / 0 / 0 | a quiet road, for up to 10 min |

The old rule caught such a freeze in 20-30 s, but it fired 50.4 times an hour on a live picture and made
every cut. At about 3 cars a minute (ASSUMPTION), a 5-min freeze hides about 15 exits at B, and a sure
count stays sure until anchors dispute it.

**2. Smaller choices**, each built as its default:
- **(i) The still tier's share.** Frozen when 14 of the last 40 pairs repeat exactly, a third. It is a
  judgement: the live night had at most 0 in any 40, with each pair at least 0.48 s apart against a
  longest unchanged native stretch of 0.42 s. **Default:** keep it, and check it in the runs of Q-32.
- **(ii) Black with noise.** Black re-encoded with noise is now caught only by the no-motion gap. Should a
  whole scene darker than `BLACK_LEVEL` for 20 s be frozen on its own? **Default:** no.
- **(iii) A standing queue at B.** With nothing moving there for more than 120 s, the page marks the
  camera wait "Stale" although the stream is live, and its text names only a stopped stream. Accept it,
  and with what words? Proposed: "Stale: the slower counting line is N min behind live (its stream may
  have stopped without a refusal, or nothing has moved at it), so these waits are N min old."
  **Default:** today's text.
- **(iv) The reopen backoff.** After freeze after freeze the wait grows to at most 300 s, and drops back
  to 10 s after a read gave 60 s of frames. It spares the City feed (read at your risk, ADR-0014), but
  a real recovery can be read up to 5 min late. **Default:** as built.
- **(v) A car counted in a still frame.** After more than 300 s with nothing moving, a car counted in a
  frame whose picture barely changed ends that stretch with a gap that cuts the car too. Accept that, or
  take such a crossing as proof that the road was live? **Default:** cut.
- **(vi) What records now say about gaps.** Each gap's start (to the minute), its length in seconds, and
  its reasons, which can name a segment number ("segment N expired before it was fetched"). Within the
  privacy rule? **Default:** written. The review judged minute-level starts safe.
- **(vii) Reopens.** Records count them since the runner started (`reconnects_a`, `reconnects_b`). Is
  that enough to measure line A's restart rate for Q-29, or do you want a count per record span (which
  overlapping 3-h spans make hard to difference)? **Default:** since the start.
- **(viii) A YouTube stream restarted under a new id.** Two paths are still slow: the new stream not yet
  listed when the channel is listed again (that listing is then kept up to 10 min), and an old playlist
  that stalls instead of ending. Drop the listing on every miss too, at the cost of listing the channel
  on every reopen while a stream is down? **Default:** no.

### Q-29. Anchoring needs about 3 h uncut: how should runs be made?

An anchor sets the count from nothing only when both ends of its trip lie inside the measured span with
`HI_MIN` (90 min) to spare. So the first anchoring epoch must run longer than 180 − T min uncut at
**both** lines: about 174 min for a trip T of about 6 min (ASSUMPTION; read it as 150-179 min). Your
97.5-min run on 2026-10-26 could not have anchored even with no cut. No code change alone lets a run of
under about 3 h anchor.

- **(a) Run longer, no code.** **Default.** Run `camera transit` or `camera estimate` for at least
  3.5 h, away from sunset: wholly after civil dusk (for example 19:00-22:30 local now) or wholly in
  daylight. Sunset at PDN was 18:22 on 2026-10-26; after DST ends on 1 Nov it is about 17:15 local.
  Around sunset the old rule cut 6 times in 7 min, and the loop's road is least known there (Q-31). A
  reopen at either line restarts the 174 min, and line A's reopen rate is not measured (0 in 117.7 min).
- **(b) Lower `hi_min` for PDN, through a new ADR.** A per-pair `hi_min` in `pairs.json`, for example
  45 min, so that `hi_min` − T is under `CHECK_MAX_MIN` (60 min). An anchoring epoch then needs only
  more than 90 − T min. The cost: a trip longer than `hi_min` cannot anchor, and its epoch comes out
  flagged (simulation only). That A→B stays under 90 min is itself an ASSUMPTION (ADR-0014).

**Neither alone promises an unflagged "now".** Under (a), every enclosed anchor left B at least
`hi_min` − T (about 84 min) before the end, which is more than `CHECK_MAX_MIN`, and they all fall within
one stretch T wide, so they give no drift. "Now" is then unflagged only if later anchors lie within 3
cars (`INLIER_MIN_CARS`) of an offset 90 min old: A and B capture must agree to about 1 % (3 in 270
cars). On 2026-10-26 at 21:30, yolox counted "normal" at 2.62 a minute at A and 2.97 at B over rough
spans, which neither shows nor rules out 1 %. A 15-min hand count of yolox "normal" at both lines
(Q-24) should come first.

### Q-30. Was SENTRI open at 21:30 on 2026-10-26, or is col-e in the wrong place?

In the live check of 2026-10-26, 21:32-21:52 local, yolox counted **0 cars** at B's col-e in every
measured span, while A's lane-5 counted about 1.75 a minute. SENTRI cannot anchor while that holds.
- **(a) SENTRI was closed or empty at B then.** CBP's published SENTRI lanes for that hour would tell.
- **(b) col-e is drawn in the wrong place.** It was drawn from two daylight frames with no car in
  column E (Q-22).

**Default:** nothing changes, and SENTRI stays unanchored. A col-e hand count (Q-24) in hours when
SENTRI is open settles it either way.

### Q-31. The loop's learnt road across the change from day to night light

In your run of 2026-10-26 (17:30-19:07 local, before line B's redraw, pooled "mixed"), the loop's exits
at B per 15 min were 17, 18, 8, 3, 4, 0, 4. They fell from about 18:00, while its entries at A stayed at
40-97 and yolox's exits at B at 18-37. A loop zone replaces a held road only after about 32 min of
refreshes that agree (`loop.AGREE_REFRESHES`), and meanwhile it says "counting", with no gap. So a road
learnt in one light probably counts short in the other, without a flag; a road saved at night would do
the same at the next daytime start. This is an inference, not proven: that run also used the old line B.
- **(a)** Mark a held or saved road stale on a large change in the whole scene's light, and relearn it
  (a gap while learning).
- **(b)** Open a gap while the whole sample bank disagrees with the held road.
- **(c)** Use the loop at B by day or by night only, or not across dusk.
- **(d) Default, as built:** nothing. The loop at B is not trusted across dusk, and runs for Q-29 avoid
  sunset.

Is the loop worth keeping at B across dusk at all?

### Q-32. Verification runs for ADR-0016, logging numbers only

The frozen rule, `QUIET_S` and the hold's lag rest on one 20-min night at line B. May one designated
agent, or you, make these runs? Each pulls the City stream at your risk (ADR-0014), and logs numbers only:
frame differences, segment times and records, never a picture.

| Run | When (local) | What it checks |
|---|---|---|
| Dusk | across sunset, for example 17:30-19:30 now (sunset 18:22; about 17:15 after 1 Nov) | the still tier through the camera's change to night, where the old rule cut 6 times in 7 min; the loop's road (Q-31) |
| Deep night | 01:00-04:00 | an empty bridge: false "nothing moved" gaps |
| Peak | a standing queue at B | the hold's lag, and the "Stale" flag (Q-28 iii) |
| Under load | any, under `camera estimate` | lag and expired segments with the queue reader running too. At night yolox took up to 538 ms a frame at B and 723 ms at A, against 500 ms at 2 fps |

Each run logs:
- at B, the most pairs that repeat exactly in any 40 (the still tier needs 14), the native repeat
  lengths (the live maximum was 0.42 s), and the black level;
- `seen_until` minus the latest frame at B, and every "nothing moved" gap;
- every reopen per line (line A's rate is unmeasured), and the reasons in `gaps_a` and `gaps_b`;
- each line's lag behind live.

Expected: no `StreamFrozen` at B, and one epoch per method and group after the loop learns its road.
**Default:** no run until you say.

### Q-33. Methods that are not measured: three choices

At PDN the loop does not measure SENTRI (Q-24). Built on 2026-10-27, each as its default:
- **(i) A run that measures nothing.** When no method of a run measures any group, `camera estimate`
  runs, writes no camera record, and the page says why. Should it refuse to start instead, and so not
  read the City stream for nothing? **Default:** it runs.
- **(ii) Replaying an old log.** For a log that does not carry its run's `not_measured`, replayed under a
  later `cameras.json`, the method `cameras.json` now refuses is shown "not measured" and the run's wait
  for it is hidden. Is that the grading you want, or should a replay keep what the run measured?
  **Default:** "not measured" wins.
- **(iii) `camera transit`'s logs.** Only `camera estimate` writes `not_measured` into its records. Should
  `camera transit` write it too, so that its logs replay exactly? **Default:** no.

---

*Answers recorded 2026-10-12 on branch `chisme-border`; Q-21 on 2026-10-24 and Q-22 on 2026-10-26 on
`chisme-border-waittime`. Q-23 to Q-26 raised 2026-10-26 with ADR-0015; Q-27 the same day, with the lane-type split.
Q-28 to Q-33, and the open point in Q-24, raised 2026-10-26/27 from that evening's run and the night's
live check, with ADR-0016 and the review of methods that are not measured.*
