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
  type.
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
count them as SENTRI: in a rendered check (2026-10-26), a loop over col-e alone counted each col-4
car passing in front of it 3 times. So `cameras.json` names col-e as counted by yolox only
(`"methods": ["yolox"]`), and the loop does not measure SENTRI: its SENTRI wait reads "not
measured". A 15-min hand count at col-e lifts that.

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
is separate.

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

---

*Answers recorded 2026-10-12 on branch `chisme-border`; Q-21 on 2026-10-24 and Q-22 on 2026-10-26 on
`chisme-border-waittime`. Q-23 to Q-26 raised 2026-10-26 with ADR-0015; Q-27 the same day, with the lane-type split.*
