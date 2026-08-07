"""Photo quality gate + untrustworthy-timestamp suppression."""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

from PIL import Image

from scraper.social import caption as caption_mod
from scraper.social import selection
from scraper.social.imaging import MAX_UPSCALE, MIN_ABS_SIDE, SourcePhoto

TZ = ZoneInfo("America/Denver")
TARGET = (1080, 860)


def _photo(w: int, h: int) -> SourcePhoto:
    return SourcePhoto(image=Image.new("RGB", (w, h)), width=w, height=h)


def test_upscale_gate_accepts_real_source_derivatives():
    """Visit El Paso serves 930x560 'optimized' derivatives. A flat 640px
    short-side floor wrongly rejected all of them even though they only need
    ~1.5x to fill the slide."""
    assert _photo(930, 560).upscale_for(TARGET) <= MAX_UPSCALE


def test_upscale_gate_rejects_thumbnails():
    assert _photo(400, 300).upscale_for(TARGET) > MAX_UPSCALE


def test_upscale_gate_rejects_wide_short_banners():
    """A 2000x300 banner passes any short-side check that 930x560 passes, but
    its height still needs ~2.9x — which is exactly what a ratio-aware gate
    catches and a pixel floor does not."""
    banner = _photo(2000, 300)
    assert banner.short_side < 560  # would fool a naive floor comparison
    assert banner.upscale_for(TARGET) > MAX_UPSCALE


def test_large_photo_needs_no_upscale():
    assert _photo(1920, 1080).upscale_for(TARGET) <= 1.0


def test_min_abs_side_is_below_the_real_world_derivative_size():
    assert MIN_ABS_SIDE < 560


# ── untrustworthy timestamps ───────────────────────────────────────────────────
def test_early_morning_times_are_not_trusted():
    """~45% of stored rows claim a 2-4am start because some sources' naive local
    times are persisted as UTC. We can't distinguish those from a real 2am, so
    they're shown without a time rather than with a wrong one."""
    for hour in (0, 2, 4, 5):
        assert not selection.has_plausible_time(datetime(2026, 8, 5, hour, tzinfo=TZ))


def test_normal_times_are_trusted():
    for hour in (6, 10, 14, 19, 23):
        assert selection.has_plausible_time(datetime(2026, 8, 5, hour, tzinfo=TZ))


def test_missing_time_is_not_plausible():
    assert not selection.has_plausible_time(None)


def test_caption_omits_untrustworthy_times():
    cand = selection.Candidate(
        row={"id": "1", "title": "Toddler Storytime", "categories": ["Family"], "venue": "Library"},
        key="k",
        score=1.0,
        start_local=datetime(2026, 8, 5, 2, 0, tzinfo=TZ),
    )
    text = caption_mod.build_caption(date(2026, 8, 5), [cand])
    assert "2:00AM" not in text and "2AM" not in text
    assert "Toddler Storytime" in text


def test_caption_keeps_trustworthy_times():
    cand = selection.Candidate(
        row={"id": "1", "title": "Evening Concert", "categories": ["Music"], "venue": "Plaza"},
        key="k",
        score=1.0,
        start_local=datetime(2026, 8, 5, 19, 30, tzinfo=TZ),
    )
    text = caption_mod.build_caption(date(2026, 8, 5), [cand])
    assert "7:30PM" in text
