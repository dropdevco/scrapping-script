# Border camera wait estimator

Estimates how long cars wait to cross northbound at the Paso del Norte bridge (Ciudad Juárez to
El Paso) from live public traffic cameras. Frames are analysed in memory and never stored; only
derived numbers are written.

**Status, 2026-11-21:** the measuring works and has run unattended for 3.5 h; it does not yet give
a wait in minutes. See [Status](#status).

## How a wait is built

The wait is the sum of three stretches of road:

| Stretch | Measured by | Status |
|---|---|---|
| Back of the line to line A (`pdn-north`) | The queue reader: line length and speed per lane | Works |
| Line A to line B (`city-pdn-north`) | Two car counters: cars between the lines ÷ cars leaving per minute | The counts have never anchored |
| Line B to the CBP booths | Nothing: no camera sees it | Not timed (`K_eff`, Q-23) |

Until all three are known, a total is a floor ("at least N min"). The two counting methods,
`loop` and `yolox`, are shown side by side and never averaged
([ADR-0015](../../../../docs/architecture/adr/0015-camera-wait-from-cars-ahead.md)).

## Cameras

| Camera | Source | Used for |
|---|---|---|
| `pdn-north` | YouTube, on the bridge | Queue reader and both counters (line A). Lanes 1-3 are normal; `lane-4` and `lane-5` are one SENTRI lane split by barrels |
| `pdn-south-street` | YouTube, the street before the bridge | Queue reader |
| `city-pdn-north` | City of El Paso, near the booths | Counted at line B only; **never displayed** on any page |

Calibration lives in [`cameras.json`](cameras.json); the line A / line B pair in
[`pairs.json`](pairs.json).

## Running it

Install with the `camera` extra (OpenCV, NumPy, yt-dlp), plus `gpu` for YOLOX on an NVIDIA GPU
through DirectML (`onnxruntime-directml` 1.24.4, Windows). The YOLOX-S model is not in the repo: it
is read from `~/.chisme/models/yolox_s.onnx` (or `$CHISME_MODEL_DIR`) and refused unless its sha256
matches `detect.MODEL_SHA256`.

```bash
python -m scraper.border camera estimate --device gpu
```

The queue reader, both counters and the camera wait in one process, with a page at
http://127.0.0.1:8090. `--device auto|gpu|cpu` picks where YOLOX runs (default
`$CHISME_YOLOX_DEVICE`, else `auto`: the GPU when it passes its checks). `CAMERA_STATE_DIR` sets
where records and learnt roads go (default `~/.chisme/camera`); a new folder learns that day's
road.

```bash
python -m scraper.border camera view
```

Queue cards only.

```bash
python -m scraper.border camera transit --device gpu --minutes 60
```

The two counters only.

```bash
python -m scraper.border camera estimate --replay <run>/transit-<date>.jsonl --queue <run>/queue-<date>.jsonl
```

Re-makes the camera records of a logged run with today's code, into
`CAMERA_STATE_DIR/camera-replay-<name>.jsonl` (appended). `--device cpu|gpu|unlogged` picks which
YOLOX records to grade when a log holds more than one.

`python -m scraper.border camera --help` lists the rest (`list`, `snapshot`, `reference`, `read`,
`run`).

## Records

Written to `CAMERA_STATE_DIR`, one file per UTC date, every time cut to the minute:

- `transit-<date>.jsonl`: per tick, method and lane type: counts, 15-min windows, epochs, each
  line's gaps with their reasons, reconnects, how far the count has come towards anchoring and
  which line is unseen and why, the YOLOX device, and per line the numbers the frozen-picture rule
  rests on.
- `queue-<date>.jsonl`: every lane reading of the queue reader, and its error lines, timed.
- `camera-<date>.jsonl`: the camera wait per lane type and method, stretch by stretch.
- `*.reference.npz`, `*.loop.npz`: the learnt empty roads (medians only, no frame).

## Code map

| File | What it does |
|---|---|
| `reader.py` | The command line: `python -m scraper.border camera ...` |
| `calibration.py` | Loads `cameras.json`: cameras, lane strips, count zones, head gap |
| `hls.py` | Our own HLS reader: segments, expiry gaps, discontinuities, native repeats |
| `stream.py` | Frames from a stream, and the frozen-picture check (`StreamFrozen`) |
| `measure.py` | The queue reader: occupancy against the learnt road, the queued run, its speed |
| `worker.py` | Runs the queue reader over the cameras and emits lane readings and cards |
| `crossing.py` | Counting zones and crossings; per-vehicle data kept at most 3 h |
| `loop.py` | The `loop` counter: a virtual induction loop on pixels |
| `detect.py` | The `yolox` counter: YOLOX-S detections with colour and class signatures |
| `gpu.py` | GPU selection, its start-up self-check and the running CPU re-check |
| `transit.py` | The line A to line B measurement: epochs, anchors, records, anchoring progress |
| `runner.py` | Runs both counters on both cameras: gaps, reconnects, the hold on a frozen line |
| `combine.py` | The camera wait from its three stretches, and how a card shows it |
| `estimate.py` | `camera estimate`: the tick loop, the queue log, replay |
| `viewer.py` | The local page |

## Status

As of 2026-11-21. Details in [ROADMAP](../../../../docs/border-estimator/ROADMAP.md) step 6.

Works:

- A 3.5-h unattended night run (2026-10-28): no cut at either counting line apart from a 5-s
  start-up gap, 0 reconnects, and no false line in 488 night readings.
- YOLOX on an RTX 4070: a 1-h run with no GPU stop, 465 of 465 re-checked frames identical to the
  CPU, and process CPU about 0.3 CPU-s/s against 2.6 on the CPU.
- Cards: an empty lane reads "no line"; Paso del Norte cards show a line's length and speed, not
  minutes ([ADR-0018](../../../../docs/architecture/adr/0018-queue-card-with-the-booth-out-of-view.md)).

Not yet:

- No count has anchored. Crossings are kept 3 h and `hi_min` is 90 min, so only cars inside a band
  about one trip long can anchor: about 6 min at night (Q-29).
- Line B to the booths is not timed, and line B barely counts the SENTRI lane.
- No timed crossing exists to grade any number against.
- Replay matches the live records within one process and one UTC date; it can still differ after
  a restart, across a UTC midnight, or when a line runs far behind the queue reader.
- Open review findings on the last two changes, all medium or low: some new paths lack a test that
  fails without them (the replay cut-off at a lane's logged minute, numbers folded in after a
  reconnect, the lock around the logged numbers), the status's "min ago" rounds up, and the repeat
  count also counts windows shorter than 40 pairs.

## Privacy and rules

- No frame, crop or per-vehicle datum is written. Colour and class signatures stay in memory at
  most 3 h. No plates, no faces.
- The City camera is counted and never displayed.
- The published estimate stays deterministic: no machine learning in the number
  ([SPEC](../../../../docs/border-estimator/SPEC.md) §1); the detector is a pinned, pretrained
  sensor ([ADR-0014](../../../../docs/architecture/adr/0014-crossing-time-from-two-cameras.md)).

## Tests

```bash
python -m pytest tests/border -q
```

1,186 tests pass (2026-11-21), across the queue reader, both counters, the stream reader, the
transit measurement, the camera wait, replay and the page.

## Docs

- [Border estimator overview](../../../../docs/border-estimator/README.md), [ROADMAP](../../../../docs/border-estimator/ROADMAP.md), [open questions](../../../../docs/border-estimator/QUESTIONS.md), [SPEC](../../../../docs/border-estimator/SPEC.md)
- Decisions: [ADR-0013](../../../../docs/architecture/adr/0013-weighted-fusion-of-wait-signals.md) to [ADR-0018](../../../../docs/architecture/adr/0018-queue-card-with-the-booth-out-of-view.md)
- The full component reference: [border-camera-reader.md](../../../../docs/components/border-camera-reader.md)
