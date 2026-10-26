"""The shared signature form (crossing.look): what reaches transit is a finite point or nothing."""
from __future__ import annotations

import math
import unittest

import pytest

pytest.importorskip("cv2")   # crossing imports hls, which imports the stream reader

from scraper.border.camera.crossing import look


class Look(unittest.TestCase):
    def test_each_part_is_clamped_to_its_range_and_rounded(self):
        self.assertEqual(look(-1.7, 0.12345, 1.2, -0.1), (-1.0, 0.123, 1.0, 0.0))

    def test_a_part_that_is_not_a_number_gives_no_signature(self):
        # min/max pass NaN through, and transit refuses a whole method's crossings over one.
        for bad in (math.nan, math.inf, -math.inf):
            for i in range(4):
                parts = [0.1, 0.2, 0.5, 0.6]
                parts[i] = bad
                with self.subTest(part=i, value=bad):
                    self.assertIsNone(look(*parts))


if __name__ == "__main__":
    unittest.main()
