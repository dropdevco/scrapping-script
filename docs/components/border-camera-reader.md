# Component: Border Camera Reader

`src/scraper/border/camera/` reads **queue length and queue speed** off the bridges' live
camera streams and turns them into a wait estimate, as a `measured` signal for the estimator
([SPEC §5.1](../border-estimator/SPEC.md#51-measured-queue-length--queue-speed-deterministic)).

It is **deterministic**: fixed image computations, no model, nothing random. The same frames
always give the same answer, so any reading can be reproduced from a recording.

| | |
|---|---|
| **Runs** | On a machine we own (Q-3), continuously: `python -m scraper.border camera run` |
| **Reads** | The FPFCH YouTube live streams (`youtube.com/@fpfch/streams`), found by title |
| **Writes** | JSON signal lines on stdout; each lane's empty-road reference and a heartbeat in `CAMERA_STATE_DIR` (default `~/.chisme/camera`) |
| **Install** | `pip install -e ".[camera]"` (OpenCV headless, numpy, yt-dlp); Node on `PATH` for yt-dlp's YouTube extraction |
| **Decision on record** | Reading YouTube streams is against YouTube's terms; the product owner chose to do it (QUESTIONS.md, Q-7). Frames are analysed in memory and **never written**. |

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
goes down or comes back if `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` are set. A wait in
minutes appears only for lanes whose `booth_distance_m` is known; today that is
`lerdo-south-booths`.

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
   queue  = run of metres occupied ≥ half the window, from the booth end, gaps ≤ 9 m,
            at least 60 % of its metres queued
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
  the speed is in calibrated metres. Every current calibration is in this case: the cameras are
  on the Juárez side and the CBP booths are on the US side. Those lanes produce a wait only once
  `booth_distance_m` is measured, and their range is widened by 25 % while `scale_verified` is
  false.

### States a reading can report

| `state` | Meaning | Wait |
|---|---|---|
| `queue` | A queue starts at the booth end of the view | `(booth distance + queue) / speed`; a **floor** with `lower_bound: true` when the queue runs past the far edge of the view |
| `no_queue` | Nothing queued at this camera | `0` if the booth is in view; **unknown** if not, because a queue could sit between the view and the booth |
| `stalled` | Queued, but it did not move over the whole window | unknown; `lower_bound: true` |
| `insufficient` | Fewer than 20 frames, or too few motion samples | unknown |

A stream that fails is never a reading. It raises instead (ADR-0011):

| Failure | Cause | Seen |
|---|---|---|
| `StreamDown` | Listed on the channel but YouTube will not serve it ("We're experiencing technical difficulties"), or not listed / not live | 7 of 16 FPFCH streams on 2026-10-19 08:16 UTC |
| `StreamFrozen` | The burned-in clock stopped changing for 20 s. Judged over the window, not frame by frame, because a frozen stream is still re-encoded and every keyframe changes the picture a little | — |
| `StreamLost` | Frames stopped arriving mid-read | — |

`camera run` alerts Telegram once when a camera stops and once when it comes back, and keeps
retrying. One camera failing never stops the others.

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
4. `booth_distance_m`: the distance along the lane from the strip's booth end to the booth, if
   measured. Use `0` when the booth is in view, and `null` when unmeasured (no wait is produced,
   only queue and speed).
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
   refused. The worker then keeps refreshing each reference from an in-memory bank of about an
   hour of samples, learning only from the emptiest of them. Readings in the first hour after
   install are the least reliable: the one miss in the evaluation below had a single 45-second
   clip of history.
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
| Rectification, occupancy, motion, window logic | 12 tests on rendered perspective queues of known length and speed (`tests/border/test_border_camera.py`) | Speed 0.99–1.04× truth from 0.1 to 3 m/s; length within ±5 m; scale cancels |
| Stream handling | 12 tests against small recorded videos and a mocked channel (`test_border_camera_stream.py`) | Down / frozen / lost all raise |
| Queue vs. no queue on real footage | 45 lane-rounds hand-labelled from nine 20-minute rounds of PDN north (3 lanes) and PDN south (2 columns), 02:16–04:56 local on 2026-10-19, 21 queued. Each round read with a reference learnt only from the rounds before it, as the worker would have it | **39/40 correct** (round 1 has no history). The miss: PDN south, left column, 02:36, with one 45 s clip of history |
| Speed on real footage | First and last frames of each clip compared by eye | Stalled lanes read 0.00–0.03 m/s; a lane that advanced about one car length (7–10 m) in 44 s read 0.16–0.23 m/s. Absolute metres are nominal until a calibration's scale is verified |
| Lane types, booth distances, scale | Not verified for any camera | All `null` / `false` in `cameras.json` |

| Daylight and dawn | Lerdo–Stanton northbound (`lerdo-north`), empty overnight and queued once the port opened at 6 a.m.; replayed the way the worker runs from 03:16 to 07:56 local | 15/15 rounds right, through the change from streetlight to sun |
| Direction, measured | Signed speed toward the booth across 15 rounds | `bota-south-approach` was first set the wrong way round (red lights at night suggested it); speed was negative every round, so it was flipped. `lerdo-south-booths` positive every round |
| A complete wait | `lerdo-south-booths`, booth in view so scale cancels | Empty until 06:36 local, then waits of 1.0–7.6 min |
| The live worker | `camera run` on PDN north and the Lerdo booths for five minutes | Four cycles, rolling speeds accumulating, heartbeat written; its first run found a JSON crash, now fixed and tested |

**Known risks:**
- **Low sun.** Long shadows of railings and gantries are structure too. At 08:40 local they lit
  patches of PDN's empty left lanes as "occupied"; the run-occupancy rule kept that from becoming
  a queue, and the worker's hourly refresh should learn the shadows, but it has not been watched
  through a full day.
- **PDN `lane-5`** reads only a short stalled stretch where daylight frames show more cars: it
  under-reads and is not validated.

**Not yet done:**
- **Booth distances.** No calibration except `lerdo-south-booths` can turn a queue into minutes
  until `booth_distance_m` is measured on a map, from each strip's booth-end row to the CBP booth.
- Confirming lane types (`cbp_lane`), directions (`direction_verified`) and scales.
- BOTA north, Lerdo (Fila), BOTA (Av. Rafael Pérez Serna) and the Zaragoza avenue view are live but
  not calibrated. Every Zaragoza bridge stream and both Guadalupe–Tornillo streams were down all
  morning.
- Grading against timed crossings (needs Phase 3).
- Calibrating the remaining live cameras.
- Measuring booth distances.

---

*Written 2026-10-19 against commit `19035e9` on branch `chisme-border`.*
