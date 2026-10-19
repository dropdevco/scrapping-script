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

## How a wait is read

```
frame ──► image grey + edges ──► warp each lane into a strip (booth at row 0, 8 px per metre)
                                   │
          ┌────────────────────────┴───────────────────────┐
          ▼                                                ▼
   per metre: occupied?                          per 12 m segment: how far did the
   (differs from the empty-road reference        strip's profile move toward the booth
    in grey level or edge strength)              since the last frame? (correlation,
                                                  then 1-D Lucas-Kanade for the fraction)
          │                                                │
          └──────────────── over a 60 s window ────────────┘
                                   ▼
   queue  = run of metres occupied ≥ half the window, from the booth end, gaps ≤ 9 m
   speed  = time-averaged speed of what is inside the queue, stationary cars included
   wait   = (distance from strip to booth + visible queue) ÷ speed
```

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
8. Learn the empty road **while the lane is clear** (night is ideal):
   `python -m scraper.border camera reference --camera <id>`. A median over a standing queue
   would learn the queue as road. After that the reference drifts toward the live picture only
   where nothing is detected, which follows dawn and dusk.
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
| `0Pg3S6s76IE` | Paso del Norte (Norte) | 240202 | live | Northbound bridge carriageways from the Juárez end, booths out of view | `pdn-north` (left 3 lanes) |
| `IcvugJWPXz8` | Paso del Norte (Sur) | 240202 | live | Seen only as a thumbnail so far | — |
| `sOHFG6_3zAs` | Lerdo (Norte) | 240204 | live | Northbound roadway on the Lerdo–Stanton bridge | `lerdo-north` |
| `CYhISA_wFQ8` | Lerdo (Sur) | 240204 | live | Toll booths and lanes; traffic direction not yet confirmed | — |
| `RVXhhbkBGbI` | Lerdo (Fila) | 240204 | live | Labelled "queue view"; which lane is the queue is not identifiable at night | — |
| `mp3RS0y77tY` | Córdova–Américas (Norte) | 240201 | live | Seen only as a thumbnail so far | — |
| `CZM5TpXLzE8` | Córdova–Américas (Sur) | 240201 | live | Seen only as a thumbnail so far | — |
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
| Real footage | Night clips, nearly empty roads | Reads `no_queue` as it should. **Not yet validated on real queues**: see below |
| Lane types, booth distances, scale | Not verified for any camera | All `null` / `false` in `cameras.json` |

**Not yet done:**
- Validation on rush-hour footage. The thresholds in `measure.py` are starting values and
  have not been tuned on real queues.
- Grading against timed crossings (needs Phase 3).
- Calibrating the remaining live cameras.
- Measuring booth distances.

---

*Written 2026-10-19 against commit `19035e9` on branch `chisme-border`.*
