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
| PDN, Mexican half (`pdn-north`, line A) | lanes 4 and 5, from the white posts to the right curb: one SENTRI lane, split by the orange barrels near the camera (corrected 2026-10-27) | lanes 1–3 |
| PDN, US half (`city-pdn-north`, line B) | column E, to the right of the white posts along the right railing | the columns left of the posts |
| Zaragoza bridge, Mexican side (FPFCH "Zaragoza (Vista Norte)") | column D, to the right of the white posts along the barrier | columns A–C |
| Zaragoza, US booths (City `BridgeZaragoza3`) | not sure; "normally the far left", left of booth lane 1 | — |

At both PDN lines the SENTRI lane is physically separated (white posts at both), so SENTRI and normal
cars can be counted and paired separately there. The Zaragoza booth answer is the owner's best recollection,
not a confirmed one.

**What changed with this answer (2026-10-26):**
- **Zones are typed.**
  - `pdn-north`: lane-5 is `sentri`, lanes 1-4 `normal`. **Corrected 2026-10-27 (owner):** the road
    at A has 4 car lanes, 3 normal (lanes 1-3, each with its own painted arrow) and 1 SENTRI on the
    very right; lane-4 and lane-5 are both that SENTRI lane, split by the orange barrels near the
    camera, so lane-4's zone is typed `sentri` too. Both keep their own strip and zone (one zone over
    ~9 m would read two cars abreast as one; no quad moved, so the saved loop roads hold). The owner
    also confirmed the direction: cars drive away from the camera, northbound
    (`direction_verified: true`).
  - `city-pdn-north`: line B was redrawn as col-1..col-4 (`normal`, left of the posts) and col-e
    (`sentri`, column E).
  - The queue reader's `pdn-north` strips all keep `cbp_lane` null, lane-4 and lane-5 included: the lane type
    lives on the count zones. A strip's number is the queue reader's (D+L)/v, which ADR-0015 rejects
    as a wait, so it is not filed as `car_sentri` until it is checked against timed crossings. Until
    2026-10-28 it was not labelled SENTRI on the card either. Since then the card names it
    "lane-4 · SENTRI" from its zone's type, a label only and shown with no minutes (ADR-0018). That
    reverses this rule, pending the owner's choice in Q-36(ii).
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
| SENTRI | lanes 4 and 5 | column E |
| Normal | lanes 1-3 | the columns left of the posts |

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

### Q-27. Do SENTRI cars queue with the other cars before the SENTRI lane (lane-4 and lane-5) splits off?

At `pdn-north`, SENTRI is lane-4 and lane-5, right of the white posts (Q-22, corrected 2026-10-27).
Sometimes lanes 1-3 back up past the camera's near edge onto the Juárez street while neither lane-4
nor lane-5 reaches line A. The camera wait then
cannot tell whether a SENTRI car joining now waits in that street line first.
- **If SENTRI has its own approach** from the street, its tail before A is 0 then, and the street line
  is the normal lanes' alone.
- **If it shares the street approach**, its tail includes the street line, the gap, and the run-up to
  where the SENTRI lane splits off.

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
  **And before 300 s** (raised 2026-10-27, after review): a car counted in a frame whose picture did not
  move still moves `moved_at` on and restarts the 300 s. So a noisy freeze with a count in it at least
  every 300 s, even a false one, reads as a quiet road for as long as it lasts: in a scripted run, one
  crossing 150 s into a 400-s noisy freeze at B left all 400 s written as empty road (ADR-0016). Should
  only frames that moved move `moved_at`? The cost: a line whose cars are counted while its whole scene
  stays still for 300 s (a standing queue, ASSUMPTION) would be cut. **Default:** a counted car moves it,
  as built.
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
- **(ix) A freeze's tail after a reopen** (raised 2026-10-27, after review). A still stretch that held a
  `StreamFrozen` is now cut when something moves, even under 300 s (ADR-0016 decision 2). Two things
  come with it:
  - if a keyframe moves the frozen picture by 0.3 grey or more inside that tail, `moved_at` passes the
    frozen mark, and 7 of 781 replayed lengths still left 0.5-2.1 s read as empty road (ASSUMPTION that
    keyframes do this). Keep the frozen mark until a run of moving frames instead?
  - after any `StreamFrozen`, the reopened read's first moving frame adds a "nothing moved for N s"
    reason and can lengthen the cut by a few seconds (13.1 s against the seam's 10.5 s in one replay),
    with no extra epoch cut.

  **Default:** both as built.

### Q-29. Anchoring needs about 3 h uncut: how should runs be made?

An anchor sets the count from nothing only when both ends of its trip lie inside the measured span with
`HI_MIN` (90 min) to spare. So the first anchoring epoch must run longer than 180 − T min uncut at
**both** lines: about 174 min for a trip T of about 6 min (ASSUMPTION; read it as 150-179 min). Your
97.5-min run on 2026-10-26 could not have anchored even with no cut. No code change alone lets a run of
under about 3 h anchor.

**Since 2026-10-28** each transit record's `anchoring`, and the status's and page's bars, show per
method and group how much of 180 − T the current uncut stretch has run (T the latest measured median,
or 0 while none is measured) and how many of the 3 enclosed anchors it has. Only an anchored count
reads 100 %, and only while anchors checked it in the last 60 min (`CHECK_MAX_MIN`; at night it
falls back to waiting, at most 99 %, as its Now is then not sure). A count carried in and confirmed
by 3 anchors that follow it is anchored too, with fewer than 3 enclosed. Cuts at line A matter as much as at B: in the daylight run of 2026-10-27 line A was cut 40
times in an hour, each time by a segment that expired before it was read or frames more than 5 s
apart. ADR-0017 takes the CPU's load as the cause (ASSUMPTION) and moves YOLOX to the GPU.

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
measured span, while A's lane-5 counted about 1.75 a minute (lane-4 was typed normal then; it is
SENTRI too since 2026-10-27, so A's SENTRI rate was at least that). SENTRI cannot anchor while that holds.
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

### Q-34. Free flow on the queue cards (ADR-0018)

On 2026-10-27 at 13:45 local your page showed minutes on an empty street at PDN: "under ~18" on a lane
with no line, and "line 51 m+" for a still 10 m blob 41 m into the view (ASSUMPTION: moved shadows
or something learnt and since gone; no frame of that column was checked)
([ADR-0018](../architecture/adr/0018-queue-card-with-the-booth-out-of-view.md), Proposed). Built on
2026-10-28 while you were away, each as its default:
- **(i) The crosswalk in the street view.** `pdn-south-street` drops a line starting more than 18 m
  into the view, and a crosswalk sits about 23-27 m in. Does a signal or a control hold northbound cars
  there? If so, such a line would read "no line" on the card, and
  combine's tail before A would be a floor (not known) where it would otherwise time the line from
  its own head. **Default:** nothing holds cars there.
- **(ii) 18 m or 15 m?** On the logged hour, 15 m also removes the 19:59 UTC right-column line. With
  dropped runs as a floor ((vii), as built) the tail stays unknown in 18 records per lane type at
  either gap; read as free road it would be 2 at 15 m against 4 at 18 m. 15 m sits exactly on
  Lerdo's one real 15 m clear head (07:16 local, 2026-10-19). **Default:** 18 m, on `pdn-north` and `pdn-south-street` only.
- **(iii) Lerdo and BOTA** have no camera wait. Should their cards also show the line only, with no
  minutes, and should they get a head gap once their real heads are logged? **Default:** both kept as
  they were: (D + L) / v on a card with a line, no head gap.
- **(iv) Is anything metering northbound cars between the Av. Juárez view and line A** (a toll collected
  on the Juárez side, a signal, a control point)? It decides whether combine's "metered before A" stays.
  Dropping it would turn a line held there into a known 0 (on the repo's own simulation, a 30-min tail
  became 0). **Default:** it stays.
- **(v) Day and night references.** A reference learnt by day never refreshed at night: false 75 m and
  50 m+ lines in 16 of 16 windows (ADR-0018). Options:
  - **(a)** relearn with no held reference once when the light changes, or keep a day and a night
    reference per lane, chosen by the frame's brightness. A fresh night reference read no false line.
    Its dawn behaviour is unmeasured, and PDN had a queue at 03:16 local on 2026-10-19 (one night;
    ASSUMPTION that it is typical), so a relearn during a queue could learn cars;
  - **(b)** absorb into the reference what stood still for 30 min (fed by regular reads only, never on
    a lane whose booth is in view, and reporting the lane as not measured for the next 30 min where
    that removed a line). On renders it hid 0 of 18 stop-and-go reads; a line standing wholly still for
    about 30 min would be learnt as road;
  - **(c)** nothing: start each run in a new `CAMERA_STATE_DIR`, in the light it will run in.

  How long do PDN's lanes stand fully still outside closures? That sets (b)'s span. **Default:** (c),
  as the 3.5-hour night run of 2026-10-28 did.
- **(vi) ADR-0018 itself.** Accept it, so PDN's cards show measurements only and the minutes come from
  the camera wait? **Default:** as built, Proposed.
- **(vii) A run the head gap drops, in the camera wait.** combine counts it as not known (a floor at
  0) in the tail before A, never a known 0, unless the dropped run ended short of line A
  (`combine._dropped`). ASSUMPTION: a run far into the view may be a real line held there. On the
  free-flow hour the review replayed, this keeps more unknown tails than reading such runs as free
  road would: 18 of 56 records per lane type against 4, and 16 with no head gap at all (replayed
  2026-10-28). A dropped run on another lane type's strip that reached the view's near edge counts
  as that type's line running on, as a kept one would, and a tail whose street runs were all
  dropped says "if it is a line", not that one is there. Or count it as free road, a known 0?
  **Default:** a floor.

### Q-35. YOLOX on the GPU: the limits of its checks (ADR-0017)

You chose the GPU where it runs better (2026-10-27). Its conditions came from the measurement's review
([ADR-0017](../architecture/adr/0017-yolox-on-the-gpu.md)); these limits are defaults, each built:
- **(i) The running check.** One frame in 30 (`CHECK_EVERY`, every 15 s at 2 fps) is also detected on
  the CPU, and 3 differing checks within 20 (`DIFFER_LIMIT`, `DIFFER_WINDOW`) stop the GPU path. A
  difference means: count, class or order differ, a box corner moves more than 0.1 px, a score more
  than 1e-3 or across 0.4, or a raw output more than 1e-3. ASSUMPTION: a real difference is a rare
  threshold flip (estimated 0.12-0.7 per 1,000 frames), so one alone does not stop a long run, while a
  broken GPU differs on every check and is stopped within 45 s. Check more often (costing CPU: one
  check is about 0.8 CPU-s), or stop on the first difference? **Default:** as built.
- **(ii) After a mid-run stop.** The stop is process-wide: every camera whose YOLOX is on the GPU (both,
  under `auto`) is a yolox gap for the rest of the run, never restarted on the CPU. Or start a new CPU
  counter at each, its records labelled `cpu`? **Default:** the gap.
- **(iii) The device label** is `gpu:<adapter name>`, without the driver version, which is only logged.
  GPU against GPU was bit-exact on one driver (617.42). Add the driver to the label? **Default:** no.
- **(iv) A replay of a log with two devices** is refused unless `--device cpu|gpu` picks one. Or grade
  both side by side in one pass? **Default:** refuse.
- **(v) Loop records** carry no device, since no YOLOX path touches a loop count. **Default:** yolox
  records only.
- **(vi) The `gpu` extra** pulls onnxruntime-directml's listed dependencies (flatbuffers, protobuf,
  sympy, mpmath), which running the model does not need; the measurement installed it with
  `--no-deps`. Say `--no-deps` in the install line? **Default:** a plain install.
- **(vii) ADR-0017 itself.** Accept decision 2's limits ((i), (ii)) and decision 4 ((iii) to (v)) as
  built, so the whole ADR reads Accepted? **Default:** as built, Proposed.
- **(viii) The name `unlogged`.** A replay's `--device unlogged` picks the records that name no device
  (logs from before the device was recorded); a live run refuses it. ASSUMPTION: the name is the
  review's choice, not yours. **Default:** `unlogged`.

### Q-36. The anchoring bars and the lane labels (2026-10-28)

Progress bars were asked for on 2026-10-28, for future runs. Built as defaults:
- **(i) The bar's trip T** is the median of the latest 15-min window holding at least 3 unflagged
  transits, else 0 (which asks for the longest stretch, 180 min). Its needed minutes give T back
  (180 − needed) on the terminal and the page, even while `show_minutes` is false. Acceptable?
  **Default:** as built.
- **(ii) Lane labels.** A queue card now names a lane by its count zone's type, "lane-4 · SENTRI", filed
  as nothing (`cbp_lane` stays null), with one note on `pdn-north` that lane-4 and lane-5 are one SENTRI
  lane split by barrels. **This reverses Q-22's "not labelled SENTRI" on the card.** That rule's reason,
  that the card's number is (D+L)/v, no longer holds at `pdn-north`, whose card shows no minutes since
  ADR-0018. Keep the label, or go back to "(lane type unconfirmed)"? **Default:** keep it. The split is
  written in `viewer.py` (`SPLIT_LANES`). Should it be a `cameras.json` field instead (a
  `calibration.py` change)? **Default:** `viewer.py`.
- **(iii) When a bar reads 100 %.** A count reads anchored only while anchors checked it in the last
  60 min (`CHECK_MAX_MIN`); after that it reads waiting, at most 99 %, with the text of a run not yet
  long enough. A count transit itself calls sure but "continued" also reads waiting. Should the bar
  follow transit's sureness (`_SURE`) exactly, and should anchors that have gone unchecked get their
  own text ("anchors last checked N min ago")? **Default:** no to both, as built.
- **(iv) A camera falling behind.** A stream that is down or a picture that repeats leaves the bar as
  it was until frames move again; the page flags it stale after 120 s. Should the bar also read
  broken when a camera's frames are older than `LAG_ALERT_S` by the wall clock? **Default:** no.

### Q-37. Does `lerdo-south-booths` time the wait to CBP, or to the bridge trust's toll gate?

`cameras.json` takes `lerdo-south-booths` (`CYhISA_wFQ8`, "Lerdo (Sur)") as booths in view
(`booth_distance_m: 0`), with the direction inferred southbound and not confirmed. The camera survey
of 2026-10-27 (about 13:14 local) found it looks **south over northbound traffic**: two vehicles drove
toward the camera to the gates in four crops 3 s apart, every pavement arrow points at the camera, its
on-screen label says "Vista Sur", and the bridge trust labels the stream "Línea Exprés (Vista de Norte
a Sur)". If these gates are the trust's own toll gates for the SENTRI lane (an inference), a wait read
there ends at the toll gate, not at CBP, and the component doc's "complete wait" of 1.0-7.6 min
(2026-10-19) measured that.
- **(a)** It is the toll gate: give the camera a measured booth distance to CBP (its minutes then
  become approximate), or keep it as a toll-gate queue only, never a crossing wait.
- **(b)** It is CBP's booths: confirm the direction (`direction_verified: true`).

**Default:** nothing changed; its readings stay as they are, now marked in doubt in the component doc.

### Q-38. Cameras at the other bridges

The survey of 2026-10-27 found live public cameras at every crossing but Santa Teresa. Only PDN has a
camera on both sides; nothing else was configured.
- **(i) Queue cards for Zaragoza and Tornillo?** `KX-bm-Lhy3w` (Zaragoza (Norte), about 4 northbound
  lanes, the view in which you labelled column D SENTRI, Q-22) and `0gT7jvaLCkg` (Guadalupe (Sur), Tornillo, 4
  booth lanes toward the camera) were live, with a clear counting line. Cards need only `cameras.json`
  entries: no City feed, no code and no ADR. They show queue and speed, with approximate minutes from
  map distances (to CBP, never 0 at Tornillo's toll plaza), never a crossing time. **Default:** not
  added until you say.
- **(ii) A Zaragoza pair.** Its only live City feed, `BridgeZaragoza3`, is labelled "Norte a Sur". Does
  it show northbound cars near the CBP booths, and which lanes are SENTRI? Only you may look, on the
  City's own page (Q-21); we never fetch or show a City picture for this. If it does, Zaragoza could be
  the next pair after PDN (ROADMAP 6.12), through a new ADR. **Default:** no look, no pair.
- **(iii) BOTA** answered "technical difficulties" on all three streams on 2026-10-27, and Stanton's
  City feeds returned 404, so neither can pair. **Default:** nothing to do.

### Q-39. The loop counter's learnt road across runs

The GPU smoke run of 2026-10-28 started at 11:04 UTC in a fresh state folder, with morning traffic
already standing at line B (`city-pdn-north`). A loop zone learns its empty road only from moments
when it is near empty (`loop.py`: `EMPTY_MIN_SAMPLES` of them), and a lane queued since the counter
started has none. While any of a line's zones learns, the loop's count at that line is a gap. So the
loop was blind at line B for **55+ min**: every loop record from 11:05 to 11:59 UTC (55 of 55) read
broken, with no epoch and no count, and the run was still going when we looked (11:59 UTC). The
saved roads (`city-pdn-north.loop.npz`) held col-1 and col-2 at 11:44 UTC, and col-1, col-2 and
col-4 at 11:54 UTC, so col-3 was still learning; the night run's file holds all four. The night
run of 2026-10-28 started at a quiet hour, and its learning ended after 5 s. YOLOX's stretch at both lines ran unbroken meanwhile
(39 min unbroken at 11:43 UTC).

Until 2026-10-28 nothing in the records named the cause: the runner moves a measurement's start
past a gap open to the end, so the span was empty and `epochs`, `gaps_a` and `gaps_b` were all
`[]`. The bar said only "a line is unseen now", and the card "count not anchored (None)". They now
say "line B unseen now (loop zones learning) since 11:04 UTC" (`anchoring.unseen`, built that day).

`seed()` keeps learnt roads across restarts of one state folder. Test runs start in a fresh folder
on purpose, so that the queue reader learns that night's or day's road (Q-34 (v), ROADMAP 6.11), and
the loop's roads start afresh with it.
- **(a) Default, as built:** keep fresh folders, and accept the loop gap at a line that starts in
  traffic. The loop gives nothing there until each zone's road has been near empty often enough,
  which on a busy morning can be most of a run.
- **(b)** Seed only the loop's roads (`*.loop.npz`) from an earlier run's folder, and keep the queue
  reader's references fresh. This carries the risk `loop.py` names: a road learnt wrong (a standing
  queue learnt as road, or a road learnt in other light, Q-31) counts short while `states()` says
  "counting", with no gap, until about 32 min of empty road replace it (`AGREE_REFRESHES`).
- **(c)** Day and night loop roads, as ROADMAP 6.11 plans for the queue reader's references: seed the
  road saved in the same light, and learn afresh otherwise.

ASSUMPTION: col-3 (and col-4 until about 11:50 UTC) stayed unlearnt because its lane stood queued,
not for another reason. No frame was looked at, and which zones are learning is not logged (the
status names the camera only; the saved roads file shows which have learnt). ASSUMPTION: a run started at a quiet hour learns within minutes, as the night run did;
that is one run.

Which: (a), (b) or (c)? And does a blind loop at one line matter while YOLOX counts there?

---

*Answers recorded 2026-10-12 on branch `chisme-border`; Q-21 on 2026-10-24 and Q-22 on 2026-10-26 on
`chisme-border-waittime`. Q-23 to Q-26 raised 2026-10-26 with ADR-0015; Q-27 the same day, with the lane-type split.
Q-28 to Q-33, and the open point in Q-24, raised 2026-10-26/27 from that evening's run and the night's
live check, with ADR-0016 and the review of methods that are not measured. Q-22's line A corrected
2026-10-27. Q-28 (v) extended and (ix) added, Q-29's progress note, and Q-34 to Q-38 raised
2026-10-28: from the free-flow review (ADR-0018), YOLOX on the GPU (ADR-0017), the anchoring bars and
lane labels, and the camera survey of 2026-10-27. Q-39 raised the same day, from the GPU smoke run's
blind loop at line B.*
