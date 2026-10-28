"""What each transit record logs for the frozen rule and the hold (QUESTIONS.md Q-32, ROADMAP
6.6), per line: the most frame pairs that repeated exactly in any window of the still tier, the
longest native repeat, the darkest frame's mean grey, and the hold's lag. Numbers only, read in
each camera's own thread (frame by frame, and from each StreamFrozen) and taken by the records
under the runner's lock.

Kept apart from test_border_camera_runner.py, whose helpers it uses, so that the two can change
on their own.
"""
from __future__ import annotations

import json
import threading
import unittest
import unittest.mock
from datetime import timedelta

import numpy as np
import pytest

pytest.importorskip("cv2")

import test_border_camera_runner as rt

from scraper.border.camera import runner as runner_mod
from scraper.border.camera.hls import Stamped
from scraper.border.camera.stream import StreamFrozen, StreamLost

T0 = rt.T0
FIELDS = [f"{name}_{end}" for name in ("most_repeats", "native_repeat_s", "darkest_grey",
                                        "hold_lag_s") for end in "ab"]


def judged(at, moved=True, repeats=None, grey=None, native=None):
    """A frame as hls.HlsReader yields it, with what its frozen check judged."""
    return Stamped(np.zeros((360, 640, 3), np.uint8), at, 0, moved, repeats, grey, native)


class Reader:
    """Stands in for hls.HlsReader: yields `given`, then raises `end`."""

    def __init__(self, given, end):
        self.given, self.end, self.gaps = given, end, []
        self.http = unittest.mock.Mock()

    def frames(self, seconds=None):
        yield from self.given
        raise self.end


class LoggedNumbers(rt.RunnerSetUp):
    def record(self) -> dict:
        records = self.run_.records(self.run_.measure())
        self.assertEqual(len(records), 1)
        return records[0]

    def test_every_field_is_written_and_none_where_nothing_was_judged(self):
        self.feed_both(600)               # frames from readers with no frozen check
        rec = self.record()
        self.assertEqual({k: rec[k] for k in FIELDS},
                         {"most_repeats_a": None, "most_repeats_b": None,
                          "native_repeat_s_a": None, "native_repeat_s_b": None,
                          "darkest_grey_a": None, "darkest_grey_b": None,
                          "hold_lag_s_a": 0.0, "hold_lag_s_b": 0.0})
        for key in ("seen_until", "gaps_a", "gaps_b", "reconnects_a", "reconnects_b",
                    "anchoring", "line_a", "line_b", "method", "written_at"):
            self.assertIn(key, rec)       # every field written before is still written
        json.dumps(rec)

    def test_the_repeat_count_is_logged_with_no_freeze_with_the_darkest_and_longest(self):
        for s in range(0, 600, 2):
            at = T0 + timedelta(seconds=s)
            self.run_._frame(self.a, "A", judged(at, grey=80.0 + s % 7))   # a clock: no count
            self.run_._frame(self.b, "B", judged(at, repeats=s % 11, grey=30.0 + s % 5,
                                                 native=0.04 * (s % 9)))
        self.assertEqual(self.run_.records({}), [])      # nothing measured takes nothing
        rec = self.record()
        self.assertEqual(rec["most_repeats_b"], 10)      # under 14: no StreamFrozen, logged
        self.assertAlmostEqual(rec["native_repeat_s_b"], 0.32)
        self.assertEqual(rec["darkest_grey_b"], 30.0)
        self.assertEqual((rec["most_repeats_a"], rec["native_repeat_s_a"], rec["darkest_grey_a"]),
                         (None, None, 80.0))
        self.assertFalse([g for g in self.run_.gaps[("loop", "B")] if "frozen" in g.reason])
        # Taken: the next records hold only what was judged after these.
        for s in range(600, 660, 2):
            at = T0 + timedelta(seconds=s)
            self.run_._frame(self.a, "A", judged(at, grey=90.0))
            self.run_._frame(self.b, "B", judged(at, repeats=2, grey=50.0, native=0.1))
        rec = self.record()
        self.assertEqual((rec["most_repeats_b"], rec["native_repeat_s_b"], rec["darkest_grey_b"],
                          rec["darkest_grey_a"]), (2, 0.1, 50.0, 90.0))
        rec = self.record()                               # no frame since
        self.assertEqual((rec["most_repeats_b"], rec["darkest_grey_b"]), (None, None))

    def test_a_freeze_raised_in_the_cameras_own_thread_reaches_the_record(self):
        given = [judged(T0 + timedelta(seconds=s), repeats=3, grey=60.0, native=0.1)
                 for s in range(60)]
        frozen = StreamFrozen("the picture repeated exactly in 15 of the last 40 pairs",
                              repeats=15, grey=55.5)
        frozen.native_repeat_s = 20.4                     # as hls.HlsReader sets it
        readers = iter([Reader(given, frozen)])

        def next_reader(cam):
            try:
                return next(readers)
            except StopIteration:
                self.run_._stop.set()
                raise StreamLost("script over") from None

        with unittest.mock.patch.object(runner_mod, "live_reader", next_reader), \
                unittest.mock.patch.object(runner_mod, "RECONNECT_S", 0.0):
            reading = threading.Thread(target=self.run_._feed, args=(self.b,),
                                       name="transit-cam-b")
            reading.start()
            reading.join(10)
        self.assertFalse(reading.is_alive())
        for s in range(60):
            self.feed(self.a, T0 + timedelta(seconds=s))
        rec = self.record()
        self.assertEqual((rec["most_repeats_b"], rec["darkest_grey_b"], rec["native_repeat_s_b"]),
                         (15, 55.5, 20.4))
        self.assertTrue([g for g in self.run_.gaps[("loop", "B")] if "frozen" in g.reason])

    def test_the_holds_lag_is_the_latest_frame_past_the_last_that_moved_in_seconds(self):
        for k in range(123):                              # every 0.5 s, to 61 s
            at = T0 + timedelta(seconds=k / 2)
            self.run_._frame(self.a, "A", judged(at))
            self.run_._frame(self.b, "B", judged(at, moved=k <= 21))   # still after 10.5 s
        measured = self.run_.measure()
        self.assertEqual(self.run_.seen_until(), T0 + timedelta(seconds=10.5))
        rec = self.run_.records(measured)[0]
        self.assertEqual((rec["hold_lag_s_a"], rec["hold_lag_s_b"]), (0.0, 50.5))
        self.assertEqual(rec["seen_until"], "2026-10-24T18:00:00+00:00")   # the lag is not cut

    def test_numbers_folded_in_while_another_thread_takes_them_are_never_lost(self):
        top = 2000

        def feed():
            for i in range(top):
                self.run_._frame(self.b, "B", judged(T0 + timedelta(seconds=i / 2), repeats=i,
                                                     grey=float(top - i), native=i / 1000))
        reading = threading.Thread(target=feed, name="transit-cam-b")
        reading.start()
        taken = []
        while reading.is_alive():
            taken.append(self.run_._numbers())
        reading.join()
        taken.append(self.run_._numbers())
        most = [n["most_repeats_b"] for n in taken if n["most_repeats_b"] is not None]
        self.assertEqual(most[-1], top - 1)
        self.assertEqual(most, sorted(most))              # each take holds what came after the last
        self.assertEqual(min(n["darkest_grey_b"] for n in taken
                             if n["darkest_grey_b"] is not None), 1.0)
        self.assertEqual(max(n["native_repeat_s_b"] for n in taken
                             if n["native_repeat_s_b"] is not None), (top - 1) / 1000)


if __name__ == "__main__":
    unittest.main()
