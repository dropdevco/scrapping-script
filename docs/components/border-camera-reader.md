# Component: Border Camera Reader

`src/scraper/border/camera/` reads **queue length and queue speed** off the bridges' live
camera streams and turns them into a wait estimate, as a `measured` signal for the estimator
([SPEC §5.1](../border-estimator/SPEC.md#51-measured-queue-length--queue-speed-deterministic)).

The queue reader is **deterministic**: fixed image computations, no model, nothing random. The
same frames always give the same answer, so any reading can be reproduced from a recording. It
also measures **crossing time between two cameras** ([below](#crossing-time-between-two-cameras),
ADR-0014). That uses two counters side by side; one of them uses a pinned YOLOX-S model.
`camera estimate` runs both in one process and turns them into each counting method's camera wait
for PDN northbound, shown only to the owner on this PC ([below](#combined-estimate-camera-estimate),
ADR-0015).

| | |
|---|---|
| **Runs** | On a machine we own (Q-3), continuously: `python -m scraper.border camera run` |
| **Reads** | The FPFCH YouTube live streams (`youtube.com/@fpfch/streams`), found by title. The City of El Paso's bridge cameras (HLS on `zoocams.elpasozoo.org`), for crossing time only: read at the owner's risk and **never displayed** (ADR-0014). The viewer also fetches CBP's published waits each cycle, **for display only**: they never feed the camera estimate |
| **Writes** | JSON signal lines on stdout; each lane's empty-road reference, `status.json` (what the viewer shows, including `cbp`, `cbp_at`, `cbp_error`) and a heartbeat in `CAMERA_STATE_DIR` (default `~/.chisme/camera`). `camera transit` writes `transit-<date>.jsonl` and each camera's learnt loop roads (`<camera>.loop.npz`) there too. `camera estimate` also writes `queue-<date>.jsonl` and `camera-<date>.jsonl`, and `--replay` writes `camera-replay-<name>.jsonl` |
| **Install** | `pip install -e ".[camera]"` (OpenCV headless, numpy, yt-dlp); for YOLOX on the NVIDIA GPU, `pip install -e ".[camera,gpu]"` (onnxruntime-directml 1.24.4, Windows only; `gpu.py`, `--device`, [ADR-0017](../architecture/adr/0017-yolox-on-the-gpu.md)); Node on `PATH` for yt-dlp's YouTube extraction; the **ffmpeg** command-line tool on `PATH` (the stamped HLS reader). For the model counter: `yolox_s.onnx` from Megvii's YOLOX 0.1.1rc0 release in `~/.chisme/models/` (sha256 `c5c2d13e…998063`, checked on load) |
| **Decision on record** | Reading YouTube streams is against YouTube's terms; the product owner chose to do it (QUESTIONS.md, Q-7). Frames are analysed in memory; the worker and the viewer **never write one**. Only an operator's explicit `camera snapshot --out` or `camera read --debug` writes a single annotated frame, to check a calibration. For a `display: false` camera (the City's) both refuse before opening anything, unless `--calibration` is passed. With the flag, they print the rule: the frame is only for drawing a counting line (owner, 2026-10-26, Q-21), stays outside the repository (a path inside it is refused before any stream opens), is never committed and is deleted after. |

---

## Run it

Needs Python 3.11+ and Node.js on `PATH` (yt-dlp uses it to read YouTube). From the repo root:

```bash
python -m venv .venv
.venv\Scripts\activate                     # macOS/Linux: source .venv/bin/activate
pip install -e ".[camera]"
pip install -e ".[camera,gpu]"               # optional: YOLOX on the NVIDIA GPU (ADR-0017)
python -m scraper.border camera list         # every calibrated camera, and whether its stream is live
```

**First, once per camera, learn the empty road** from moments spread over time (an hour here):

```bash
python -m scraper.border camera reference --camera lerdo-south-booths --rounds 6 --gap 600
```

**Or just open the viewer.** It learns the road by itself for any camera that has none (three
readings, about six minutes at the default two-minute cadence), then shows every bridge's wait live:

```bash
python -m scraper.border camera view          # http://127.0.0.1:8090, every calibrated camera
```

Or from the command line:

```bash
python -m scraper.border camera read --camera lerdo-south-booths            # one 60 s reading
python -m scraper.border camera read --camera pdn-north --debug pdn.jpg      # plus a picture of what it saw
python -m scraper.border camera run --cameras pdn-north,lerdo-south-booths  # the always-on worker
```

`run` prints one JSON signal per lane every 5 minutes, writes `heartbeat.json` and the learnt
references to `CAMERA_STATE_DIR` (default `~/.chisme/camera`), and alerts Telegram when a stream
goes down or comes back if `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` are set. Only one worker
(`run` or `view`) can use a `CAMERA_STATE_DIR` at a time; a second one refuses to start.

Every calibrated lane now has a `booth_distance_m`, so every lane's signal can give minutes.
`lerdo-south-booths` sees its booth, so its distance is exact. The other four cameras' distances
are **map estimates** (2026-10-22) with a ± and `booth_distance_verified: false`, so their
minutes are shown as approximate (see [What the viewer shows](#what-the-viewer-shows)). On the
cards, PDN's lanes (`pdn-north`, `pdn-south-street`) show their line and its speed instead of
minutes, because the camera wait times their road to the booth ([ADR-0018](../architecture/adr/0018-queue-card-with-the-booth-out-of-view.md), Proposed).

Every live frame is stamped with **when it was captured**, from the stream's own clock (`hls.py`):
- YouTube: PROGRAM-DATE-TIME plus the frame's PTS;
- the City: segment Last-Modified, fitted over segments. A segment with no Last-Modified, or
  one that cannot be parsed, is placed by the others. How long the server takes to write a
  segment after its last frame is unknown (0-5 s, ASSUMPTION) until a timed crossing calibrates
  it.

It is never stamped with when it arrived. On 2026-10-23 arrival trailed capture by 2.6-19 s,
and OpenCV's own reader delivered 180 frames in 0.45 s. A segment missed or expired is a gap,
and no pair of frames is timed across one. A reading's `observed_at` is its last frame's capture
time.

---

## How a wait is read

```
frame ──► image grey + edges ──► warp each lane into a strip (booth at row 0, 8 px per metre)
                                  └► grey *structure*: the strip minus a 1 m blur of itself
                                   │
          ┌────────────────────────┴───────────────────────┐
          ▼                                                ▼
   per metre: occupied?                          per 12 m segment: how far did the
   (structure or edges differ from the           strip's profile move toward the booth
    empty-road reference)                        since the last frame? (correlation,
                                                  then 1-D Lucas-Kanade for the fraction)
          │                                                │
          └──────────────── over a 60 s window ────────────┘
                                   ▼
   queue  = run of metres occupied ≥ half the window, gaps ≤ 9 m, at least 60 % of its
            metres queued and occupied ≥ 67 % of the window on average. With the booth in
            view it must start at the booth end; otherwise any such run ≥ 10 m counts, up to
            the camera's max_head_gap_m into the view where cameras.json sets one (see
            below). Its length is always counted from the strip's booth end
   speed  = time-averaged speed of what is inside the queue, stationary cars included
            (the worker publishes a 15-minute rolling average of it)
   wait   = (distance from strip to booth + visible queue) ÷ speed
```

Four choices in that pipeline were made on real footage, not in advance (the fourth from a logged
hour's readings):

- **Structure, not brightness.** At night the empty road has no single brightness: the
  headlights of whatever is queued nearby light it. The same empty Paso del Norte (Sur) street
  measured 86 to 121 grey levels from one 20-minute round to the next. The strip minus a 1 m
  blur keeps what a vehicle adds (outline, windows, lamps) and drops what light does. The blur
  is in strip metres: a blur fixed in image pixels was tried and filtered away the body of a car
  close to the camera.
- **The empty road is learnt from the emptiest moments.** A median over one clip learnt a
  waiting queue as road. A median across rounds still learnt cars where the lane was queued
  three rounds in five. The reference is re-learnt from the samples that differ least from it
  (three passes). When an hour holds no empty moment, the reference already held is kept.
- **A queue is dense.** Static clutter (a vendor's cart, people standing in the street) made
  islands that the gap rule chained together. Every real queue in the labelled rounds was at
  least 70 % queued metres; every false one was under 50 %.
- **A line starts near the view's booth end, where the camera says so.** `max_head_gap_m` in
  `cameras.json`, 18 m on `pdn-north` and `pdn-south-street` only: with the booth out of view, the
  nearest run that would be a line is no line if it starts further into the view than that (a blob
  nearer the booth end, too short or sparse to be a line itself, such as a parked object, does not
  carry it), and the reading logs the run it dropped as `far_run_m`
  ([ADR-0018](../architecture/adr/0018-queue-card-with-the-booth-out-of-view.md)
  decision 3). In the free-flow hour of 2026-10-27 (19:05-20:01 UTC), 11 of 14 lines on empty lanes
  started 20-41 m into the view; the 3 left start 0, 1 and 18 m in. ASSUMPTION: 18 m is fitted to that
  hour, with one real point beside it (`lerdo-north`'s 15 m clear head at 07:16 local, 2026-10-19).
  It also drops a line held inside the street's view (a crosswalk about 23-27 m in: Q-34), which
  combine would otherwise time from its head: combine counts a dropped run as a floor in its tail
  before A, never a known 0, unless it ended short of line A (`combine._dropped`; ASSUMPTION,
  Q-34(vii)). In effect since 2026-10-28, when `calibration.Camera` began reading the field.

A photometric correction (matching the scene's brightness outside the lanes) was also built
and scored. It made the result worse and is not used.

That last line is a travel time for one lane: a car joining the tail covers the queue's length,
and the road on to the booth, at the queue's average speed. With the booth out of view, the road
between the view and the booth is taken to move like the line in view (ASSUMPTION: uniform
speed along the whole stretch; nothing measures the part out of view). Two consequences:

- **When the booth is in view, scale cancels.** A calibration whose metres are 1.5× too long
  stretches length and speed equally, so the wait is unchanged (pinned by a test).
- **When the booth is out of view it does not.** The distance to the booth is in real metres and
  the speed is in calibrated metres. Four of the five calibrations are in this case (all but
  `lerdo-south-booths`): the cameras are on the Juárez side and the CBP booths are on the US
  side. Their range is wider for two reasons. First, the distance is `booth_distance_m ±
  booth_distance_uncertainty_m`. Second, while `scale_verified` is false, the low end is
  divided and the high end multiplied by a further 1.25 (`SCALE_SLACK`; ASSUMPTION: nobody has
  timed a scale error, so 25 % has no measured basis).

The range is `measure.wait_bounds`:
- **low** = (distance − uncertainty, never below 0, + queue) ÷ the fastest speed;
- **high** = (distance + uncertainty + queue) ÷ the slowest.

Each end then moves by the 25 % scale slack when that applies. A one-minute window takes its
fastest and slowest quarter-minute speeds. The 15-minute rolling wait, which is what gets
published and shown, has one averaged speed, so its range reflects only the distance's ± and
the scale slack, not how much the speed varied. Neither range covers how fast the road out of
view moves: that is the visible speed, by the assumption above.

### States a reading can report

These are the states of the **signal** a reading publishes. The viewer can still show a number
for some of the "unknown" ones; see [What the viewer shows](#what-the-viewer-shows).

| `state` | Meaning | Wait in the signal |
|---|---|---|
| `queue` | A dense queue in view | `(booth distance + queue) / speed`. When the queue runs past the far edge of the view there is no `high`, and with the booth in view the wait is a **floor** (`lower_bound: true`). With the booth out of view it is not: the unseen road is timed at the visible speed, and a line that merges and opens gaps out there moves faster |
| `no_queue` | No line at this camera: nothing queued or, with the booth out of view on a camera that sets `max_head_gap_m`, only a run starting further into the view than that (kept as `detail.far_run_m`; combine reads it as a floor, not a 0) | `0` if the booth is in view; **unknown** if not, because a queue could sit between the view and the booth |
| `stalled` | Queued, but it did not move over the whole window | unknown; `lower_bound: true` |
| `insufficient` | Fewer than 20 frames, or too few motion samples | unknown |

`camera run` and `camera view` publish the **15-minute rolling** wait, with that rolling wait's
own range. The one-minute window's range is never used, so the minutes always fall inside the
published `low`–`high`. A queue running past the view is published with its `low` and no
`high`; it is a floor only with the booth in view. `camera read` prints a floor as
`at least {low} min`.

### What the viewer shows

The viewer (`camera view`) gives a number whenever one can be stood behind, and says what kind
of number it is. `worker.lane_summary` decides; the page only prints it. With the booth out of view
it shows no minutes where nothing in view can be timed, nor on a camera whose road on to the booth
the camera wait times ([ADR-0018](../architecture/adr/0018-queue-card-with-the-booth-out-of-view.md),
Proposed, 2026-10-28). Which cameras those are is read from `pairs.json` (each pair's `a` and
`street`: `pdn-north`, `pdn-south-street`) when the Worker starts.

A lane with no CBP lane is named by its camera's count zone of the same lane name: `lane-1 · normal`,
`lane-4 · SENTRI` (`pdn-north`). Where no typed zone exists (`pdn-south-street`, Lerdo, BOTA) it keeps
`(lane type unconfirmed)`. It is a label only: `cbp_lane` stays null on every strip, so no reading is
filed as SENTRI. `pdn-north`'s card says once that lane-4 and lane-5 are one SENTRI lane, split by
barrels near the camera (`viewer.SPLIT_LANES`, shown only while `cameras.json` types both
`sentri`). The labels come from the Worker's cameras; a page that reads `status.json` from file has
none.

| Shown | When | How |
|---|---|---|
| `12 min` | A queue, booth in view | The 15-minute rolling wait. With the booth in view the scale cancels, so it is never marked approximate |
| `≈ 12 min`, range `9–16` | A queue, booth out of view, on a camera with no camera wait (`lerdo-north`, `bota-south-approach`) | The rolling wait and its range. It is always approximate: the line under it says "road out of view assumed to move like the line in view", plus a map-estimated distance or an unverified scale where those apply. A line running past the view has no high end, so no range is printed, and its minutes stop at the edge of the view: the line under it also says "the line past the view not counted" |
| `≥ 9 min` | The line runs past the edge of the view, booth in view | A floor, stated at its range's **low** end, not at the central wait |
| `3 min` (or `≈ 3`) with "this minute's line" | A line in view, but too few motion samples this minute to time it (booth in view, or a camera with no camera wait) | This minute's line, timed at the last 15 minutes' speed. Never an older line's wait: a short line at the booth is always too short to time, and showing a long line's wait from ten minutes ago would be wrong exactly when the line has cleared |
| `0 min` | No queue, booth in view | — |
| `no line` | No queue in view, booth out of view | No minutes. The line under it names the road on to the booth and what times it: "the 365 m to the booth are out of view: the camera wait times them" on a camera of a pair (`pairs.json` `a` or `street`: `pdn-north`, `pdn-south-street`); "…; nothing here times them" elsewhere (`lerdo-north`, `bota-south-approach`). Until 2026-10-28 this was `under ~N min`, (distance + uncertainty) at the slowest recent queue speed: (D + L) / v with L = 0, the form ADR-0015 rejects; on 2026-10-27 it read 18-41 min on empty lanes |
| `line 51 m+` | A line, booth out of view, on a camera of a pair (`pdn-north`, `pdn-south-street`) | Its length, and its speed or that it is not moving; no minutes: the wait is the camera wait's (ADR-0015). Its signal still carries (D + L) / v (`signal_from_roll`), which is filed under no CBP lane while the strip's `cbp_lane` is null; a test fails if one is typed. `lerdo-north` and `bota-south-approach` keep `≈ N` (Q-34) |
| `—` and a reason | No booth distance with a line in view (with no line: `no line` and "booth distance not measured"); too few frames; and, with the booth in view or on a camera with no camera wait, a queue that has not moved in its recent readings, or a line too short or too still to time with no recent speed on that lane (speed is timed only on 12 m segments wholly inside the line, so a line under 12–18 m is never timed) | — |

Under each bridge, CBP's own published waits are printed for comparison. They are fetched in
parallel with the cameras, with one short attempt, and never feed the camera number.

A camera whose reading fails for any reason shows **down** (the stream) or **error** (anything
else, such as a yt-dlp timeout) and is retried next cycle; the others carry on.

A stream that fails is never a reading. It raises instead (ADR-0011):

| Failure | Cause | Seen |
|---|---|---|
| `StreamDown` | Listed on the channel but YouTube will not serve it ("We're experiencing technical difficulties"), or not listed / not live | 7 of 16 FPFCH streams on 2026-10-19 08:16 UTC |
| `StreamFrozen` | With a burned-in clock (the YouTube cameras): the clock stopped changing for 20 s. Judged over the window (at most 10 % of the pairs changed it), not frame by frame, because a frozen stream is still re-encoded and every keyframe changes the picture a little. Without one (the City's whole scene, read at 2 fps or less; `stream.frozen_check` refuses faster): at least 14 of the last 40 pairs (20 s) repeat exactly at 320×180 ([ADR-0016](../architecture/adr/0016-frozen-when-the-picture-repeats.md)). A freeze re-encoded with noise is not caught here: the runner's no-motion gap takes it back ([below](#crossing-time-between-two-cameras)). `StreamFrozen.seconds` (20) says how far back the runner takes it back | City, 2026-10-26: 0 of 2362 live pairs repeated exactly (21:32-21:52 local), while the earlier whole-scene rule (at most 10 % of pairs change by 0.3 grey) fired 50.4 times an hour that night and made every cut at line B; the evening's 10.5 cuts an hour at B are attributed to it by their size (records then gave no reason) |
| `StreamLost` | Frames stopped arriving mid-read | — |

A channel's stream list is cached for 10 min (`LISTING_TTL`). It is dropped at once when yt-dlp
refuses a listed stream's URL, or when a read finds the playlist gone (404/410) or ended, so a
stream restarted under a new video id is found on the next try (`stream.forget`; ADR-0016 decision
7). Two restart paths are not covered: the new stream not yet listed when the channel is listed
again (that listing is then kept for up to 10 min), and an old playlist that stalls instead of
ending (Q-28).

`camera run` alerts Telegram once when a camera stops and once when it comes back, and keeps
retrying. One camera failing, for any reason, never stops the others or the worker.

---

## Crossing time between two cameras

`python -m scraper.border camera transit` (ADR-0014) measures how long cars take from a counting
line on one camera (A) to a counting line on another (B). Today the only pair is Paso del Norte
northbound:
- **A** is on FPFCH `pdn-north`, the Mexican half of the bridge: a 1.5 m band across all five
  strips (four lanes), 66-67.5 m from the strips' booth end. lane-4 and lane-5, right of the white
  posts, are `sentri`: one SENTRI lane split by the orange barrels near the camera, kept as two zones
  so the loop does not read two cars abreast as one. lanes 1-3 are `normal` (Q-22, corrected by the
  owner 2026-10-27).
- **B** is on the City of El Paso's `bridgepdn1` (camera `city-pdn-north`, `display: false`), the
  US half.
  - **How it was drawn:** redrawn on 2026-10-26 from two daylight frames, by the owner's leave, with
    columns per Q-22.
  - **The zones:** col-1..col-4 (`normal`) are the columns left of the white posts. col-e (`sentri`)
    is column E, between the posts and the right railing.
  - **Where they sit:** about 10-13.5 m from a camera about 3-4 m up. Both figures come from a rough
    camera model; nothing was measured on site.
  - **Not yet hand-counted.** `cameras.json`'s notes give the limits:
    - only 5-9 m of road before each zone;
    - col-e not yet confirmed by a car in it;
    - the loop counter does count a car again in the next column, because seen this low a car's body
      covers the zone beyond it. In a rendered check (2026-10-26, synthetic frames, script not kept
      in the repo), a loop over col-e alone counted col-4 cars that passed in front of it and never
      on its ground: 3 times each with lengths read from square pixels, and still 2 for 6 cars with
      the zones' `metres` (re-run 2026-10-27). So col-e is counted by yolox only
      (`"methods": ["yolox"]`), and the loop does not measure SENTRI until col-e is hand-counted
      (Q-24);
    - the City has no ground rectangle, so each column carries `"metres": [3.5, 3.5]`: the
      width is `loop.ASSUMED_LANE_M` and the length is what the rough model drew (both
      ASSUMPTION). Read with square pixels instead, no two columns came out the same length (col-1
      about 1.4 m, col-e about 6.7 m), and the loop did not judge a car across a column line
      (`loop._beside`). Now col-1..col-4 are judged in pairs, as line A's lanes are, only because
      their metres are equal (ASSUMPTION, not measured). A car whose body covers the next column in
      the picture is still counted there: it reads wider than `CAR_MAX_M`. So the loop's normal
      count at B also counts col-1..col-3 cars again in col-2..col-4, by how much is not measured
      (ASSUMPTION, not yet checked by a render or a hand count). That over-count stays inside the
      group, so it reads as drift, not as another group's cars. The loop's normal stays measured,
      but cannot vote before the normal hand count at B (Q-24); if that count fails, col-2..col-4
      get `"methods": ["yolox"]` too, which leaves the loop measuring nothing at PDN;
    - a zone's `"metres"` count only on a camera with no ground rectangle. The order is: metres
      given to `LoopCounter`, then the camera's ground rectangle, then the zone's own metres, then
      square pixels. On a calibrated camera a zone's metres are not used, and the loop warns that
      they are not. No camera has both today, so no count changed (2026-10-27; before, zone metres
      silently won, against the code's own comments).

Both cameras are read continuously. Every frame goes to **two counters**, and their results are
never mixed:

| Method | How a car is counted | Known limits (rendered scenes and simulations) |
|---|---|---|
| `loop` (`loop.py`, deterministic) | A virtual loop per zone: each car's back edge is followed through the zone against a learnt empty road, and counted when it crosses the far edge | 0 missed and 0 false in rendered stop-and-go and crawls with 1-2 m gaps. With staggered starts, as real queues start (each car 1-2 s after the one ahead), 2 of 1522 missed and 5 false. But the rendered cars are flat, so no car hides the road. From `pdn-north`, which sees the traffic from behind, a stopped car hides roughly 1.5-2.5 m of road ahead of its bumper at line A (ASSUMPTION: camera pose from the unverified 10.5 × 30 m rectangle, typical car shapes). With that road painted over, the loop counted cars with 1.1 m of road showing and none with 0.5 m, so stopped gaps under about 2.5-3.5 m may read as one car. That result does not carry over to line A until line A is hand-counted. Cars 0.5 m apart read as one. Half the resolution misses most. At line A as drawn it misses cars in free flow from about 5 m/s: about 30 % at 5-6 m/s with random gaps (a 3 m zone missed none up to 6 m/s). Learns the empty road from near-empty moments only, and stays "learning" (a gap) until the lane clears. A road learnt wrong (a queue standing still through the first learning, a repair patch, a nudged camera) is replaced only after about 32 minutes of one unchanging picture (ASSUMPTION: no queue stands wholly still that long); until then the zone counts short while it says "counting" (rendered: 27 of 54 missed). A car already in a zone when counting resumes after any gap came in unseen and is never counted, as with `yolox`. No gap is reported for it, since `transit` sets the next epoch's offset afresh (in simulation, a gap for it made no unflagged number closer to the truth) |
| `yolox` (`detect.py`, YOLOX-S) | Car, truck and bus boxes in the zones' tiles, tracked frame to frame, counted when a track crosses the far edge | Not trusted above 8 m/s. At line A as drawn, short from about 6 m/s: in simulation with its 9.1 m of road in view, 8 of 87 lost at 8 m/s and 19 of 95 at 12 m/s (with 18 m in view, 0 and 2 of 96). Not trusted in creeping queues until a hand count at line A settles it: in `detect.py`'s simulation at 0.5 m/s, boxes jittering by 1.5 px at `pdn-north` counted 9 of 66 cars twice and lost 18 (14 % short); with jitter of 2 % of a box and 1 box in 4 missed, 0 twice and 2 lost. How much real YOLOX boxes jitter has not been measured. On the CPU 0.3-0.34 CPU-s a model pass on 2026-10-24 (`detect.py`), and 0.41 at 4 threads in ADR-0017's measurement of 2026-10-27/28; line A at `pdn-north` takes two passes a frame (0.82 CPU-s in that measurement). On the GPU (`gpu.py`, [ADR-0017](../architecture/adr/0017-yolox-on-the-gpu.md)) about 0.006 CPU-s a pass, plus a CPU re-check of one frame in 30 (about 0.8 CPU-s a check, 0.027 CPU-s a frame on average); the same boxes as the CPU on every live frame measured, but not bit-identical, so its counts are never graded with the CPU's |

Line A as drawn is 1.5 m long with about 9.3 m of road (nominal) in view below it, which breaks
`detect.py`'s rule for drawing zones (a car seen whole in 3 frames before the zone) above about
6 m/s. Both counters therefore run short at A in free flow, and a transit reads that as drift.
Free-flow speeds at line A are unmeasured (ASSUMPTION that they reach 5-6 m/s). Owner task, from
a daylight frame: redraw line A about 3 m long with about 18 m of road in view below it. The
runner then relearns its loop roads by itself: a road saved for a zone, frame size, ground
rectangle or mask since changed is dropped on start, with a WARNING.

Transit = the horizontal gap between the running counts at A and B (`transit.py`). The cars
between the lines at the start are unknown. They come from **anchors**: a few distinctive cars,
matched in FIFO order by colour and value (the signature's first three parts; its width share is
measured differently at the two cameras and is not compared), and by vehicle class where the
counter knows it (`yolox` does, `loop` does not). Every gap cuts the count into a new epoch, and
no transit spans one. Every one of these is a gap, never "no cars":
- a reconnect;
- an expired segment;
- a timeline cut (encoder restart);
- a frozen picture, taken back as far as the reader's rule judged it (`StreamFrozen.seconds`,
  20 s);
- nothing moved at the line for more than 300 s (`QUIET_S`, ASSUMPTION): no frame whose picture
  moved (`hls.Stamped.moved`: the clock ticked, or the whole scene changed by 0.3 grey) and no car
  counted there. Once something moves again, the stretch from the last frame that moved becomes a
  gap, "nothing moved for N s", and the cars in it are cut out. So does a shorter one that held a
  `StreamFrozen`: a fresh read's check raises only on a full window, so the end of a freeze read
  after a reopen was never taken back;
- for `yolox` at each camera on the GPU (the stop is process-wide), from its frame before the GPU path
  stopped (`GpuFailed`) to the end of the run: "YOLOX stopped: …" ([ADR-0017](../architecture/adr/0017-yolox-on-the-gpu.md));
- frames more than 5 s apart, or a stamp that does not move forward, found by the counters
  themselves;
- for `loop`, a zone still learning its road.

Across a gap the count is carried (the cars seen, plus each line's rate times the time unseen),
flagged, until the new epoch's anchors confirm it. A confirming anchor must lie near the median of
the last 5 anchors kept before it (near the carried count before any), so the check follows the
counters' drift: held at the carried count, it kept 8 of 45 true anchors at about 20 cars an hour
of drift (simulation, 2026-10-26). A count is flagged too once no anchor (or given
hand count) has checked it for `CHECK_MAX_MIN`, 60 min (ASSUMPTION): each transit by when its car
crossed A, and each window or cars-between reading by its time, even while older anchors in the
same measurement keep the epoch "anchors".

A line counts as seen only up to its last frame that moved or counted a car (`moved_at`, kept by
the runner across reopened streams), not up to its latest frame
([ADR-0016](../architecture/adr/0016-frozen-when-the-picture-repeats.md)). The reader no longer
catches a picture frozen with noise, and a written record is never corrected, so such a stretch is
held rather than written as empty road. The hold is a lag:
- at line B on 2026-10-26 21:32-21:52 local, the time from one moving pair to the next was p50
  0.52 s, p99 8.4 s and at most 16.5 s;
- with that night's motion bursts thinned to a half and a quarter (ASSUMPTION: bursts scale with
  traffic), p99 was 49 s and 112 s, the longest 216 s and 407 s;
- where nothing moves for minutes, as may happen in a standing queue (ASSUMPTION, not measured),
  the line stays that far behind: stale, never an empty road.

A noisy freeze shorter than 300 s is seen again as a quiet road; the earlier rule caught it in
20-30 s (Q-28). After `StreamFrozen` after `StreamFrozen`, the runner waits 10, 20, 40 s and so on,
up to 300 s (`RECONNECT_MAX_S`), before opening the stream again. It goes back to 10 s only after a
read gave 60 s of frames (`RESET_AFTER_S`, ASSUMPTION); a read that ends any other way leaves the
wait as it is.

Signatures are kept at most 3 h, and an anchor sets an offset on its own only when both ends of
its trip lie inside the measured span with 90 min (`HI_MIN`) to spare (it is then enclosed). 90
is the largest ceiling at which every trip up to it can be enclosed at all. That is necessary,
not enough: in an epoch of L min, a trip of T min is enclosed only for anchors that crossed A in
a stretch T + L − 180 min wide, so at most T min, and in one measurement a short trip seldom has
the 3 enclosed anchors (`MIN_ANCHORS`) it needs to set the count. In simulation (one 3 h
measurement, 1 in 10 cars distinctive), 2-min trips set it in 0 of 10 runs, 3-min in 2 of 10 and
5-min in 7 of 10. A short trip's anchors can still confirm the count carried from the
measurement before, which the runner chains from tick to tick: a count anchored while trips fell
from 40 min stayed sure, and within 3 cars, once they were 1 min (6 of 6 runs). From a cold
start, 1- and 2-min trips stayed flagged for 3-182 min past the first 3 h, and in 2 of 13 runs
for all 4 h simulated after it (simulation only, 2026-10-26: first in first out, 3-10 % of cars
distinctive, an ASSUMPTION for PDN). A trip longer than 90 min cannot anchor, since its true
partner is outside the search. Its epoch then comes out flagged ("few-anchors" or "carried"), not
anchored wrong (ASSUMPTION, simulation only: with 110- and 150-min trips, 0 of 160 epochs sure
and wrong, against 60 before the 2026-10-26 audit). That A→B stays under 90 min is an ASSUMPTION
until a hand-timed or GPS-timed A→B exists. If the City refuses our client, that camera stops.

What is written: one record per method and lane-type group per minute to
`CAMERA_STATE_DIR/transit-<date>.jsonl`.
It holds counts, rates, transit statistics per 15-minute window, epochs, each line's unseen
stretches (`gaps_a`, `gaps_b`: the minute each began, its length in seconds as the epochs were cut,
that is padded and merged, and its reasons), how many times each line's stream was opened again
since the runner started (`reconnects_a`, `reconnects_b`), how far its latest epoch has come towards
anchoring and which line is unseen now and why (`anchoring`, below), and, on a yolox record,
which path counted at each camera (`devices`, below): derived numbers only. A window with fewer
than 3 transits gets no median or percentiles. That ties no written time to a car, but it is not
anonymity: with 3, the median and percentiles give the three times back. Times that could be a
car's own are cut to the minute (`seen_until`, `now.at`, an epoch's edges, a gap's start). A gap's
reasons can name a segment number ("segment N expired before it was fetched"), never a car.

`lane_type` is the group the record measures. Each lane type that zones at both lines carry is
measured on its own, from its own lanes' crossings and with its own carried count. At PDN those are
"normal" and "sentri" (Q-22). A lane is not measured if it has no type, or if the other line lacks its
type; a warning names it at start. Only a pair whose lines share no lane type is pooled, as "mixed".
`runner.py` can also leave one method off a group when a zone of that group names the counters that
count it and that method is not among them (`cameras.json` `"methods"`; `not_measured`: a warning
at start, "not measured" in the status, no record). At PDN, col-e is counted by yolox only, so the
loop measures "normal" and yolox measures both: a tick gives 3 records, not 4. The camera wait lists
the loop's SENTRI as "—, not measured: city-pdn-north col-e is counted by yolox only", and that
group cannot vote. The loop's "normal" at line B is expected to run high, because a col-1..col-3
car is counted again in the next column (above; ASSUMPTION, not yet checked by a render or a hand
count). That count cannot vote before the normal hand count at line B (Q-24).

Each record measures at most the
last 3 h, carrying the cars-between count from the measurement before where that count can be
checked. So each window says which half of it is complete: take a window's `by_exit` from a
record where `by_exit_complete` is true, and its `by_entry` from one where `by_entry_complete`
is true. Over a 3 h horizon (2 × `HI_MIN`) no window is both, so these always come from
different records. A tick that cannot measure logs a WARNING and starts that method and
group afresh next tick. Signatures and single crossings live in memory for at most 3 h: 3 h of stream time
before the newest frame, plus the wall time since that frame arrived, so they expire even while
both streams are down. No plate or face is ever read.

This is **not the full wait, and not a bound on it**:
- A→B covers the middle of the bridge only, not the street tail before A nor the stretch and
  booth after B.
- It is read by exit: it is the A→B time of cars that crossed A about one transit earlier. In a
  queue that is clearing, a car entering now takes less.
- SENTRI (A lane-4 and lane-5, B col-e) and normal (A lanes 1-3, B col-1..col-4) are measured apart
  (Q-22).
  Normal pools CBP's standard and Ready (ASSUMPTION), so within it the pairing holds only while
  those two do not reorder between A and B. The rest of this point applies to a pooled ("mixed")
  pair. The pairing of the
  k-th car at A with the (offset + k)-th at B holds within one lane type only. Pooled, given the
  true count, a window's median comes out near the flow-weighted mean of the types' trips, the
  trip of none, and p10-p90 is not their spread: in simulation, with half the cars at 7 min and
  half at 45, medians were 23.5-29.1 min. The runner gives no count. Anchored, the offset follows
  one type, the one that supplies most enclosed anchors, not most anchors: enclosure favours the
  longer trip but does not settle it. With a quarter of the cars at 12 min and the rest at 45,
  and the 12-min cars 4 times as likely distinctive (they supplied most anchors in 12 of 12
  runs), the offset followed the 45-min type in 4 runs (the cars between 42-56 too high), the
  12-min type in 3 (140-160 too low), and came out flagged in 5. With both types equally likely
  distinctive, it followed the 45-min type in 8 of 12 runs (13-72 too high) and came out flagged
  in 4. Every anchored run was unflagged; where its windows were read, their medians were 41-47
  or 7-11 min, against a flow-weighted mean near 37.
- An error in the offset, or drift, moves it either way.
- Until GPS-timed crossings grade it, it is for grading and calibration. The owner chose to see it
  on this PC, never in public (ADR-0015 decision 2, accepted 2026-10-26). There it is shown only as
  part of the camera wait and through its display rule ([below](#combined-estimate-camera-estimate)).
  It grades the queue reader only on the same stretch (ADR-0014).

---

## Combined estimate (`camera estimate`)

```bash
python -m scraper.border camera estimate     # --port 8090 --every 60 --queue-every 120 --methods loop,yolox --device auto|gpu|cpu
python -m scraper.border camera estimate --replay transit-<date>.jsonl --queue queue-<date>.jsonl [--methods loop,yolox] [--device cpu|gpu|unlogged]
```

`camera estimate` ([ADR-0015](../architecture/adr/0015-camera-wait-from-cars-ahead.md)) is one process
that runs the queue reader and both crossing counters. It gives each counting method's camera wait for
PDN northbound, per lane-type group, side by side and never averaged.

**What runs (`estimate.py`).**
- **Locks.** It holds `transit.lock`, and through its Worker `worker.lock`. So `camera view`, `run`
  and `transit` refuse while it runs, and it refuses while any of them runs.
- **The counters.** `TransitRunner(pdn-north, city-pdn-north)`, as `camera transit` runs it, with both
  methods by default.
- **Where YOLOX runs.** `--device auto|gpu|cpu` on `camera estimate` and `camera transit` (default
  `$CHISME_YOLOX_DEVICE`, else `auto`: the GPU when it passes `gpu.py`'s checks, else the CPU with a
  warning; [ADR-0017](../architecture/adr/0017-yolox-on-the-gpu.md)). An explicit `gpu` the GPU
  refuses stops the command before anything is read, with a message, and lets go of the locks. A GPU
  path that stops mid-run (`GpuFailed`: a failed run, a bad output, or 3 differing CPU re-checks
  within 20) stops for the whole process (`gpu.STATE`): every camera whose YOLOX runs on the GPU
  drops its YOLOX counter at its next frame, for the rest of the run: its line is a yolox gap
  ("YOLOX stopped: …") from its frame before, never counted on the CPU instead, and the loop goes on.
  Before 2026-10-28 such a stop fell into the reader's catch-all and reopened the stream on every
  frame, darkening both methods. Each yolox record carries `devices`: per camera `device` (`cpu` or
  `gpu:<adapter>`), on the GPU `cpu_checked` and `cpu_differed`, and after a stop `stopped` and
  `stopped_at` (to the minute). So does the camera record's yolox entry.
- **Anchoring progress.** Each transit record carries `anchoring` (`transit.anchoring`):
  - `state`: `anchored` (the latest epoch's count set by anchors, or given), `waiting`, or `broken` (a
    gap at either line runs to the end of what is measured);
  - `unbroken_min`, the latest epoch at both lines;
  - `needed_min`: 2 × 90 − T, with T (`transit_min`) the median of the latest 15-min window holding at
    least 3 unflagged transits, at most 90; with none, T = 0 and `transit_assumed`;
  - `percent`, rounded down, at most 99 until anchored and 0 when broken;
  - `enclosed` and `min_anchors`;
  - `unseen` (since 2026-10-28): while broken, each line whose unseen stretch runs to the end, as
    `{"line": "B", "since": "<minute>", "reasons": ["loop zones learning"]}`, with the minute the
    stretch began (padded and merged as `gaps_a` / `gaps_b`, not clipped to the record's span) and
    every reason merged into it; `[]` whenever the state is not broken, so every new record has
    the same keys. It is needed because the runner moves a measurement's start past a gap open to
    the end: the span then lies wholly inside it, and the record's `epochs`, `gaps_a` and `gaps_b`
    are all empty. Seen live on 2026-10-28 from 11:04 UTC, when line B's loop zones were learning
    the empty road with traffic standing on it (Q-39). Records written before then have no
    `unseen`, and are read as before.

  The status shows it as a bar under each count line, and the page as a bar per group and method,
  with one note saying what it is. It says nothing of whether a wait is right.
- **The queue reader.** A `Worker` on `pdn-north` and `pdn-south-street`, with 60 s reads every 120 s.
  A `display: false` camera is never given to it. So `pdn-north` is pulled twice, once by the runner
  and once by the Worker (the owner's OK, 2026-10-26), and the City once.
- **The page.** The existing page on 127.0.0.1, with a "camera estimate" section fed from
  `/estimate.json`.
- **Threads.** `cv2.setNumThreads(4)`, the thread count `detect.py`'s timings were taken at.
  YOLOX on the GPU is the first relief if the CPU falls behind (ADR-0017); `--methods loop` is the
  fallback where there is no GPU path. It gives no SENTRI wait while col-e is
  counted by yolox only (Q-24): SENTRI then shows only "—, not measured".

**Each tick (every `--every` s):**
1. `runner.measure()`.
2. The runner's records are appended to `transit-<date>.jsonl`, as `camera transit` does.
   - Each record names its tick (`run`: the process start, cut to the minute; `tick`: a count), so
     a replay splits the log into the live ticks exactly. It also carries the run's `not_measured`
     (method -> group -> why, `{}` when there is none), so a replay lists the same methods as not
     measured.
   - When no method of the run measures any group (every (method, group) is in `not_measured`), no
     camera record is written. The page flags "No camera wait: no method of this run measures any
     group (...)" with the reasons, and the status says so, not "nothing measured yet".
3. `combine.combine(records, queue signals, pair, at, not_measured=runner.not_measured)` builds one
   camera record, appended to `camera-<date>.jsonl`. A method in `not_measured` is listed with its
   reason, never as a wait; it wins over a record of the same method and group, which then feeds
   neither `agree` nor the vote's reasons.
   - `at` is the records' `seen_until`, cut to the minute.
   - Only queue readings whose `observed_at`, cut to the minute, is not after `at` are used.
4. A status prints, ending with this tick's alerts (`  alert: ...`). Without Telegram set up, the
   terminal is the only place they show.

Every lane signal the Worker emits, error lines included, is appended to `queue-<date>.jsonl` with
`observed_at` cut to the minute. `QueueLog` keeps the last 30 min of readings in memory (`KEEP_S`):
the 15 min `combine` rolls over, plus 15 min for the counting lines to run behind the reader.

**`--replay`** combines logged files tick by tick (by each record's `run` and `tick`; an older record
by its `seen_until`) under the live rule, writes `camera-replay-<name>.jsonl` and prints
`combine.self_check`. It can differ from what a live tick wrote where a reading reached the log after
that tick, after a restart (the new process starts with no readings in memory), or across a UTC
midnight (one `--queue` file). It uses each tick's logged `not_measured`. For a record without it
(`camera transit`'s, or one written before it was logged), `not_measured` is rebuilt from
`cameras.json` `"methods"` as they are now and from `--methods` (give the live run's), for the
record's own kind of group only (a log of untyped "mixed" records gains no typed group). Where those
are not the live run's, the replay can differ in the methods it lists as not measured, and a method
they now refuse is listed so even where the run measured it. It reads files with or without a BOM,
and skips unreadable lines. A replay grades the yolox records of one path only (`replay_device`): a
log whose yolox records name more than one (`combine.record_device`: `cpu`, `gpu:<adapter>`, or
`device not logged` for older records) is refused unless `--device cpu` or `--device gpu` picks
one (`--device unlogged` picks the records that name none), and the others are listed as not
measured in their ticks ("counted on …; this replay grades …
only (--device)"). `self_check` grades each path apart (`yolox/<group> on <device>`). Here
`--device` never reads `$CHISME_YOLOX_DEVICE`.

**The wait (`combine.py`, pure).** For each group and method:

```
W = tail_to_A + N / μ + q_B · K_eff / μ
```

All of it is in that method's counted units (ADR-0015 decision 1).

| Segment | From | Not known (the total is then a floor) |
|---|---|---|
| tail→A | The queue reader. Line A lies 66.0 m from each `pdn-north` strip's booth end, with 9.0 m of run-up in view below it; both are computed at load from `cameras.json` through the ground homography (the design's nominal was 66 and 9.3). The tail is 0 while no lane's line, nor a run the head gap dropped from a lane (`far_run_m`), reaches A. Then (queue − 66 m) ÷ the lane's 15-min speed in view. Past the near edge: run-up, the 70 ± 83 m map gap, and the street's line. Across a group's lanes, the median lane's minutes, with the range from the lowest low to the highest high (ASSUMPTION: which lane a car joins is not known). The street's columns are not typed, so they count for every group (ASSUMPTION). With no strip of `pdn-north` past its near edge, a line on the street has free road to A and is timed from its own head (metered before A); when every street run is one the head gap dropped, the card says "if it is a line, it is metered before A", not that a line is there. Readings after the tick's time are not used | A lane read longer than 300 s ago (ASSUMPTION), no recent speed, a line stalled, or a line running past the street's view, a run the head gap dropped on a lane that reached A or on the street (`combine._dropped`, Q-34 (vii)), or the group's own lanes short of A while another lane type's run past `pdn-north`'s near edge (or, dropped by the head gap, reached it) with the street queued (whether this type's cars queue in that line is Q-27): a floor of 0 |
| A→B | N ÷ μ from the runner's record. The range is 2σ, with σ² = `offset_sd`² + (3 + \|drift\| × max(T, 15)/60)², and a rate term of 2/√n_B. The range is an ASSUMPTION until `self_check` has 30 windows | `offset_sd` not known (few-anchors, disputed, unknown), line B stalled (under 0.05 cars/min), or n_B ≤ 4, which gives no high |
| B→booth | q_B × K_eff ÷ μ, plus 0-2 cars per primary booth under the canopy | Today, always. It needs all of: a K_eff for that method, lane type and UTC hour in `pairs.json`; q_B from B's occupancy, which the runner does not record yet; and `primary_booths`. So every total is a floor today |

- `agree` is true when both methods have minutes within max(3 min, 20 %) (ASSUMPTION). No mean or
  combined key exists.
- Each group has `lane`: `car_sentri` for "sentri", and null for "normal" and "mixed".
- Each group also has `would_vote: false`, with its reasons:
  - the vote is undecided;
  - no CBP lane id;
  - lines not verified;
  - no hand count within 5 % at A and B;
  - A→B range not calibrated;
  - K_eff from fewer than 3 timings;
  - a tail unchecked by a timed crossing;
  - a method the runner does not measure for the group: "<method> not measured: <why>".
- While col-e is counted by yolox only, SENTRI has one method, so its `agree` stays None. The page
  then says "Agreement: — (not possible: loop not measured here)", not that both methods need
  minutes.
- No Signal is built.

**What the owner sees (`combine.display`, the one rule).**
- **Where:** the terminal status and the page show each method's wait per group, side by side.
- **Minutes** appear only when:
  - the count is sure: its source is anchors, continued or given, its `offset_sd` is known, and
    anchors checked it within 60 min;
  - and every segment is known.
- **Otherwise:**
  - a floor with a sure count shows "≥ N min" and why each segment is unknown;
  - an unsure count shows "—, count not anchored (source)", or "count not checked by anchors in the
    last 60 min", or, when there is no count at all, "no count of the cars between the lines". That
    is a line down at the live edge, or no epoch at all (the span wholly inside a gap, so the count
    has no source: before 2026-10-28 this read "count not anchored (None)"). Where the record's
    `anchoring.unseen` says, it goes on with which line and why, for example ": line B unseen now
    (loop zones learning) since 11:04 UTC". The A→B segment's reason says the same;
  - a method the runner does not measure for that group shows "—, not measured: <why>".
- **`show_minutes`** is true in `pairs.json`. Set to false, only "recorded, not shown" appears.
- **The page also shows**, per group:
  - each stretch through the same rule (A→B gated by its count; the tail and B→booth not);
  - the count's source and sd;
  - each method's anchoring bar and its line of text, with one note for the section saying what the
    bars are (`viewer.ANCHOR_NOTE`);
  - where YOLOX ran (`device_text`), and a flag once its GPU path stopped;
  - the flags;
  - the agreement and vote reasons;
  - CBP's published wait (SENTRI beside "sentri", standard and Ready beside "normal"). It is for
    comparison only, and never an input.
- **The terminal** is in ASCII (">=" and "-"), because this PC's cp1252 console cannot print "≥".
- **The status** has:
  - each stream's seconds behind live, errors and loop learning, then ", nothing moved for N s"
    while a line has been held 10 s or more, and ", reopened Nx" once its stream was opened again;
  - the counts and rates per method and group, with the epoch source and sd;
  - under each record's count line, its anchoring bar, for example `anchoring loop  normal
    [#####.....] 58% 101/174 min, anchors 1/3`. A count carried in and confirmed by the anchors that
    follow it reads `100%: anchored, the count carried in confirmed by distinctive cars that follow
    it`; anchors that have not checked the count for 60 min (`CHECK_MAX_MIN`, at night) read waiting,
    at most 99 %, as the Now is then not sure; a broken bar names the line unseen now and why, for
    example `broken: line B unseen now (loop zones learning) since 11:04 UTC; the stretch starts
    again once both are seen` (the page says the same; a record from before `unseen` keeps "a line
    is unseen now (a gap at A or B)"); and once YOLOX has stopped, a broken yolox bar reads
    `broken for the rest of this run: YOLOX stopped, its line unseen (YOLOX STOPPED)`. A stream down
    or a still picture keeps the bar as it was until frames move again (the page flags it stale
    after 120 s). Its needed minutes give T back (180 − needed),
    from unflagged transits only, even while `show_minutes` is false (Q-36);
  - where YOLOX runs at each camera (`yolox device: …`), and `YOLOX STOPPED at <camera>: <why>` once
    its GPU path has stopped;
  - one line per queue lane (whether it reaches A);
  - the process CPU.

  It prints no runner minutes ("now ~N", median transit).
- **The City picture is never shown.** `/overlay/city-pdn-north.jpg` returns 404.

**Alerts.**
- **City refuses us (401/403).** Line B stops for good and is never retried. From then on no transit
  or camera line is written, one alert says so, and the page shows no total.
- **A line falls more than 120 s behind live** (ASSUMPTION). One alert, and one more when it catches up.
- **YOLOX's GPU path stops.** The stop is process-wide: one alert per camera on the GPU (both, under
  `auto`), never cleared: its yolox count there is a gap for the rest of the run, never made on the
  CPU instead; the loop goes on.
- **A line's stream stops without a refusal.** Each tick re-measures its last minute (for up to 3 h,
  `SIGNATURE_TTL_S`). The page flags the wait "Stale: the slower counting line is N min behind live"
  once the record is more than 120 s (ASSUMPTION, `LAG_ALERT_S`) behind its tick. Times are cut to the
  minute, so that is 3 whole minutes or more. A line held at its last moving frame (nothing moved
  there for minutes, as may happen in a standing queue) raises the same flag while its stream is
  live, and the flag's text names only a stopped stream (Q-28). The alert above reads each line's
  latest frame, so the hold raises no alert.
- **A tick fails** (a full disk, say). It is logged and retried next tick. The page says "No camera
  wait this minute: the tick failed (<type>)" instead of keeping the last record.

Tonight's expectation: A→B unsure or flagged after dusk, loop "learning" at B while the US half stands
queued, and every total a floor.

---

## Calibrating a camera

A calibration lives in `src/scraper/border/camera/cameras.json`. **Nothing in it is guessed
silently.** Whatever was not verified says so, and travels with every reading.

1. Grab a frame and pick **four image points that are corners of a rectangle on the road**, in
   order: near-left, near-right, far-right, far-left. Two lane edges and two lines across them
   work well. Give the rectangle's size in metres (`width_m`, `length_m`).
2. Set `scale_verified: true` only if those metres were measured (a map, a survey, a known
   marking). Otherwise leave it `false`.
3. Define each lane as a strip of that plane: `across` and `along` are fractions of the
   rectangle, and `along` may run past 0 or 1 to cover more road. Set `booth` to the end the
   inspection booth is at (`far` or `near`).
4. `booth_distance_m`: the distance along the lane from the **strip's booth-end row** to the
   booth. That row is not always where it looks:
   - A far-booth strip may run on past a landmark. PDN north's strips run under and past the
     Feliz Viaje sign.
   - A near-booth strip starts in front of the camera, not at it.

   Measure to that row, or correct for the gap, and say how in the camera's `notes`. Use `0`
   when the booth is in view, and `null` when unknown: then no wait is produced, only queue and
   speed. An estimate (a map, not a survey) also gets:
   - `booth_distance_uncertainty_m`: its ±, which widens the range (`null` or missing means 0;
     negative values are refused);
   - `booth_distance_verified: false`, which marks the minutes approximate.

   If `booth_distance_verified` is missing, it defaults to true only for `0` (booth in view).
5. `cbp_lane`: the CBP lane type (`car`, `car_sentri`, `car_ready`, …) **once someone has
   confirmed it**. Until then it is `null`, and the signal's `lane` is `null` so fusion cannot
   file it under the wrong lane.
6. `mask` the overlays (logos, QR codes, the URL strip, signs that hide the road). Set `clock`
   to the burned-in date and time box.
7. Check it: `python -m scraper.border camera snapshot --camera <id> --out check.jpg` draws the
   rectangle (yellow), the lane strips (green, labelled at the booth end) and the masks (red). A
   `display: false` camera is refused unless `--calibration` is passed (Q-21). With the flag,
   `--out` (or `read --debug`) must point outside the repository, for example
   `--out "$env:TEMP\check.jpg"`: a path inside it is refused before any stream opens. Delete the
   frame once the line is drawn.
8. Learn the road from **moments spread across time**, including some when the lane is clear:
   `python -m scraper.border camera reference --camera <id> --rounds 6 --gap 600` takes six
   live samples ten minutes apart (or pass `--source` once per recorded clip). One moment is
   refused. The worker then rebuilds each reference on every reading, once its in-memory bank
   holds 18 samples, learning only from the emptiest of them. The bank keeps 36 samples, three
   per reading. That is about an hour at `run`'s 5-minute cadence, and about 24 minutes at the
   viewer's 2-minute cadence. A lane with no saved reference is learnt by the worker itself. Lanes
   already learnt keep theirs, and are never rebuilt from those few minutes of samples. A reference
   saved for a different calibration, or unreadable, is deleted and relearnt. A different
   calibration is any change of frame size, ground rectangle or its metres, the lane's bounds or
   booth end, or the mask, not only a different strip size; a reference saved before 2026-10-26
   has no record of these and is relearnt once. References are written whole and then moved
   into place, so a killed worker cannot leave half a file. Readings in the first hour after
   install are the least reliable: the one miss in the evaluation below (under the earlier
   rules) had a single 45-second clip of history.
9. **A negative speed means `booth` points the wrong way.** On footage with traffic, the
   reader's signed speed toward the booth is a check on the calibration's direction.

---

## Stream inventory (roadmap step 6.1)

Listed with `yt-dlp --flat-playlist` on `youtube.com/@fpfch/streams`, 2026-10-19 08:16 UTC
(02:16 at the bridges). "Down" means YouTube answered "We're experiencing technical
difficulties" on two consecutive checks 20 minutes apart. Titles are abbreviated after "Puente
Internacional".

| Video id | Title | Port | Status | What it shows (night frames) | Calibrated |
|---|---|---|---|---|---|
| `0Pg3S6s76IE` | Paso del Norte (Norte) | 240202 | live | Northbound bridge carriageways from the Juárez end, booths out of view | `pdn-north` (5 lanes) |
| `IcvugJWPXz8` | Paso del Norte (Sur) | 240202 | live | The Juárez street feeding the bridge; the queue drives toward the camera | `pdn-south-street` (2 columns) |
| `sOHFG6_3zAs` | Lerdo (Norte) | 240204 | live | Northbound roadway on the Lerdo–Stanton bridge | `lerdo-north` |
| `CYhISA_wFQ8` | Lerdo (Sur) | 240204 | live | Gates staffed by officers, traffic driving toward them; direction inferred southbound, not confirmed (2026-10-27: it looks south over **northbound** traffic, Q-37) | `lerdo-south-booths` (2 lanes, **booth in view**) |
| `RVXhhbkBGbI` | Lerdo (Fila) | 240204 | live | Labelled "queue view"; which lane is the queue is not identifiable at night | — |
| `mp3RS0y77tY` | Córdova–Américas (Norte) | 240201 | live | Seen only as a thumbnail so far | — |
| `CZM5TpXLzE8` | Córdova–Américas (Sur) | 240201 | live | Approach road, bridge behind the camera; the queue drives toward it | `bota-south-approach` (2 lanes) |
| `Y3OESQEXBlI` | Córdova–Américas (Av. Rafael Pérez Serna) | 240201 | live | Seen only as a thumbnail so far | — |
| `o5h9RB7qwY8` | Zaragoza (Av. Waterfill) – Vista Sur | 240203 | live | Seen only as a thumbnail so far | — |
| `ZZFgkly8XyE` | Zaragoza (Av. Waterfill) | 240203 | down | — | — |
| `KX-bm-Lhy3w` | Zaragoza (Norte) | 240203 | down | — | — |
| `GC5RY3zipa4` | Zaragoza (Sur) | 240203 | down | — | — |
| `707L8Qkhh9E` | Zaragoza (Vista Norte-Sur Carga) | 240203 | down | — | — |
| `HCMKTKZKqq4` | Zaragoza (Vista Sur-Norte Carga) | 240203 | down | — | — |
| `8u-hBawdG8c` | Guadalupe (Norte) | 240401 (Tornillo) | down | — | — |
| `0gT7jvaLCkg` | Guadalupe (Sur) | 240401 (Tornillo) | down | — | — |

No stream covers **Santa Teresa / San Jerónimo**. The BOTA streams carry a banner saying the
bridge is not administered by FPFCH.

**Rechecked 2026-10-27, about 19:00-19:14 UTC** (a camera survey; "live" is YouTube's listing, and a
current frame was taken from each live Juárez-side stream; no City picture was fetched):
- **Live then:** Zaragoza (Norte) `KX-bm-Lhy3w`, (Sur) `GC5RY3zipa4`, Waterfill `o5h9RB7qwY8`, both
  cargo views (`707L8Qkhh9E`, `HCMKTKZKqq4`; the frames and the bridge trust's page agree, and the
  YouTube titles are the ones with the directions swapped), and both Guadalupe–Tornillo streams
  (`8u-hBawdG8c`, `0gT7jvaLCkg`), so the "down" above no longer holds. `ZZFgkly8XyE` had ended. All
  three BOTA streams answered "technical difficulties", `CZM5TpXLzE8` (`bota-south-approach`)
  included, on three checks that day.
- **`lerdo-south-booths` looks south over northbound traffic.** Two vehicles drove toward the camera
  to the gates in four crops 3 s apart, the pavement arrows point at the camera, and the bridge
  trust labels the stream "Línea Exprés (Vista de Norte a Sur)". If these are the trust's own SENTRI
  toll gates (an inference), its `booth_distance_m: 0` is wrong and its waits end at the toll gate,
  not at CBP (Q-37).
- **No new pair is possible yet.** Zaragoza's only live City feed, `BridgeZaragoza3`, is labelled
  north to south; the City's Stanton feeds and Zaragoza 1-2 returned 404. Tornillo and BOTA have no
  US-side camera, and no public camera was found for Santa Teresa (Q-38).

---

## Verification status

| What | How it was checked | Result |
|---|---|---|
| Rectification, occupancy, motion, window logic, the range | 60 tests on rendered perspective queues of known length and speed, plus the range, calibration fields, saved references, the queue-run rules and the max head gap, read from `cameras.json` and refused when negative (`tests/border/test_border_camera.py`, 2026-10-28) | Speed 0.99–1.04× truth from 0.1 to 3 m/s; length within ±5 m; scale cancels; the range widens by the distance's ± and by the 25 % scale slack only when the booth is out of view; a stale or corrupt reference is replaced without a crash on Windows; learning one lane leaves the others' saved road untouched |
| Stream handling | 31 tests against small recorded videos, synthetic frames and a mocked channel (`test_border_camera_stream.py`, 2026-10-27) | Down / frozen / lost all raise. A synthetic quiet road, whose frames never repeat, never reads as frozen (a live one at dusk is unchecked, Q-32); a third of 20 s of pairs repeating exactly does; half a grey level over the whole scene moves and a fifth does not (the bar the runner's `QUIET_S` was measured with); a pair changing by one pixel is never frozen (compared at 320×180); a stopped clock does though no two frames repeat; a whole scene read faster than 2 fps is refused before anything opens; each frame says whether it moved; a failed resolve, or a read that found its stream gone, lists the channel afresh, and a transient failure (HTTP 503, in `test_border_camera_hls.py`) keeps the listing |
| What the viewer shows, and the worker | 57 tests (`test_border_camera_viewer.py`, 2026-10-28) | Every row of [What the viewer shows](#what-the-viewer-shows); which cameras hand the wait to the camera wait (`pairs.json`), and that no strip of theirs has a `cbp_lane`; the lane labels from the count zones and the one note on `pdn-north`'s split SENTRI lane; the anchoring bar and its text; the published signal pairs the rolling wait with its own range; an unexpected failure, a closed output or a full disk marks the camera `error` and the worker carries on; CBP is fetched alongside the cameras; one worker per state folder |
| Queue vs. no queue on real footage | 45 lane-rounds hand-labelled from nine 20-minute rounds of PDN north (3 lanes) and PDN south (2 columns), 02:16–04:56 local on 2026-10-19, 21 queued. Each round read with a reference learnt only from the rounds before it, as the worker would have it. The structure test, the emptiest-moment references, `QUEUE_DENSITY` and `RUN_OCCUPANCY` were all chosen against these same labels, so this is a score on the tuning set, not a held-out result | **40/40** under the current queue rules (commit `47df461`; `measure.py`'s queue rules have not changed since). **39/40** under the rules before it (`7515bec`): the miss was PDN south, left column, 02:36, with one 45 s clip of history. Round 1 has no history and is not scored |
| Speed on real footage | First and last frames of each clip compared by eye | Stalled lanes read 0.00–0.03 m/s; a lane that advanced about one car length (7–10 m) in 44 s read 0.16–0.23 m/s. Absolute metres are nominal until a calibration's scale is verified |
| The camera estimate: the wait, the command, the page section, the refusal of City frames | 2026-10-26, updated 2026-10-27, on simulated or scripted inputs only, with no stream: 42 tests (`test_border_camera_combine.py`) on `queue_sim.py`'s first-in-first-out bottleneck through the real `transit.measure`; 34 (`test_border_camera_estimate.py`) with fake runner and worker; 10 in the viewer's `EstimateSection` and 2 in `AnchoringBars`; 7 in the reader's `HiddenFrames` (counts of 2026-10-28) | With K_eff timed in the simulation, the total is within 5 % of the simulated truth, with the line wholly past A and with it straddling A. With half the cars counted at B, A→B stays within its range, and a K of real cars reads 1/p_B (2×) high. Replay equals the live records when every reading was logged before its tick, and keeps two ticks at one `seen_until` apart; a test pins the known difference (a reading logged after its tick). A replay lists as not measured what the log says, whatever its `--methods`; a log without it is rebuilt, and gains no typed group if its records are "mixed". A not-measured method keeps its place in the methods' order, wins over a record of its own, and shows why, with no count line. A run that measures no group says so. The City is refused without `--calibration`, and with it when the file would land inside the repository. Each yolox record, the camera record, the status and the page name the device; a replay refuses a log of two devices unless `--device` picks one, and lists the other's yolox records as not measured; `self_check` grades each device apart. A GPU refused at start stops `camera estimate` and lets go of both locks. Run live on 2026-10-27 (12:58-14:01 local), with YOLOX on the CPU; the 3.5-hour night run of 2026-10-28 counts here once its results are recorded (ROADMAP 6.8); nothing yet against a hand count or a timed crossing |
| Lane types, scale | Lane types at PDN only: count zones typed from the owner's labelled frames (Q-22), not hand-counted; every queue-reader strip keeps `cbp_lane` `null`. Scale: not verified for any camera | Elsewhere `null`; scales `false` in `cameras.json` |
| Booth distances | `lerdo-south-booths`: booth in view, exact. The other four: Google satellite map with its scale bar (2026-10-22), then corrected to each strip's booth-end row from a live frame. For PDN north, the strip runs ~45 m past the Feliz Viaje gantry's posts. For the two near-booth cameras, the camera position implied by their calibration puts the strip's row ~30 m (PDN south) and ~17 m (BOTA) in front of the camera | PDN north 365 ± 35 m, PDN south 510 ± 75 m, BOTA 615 ± 70 m, Lerdo north 335 ± 35 m; all `booth_distance_verified: false`. Lerdo north is **not yet corrected**: its strip ends short of the far gantry by a gap the flat-road calibration cannot measure (the road climbs there), so 335 m is likely low |
| YOLOX on the GPU ([ADR-0017](../architecture/adr/0017-yolox-on-the-gpu.md)) | Both paths side by side in a test script, not through the runner, on live `pdn-north`: 2026-10-27 13:28-13:34 local (721 frames) and 2026-10-28 00:02-00:12 local (1,201 frames); that night darkened and as dusk; constructed threshold cases. `test_border_camera_gpu.py`, 41 tests, 3 of them on the real GPU | The same boxes to the pixel on every live frame and the same crossings, 31 = 31 and 15 = 15; raw outputs never bit-identical (at most 3.35e-4); threshold flips can be constructed (estimated 0.12-0.7 decisions per 1,000 frames). Not yet counted live through the runner |
| Daylight and dawn | Lerdo–Stanton northbound (`lerdo-north`), empty overnight and queued once the port opened at 6 a.m.; replayed the way the worker runs from 03:16 to 07:56 local | 15/15 rounds right, through the change from streetlight to sun. Also in-sample: the rule letting a queue start anywhere, and `MIN_RUN_M`, were written after a missed lane at 07:16 in these same rounds, which were then re-scored |
| Direction, measured | Signed speed toward the booth across 15 rounds | `bota-south-approach` was first set the wrong way round (red lights at night suggested it); speed was negative every round, so it was flipped. `lerdo-south-booths` positive every round |
| A complete wait | `lerdo-south-booths`, booth in view so scale cancels | Empty until 06:36 local, then waits of 1.0–7.6 min. **In doubt since 2026-10-27:** the camera looks south over northbound SENTRI traffic at gates that may be the bridge trust's toll gates, not CBP's booths; if so, these are waits to the toll gate (Q-37) |
| The live worker | `camera run` on PDN north and the Lerdo booths for five minutes | Four cycles, rolling speeds accumulating, heartbeat written; its first run found a JSON crash, now fixed and tested |
| Crossing time: stamping, both counters, transit, the runner | 265 tests on 2026-10-28 (`pytest --collect-only`: hls 34, loop 43, detect 57, crossing 18, transit 54, runner 59), plus 41 in `test_border_camera_gpu.py`, on rendered scenes, simulated boxes, simulated crossings and scripted readers, cut to run in seconds. They include the per-zone `methods` and `metres` checks (`MethodsAndMetres`), the col-e yolox-only `RealZones` test, the loop's metres tests, the runner's freeze, no-motion, hold and backoff tests (ADR-0016), the runner's GPU-stop tests (`YoloxDevice` and `SharedGpuStop`: a `GpuFailed` stops YOLOX at every camera on the GPU, each a gap from its own frame before, no reconnect), `--device` (`TransitCommand`) and `transit.anchoring` (`AnchoringTest`). `RealZones`' check that `pdn-north`'s geometry is unchanged since `1221c9f` leaves out what moves no pixel: the notes, the confirmed direction, the head-gap limit (`max_head_gap_m`, ADR-0018), the zones' lane types and the strips' CBP lanes. Longer runs of the same scenes and simulations on 2026-10-24 and 2026-10-26 | The tests hold the behaviour but assert few of the figures. The figures in the counters table above, in [Crossing time](#crossing-time-between-two-cameras) and in each module's docstring come from the longer runs, and the docstrings date them and say how they were run. `detect.py`'s creeping-queue and line A figures rebuild from the test suite's `drive` and `Road`. The loop's staggered, occluded and random-gap runs, the line-B occluder run (col-4 cars in front of col-e, 2026-10-26), the crossing-time simulations (mixed lane types, short trips, reordering) and the replays behind ADR-0016 (frame differences logged live on 2026-10-26, with no picture kept) were one-off scripts not kept in the repo. Nothing yet against a hand count, a timed crossing or real YOLOX boxes |

**No held-out score exists yet.** Label new rounds (PDN by day, low sun, rain) before quoting an
accuracy.

**Known risks:**
- **Low sun.** Long shadows of railings and gantries are structure too. At 08:40 local they lit
  patches of PDN's empty left lanes as "occupied"; the run-occupancy rule kept that from becoming
  a queue, and the worker's reference refresh should learn the shadows, but it has not been
  watched through a full day.
- **A reference learnt in one light never refreshes in the other.** On 2026-10-27 the road was
  learnt around solar noon, and after that no sample passed the refresh check (0 of 121 frames and 0
  of 18 samples at midday, 0 of 151 at night). Against those references, on 2026-10-28 00:34-00:50
  local, `pdn-north` lane-5 read a false 75 m line and the street's right column a false 50 m+ line
  in 16 of 16 one-minute windows each; a reference learnt fresh that night read none. Until day and
  night references exist (ROADMAP 6.11, Q-34 v), start a run in a new `CAMERA_STATE_DIR` in the
  light it will run in, and treat readings after dawn or dusk as suspect (ASSUMPTION that the lock
  returns at dawn).
- **Map distances.** Every out-of-view lane's minutes rest on a map estimate. Until they are
  graded against timed crossings, treat them as the approximate figures the viewer says they
  are.
- **The road out of view.** Every out-of-view lane's minutes also time the road between the view
  and the booth at the speed of the line in view (ASSUMPTION). Since 2026-10-28 the cards show them
  only at `lerdo-north` and `bota-south-approach`; every such lane's signal still carries them
  (ADR-0018). On 2026-10-22 PDN north read
  99-128 min against CBP's 47. ADR-0014 names a hidden stretch that moves differently as one
  likely cause, with the speed reader and the scales; which dominates is not yet known.
- **PDN `lane-5`** reads only a short stalled stretch where daylight frames show more cars: it
  under-reads and is not validated. Its count zone is typed SENTRI (Q-22), as is lane-4's since
  2026-10-27, but both strips keep `cbp_lane` `null`, so their card rows and signals are not filed
  as SENTRI. Nothing stores the queue
  reader's signals yet.

**Not yet done:**
- **Verifying booth distances.** The four map estimates need a survey, or grading against timed
  crossings. Lerdo north's also needs correcting to its strip's booth-end row.
- Confirming lane types (`cbp_lane`) and scales outside PDN, and directions (`direction_verified`)
  on every camera but `pdn-north` (owner, 2026-10-27) and `city-pdn-north`. At PDN, a hand count per
  lane type at both lines (Q-24).
- Settling `lerdo-south-booths`: it looks over northbound traffic, at gates that may be the bridge
  trust's toll gates rather than CBP's booths (Q-37).
- Timing line B to the booths (K_eff) and recording B's occupancy, without which every camera wait
  stays a floor (Q-23).
- The verification runs ADR-0016 rests on: dusk across sunset, deep night, peak, and one under
  `camera estimate`, with numbers-only logging (Q-32). Its still tier, `QUIET_S` and the hold's lag
  rest on one 20-min night sample at line B.
- An uncut run long enough to anchor: about 3 h at both lines, away from sunset (Q-29). No run so
  far could anchor. Each record's `anchoring`, and the status's and page's bars, now show how far a
  run has come (2026-10-28).
- A run started in traffic leaves the loop blind at a line until that line's road is empty: on
  2026-10-28 line B's loop zones were still learning 55 min after the 11:04 UTC start (every loop
  record broken, still so when looked at). The record, the bars and the camera wait now name the
  line and why; whether to seed the loop's roads from an earlier run is Q-39.
- Day and night references for the queue reader (ROADMAP 6.11).
- Lerdo (Fila), the Zaragoza streams (`KX-bm-Lhy3w`, `GC5RY3zipa4`, Waterfill `o5h9RB7qwY8`) and both
  Guadalupe–Tornillo streams (`8u-hBawdG8c`, `0gT7jvaLCkg`) were live on 2026-10-27 but are not
  calibrated; queue cards for `KX-bm-Lhy3w` and `0gT7jvaLCkg` are an owner question (Q-38). All
  three BOTA streams were down that day.
- Grading against timed crossings (needs Phase 3).
- Calibrating the remaining live cameras.

---

*Written 2026-10-19 against commit `19035e9` on branch `chisme-border`; updated 2026-10-22 on
branch `chisme-border-waittime` for map booth distances, the viewer's minutes and the review
fixes; updated 2026-10-26 for the camera audit's fixes, and again for lane types (Q-22), line B's
redraw and `camera estimate` (ADR-0015); updated 2026-10-27 for the frozen rule, the hold and the
no-motion gap (ADR-0016), and the not-measured review fixes; updated 2026-10-28 for YOLOX on the GPU
(ADR-0017), the queue cards with the booth out of view (ADR-0018), the anchoring bars, the lane
labels, `pdn-north`'s lane-4 (SENTRI) and its confirmed direction, the freeze tail after a reopen,
and the camera survey of 2026-10-27; and again that day for the bars and the camera wait naming the
line unseen now and why (`anchoring.unseen`, Q-39).*
