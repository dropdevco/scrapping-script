# ADR-0014: Crossing time is measured between a Mexican-side and a US-side camera, by two methods side by side

- **Status:** Accepted, 2026-10-24, by the product owner
- **Date:** 2026-10-24
- **Amends:** [SPEC.md §1](../../border-estimator/SPEC.md) "No machine learning" and the camera doc's "no
  model" (`docs/components/border-camera-reader.md`). Both now apply to the **fusion**, which stays
  deterministic ([ADR-0013](0013-weighted-fusion-of-wait-signals.md), not superseded). One **sensor** may now
  use a pinned, pretrained vision model.
- **Keeps:** frames are analysed in memory and never stored by the worker (QUESTIONS.md Q-7). CBP's
  published number is never an input to a camera number.
- **References:** QUESTIONS.md Q-21; the plan and evidence below.

## Context

On 2026-10-22 the camera estimate was checked live against CBP's published number for 16 minutes.
The estimate is wait = (map distance to the booth + visible line) ÷ visible speed, used for the
cameras that cannot see the booth:
- PDN north read 99-128 min against CBP's 47;
- BOTA read 288-513 min, then 69-93, against 65;
- PDN south read low.

CBP is not the truth either: its feed marks every El Paso car lane "Manual", meaning an officer's
estimate. Today's analysis named the likely causes, but nobody can say which dominates. They are:
- the queue out of view behaves differently from the part in view (lanes fan out, merge and leave gaps);
- the speed reader may read low;
- the camera scales are unverified.

A measured crossing time sidesteps all of these. On 2026-10-23 a research workflow found where one can be
measured:
- **CBP has no public camera.** The public US-side video is the City of El Paso's "Bridge Cameras" page
  (https://www2.elpasotexas.gov/misc/externally_linked/bridges/cameras.html, HLS on
  zoocams.elpasozoo.org). The owner confirmed this is the site they meant.
- **It covers Paso del Norte and Ysleta-Zaragoza.** Stanton's streams return 404, and BOTA and
  Tornillo have none. Zaragoza 1 was down again on 2026-10-24. So PDN northbound is the one pair today:
  FPFCH `pdn-north` on the Mexican half, City `bridgepdn1` on the US half.
- **No City view shows the CBP booth up close.** Line A to line B is part of the crossing. The street
  tail before A and the stretch after B still come from the queue reader.
- **Timing can be exact.** YouTube playlists carry a PROGRAM-DATE-TIME for their segments, and the City
  server stamps each 5-second segment with Last-Modified. Measured delivery delays of 2.6-19 s therefore
  drop out if every frame is stamped with its capture time, never its arrival time. Today's reader does
  the opposite: it times frames by counting them and stamps a reading when the read ends.
- **Matching each car by its looks is weak here.** Across viewpoints, published re-identification falls
  from 92 % to 34-68 % at full resolution. Our cars are 50-70 px wide at line A, about 3 in 4 are a
  neutral colour, colour is lost at night, and lane types mix. **Counting** cars at two lines and reading
  the transit from the gap between the two running totals is far easier. A few distinctive cars matched
  for certain keep the totals honest.

## Decision

1. **Measure crossing time at PDN northbound** between a line on FPFCH `pdn-north` (A) and a line on City
   `bridgepdn1` (B). Transit = the horizontal gap between the running counts at A and at B. Cars between
   the lines now = the vertical gap.
2. **Two methods, run side by side on the same frames, logged separately, compared before either is
   trusted:**
   - **Deterministic:** a virtual loop on each lane strip. A car is counted when the short zone goes from
     occupied to clear, judged against the learnt empty road the queue reader already keeps. No model.
   - **Vision model:** the YOLOX-S detector, run through OpenCV. Car, truck and bus boxes are tracked
     from frame to frame, and a track is counted when it crosses the line. Rules for the model:
     - weights `yolox_s.onnx` from Megvii's 0.1.1rc0 release, Apache-2.0;
     - sha256 c5c2d13e…998063, pinned in code; any other file is refused;
     - run locally on CPU, never trained or fine-tuned on our data;
     - the same frames give the same counts.
3. **Every frame is stamped with its capture time from the stream itself:**
   - YouTube: PROGRAM-DATE-TIME plus the frame's PTS;
   - City: Last-Modified, fitted to PTS over many segments.

   Never the arrival time, never a frame count, never this PC's clock (it runs 1.46 s slow, unsynced).
   Reading gaps are recorded as gaps, never papered over.
4. **No licence plates, ever.** No plate or face is read or cropped, and processing runs at reduced
   resolution. Per-vehicle data (a box, a colour signature) lives in memory for at most the longest
   believable wait (3 h). Only counts and transits per window are written.
5. **The City feeds:** the owner accepts the risk of reading them automatically (no terms are posted, and
   the page shows a maintenance banner). They are **never displayed**: not embedded, not proxied, no frame
   or overlay of them on our pages. We identify ourselves honestly (our normal User-Agent) and never
   disguise the client to get past a block. If the City blocks us, we stop.
6. **Measured transits are for grading and calibration first.** They become a live signal only through
   a later decision. A completed transit is one wait old, and SPEC's 10-minute half-life for measured
   signals would weight a 47-minute transit at about 4 %.

## Alternatives considered

| Alternative | Why not (now) |
|---|---|
| Re-identify every car between the cameras (the original idea) | Rear-to-front and low resolution make individual matches unreliable. Counting needs no identity, so matching is kept to a few certain anchors |
| Deterministic only | The owner wants a model-based method beside it. Running both on the same frames shows which is right where (night, stop-and-go) |
| Model only | It cannot be checked without a second, independent count. It also adds a binary the project cannot inspect |
| Plates (most accurate matching) | Personal data; legal status in Mexico and Texas unknown. Refused by the owner |
| Ask TTI / the City for measured data first | Still planned. It is not a reason to wait: the data may never come, and both methods are needed for the bridges and lanes TTI does not cover |
| GPS-logged team crossings | Planned for later (owner, 2026-10-24). They grade both methods end to end |
| Keep `L / v` with map distances | It reads 0.5-8x away from CBP, for causes nobody can yet separate |

## Consequences

- **SPEC §1 changes.** The "no machine learning" rule now governs the fusion and the published number,
  and one sensor may use the pinned detector.
- **New moving parts:**
  - an HLS reader of our own;
  - a 36 MB model file outside the repo, at `~/.chisme/models/`, checked against its hash;
  - continuous reading of two streams instead of 60 s every 2-5 min. That raises the YouTube load that
    Q-7 accepted.
- **Coverage.** Only PDN northbound has a camera pair. Zaragoza needs FPFCH's Zaragoza bridge streams
  back, and Stanton needs the City's.
- **Drift.** Counts drift. Transit error grows with (count bias at A − count bias at B) × hours since the
  last anchor. Both methods must be checked against hand counts, and later GPS crossings, before any
  number is published.
- **A 90-minute ceiling.** Signatures are kept at most 3 h. An anchor is only trusted when both ends of the
  trip lie inside the measured span with the ceiling to spare, so the slowest trip from A to B that can be
  anchored is 90 min, not FHWA's 120. A→B is the middle of the bridge, not the whole wait (ASSUMPTION that
  it stays under that). Found by the integration review on 2026-10-25: at 120, no trip under 60 min, PDN's
  ~47 among them, could ever anchor.
- **Stream failures are gaps.** These are all gaps at that line, never zero cars:
  - an encoder restart (a timeline cut);
  - a frozen picture;
  - a reconnect;
  - a segment that expired before it was fetched;
  - a counter still learning.

  If the City refuses our client (HTTP 401/403), that camera is no longer read.
