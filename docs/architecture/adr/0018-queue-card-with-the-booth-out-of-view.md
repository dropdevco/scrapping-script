# ADR-0018: With the booth out of view, a queue card shows no minutes for an empty lane, and only the line where the camera wait times the road

- **Status:** **Proposed.** Built on 2026-10-28 while the owner was away, each part as its default,
  and replayed on the logged free-flow hour of 2026-10-27 only; nothing has run live. Decision 3
  took effect later the same day, when `calibration.Camera` began reading `max_head_gap_m`. The
  owner's choices are QUESTIONS.md Q-34.
- **Date:** 2026-10-28
- **Builds on:** [ADR-0015](0015-camera-wait-from-cars-ahead.md), which rejects (D + L) / v with map
  distances as a wait, and whose camera wait times PDN's road from the back of the line to the
  booths. It is unchanged, as is its rule for what the camera wait shows (`combine.display`).
- **Changes:** the component doc's queue-card row "`under ~20 min`" (`worker.lane_summary`), and the
  queue rule in `measure._queue_run` (a per-camera `max_head_gap_m` in `cameras.json`).
- **References:** QUESTIONS.md Q-34; `docs/components/border-camera-reader.md`, "What the viewer
  shows"; the module docstrings of `worker.py` (`lane_summary`) and `measure.py` (`_queue_run`). The
  replays below are one-off scripts over the logged readings, not kept in the repository.

## Context

On 2026-10-27 at 13:45 local the owner's page showed minutes on an empty street at Paso del Norte
(`camera estimate`, the run of 12:58-14:01 local). The free-flow review replayed that run's logged
hour, 19:05-20:01 UTC: 203 lane readings and 63 camera records. Three causes:

- **A ceiling on an empty lane.** With no line in view and the booth out of view, the card showed
  "under ~N": (distance + uncertainty) at the slowest recent queue speed among the camera's lanes, ×
  1.25 while the scale is unverified. At 13:45 the street's card read "under ~18":
  (510 + 75) m ÷ 0.6592 m/s ÷ 60 × 1.25 = 18.49, at the speed of one 12 m right-column window read at
  19:33 UTC that was itself a false line. On that hour 26 cards with no line showed 18-41 min. It is
  (D + L) / v with L = 0 at a borrowed speed, the form ADR-0015 rejects, and it can never show free
  flow.
- **Still blobs far into the view read as lines.** The 14 lines of that hour started 0, 41, 41, 41, 27,
  28, 40, 20, 20, 1, 23, 39, 18 and 32 m into the view. The left column's "line 51 m+" at 13:45 was a
  10 m blob at 41-50 m with 41 m of empty road in front of it; on a frame checked at 19:51-19:55 UTC
  the right column's mismatch lay on shadows, and the left column's blob was not seen. A line far
  into the view with free road before it is not waiting for the booth. The camera wait's tail then
  took such a street line, with every bridge lane clear, as a line held before line A ("metered
  before A"), and left the tail unknown in 16 of the 63 records.
- **A midday reference that never refreshed.** The road was learnt around solar noon, and after that
  no sample passed the refresh check (0 of 121 frames and 0 of 18 samples at midday, 0 of 151 at
  night), so moved shadows and anything learnt and then gone stay "occupied". Against those references,
  on 2026-10-28 00:34-00:50 local, lane-5 read a false 75 m line and the street's right column a false
  50 m+ line in 16 of 16 one-minute windows each. A reference learnt fresh that night read 0 false
  lane-windows (0 of 20 on the street, 0 of 50 on the bridge).

Every PDN strip has its booth out of view: 365 ± 35 m (`pdn-north`) and 510 ± 75 m
(`pdn-south-street`), map estimates. That road is the camera wait's to time (ADR-0015). `pairs.json`
has one pair: `lerdo-north` and `bota-south-approach` have their booths out of view and no camera wait.

## Decision

1. **No line, booth out of view, on every camera: "no line", no minutes.** The line under it names the
   road on to the booth and what times it: "the 365 m to the booth are out of view: the camera wait
   times them" on a camera that `pairs.json` names as a pair's `a` or `street` (`pdn-north`,
   `pdn-south-street`), and "…; nothing here times them" on any other (`lerdo-north`,
   `bota-south-approach`). With no booth distance it says "booth distance not measured". The "under
   ~N" ceiling is gone, with its `upper_bound` key and the slowest-speed input. The published
   `no_queue` signal is unchanged: it had no minutes already.
2. **A line, booth out of view, on a camera of a pair: its length and speed, no minutes.** The card
   shows "line 51 m+" and "moving v m/s", or "not moving over the last N readings", and says the
   camera wait times the road on. `lerdo-north`, `bota-south-approach`, booth-in-view lanes and lanes
   with no booth distance keep their minutes as before. The lane's published signal still carries
   (D + L) / v (`signal_from_roll` is unchanged). It is filed under no CBP lane while the strip's
   `cbp_lane` is null, and a test fails if any strip of `pdn-north` or `pdn-south-street` gets one, so
   typing one must drop those minutes first.
3. **A per-camera head gap** (built). `max_head_gap_m` in `cameras.json`, 18 m on
   `pdn-north` and `pdn-south-street` only: with the booth out of view, the nearest run of queued
   metres that would be a line is no line if it starts further into the view than that; a blob
   nearer the booth end, too short or sparse to be a line itself (a parked object), does not carry
   it. The reading keeps the run it dropped as `detail.far_run_m`. The gap is counted from the first
   metre in view, as the booth-in-view rule counts its own. ASSUMPTION: 18 m is fitted to that one
   hour, with one real point beside it: `lerdo-north`'s queue at 07:16 local on 2026-10-19, whose
   first 15 m had just cleared. A camera that does not set it has no limit, as before. combine
   reads a reading whose run was dropped as a floor in its tail before A, never as a known 0
   (`combine._dropped`; ASSUMPTION, Q-34(vii)), unless the dropped run ended short of line A.

## Alternatives considered

| Alternative | Why not |
|---|---|
| Keep "under ~N" | (D + L) / v with L = 0 at a borrowed speed. It read 18-41 min on empty lanes and can never show free flow |
| A drive time for the unseen road | About 1-2 min for 510 m at an assumed 15-30 km/h: unmeasured, and well below CBP's published 8-10 min for standard and Ready lanes that afternoon (free-flow review). The booth, most of a free-flow crossing, is seen by no camera |
| Line-only cards at Lerdo and BOTA too | Not taken: they have no camera wait, so their cards would carry no figure at all. The owner decides (Q-34) |
| One head gap for every camera | Not taken. 18 m is fitted to one PDN hour, and Lerdo's one real clear head (15 m) is 3 m under it. On renders, queues filling each lane from its booth end were never dropped (0 of 44 line windows; the largest head was 5 m), but the reader also missed most rendered lines on the street's right column (7 of 8 windows) and on `lerdo-north` (6 of 8), so renders cannot validate it there |
| Absorb into the reference what stood still for the last 18 samples (about 12 min; the review's F3) | Rejected as proposed. On renders it hid real stop-and-go lines: over 64 min, 14 of 18 reads (0 on today's rule). Right after a learning cycle its window is about 3 min, not 12, because learning puts 15 samples a read into the bank. Where the booth is in view a standing line would become a published 0. A slowly moving shadow edge defeats it as it defeats today's rule. A 30-min span fed only by regular reads hid 0 of 18; it is left with day and night references as the follow-up (Q-34 v) |
| Drop the "metered before A" tail (the review's F4) | Rejected. On the repo's own METERED simulation (a line held before A, truth 34.0 min, 30.0 of it before A) it turns a known 30.0-min tail into a known 0: the floor falls from ≥ 30.9 to ≥ 0.9 min. The false street lines it would have cleared are mostly decision 3's: with it, the street blocks the tail in 4 records, not 16 |
| Apply that rule on the cards instead (no minutes for a street line while the bridge is clear) | More code across cameras in the worker, and decision 2 already shows no minutes there |

## Consequences

- **Replayed on the logged hour** (19:05-20:01 UTC, the same 203 readings, the code before and after):

  | | Before | After |
  |---|---|---|
  | Cards with minutes | 35 | 0 |
  | Minutes on lanes with no line | 26 | 0 |
  | Lines (decision 3 passed in by hand) | 14 | 3 |
  | Published signals with minutes (decision 3 passed in by hand) | 9 | 3 |

  The 0s need only decisions 1 and 2. Without decision 3, as wired today, the 14 lines stay, shown as
  length and speed. With it, every card at 13:45 reads "no line".
- **PDN's cards show measurements only.** Their wait comes from the camera wait, which needs `camera
  estimate` running. Under `camera view` or `camera run` alone no PDN card shows minutes.
- **Lerdo and BOTA keep (D + L) / v on cards with a line**, approximate, as nothing else times their
  road (Q-34 iii).
- **What decision 3 leaves.** The 3 lines it keeps on that hour (the right column at 19:33, 19:57 and
  19:59 UTC, starting 0, 1 and 18 m in) still show as lines. It does nothing for false lines that
  start at the booth end, such as lane-5's at night against a day reference.
- **What decision 3 costs.** A stop-and-go clearance more than 18 m long at a real line's head drops
  that line for one reading. A line held inside the street's view, at a crosswalk about 23-27 m in,
  reads "no line", where combine would otherwise time it from its own head (`_street`, `from_head`).
  Whether anything holds cars there is Q-34 i. `far_run_m` keeps such a run findable in the log.
- **"Metered before A" stays.** With decision 3, the hour's 4 records with the street blocking the tail
  (19:57-20:00 UTC) stay unknown: the honest answer for an unconfirmed line (Q-34 iv).
- **The day reference lock is not fixed here.** A run started on a reference learnt by day reads false
  lines at night (16 of 16 windows above), and the reverse is expected at dawn (ASSUMPTION). Until day
  and night references exist, a run learns its road in a new `CAMERA_STATE_DIR`, as the 3.5-hour night
  run of 2026-10-28 did (ROADMAP 6.8).
- **Interface.** `worker.lane_summary(est, lane, roll, observed, *, scale_verified=True,
  camera_wait=False)`: `slowest_speed` is gone, and the two flags are keyword-only, so an old caller
  fails rather than setting one by position. `kind` loses `upper` and gains `clear` and `line`.
  `worker.camera_wait_cameras()` reads `pairs.json` once, when the Worker starts.
  `measure._queue_run(share, seen, booth_in_view, max_head_gap_m=None)`.
- **Calibration:** `calibration.Camera.max_head_gap_m`, read from `cameras.json` (a negative one is
  refused). `test_border_camera_crossing.py`'s geometry check leaves the field out, as it leaves out
  the notes.
- **Tests:** `test_border_camera_viewer.py`, `test_border_camera.py` (`QueueRuns`, `HeadGap`,
  `HeadGapField`) and `test_border_camera_combine.py` (`TailTest`'s two head-gap cases).
