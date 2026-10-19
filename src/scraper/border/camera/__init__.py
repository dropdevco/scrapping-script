"""Reading queue length and queue speed off the bridge cameras, deterministically.

No model, no training data, nothing random: the same frames always give the same answer.

    1. calibration  a camera's road plane: four image points of a rectangle on the road,
                    and each lane as a strip of that plane, booth end first
    2. measure      per frame, which metres of the strip hold a vehicle (difference from an
                    empty-road reference); per frame pair, how far the vehicles moved toward
                    the booth (1-D correlation of the strip's profile)
    3. window       over a minute of frames: the queue is the run of metres occupied most of
                    the time, starting at the booth end; its speed is the time-averaged speed
                    of whatever is in it, stationary cars included

    wait ≈ (distance from the visible queue's booth end to the booth + visible length) / speed

That is Little's law for one lane: a car at the tail covers the queue's length at the
queue's average speed. When the whole queue and the booth are in view, an error in the
calibration's scale stretches length and speed by the same factor and cancels out of the
wait. When the booth is out of view the distance to it does not cancel, which is why each
calibration records whether its scale was verified.

Needs the `camera` extra (OpenCV, numpy, yt-dlp). Nothing outside this package imports it,
so `poll` never pays for OpenCV.
"""
