# Component: Border Camera Reader

`src/scraper/border/camera/` reads **queue length and queue speed** off the bridges' live
camera streams and turns them into a wait estimate, as a `measured` signal for the estimator
([SPEC §5.1](../border-estimator/SPEC.md#51-measured-queue-length--queue-speed-deterministic)).

The queue reader is **deterministic**: fixed image computations, no model, nothing random. The
same frames always give the same answer, so any reading can be reproduced from a recording. It
also measures **crossing time between two cameras** ([below](#crossing-time-between-two-cameras),
ADR-0014). That uses two counters side by side; one of them uses a pinned YOLOX-S model.

| | |
|---|---|
| **Runs** | On a machine we own (Q-3), continuously: `python -m scraper.border camera run` |
| **Reads** | The FPFCH YouTube live streams (`youtube.com/@fpfch/streams`), found by title. The City of El Paso's bridge cameras (HLS on `zoocams.elpasozoo.org`), for crossing time only: read at the owner's risk and **never displayed** (ADR-0014). The viewer also fetches CBP's published waits each cycle, **for display only**: they never feed the camera estimate |
| **Writes** | JSON signal lines on stdout; each lane's empty-road reference, `status.json` (what the viewer shows, including `cbp`, `cbp_at`, `cbp_error`) and a heartbeat in `CAMERA_STATE_DIR` (default `~/.chisme/camera`) |
| **Install** | `pip install -e ".[camera]"` (OpenCV headless, numpy, yt-dlp); Node on `PATH` for yt-dlp's YouTube extraction; the **ffmpeg** command-line tool on `PATH` (the stamped HLS reader). For the model counter: `yolox_s.onnx` from Megvii's YOLOX 0.1.1rc0 release in `~/.chisme/models/` (sha256 `c5c2d13e…998063`, checked on load) |
| **Decision on record** | Reading YouTube streams is against YouTube's terms; the product owner chose to do it (QUESTIONS.md, Q-7). Frames are analysed in memory; the worker and the viewer **never write one**. Only an operator's explicit `camera snapshot --out` or `camera read --debug` writes a single annotated frame, to check a calibration. |

---

## Run it

Needs Python 3.11+ and Node.js on `PATH` (yt-dlp uses it to read YouTube). From the repo root:

```bash
python -m venv .venv
.venv\Scripts\activate                     # macOS/Linux: source .venv/bin/activate
pip install -e ".[camera]"
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

Every calibrated lane now has a `booth_distance_m`, so every lane can give minutes.
`lerdo-south-booths` sees its booth, so its distance is exact. The other four cameras' distances
are **map estimates** (2026-10-22) with a ± and `booth_distance_verified: false`, so their
minutes are shown as approximate (see [What the viewer shows](#what-the-viewer-shows)).

Every live frame is stamped with **when it was captured**, from the stream's own clock (`hls.py`):
- YouTube: PROGRAM-DATE-TIME plus the frame's PTS;
- the City: segment Last-Modified, fitted over segments.

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
            view it must start at the booth end; otherwise any such run ≥ 10 m counts. Its
            length is always counted from the strip's booth end
   speed  = time-averaged speed of what is inside the queue, stationary cars included
            (the worker publishes a 15-minute rolling average of it)
   wait   = (distance from strip to booth + visible queue) ÷ speed
```

Three choices in that pipeline were made on real footage, not in advance:

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

A photometric correction (matching the scene's brightness outside the lanes) was also built
and scored. It made the result worse and is not used.

That last line is Little's law for one lane: a car joining the tail covers the queue's length
at the queue's average speed. Two consequences:

- **When the booth is in view, scale cancels.** A calibration whose metres are 1.5× too long
  stretches length and speed equally, so the wait is unchanged (pinned by a test).
- **When the booth is out of view it does not.** The distance to the booth is in real metres and
  the speed is in calibrated metres. Four of the five calibrations are in this case (all but
  `lerdo-south-booths`): the cameras are on the Juárez side and the CBP booths are on the US
  side. Their range is wider for two reasons. First, the distance is `booth_distance_m ±
  booth_distance_uncertainty_m`. Second, while `scale_verified` is false, the range is
  stretched by a further 25 % each way (`SCALE_SLACK`).

The range is `measure.wait_bounds`:
- **low** = (distance − uncertainty, never below 0, + queue) ÷ the fastest speed;
- **high** = (distance + uncertainty + queue) ÷ the slowest.

Each end then moves by the 25 % scale slack when that applies. A one-minute window takes its
fastest and slowest quarter-minute speeds. The 15-minute rolling wait, which is what gets
published and shown, has one averaged speed, so its range reflects only the distance's ± and
the scale slack, not how much the speed varied.

### States a reading can report

These are the states of the **signal** a reading publishes. The viewer can still show a number
for some of the "unknown" ones; see [What the viewer shows](#what-the-viewer-shows).

| `state` | Meaning | Wait in the signal |
|---|---|---|
| `queue` | A dense queue in view | `(booth distance + queue) / speed`; a **floor** with `lower_bound: true` when the queue runs past the far edge of the view |
| `no_queue` | Nothing queued at this camera | `0` if the booth is in view; **unknown** if not, because a queue could sit between the view and the booth |
| `stalled` | Queued, but it did not move over the whole window | unknown; `lower_bound: true` |
| `insufficient` | Fewer than 20 frames, or too few motion samples | unknown |

`camera run` and `camera view` publish the **15-minute rolling** wait, with that rolling wait's
own range. The one-minute window's range is never used, so the minutes always fall inside the
published `low`–`high`. A floor is published with its `low` and no `high`.

### What the viewer shows

The viewer (`camera view`) gives a number whenever one can be stood behind, and says what kind
of number it is. `worker.lane_summary` decides; the page only prints it.

| Shown | When | How |
|---|---|---|
| `12 min` | A queue, booth in view | The 15-minute rolling wait. With the booth in view the scale cancels, so it is never marked approximate |
| `≈ 12 min`, range `9–16` | A queue, booth out of view, with a map-estimated distance or an unverified scale | The rolling wait and its range; the line under it says which of the two makes it approximate |
| `≥ 9 min` | The line runs past the edge of the view | A floor, stated at its range's **low** end, not at the central wait |
| `3 min` (or `≈ 3`) with "this minute's line" | A line in view, but too few motion samples this minute to time it | This minute's line, timed at the last 15 minutes' speed. Never an older line's wait: a short line at the booth is always too short to time, and showing a long line's wait from ten minutes ago would be wrong exactly when the line has cleared |
| `0 min` | No queue, booth in view | — |
| `under ~20 min` | No queue in view, booth out of view | A line could still wait between the view and the booth. It would be shorter than that stretch, so it is (distance + uncertainty) timed at the **slowest** recent queue speed among this camera's lanes that are moving at all, × 1.25 while the scale is unverified. It is "probably under", not a strict bound: a hidden line could move slower still |
| `—` and a reason | No booth distance; a queue that has not moved in its recent readings; a line too short or too still to time with no recent speed on that lane (speed is timed only on 12 m segments wholly inside the line, so a line under 12–18 m is never timed); no recent queue on this camera to time a hidden line by; too few frames | — |

Under each bridge, CBP's own published waits are printed for comparison. They are fetched in
parallel with the cameras, with one short attempt, and never feed the camera number.

A camera whose reading fails for any reason shows **down** (the stream) or **error** (anything
else, such as a yt-dlp timeout) and is retried next cycle; the others carry on.

A stream that fails is never a reading. It raises instead (ADR-0011):

| Failure | Cause | Seen |
|---|---|---|
| `StreamDown` | Listed on the channel but YouTube will not serve it ("We're experiencing technical difficulties"), or not listed / not live | 7 of 16 FPFCH streams on 2026-10-19 08:16 UTC |
| `StreamFrozen` | The burned-in clock stopped changing for 20 s. Judged over the window, not frame by frame, because a frozen stream is still re-encoded and every keyframe changes the picture a little | — |
| `StreamLost` | Frames stopped arriving mid-read | — |

`camera run` alerts Telegram once when a camera stops and once when it comes back, and keeps
retrying. One camera failing, for any reason, never stops the others or the worker.

---

## Crossing time between two cameras

`python -m scraper.border camera transit` (ADR-0014) measures how long cars take from a counting
line on one camera (A) to a counting line on another (B). Today the only pair is Paso del Norte
northbound:
- **A** is on FPFCH `pdn-north`, the Mexican half of the bridge: a 1.5 m band across all five
  lanes, 66-67.5 m from the strips' booth end.
- **B** is on the City of El Paso's `bridgepdn1` (camera `city-pdn-north`, `display: false`), the
  US half. B's three column zones are **provisional**: drawn from one night frame with no lane
  markings in view. Redraw them from a daylight frame, and confirm which column is SENTRI, Ready or
  general.

Both cameras are read continuously. Every frame goes to **two counters**, and their results are
never mixed:

| Method | How a car is counted | Known limits (rendered and synthetic tests) |
|---|---|---|
| `loop` (`loop.py`, deterministic) | A virtual loop per zone: each car's back edge is followed through the zone against a learnt empty road, and counted when it crosses the far edge | 0 missed and 0 false in rendered stop-and-go and crawls with 1-2 m gaps, but the rendered cars are flat, so no car hides the road. From `pdn-north`, which sees the traffic from behind, a stopped car hides roughly 1.5-2.5 m of road ahead of its bumper at line A (ASSUMPTION: camera pose from the unverified 10.5 × 30 m rectangle, typical car shapes). With that road painted over, the loop counted cars with 1.1 m of road showing and none with 0.5 m, so stopped gaps under about 2.5-3.5 m may read as one car. That result does not carry over to line A until line A is hand-counted. Cars 0.5 m apart read as one. Half the resolution misses most. Learns the empty road from near-empty moments only, and stays "learning" (a gap) until the lane clears |
| `yolox` (`detect.py`, YOLOX-S) | Car, truck and bus boxes in the zones' tiles, tracked frame to frame, counted when a track crosses the far edge | Not trusted above 8 m/s. Not trusted in creeping queues until a hand count at line A settles it: in `detect.py`'s exact-box simulation, boxes jittering by about 1.5 px at `pdn-north` counted some cars twice, and a rebuild of that simulation lost more cars than it counted twice. How much real YOLOX boxes jitter has not been measured. About 0.3 CPU-s a frame |

Transit = the horizontal gap between the running counts at A and B (`transit.py`). The cars
between the lines at the start are unknown. They come from **anchors**: a few distinctive cars,
matched in FIFO order by a colour-and-width signature. Every gap cuts the count into a new epoch,
and that epoch needs anchors or a carried count before its transits are trusted (unsure ones are
flagged). Every one of these is a gap, never "no cars":
- a reconnect;
- an expired segment;
- a timeline cut (encoder restart);
- a frozen picture;
- a counter still learning.

A trip between the lines longer than 90 min is never anchored: signatures are kept at most 3 h, and an
anchor needs the ceiling to spare at both ends. If the City refuses our client, that camera stops.

What is written: one record per method per minute to `CAMERA_STATE_DIR/transit-<date>.jsonl`.
It holds counts, rates, transit statistics per 15-minute window, and epochs: derived numbers
only. Signatures and single crossings live in memory for at most 3 h. No plate or face is ever
read.

This is **not the full wait, and not a bound on it**:
- A→B covers the middle of the bridge only, not the street tail before A nor the stretch and
  booth after B.
- It is read by exit: it is the A→B time of cars that crossed A about one transit earlier. In a
  queue that is clearing, a car entering now takes less.
- Every lane at A and every column at B is counted together: `runner.py` does not split them by
  lane type, and no lane type is confirmed. So it mixes lane types: it reads above a fast lane's (SENTRI, Ready) A→B and below
  the general lane's.
- An error in the offset, or drift, moves it either way.
- Until GPS-timed crossings grade it, it is for grading and calibration, not for display. It
  grades the queue reader only on the same stretch (ADR-0014).

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
   rectangle (yellow), the lane strips (green, labelled at the booth end) and the masks (red).
8. Learn the road from **moments spread across time**, including some when the lane is clear:
   `python -m scraper.border camera reference --camera <id> --rounds 6 --gap 600` takes six
   live samples ten minutes apart (or pass `--source` once per recorded clip). One moment is
   refused. The worker then rebuilds each reference on every reading, once its in-memory bank
   holds 18 samples, learning only from the emptiest of them. The bank keeps 36 samples, three
   per reading. That is about an hour at `run`'s 5-minute cadence, and about 24 minutes at the
   viewer's 2-minute cadence. A lane with no saved reference is learnt by the worker itself. Lanes
   already learnt keep theirs, and are never rebuilt from those few minutes of samples. A reference
   saved for a different calibration, or unreadable, is deleted and relearnt; references are
   written whole and then moved into place, so a killed worker cannot leave half a file. Readings in the first hour after install are the least reliable: the one miss in the
   evaluation below (under the earlier rules) had a single 45-second clip of history.
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
| `CYhISA_wFQ8` | Lerdo (Sur) | 240204 | live | Gates staffed by officers, traffic driving toward them; direction inferred southbound, not confirmed | `lerdo-south-booths` (2 lanes, **booth in view**) |
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

---

## Verification status

| What | How it was checked | Result |
|---|---|---|
| Rectification, occupancy, motion, window logic, the range | 36 tests on rendered perspective queues of known length and speed, plus the range, calibration fields and saved references (`tests/border/test_border_camera.py`) | Speed 0.99–1.04× truth from 0.1 to 3 m/s; length within ±5 m; scale cancels; the range widens by the distance's ± and by the 25 % scale slack only when the booth is out of view; a stale or corrupt reference is replaced without a crash on Windows; learning one lane leaves the others' saved road untouched |
| Stream handling | 13 tests against small recorded videos and a mocked channel (`test_border_camera_stream.py`) | Down / frozen / lost all raise |
| What the viewer shows, and the worker | 27 tests (`test_border_camera_viewer.py`) | Every row of [What the viewer shows](#what-the-viewer-shows); the published signal pairs the rolling wait with its own range; an unexpected failure, a closed output or a full disk marks the camera `error` and the worker carries on; CBP is fetched alongside the cameras; one worker per state folder |
| Queue vs. no queue on real footage | 45 lane-rounds hand-labelled from nine 20-minute rounds of PDN north (3 lanes) and PDN south (2 columns), 02:16–04:56 local on 2026-10-19, 21 queued. Each round read with a reference learnt only from the rounds before it, as the worker would have it. The structure test, the emptiest-moment references, `QUEUE_DENSITY` and `RUN_OCCUPANCY` were all chosen against these same labels, so this is a score on the tuning set, not a held-out result | **40/40** under the current queue rules (commit `47df461`; `measure.py`'s queue rules have not changed since). **39/40** under the rules before it (`7515bec`): the miss was PDN south, left column, 02:36, with one 45 s clip of history. Round 1 has no history and is not scored |
| Speed on real footage | First and last frames of each clip compared by eye | Stalled lanes read 0.00–0.03 m/s; a lane that advanced about one car length (7–10 m) in 44 s read 0.16–0.23 m/s. Absolute metres are nominal until a calibration's scale is verified |
| Lane types, scale | Not verified for any camera | `null` / `false` in `cameras.json` |
| Booth distances | `lerdo-south-booths`: booth in view, exact. The other four: Google satellite map with its scale bar (2026-10-22), then corrected to each strip's booth-end row from a live frame. For PDN north, the strip runs ~45 m past the Feliz Viaje gantry's posts. For the two near-booth cameras, the camera position implied by their calibration puts the strip's row ~30 m (PDN south) and ~17 m (BOTA) in front of the camera | PDN north 365 ± 35 m, PDN south 510 ± 75 m, BOTA 615 ± 70 m, Lerdo north 335 ± 35 m; all `booth_distance_verified: false`. Lerdo north is **not yet corrected**: its strip ends short of the far gantry by a gap the flat-road calibration cannot measure (the road climbs there), so 335 m is likely low |
| Daylight and dawn | Lerdo–Stanton northbound (`lerdo-north`), empty overnight and queued once the port opened at 6 a.m.; replayed the way the worker runs from 03:16 to 07:56 local | 15/15 rounds right, through the change from streetlight to sun. Also in-sample: the rule letting a queue start anywhere, and `MIN_RUN_M`, were written after a missed lane at 07:16 in these same rounds, which were then re-scored |
| Direction, measured | Signed speed toward the booth across 15 rounds | `bota-south-approach` was first set the wrong way round (red lights at night suggested it); speed was negative every round, so it was flipped. `lerdo-south-booths` positive every round |
| A complete wait | `lerdo-south-booths`, booth in view so scale cancels | Empty until 06:36 local, then waits of 1.0–7.6 min |
| The live worker | `camera run` on PDN north and the Lerdo booths for five minutes | Four cycles, rolling speeds accumulating, heartbeat written; its first run found a JSON crash, now fixed and tested |

**No held-out score exists yet.** Label new rounds (PDN by day, low sun, rain) before quoting an
accuracy.

**Known risks:**
- **Low sun.** Long shadows of railings and gantries are structure too. At 08:40 local they lit
  patches of PDN's empty left lanes as "occupied"; the run-occupancy rule kept that from becoming
  a queue, and the worker's reference refresh should learn the shadows, but it has not been
  watched through a full day.
- **Map distances.** Every out-of-view lane's minutes rest on a map estimate. Until they are
  graded against timed crossings, treat them as the approximate figures the viewer says they
  are.
- **PDN `lane-5`** reads only a short stalled stretch where daylight frames show more cars: it
  under-reads and is not validated.

**Not yet done:**
- **Verifying booth distances.** The four map estimates need a survey, or grading against timed
  crossings. Lerdo north's also needs correcting to its strip's booth-end row.
- Confirming lane types (`cbp_lane`), directions (`direction_verified`) and scales.
- BOTA north, Lerdo (Fila), BOTA (Av. Rafael Pérez Serna) and the Zaragoza avenue view are live but
  not calibrated. Every Zaragoza bridge stream and both Guadalupe–Tornillo streams were down all
  morning.
- Grading against timed crossings (needs Phase 3).
- Calibrating the remaining live cameras.

---

*Written 2026-10-19 against commit `19035e9` on branch `chisme-border`; updated 2026-10-22 on
branch `chisme-border-waittime` for map booth distances, the viewer's minutes and the review
fixes.*
