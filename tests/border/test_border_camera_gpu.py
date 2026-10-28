"""The GPU path for YOLOX (gpu.py): choosing the path, the adapter, the session's options, the
start-up self-check, the one lock, the running check, and a GPU lost mid-run.

None of it needs a GPU or onnxruntime: a stand-in onnxruntime module is put in sys.modules, and
stand-in networks "detect" bright rectangles in their input through the same output tensor the
real network writes (as test_border_camera_detect's BrightBoxes do), so both paths run the whole
of detect.Yolox. The last class runs the real thing, and is skipped unless onnxruntime-directml
1.24.4, an NVIDIA adapter and the pinned model are all present.
"""
from __future__ import annotations

import contextlib
import math
import os
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
import unittest.mock
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

pytest.importorskip("cv2")

import cv2

from scraper.border.camera import detect, gpu
from scraper.border.camera.calibration import camera_from_dict
from scraper.border.camera.detect import Detection, DetectorCounter, Tile, Yolox
from scraper.border.camera.hls import Stamped

CAR = 2
NVIDIA_NAME = "NVIDIA GeForce RTX 4070 Laptop GPU"
SRC = Path(__file__).resolve().parents[2] / "src"


def bright_output(blob: np.ndarray, obj: float = 0.9) -> np.ndarray:
    """The raw (1, 8400, 85) output a network writes when every bright rectangle in its input is
    a car with objectness `obj` and class probability 0.9."""
    out = np.zeros((1, 8400, 85), np.float32)
    out[0, :, 2:4] = math.log(1 / 8)            # empty anchors: tiny boxes with no score
    mask = (blob[0].min(axis=0) > 128).astype(np.uint8)
    n, _, stats, _ = cv2.connectedComponentsWithStats(mask)
    for x, y, w, h, _ in stats[1:n]:
        cx, cy = x + w / 2, y + h / 2
        gx, gy = int(cx // 8), int(cy // 8)
        i = gy * 80 + gx
        out[0, i, 0:4] = (cx / 8 - gx, cy / 8 - gy, math.log(w / 8), math.log(h / 8))
        out[0, i, 4] = obj
        out[0, i, 5 + CAR] = 0.9
    return out


class BrightNet:
    """The CPU path's stand-in (cv2.dnn's Net)."""

    def __init__(self, obj: float = 0.9):
        self.obj = obj
        self.calls = 0

    def setInput(self, blob):  # OpenCV's name
        self.blob = blob

    def forward(self):
        self.calls += 1
        return bright_output(self.blob, self.obj)


# --- a stand-in onnxruntime -------------------------------------------------------------------
def ep_device(name: str, vendor: int, device_id: int, perf: int = 0,
              ep: str = "DmlExecutionProvider", kind: str = "GPU"):
    """One of onnxruntime.get_ep_devices(): an OrtEpDevice and its OrtHardwareDevice."""
    return SimpleNamespace(
        ep_name=ep, ep_options={"device_id": str(device_id)},
        device=SimpleNamespace(type=kind, vendor_id=vendor, device_id=0x2860, metadata={
            "Description": name, "DxgiAdapterNumber": str(device_id),
            "DxgiHighPerformanceIndex": str(perf), "LUID": "81305"}))


def nvidia(device_id: int, perf: int = 0, name: str = NVIDIA_NAME):
    return ep_device(name, 0x10DE, device_id, perf)


def intel(device_id: int, perf: int = 1):
    return ep_device("Intel(R) UHD Graphics", 0x8086, device_id, perf)


def software(device_id: int):
    return ep_device("Microsoft Basic Render Driver", 0x1414, device_id, 9)


class FakeOrt(types.ModuleType):
    """What gpu.py uses of onnxruntime-directml. Its sessions write bright_output (with objectness
    `obj`) through `edit`, and count runs and runs that overlapped another."""

    OrtHardwareDeviceType = SimpleNamespace(CPU="CPU", GPU="GPU", NPU="NPU")
    ExecutionMode = SimpleNamespace(ORT_SEQUENTIAL="sequential", ORT_PARALLEL="parallel")

    def __init__(self, devices=None, version=gpu.ORT_VERSION, dml=True):
        super().__init__("onnxruntime")
        self.__version__ = version
        self.__file__ = str(Path(tempfile.gettempdir()) / "no-such-onnxruntime" / "__init__.py")
        self.devices = devices if devices is not None else [intel(0), nvidia(1), software(2)]
        self.dml = dml
        self.sessions: list[FakeSession] = []
        self.session_providers = ["DmlExecutionProvider", "CPUExecutionProvider"]
        self.placed = [("DmlExecutionProvider", 3)]   # (provider, nodes) per assigned subgraph
        self.create_error: Exception | None = None
        self.fail: Exception | None = None             # what a run raises
        self.obj = 0.9
        self.edit = lambda out: out                    # applied to every output
        self.delay = 0.0
        self.runs = self.overlaps = self._inside = 0
        self._count = threading.Lock()

    def get_available_providers(self):
        return (["DmlExecutionProvider"] if self.dml else []) + ["CPUExecutionProvider"]

    def get_ep_devices(self):
        return list(self.devices)

    def SessionOptions(self):  # onnxruntime's name
        return FakeOptions()

    def InferenceSession(self, model, sess_options=None, providers=None, **kwargs):  # its name
        if self.create_error is not None:
            raise self.create_error
        session = FakeSession(self, model, sess_options, providers, kwargs)
        self.sessions.append(session)
        return session


class FakeOptions:
    def __init__(self):
        self.entries: dict[str, str] = {}

    def add_session_config_entry(self, key, value):
        self.entries[key] = value


class FakeSession:
    def __init__(self, ort: FakeOrt, model, options, providers, kwargs):
        self.ort, self.model, self.options = ort, model, options
        self.providers, self.kwargs = providers, kwargs

    def get_providers(self):
        return list(self.ort.session_providers)

    def get_provider_graph_assignment_info(self):
        return [SimpleNamespace(ep_name=ep, get_nodes=lambda n=n: [SimpleNamespace()] * n)
                for ep, n in self.ort.placed]

    def get_inputs(self):
        return [SimpleNamespace(name="images")]

    def get_outputs(self):
        return [SimpleNamespace(name="output")]

    def run(self, names, feed):
        ort = self.ort
        with ort._count:
            ort._inside += 1
            ort.overlaps += ort._inside > 1
        try:
            time.sleep(ort.delay)
            if ort.fail is not None:
                raise ort.fail
            ort.runs += 1
            return [ort.edit(bright_output(feed["images"], ort.obj))]
        finally:
            with ort._count:
                ort._inside -= 1


# --- helpers ----------------------------------------------------------------------------------
class GpuCase(unittest.TestCase):
    """Each test with its own process state (lock, stop) and no CHISME_YOLOX_DEVICE."""

    def setUp(self):
        self.enterContext(unittest.mock.patch.object(gpu, "STATE", gpu.GpuState()))
        env = {k: v for k, v in os.environ.items() if k != gpu.DEVICE_ENV}
        self.enterContext(unittest.mock.patch.dict(os.environ, env, clear=True))
        self.ort = FakeOrt()
        self.cpu = BrightNet()

    @contextlib.contextmanager
    def installed(self, ort=None):
        """onnxruntime as `ort` (None: not installed), and a pinned model that reads as bytes."""
        ort = self.ort if ort is None else ort
        with unittest.mock.patch.dict(sys.modules, {"onnxruntime": ort or None}), \
                unittest.mock.patch.object(detect, "read_model", return_value=b"pinned"), \
                unittest.mock.patch.object(detect, "cpu_net", return_value=self.cpu):
            yield

    def select(self, device="gpu", ort=None):
        with self.installed(ort):
            return gpu.select_device(device)

    def load(self, device="gpu", ort=None):
        with self.installed(ort):
            return gpu.load_yolox(device=device, name="cam")


def frame(width=320, height=180):
    """A dark frame with one bright car-sized rectangle."""
    image = np.zeros((height, width, 3), np.uint8)
    image[60:100, 100:180] = 255
    return image


WHOLE = [Tile(0, 0, 320, 180)]


# --- choosing the path ------------------------------------------------------------------------
class Choosing(GpuCase):
    def test_the_argument_then_the_environment_then_auto(self):
        self.assertEqual(gpu.requested_device(), "auto")
        with unittest.mock.patch.dict(os.environ, {gpu.DEVICE_ENV: " GPU "}):
            self.assertEqual(gpu.requested_device(), "gpu")
            self.assertEqual(gpu.requested_device("cpu"), "cpu")
        with unittest.mock.patch.dict(os.environ, {gpu.DEVICE_ENV: ""}):
            self.assertEqual(gpu.requested_device(), "auto")

    def test_an_unknown_device_is_refused_by_name(self):
        with self.assertRaises(ValueError) as err:
            gpu.requested_device("cuda")
        self.assertIn(gpu.DEVICE_ENV, str(err.exception))
        with unittest.mock.patch.dict(os.environ, {gpu.DEVICE_ENV: "nvidia"}), \
                self.assertRaises(ValueError):
            self.select(None)

    def test_the_cpu_path_never_touches_onnxruntime(self):
        with unittest.mock.patch.object(gpu, "import_onnxruntime") as ort:
            chosen = self.select("cpu")
            yolo = self.load("cpu")
        ort.assert_not_called()
        self.assertEqual((chosen.device, chosen.gpu, chosen.cpu), ("cpu", None, self.cpu))
        self.assertIs(type(yolo), Yolox)
        self.assertEqual((yolo.device, yolo.record()), ("cpu", {"device": "cpu"}))

    def test_importing_the_detector_never_imports_onnxruntime(self):
        code = ("import sys; import scraper.border.camera.detect, scraper.border.camera.gpu; "
                "print('onnxruntime' in sys.modules)")
        env = {**os.environ, "PYTHONPATH": os.pathsep.join(
            p for p in (str(SRC), os.environ.get("PYTHONPATH")) if p)}
        done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                              timeout=300, env=env, check=False)
        self.assertEqual((done.returncode, done.stdout.strip()), (0, "False"), done.stderr)

    def test_auto_runs_on_the_cpu_and_says_why_when_onnxruntime_is_missing(self):
        with self.assertLogs(gpu.log, "WARNING") as logs:
            chosen = self.select("auto", ort=False)
        self.assertEqual(chosen.device, "cpu")
        self.assertIn("not installed", chosen.reason)
        self.assertIn("runs on the CPU", logs.output[0])

    def test_gpu_is_refused_never_replaced_when_onnxruntime_is_missing(self):
        with self.assertRaises(gpu.GpuRefused) as err:
            self.select("gpu", ort=False)
        self.assertIn(".[gpu]", str(err.exception))

    def test_only_the_measured_onnxruntime_is_accepted(self):
        with self.assertRaises(gpu.GpuRefused) as err:
            self.select("gpu", FakeOrt(version="1.23.2"))
        self.assertIn("1.24.4", str(err.exception))
        with self.assertRaises(gpu.GpuRefused) as err:
            self.select("gpu", FakeOrt(dml=False))
        self.assertIn("DmlExecutionProvider", str(err.exception))

    def test_auto_takes_the_gpu_when_its_checks_pass(self):
        with self.assertLogs(gpu.log, "INFO") as logs:
            yolo = self.load("auto")
        self.assertIsInstance(yolo, gpu.GpuYolox)
        self.assertEqual(yolo.device, f"gpu:{NVIDIA_NAME}")
        self.assertIs(yolo.cpu_net, self.cpu)
        said = "\n".join(logs.output)
        self.assertIn("self-check passed", said)
        for logged in (NVIDIA_NAME, "device_id 1", "driver", "onnxruntime 1.24.4", "DirectML"):
            self.assertIn(logged, said)

    def test_the_adapter_and_versions_are_logged_once_per_process(self):
        with self.assertLogs(gpu.log, "INFO") as logs:
            self.load("gpu")
            self.load("gpu")
        self.assertEqual(sum("onnxruntime 1.24.4" in line for line in logs.output), 1)

    def test_yolox_load_goes_through_the_selection(self):
        with self.installed():
            self.assertIsInstance(Yolox.load(device="gpu"), gpu.GpuYolox)
            self.assertIs(type(Yolox.load(device="cpu")), Yolox)


# --- the adapter ------------------------------------------------------------------------------
class Adapters(GpuCase):
    def test_the_nvidia_adapter_is_found_by_vendor_and_name_wherever_it_is_listed(self):
        # The RTX 4070 was DXGI adapter 1 in one run and 0 in the next: never a fixed index.
        for devices, want in (([intel(0), nvidia(1), software(2)], "1"),
                              ([nvidia(0), intel(1), intel(2), software(4)], "0")):
            ort = FakeOrt(devices)
            self.assertEqual(self.select("gpu", ort).device, f"gpu:{NVIDIA_NAME}")
            self.assertEqual(ort.sessions[0].providers,
                             [("DmlExecutionProvider", {"device_id": want})])

    def test_no_nvidia_adapter_is_refused_and_what_there_is_is_named(self):
        cpu_ep = ep_device("13th Gen Intel(R) Core(TM) i7-13620H", 0x8086, 0,
                           ep="CPUExecutionProvider", kind="CPU")
        with self.assertRaises(gpu.GpuRefused) as err:
            self.select("gpu", FakeOrt([intel(0), software(1), cpu_ep]))
        self.assertIn("Intel(R) UHD Graphics", str(err.exception))
        self.assertIn("Microsoft Basic Render Driver", str(err.exception))
        self.assertNotIn("i7-13620H", str(err.exception))     # not a DirectML device

    def test_the_name_alone_or_the_vendor_alone_is_not_enough(self):
        named = ep_device("NVIDIA lookalike", 0x1414, 0)
        unnamed = ep_device("Some GPU", 0x10DE, 1)
        with self.assertRaises(gpu.GpuRefused):
            self.select("gpu", FakeOrt([named, unnamed]))

    def test_of_two_nvidia_adapters_dxgis_highest_performance_one_is_taken(self):
        ort = FakeOrt([nvidia(0, perf=2, name="NVIDIA Small"), nvidia(1, perf=0), intel(2)])
        self.assertEqual(self.select("gpu", ort).device, f"gpu:{NVIDIA_NAME}")


# --- the session ------------------------------------------------------------------------------
class Session(GpuCase):
    def test_nothing_falls_back_to_the_cpu_and_the_options_are_directmls(self):
        self.select("gpu")
        (session,) = self.ort.sessions
        self.assertEqual(session.kwargs, {"enable_fallback": 0})
        self.assertEqual(session.model, b"pinned")      # the bytes read_model checked
        o = session.options
        self.assertEqual(o.entries, {"session.disable_cpu_ep_fallback": "1",
                                     "session.record_ep_graph_assignment_info": "1"})
        self.assertEqual((o.enable_mem_pattern, o.execution_mode, o.intra_op_num_threads,
                          o.inter_op_num_threads), (False, "sequential", 1, 1))

    def test_a_node_placed_on_the_cpu_refuses_the_gpu(self):
        self.ort.placed = [("DmlExecutionProvider", 300), ("CPUExecutionProvider", 1)]
        with self.assertRaises(gpu.GpuRefused) as err:
            self.select("gpu")
        self.assertIn("CPUExecutionProvider", str(err.exception))
        self.ort.placed = []                            # nothing recorded proves nothing
        with self.assertRaises(gpu.GpuRefused):
            self.select("gpu")

    def test_a_session_led_by_another_provider_is_refused(self):
        self.ort.session_providers = ["CPUExecutionProvider"]
        with self.assertRaises(gpu.GpuRefused):
            self.select("gpu")

    def test_a_session_that_cannot_be_made_is_refused_and_auto_says_why(self):
        self.ort.create_error = RuntimeError("D3D12 device could not be created")
        with self.assertRaises(gpu.GpuRefused) as err:
            self.select("gpu")
        self.assertIn("D3D12", str(err.exception))
        with self.assertLogs(gpu.log, "WARNING"):
            self.assertEqual(self.select("auto").device, "cpu")

    def test_the_net_takes_only_the_graphs_input_shape(self):
        net = gpu.DmlNet(b"pinned", gpu.find_adapter(self.ort), self.ort)
        with self.assertRaises(RuntimeError):
            net.forward()
        with self.assertRaises(ValueError):
            net.setInput(np.zeros((1, 3, 320, 320), np.float32))
        blob, _ = detect.preprocess(frame())
        net.setInput(blob)
        out = net.forward()
        self.assertEqual((out.shape, out.dtype, out.flags.c_contiguous),
                         ((1, 8400, 85), np.float32, True))


# --- the self-check ---------------------------------------------------------------------------
class SelfCheck(GpuCase):
    def test_the_drawn_scenes_are_the_same_pixels_every_time(self):
        a, b = gpu.check_scenes(), gpu.check_scenes()
        self.assertEqual(len(a), gpu.CHECK_SCENES)
        self.assertEqual(a[0].shape, (480, 854, 3))
        self.assertTrue(all(np.array_equal(x, y) for x, y in zip(a, b, strict=True)))
        self.assertFalse(np.array_equal(a[0], a[1]))

    def test_paths_that_agree_pass_and_say_how_closely(self):
        self.ort.edit = lambda out: (out.__setitem__((0, 0, 5), 5e-4), out)[1]
        chosen = self.select("gpu")
        self.assertIn("vehicles alike", chosen.reason)
        self.assertIn("0.0005", chosen.reason)
        self.assertEqual(self.cpu.calls, gpu.CHECK_SCENES * 3)   # each scene whole and in two

    def test_a_raw_difference_above_the_tolerance_refuses_the_gpu(self):
        # An empty anchor's class score: no detection changes, only the raw output.
        self.ort.edit = lambda out: (out.__setitem__((0, 0, 5), 2e-3), out)[1]
        with self.assertRaises(gpu.GpuRefused) as err:
            self.select("gpu")
        self.assertIn("raw outputs differ", str(err.exception))

    def test_a_score_on_the_other_side_of_a_threshold_refuses_the_gpu(self):
        # 0.4 starts a track: 0.40005 on the CPU, 0.39999 on the GPU, 7e-5 apart in the raw.
        self.cpu.obj, self.ort.obj = 0.4445, 0.44443
        with self.assertRaises(gpu.GpuRefused) as err:
            self.select("gpu")
        self.assertIn("score", str(err.exception))

    def test_scenes_with_no_vehicle_prove_nothing(self):
        self.cpu.obj = self.ort.obj = 0.0
        with self.assertRaises(gpu.GpuRefused) as err:
            self.select("gpu")
        self.assertIn("no vehicle", str(err.exception))

    def test_a_run_that_fails_during_the_self_check_refuses_the_gpu(self):
        self.ort.fail = RuntimeError("80004005 Unspecified error")
        with self.assertRaises(gpu.GpuRefused) as err:
            self.select("gpu")
        self.assertIn("80004005", str(err.exception))


class Differences(unittest.TestCase):
    A = Detection((10.0, 20.0, 110.0, 90.0), 0.8, CAR)
    B = Detection((200.0, 20.0, 260.0, 60.0), 0.3, 7)

    def moved(self, d, dx=0.0, score=None, cls=None):
        return Detection(tuple(c + dx for c in d.box), d.score if score is None else score,
                         d.cls if cls is None else cls)

    def test_the_same_boxes_within_the_measured_noise_agree(self):
        self.assertIsNone(gpu.differences([self.A, self.B], [self.A, self.B]))
        self.assertIsNone(gpu.differences([self.moved(self.A, 0.05, 0.8 + 5e-4)], [self.A]))
        self.assertIsNone(gpu.differences([], []))

    def test_count_class_order_box_and_score_each_differ(self):
        for gpu_side, why in (([self.A], "1 boxes on the GPU, 2 on the CPU"),
                              ([self.A, self.moved(self.B, cls=5)], "class"),
                              ([self.B, self.A], "class"),
                              ([self.A, self.moved(self.B, 0.2)], "px apart"),
                              ([self.A, self.moved(self.B, score=0.302)], "score")):
            self.assertIn(why, gpu.differences(gpu_side, [self.A, self.B]))

    def test_a_score_across_the_track_threshold_differs_however_close(self):
        a = self.moved(self.A, score=detect.HIGH_SCORE)
        b = self.moved(self.A, score=detect.HIGH_SCORE - 1e-7)
        self.assertIn("score", gpu.differences([a], [b]))


# --- the one lock -----------------------------------------------------------------------------
class Lock(GpuCase):
    def hammer(self, nets, runs=20):
        blob, _ = detect.preprocess(frame())
        errors = []

        def run(net):
            for _ in range(runs):
                try:
                    net.setInput(blob)
                    net.forward()
                except Exception as exc:  # noqa: BLE001 (counted, not raised in a thread)
                    errors.append(exc)

        threads = [threading.Thread(target=run, args=(n,)) for n in nets]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        return errors

    def test_two_sessions_in_two_threads_never_run_at_once(self):
        nets = [self.select("gpu").gpu, self.select("gpu").gpu]
        self.assertIsNot(nets[0].session, nets[1].session)
        self.ort.runs, self.ort.delay = 0, 0.002
        self.assertEqual(self.hammer(nets), [])
        self.assertEqual((self.ort.runs, self.ort.overlaps), (40, 0))

    def test_without_the_lock_they_would(self):
        # The control: the stand-in sees overlapping runs when nothing serialises them.
        nets = [self.select("gpu").gpu, self.select("gpu").gpu]
        self.ort.delay = 0.002
        with unittest.mock.patch.object(gpu.STATE, "lock", contextlib.nullcontext()):
            self.hammer(nets)
        self.assertGreater(self.ort.overlaps, 0)


# --- the running check ------------------------------------------------------------------------
class Running(GpuCase):
    def gpu_yolox(self, every=gpu.CHECK_EVERY):
        yolo = self.load("gpu")
        yolo.check = gpu.CrossCheck(every)
        self.cpu.calls = 0
        return yolo

    def test_the_first_frame_and_every_nth_after_are_also_run_on_the_cpu(self):
        yolo = self.gpu_yolox()
        for _ in range(61):
            (found,) = yolo.detect(frame(), WHOLE)
        self.assertEqual(self.cpu.calls, 3)             # frames 0, 30 and 60
        self.assertEqual(yolo.record(), {"device": f"gpu:{NVIDIA_NAME}", "cpu_checked": 3,
                                         "cpu_differed": 0})
        self.assertEqual(yolo.check.frames, 61)
        np.testing.assert_allclose(found.box, (100, 60, 180, 100), atol=1.5)

    def test_a_difference_is_counted_and_logged_and_the_gpus_boxes_are_kept(self):
        yolo = self.gpu_yolox()
        self.cpu.obj, self.ort.obj = 0.4445, 0.44443     # 0.40005 on the CPU, 0.39999 on the GPU
        with self.assertLogs(gpu.log, "WARNING") as logs:
            (found,) = yolo.detect(frame(), WHOLE)
        self.assertLess(found.score, detect.HIGH_SCORE)  # the GPU's, never the CPU's
        self.assertIn("The GPU's boxes are kept", logs.output[0])
        self.assertEqual(yolo.record()["cpu_differed"], 1)
        self.assertIsNone(gpu.STATE.stopped)

    def test_a_raw_difference_beyond_the_tolerance_counts_as_a_difference(self):
        yolo = self.gpu_yolox()
        self.ort.edit = lambda out: (out.__setitem__((0, 0, 5), 2e-3), out)[1]
        with self.assertLogs(gpu.log, "WARNING") as logs:
            yolo.detect(frame(), WHOLE)
        self.assertIn("raw outputs differ", logs.output[0])
        self.assertEqual(yolo.record()["cpu_differed"], 1)

    def test_differences_close_together_stop_the_gpu_for_the_rest_of_the_run(self):
        yolo = self.gpu_yolox(every=1)
        other = self.gpu_yolox(every=1)                  # the other camera's
        self.cpu.obj, self.ort.obj = 0.4445, 0.44443
        with self.assertLogs(gpu.log, "WARNING"):
            for _ in range(gpu.DIFFER_LIMIT - 1):
                yolo.detect(frame(), WHOLE)
            with self.assertRaises(gpu.GpuFailed) as err:
                yolo.detect(frame(), WHOLE)
        self.assertIn("checks against the CPU differed", str(err.exception))
        self.assertIsNotNone(gpu.STATE.stopped)
        runs, cpu_calls = self.ort.runs, self.cpu.calls
        for y in (yolo, other):
            with self.assertRaises(gpu.GpuFailed):
                y.detect(frame(), WHOLE)
        self.assertEqual((self.ort.runs, self.cpu.calls), (runs, cpu_calls))  # nothing ran
        with self.assertLogs(gpu.log, "WARNING"):
            self.assertIn("stopped earlier", self.select("auto").reason)
        with self.assertRaises(gpu.GpuRefused):
            self.select("gpu")

    def test_differences_far_apart_do_not_stop_it(self):
        yolo = self.gpu_yolox(every=1)
        with self.assertLogs(gpu.log, "WARNING"):
            for n in range(3 * gpu.DIFFER_WINDOW + 1):
                self.ort.obj = 0.44443 if n % gpu.DIFFER_WINDOW == 0 else 0.9
                self.cpu.obj = 0.4445 if n % gpu.DIFFER_WINDOW == 0 else 0.9
                yolo.detect(frame(), WHOLE)
        self.assertEqual(yolo.record()["cpu_differed"], 4)
        self.assertIsNone(gpu.STATE.stopped)


# --- a GPU lost mid-run -----------------------------------------------------------------------
class Lost(GpuCase):
    def test_a_failed_run_stops_the_gpu_path_and_the_cpu_never_stands_in(self):
        yolo = self.load("gpu")
        yolo.detect(frame(), WHOLE)                      # frame 0: checked
        cpu_calls = self.cpu.calls
        self.ort.fail = RuntimeError("887A0005 The GPU device instance has been suspended")
        with self.assertLogs(gpu.log, "ERROR"), self.assertRaises(gpu.GpuFailed) as err:
            yolo.detect(frame(), WHOLE)
        self.assertIn("887A0005", str(err.exception))
        self.assertEqual(self.cpu.calls, cpu_calls)
        self.ort.fail = None                             # back, but not for this run
        runs = self.ort.runs
        with self.assertRaises(gpu.GpuFailed):
            yolo.detect(frame(), WHOLE)
        self.assertEqual(self.ort.runs, runs)

    def test_an_output_that_is_not_finite_stops_it(self):
        yolo = self.load("gpu")
        self.ort.edit = lambda out: out * np.nan
        with self.assertLogs(gpu.log, "ERROR"), self.assertRaises(gpu.GpuFailed) as err:
            yolo.detect(frame(), WHOLE)
        self.assertIn("not finite", str(err.exception))

    def test_a_counter_on_the_gpu_says_so_and_lets_the_failure_through(self):
        zone = {"line": "A", "lane": "1", "quad": [(80, 150), (200, 150), (200, 120), (80, 120)]}
        camera = camera_from_dict("cam", {"title": "test", "port_number": "000000",
                                          "direction": "north", "frame_size": [320, 180],
                                          "count_zones": [zone]})
        counter = DetectorCounter(camera, detector=self.load("gpu"))
        at = datetime(2026, 10, 28, 6, 0, tzinfo=UTC)
        counter.add(Stamped(frame(), at, 0))
        self.assertEqual(counter.device, f"gpu:{NVIDIA_NAME}")
        self.assertEqual(counter.device_record(), {"device": f"gpu:{NVIDIA_NAME}",
                                                   "cpu_checked": 1, "cpu_differed": 0})
        self.ort.fail = RuntimeError("device removed")
        with self.assertLogs(gpu.log, "ERROR"), self.assertRaises(gpu.GpuFailed):
            counter.add(Stamped(frame(), at.replace(second=1), 90000))


# --- the real GPU -----------------------------------------------------------------------------
def _no_real_gpu() -> str | None:
    if not detect.default_model_path().is_file():
        return "the pinned model is not installed"
    try:
        gpu.find_adapter(gpu.import_onnxruntime())
    except gpu.GpuRefused as exc:
        return str(exc)
    return None


NO_REAL_GPU = _no_real_gpu()


@unittest.skipIf(NO_REAL_GPU is not None, f"no GPU path here: {NO_REAL_GPU}")
class RealGpu(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.state = unittest.mock.patch.object(gpu, "STATE", gpu.GpuState())
        cls.state.start()
        cls.yolo = gpu.load_yolox(device="gpu", name="test")

    @classmethod
    def tearDownClass(cls):
        cls.state.stop()

    def test_it_runs_on_the_nvidia_adapter_with_every_node_on_directml(self):
        self.assertIsInstance(self.yolo, gpu.GpuYolox)
        self.assertTrue(self.yolo.device.startswith("gpu:NVIDIA"), self.yolo.device)
        session = self.yolo.net.session
        self.assertFalse(session._enable_fallback)        # ORT's own session fallback is off
        placed = session.get_provider_graph_assignment_info()
        self.assertGreater(sum(len(s.get_nodes()) for s in placed), 0)
        self.assertEqual({s.ep_name for s in placed}, {"DmlExecutionProvider"})

    def test_its_output_is_laid_out_as_opencvs_and_close_to_it(self):
        blob, _ = detect.preprocess(gpu.check_scenes(1)[0])
        self.yolo.net.setInput(blob)
        out = self.yolo.net.forward()
        self.yolo.cpu_net.setInput(blob)
        ref = self.yolo.cpu_net.forward()
        self.assertEqual((out.shape, out.dtype, out.strides), (ref.shape, ref.dtype, ref.strides))
        self.assertLessEqual(float(np.abs(out.astype(np.float64) - ref).max()), gpu.RAW_TOLERANCE)

    def test_the_drawn_scenes_give_the_same_vehicles_on_both_paths(self):
        worst, alike = gpu.self_check(self.yolo.net, self.yolo.cpu_net)
        self.assertLessEqual(worst, gpu.RAW_TOLERANCE)
        self.assertGreaterEqual(alike, 12)                 # 27 on 2026-10-28


if __name__ == "__main__":
    unittest.main()
