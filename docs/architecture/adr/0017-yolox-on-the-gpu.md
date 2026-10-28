# ADR-0017: YOLOX-S runs on the NVIDIA GPU by default, only while it finds the CPU's cars, and never takes the CPU's place silently

- **Status:** **Accepted in part.** Decisions 1, 3 and 5 were accepted on 2026-10-27 by the product
  owner: run the model where it runs better (the owner's choice as `gpu.py` and commit `6e22bcf`
  record it; decision 5 is what the measurement makes of that choice). Decision 2's conditions stand
  under that choice, as built on 2026-10-27/28 from the measurement's review. Their limits (how
  often the running check runs, how many differences stop it, what a stop leaves behind) are the
  builder's defaults, and decision 4 and the runner's handling of a stop were built on 2026-10-28
  while the owner was away: **Proposed**, QUESTIONS.md Q-35.
- **Date:** 2026-10-28
- **Amends:** [ADR-0014](0014-crossing-time-from-two-cameras.md) decision 2, the model rules "run
  through OpenCV" and "run locally on CPU". The model now runs locally on the CPU through OpenCV
  **or** on the NVIDIA GPU through ONNX Runtime's DirectML build. ADR-0014's "the same frames give
  the same counts" now holds **per path, not across paths**. Everything else in ADR-0014 stands:
  the pinned file and its sha256, never trained or fine-tuned on our data, frames analysed in memory
  only.
- **Keeps:** [ADR-0015](0015-camera-wait-from-cars-ahead.md) and
  [ADR-0016](0016-frozen-when-the-picture-repeats.md) unchanged. A YOLOX stop is one more gap at its
  line, never zero cars.
- **References:** `src/scraper/border/camera/gpu.py` (module docstring), `detect.py`, `runner.py`,
  `estimate.py`, `combine.py`; QUESTIONS.md Q-35; `docs/components/border-camera-reader.md`.

## Context

**The CPU fell behind at line A by day.** The owner's daylight run of 2026-10-27 (12:58-14:01 local,
`camera estimate`, YOLOX on the CPU) wrote 40 cuts at line A between 19:03 and 19:59 UTC, spread
through the hour (7, 6, 9, 7, 2 and 9 per 10 min from 19:00 UTC). Every one was a segment that expired
before it was read or frames more than 5 s apart (the records' `gaps_a`, counted 2026-10-28); line B
had none but the loop's start-up learning. A segment expires when the reader falls behind the
stream's playlist. On the CPU, line A's two 640×640 passes cost 241.9 ms and 0.82 CPU-s a frame
(4 threads; the measurement below), so at 2 fps line A alone asks 1.64 CPU-s every second, beside
line B, the queue reader and the page. ASSUMPTION: the counters' own load caused the cuts. Other work
shared the PC for part of that hour (the GPU measurement below ran at 19:28-19:34 UTC), but the cuts
came all through it. Every cut restarts the roughly 174 uncut minutes a count needs to anchor
(ADR-0016, Q-29), so at that rate no daylight run could anchor.

**OpenCV offers no other target.** The installed OpenCV is 5.0.0. Its new DNN engine runs this model
on the CPU and has no other target (`detect.cpu_net`). A GPU needs another runtime.

**The measurement** (2026-10-27/28; onnxruntime-directml 1.24.4, DirectML 1.15.4, NVIDIA driver
617.42, RTX 4070; the pinned model, float32; both paths run side by side in a test script, not
through the runner; frames analysed in memory only):

| | CPU (OpenCV, 4 threads) | GPU (DirectML) |
|---|---|---|
| One 640×640 pass | 113.9 ms, 0.41 CPU-s | 5.4 ms, 0.006 CPU-s |
| One frame of line A (2 tiles) | 241.9 ms, 0.82 CPU-s | 21.2 ms, 0.021 CPU-s |

- **Live, by day:** `pdn-north`, 2026-10-27 13:28-13:34 local, 721 frames, 532 vehicle detections.
  0 frames differed in count, class, order or pixel box. The crossing lists were identical: 31 and
  31, with the same times, lanes, classes, confidences and signatures.
- **Live, by night:** 2026-10-28 00:02-00:12 local, 1,201 frames, 223 detections. 0 frames differed
  in their boxes. 15 and 15 crossings, identical, and the tracker state matched on every frame.
- **Replays of that night,** darkened (×0.3) and as dusk (gamma, blue tint, noise): 15 and 15
  identical crossings each. Edge cases (the densest frames at 7 brightness levels, tile seams shifted
  −20 to +20 px, fine brightness steps, many cars on a still): 0 changed boxes.
- **Not bit-exact.** The raw outputs differed by up to 3.35e-4, mostly in box size. 0 of 2,402 live
  passes were bit-identical. GPU against GPU, in a second process on the same frames, was
  bit-identical.

**What the review found** (the measurement's verdict, tried against the same machine):
- **The adapter number moves.** The RTX 4070 was DirectML adapter 1 in one run and 0 in the next. By
  the old number the model then ran on the Intel graphics: outputs 0.50 away from the CPU's, at
  42.5 ms a pass. The Intel chip was 7-9× slower than the NVIDIA card in both runs.
- **Two sessions at once crash.** The runner runs one network per camera, each in its own thread. Two
  GPU sessions running at once failed 7 of 600 runs with ONNX Runtime error 80004005 in one trial of
  three, and crashed the whole process in the other two. With one lock around every run: 600 of 600,
  equal to running them one at a time.
- **A tiny difference can flip a threshold.** Real frames nudged by at most 8 grey levels in 12-43
  pixels put the two paths on opposite sides of a threshold: score 0.1 (the GPU found an extra car
  box), the NMS overlap 0.45 (a different box survived, 0.73 px away, its score 0.0084 different) and
  score 0.4 (only the GPU started a track; the crossings in that 41-frame window were the same, but
  one crossing record's confidence or signature differed). No flip could be made at a tile seam.
- **Near misses on real data.** One live overlap sat 1.97e-7 from 0.45, and one score in the dark
  replay 6.6e-7 from 0.1, closer than the paths differ. Neither flipped, because both paths moved the
  same way.
- **How often** (estimates, assuming values near a threshold are spread evenly): about 0.12 decision
  flips per 1,000 frames live at night, about 0.5 by day on the two tiles of a many-car still and 0.7
  on its whole frame. 2 of the 6 constructed flips changed the frame's box set.
- **ONNX Runtime falls back on its own.** Found while building: its Python session class rebuilds the
  session on the CPU after a provider error, at creation or at any later run, and carries on.

## Decision

1. **`auto` by default.** `camera transit` and `camera estimate` take `--device auto|gpu|cpu`; the
   default is `$CHISME_YOLOX_DEVICE`, else `auto`, read once at start. `gpu.select_device` then
   places each camera's network:
   - **`auto`:** the GPU when every condition in 2 holds at start, otherwise the CPU, with a warning
     that says why;
   - **`gpu`:** the GPU, or `GpuRefused`: the command says why and stops before any stream is read,
     letting go of its locks;
   - **`cpu`:** the CPU; onnxruntime is never imported.
2. **The GPU is used only under these conditions, each enforced in `gpu.py`:**
   - **FP32, the pinned model.** The bytes come from `detect.read_model` (sha256 checked before any
     device is tried), and the same bytes go to both paths. Only onnxruntime-directml 1.24.4
     (`ORT_VERSION`) is accepted; the `gpu` extra in `pyproject.toml` pins it, on Windows only. This
     version has no FP16 option, and none may be added.
   - **The adapter by vendor and name, at every start** (`find_adapter`), from ONNX Runtime's own list
     of DirectML devices: an NVIDIA GPU (vendor 0x10DE, name starting "NVIDIA"), never a fixed
     number, never the Intel chip or a software adapter. Its name, id, PCI ids, LUID and driver, and
     the ONNX Runtime and DirectML versions, are logged once per process.
   - **No fallback to the CPU inside ONNX Runtime:** not for one node
     (`session.disable_cpu_ep_fallback`) and not for the whole session (its own fallback off). Every
     node is read back and must run on DirectML.
   - **One lock** (`GpuState.lock`) around every GPU run and session creation in the process.
   - **A self-check at every start** (`self_check`): 6 drawn, seeded scenes at 854×480, each run
     whole and as two tiles on both paths. The detections must agree (count, class and order; every
     box corner within 0.1 px; every score within 1e-3 and on the same side of 0.4, the score that
     starts a track) and the raw outputs lie within 1e-3 (`RAW_TOLERANCE`). Scenes that give no
     vehicle at all refuse the GPU.
   - **A running check while counting** (`CrossCheck`): one frame in 30 (`CHECK_EVERY`, the first
     included) is detected on the CPU too and compared the same way. A difference is counted and
     logged, and the GPU's boxes are still the ones used: the paths are never switched mid-run. 3
     differing checks within 20 (`DIFFER_LIMIT`, `DIFFER_WINDOW`) stop the GPU path.
   - **A stop is a gap, never a switch.** A failed run, an output of the wrong shape or not finite,
     or the running check's limit raises `GpuFailed`. The GPU path is then stopped for the rest of
     the process: every camera whose YOLOX runs on the GPU (both, under `auto`) raises it at its
     next frame. The runner drops each one's YOLOX counter: its line is a yolox gap from that
     camera's frame before ("YOLOX stopped: …"), the loop counter goes on, and the log, the status
     line ("YOLOX STOPPED at <camera>"), one alert per camera and the page say so. YOLOX is never
     restarted on the CPU in its place.
3. **The CPU path stays as it was** (`detect.cpu_net`, OpenCV): it runs wherever the `gpu` extra is
   not installed, a check fails, or the CPU is asked for.
4. **Records name the path.** Each yolox transit record carries `devices`: per camera, `device`
   (`cpu` or `gpu:<adapter name>`), on the GPU `cpu_checked` and `cpu_differed`, and after a stop
   `stopped` and `stopped_at` (to the minute). The camera record's yolox entry keeps it. Records of
   the two paths are never graded together: `combine.record_device` names each record's path (an
   older yolox record is "device not logged", never pooled with `cpu`), `combine.self_check` grades
   each path apart, and a replay grades one path (`estimate.replay_device`): a log with more than one
   is refused unless `--device cpu` or `--device gpu` picks one, and the other path's records are
   listed as not measured in their ticks.
5. **"The same frames give the same counts" holds per path, not across paths.** A count is
   reproducible on the path that made it. Between the paths it held on every frame measured, but it is
   not guaranteed.

## Alternatives considered

| Alternative | Why not |
|---|---|
| Stay on the CPU and shed load (`--methods loop`, fewer frames a second, fewer tiles) | `--methods loop` gives no SENTRI wait while col-e is counted by yolox only (Q-24). Fewer frames or tiles change what is counted: line A already breaks `detect.py`'s zone rule (a car seen whole in 3 frames before the zone) above about 6 m/s. And the CPU path costs 0.82 CPU-s a frame of line A, which is what fell behind |
| OpenCV with a CUDA build | The installed 5.0.0's new DNN engine has no target but the CPU (`detect.cpu_net`), and the project's `opencv-python-headless` wheel carries no CUDA. A source build was not tried. It would also replace the `cv2` that the CPU path, and every count made so far, runs on |
| `onnxruntime-gpu` with CUDA | Needs the CUDA and cuDNN libraries that match its build, installed beside the driver. Not tried. The DirectML build carries what it needs in its wheel, and it was the one measured |
| OpenVINO on the Intel graphics | Not tried. The Intel chip, reached through DirectML, was 7-9× slower than the NVIDIA card (42.5-47.5 ms a pass), and its outputs were 0.50-0.79 away from the CPU's. It would be a second new runtime for a slower device |
| The GPU only when asked for (opt-in) instead of `auto` | The owner chose the GPU where it runs better. Opt-in leaves every run that forgets the flag on the lagging CPU. `auto` takes the GPU only when its checks pass and says which path it took; the records name it, and a replay refuses to mix them |
| The GPU with no checks (the measured wrapper as it stood) | Choosing by number landed on the Intel chip once the number moved; two sessions at once crash; ONNX Runtime's own session fallback would carry on on the CPU unseen |
| On a GPU stop, carry on with YOLOX on the CPU | A silent switch of path mid-run, which the review ruled out. A new CPU counter, its records labelled `cpu`, is an owner question (Q-35) |

## Consequences

- **The CPU is freed.** On the GPU a frame of line A costs 0.021 CPU-s, plus the running check's
  average 0.027 (one frame in 30 at about 0.8 CPU-s), about 0.05 against 0.82. During the live
  comparison, with the stream reader and the CPU model running beside it, the GPU path took 55 ms and
  0.058 CPU-s a frame, against 250 ms and 0.86 CPU-s.
- **Not bit-exact, so paths stay apart.** A box on a threshold can differ between the paths (estimated
  0.12-0.7 decisions per 1,000 frames, most of which change no box). No count difference was seen in
  76 crossings, live and replayed, but they are not independent: the 46 live ones bound it only to
  about 6.5 % a crossing. Hence decision 4.
- **GPU against GPU was bit-exact on one machine and one driver.** The device label does not carry the
  driver version, which is only logged (Q-35).
- **The cameras' GPU passes run one at a time**, behind the lock. ASSUMPTION: at 5.4 ms a pass that
  costs little; it has not been measured with both cameras counting.
- **A start on the GPU takes about 5 s per network** (5.09 s in the build's check: model, session and
  18 + 18 self-check passes, at below-normal priority).
- **One more optional dependency:** the `gpu` extra, onnxruntime-directml 1.24.4, Windows only. pip
  also pulls its listed dependencies (flatbuffers, protobuf, sympy, mpmath), which running the model
  does not need; the measurement installed it with `--no-deps` (Q-35).
- **Records grow** by `devices` (yolox records only; loop records carry none, Q-35) and, in the same
  change set, by `anchoring` for the progress bars.
- **A GPU stop costs the yolox count of every camera on the GPU for the rest of the run** (both, under
  `auto`; the pair's yolox is unmeasured from the first stop either way). The loop goes on. Before the
  runner's wiring (2026-10-28) a `GpuFailed` fell into the reader's catch-all and reopened the stream
  on every frame, darkening both methods; commit `6e22bcf` therefore told runs to set
  `CHISME_YOLOX_DEVICE=cpu` until then.
- **Not yet run live through the runner.** The live comparisons ran both paths in a test script. The
  3.5-hour night run of 2026-10-28 was launched with YOLOX on the CPU; its records (at `59aaa95`) do
  not name the device, and its results are not yet recorded (ROADMAP 6.8).
- **Tests** on 2026-10-28 (`pytest --collect-only`): `test_border_camera_gpu.py` 41 (3 of them on the
  real GPU, skipped where there is none), `_detect.py` 57, and in the runner, estimate and combine
  files the GPU-stop (`YoloxDevice`), `--device` (`TransitCommand`) and device tests (`DeviceTest`).
