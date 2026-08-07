from __future__ import annotations

from datetime import date, datetime
from io import BytesIO
from zoneinfo import ZoneInfo

from PIL import Image

from scraper.social import caption as caption_mod
from scraper.social import render, selection

TZ = "America/Denver"


def _candidate(title: str, hour: int = 19, categories=None, venue="The Venue", idx: int = 0):
    row = {
        "id": f"id-{idx}",
        "title": title,
        "categories": categories or ["Music"],
        "venue": venue,
        "location": "123 Main St, El Paso, TX 79901",
        "start_time": f"2026-08-05T{hour:02d}:00:00-06:00",
    }
    return selection.Candidate(
        row=row,
        key=f"key-{idx}",
        score=1.0,
        start_local=datetime(2026, 8, 5, hour, 0, tzinfo=ZoneInfo(TZ)),
    )


# ── caption ────────────────────────────────────────────────────────────────────
def test_caption_stays_within_budget_with_long_titles():
    picked = [_candidate("X" * 120, idx=i) for i in range(9)]
    text = caption_mod.build_caption(date(2026, 8, 5), picked)
    assert len(text) <= caption_mod.MAX_CAPTION


def test_caption_hashtag_count_is_capped():
    picked = [
        _candidate("Show", categories=[c], idx=i)
        for i, c in enumerate(
            ["Music", "Festivals", "Food & Drink", "Arts & Theatre", "Sports", "Family", "Tech"]
        )
    ]
    text = caption_mod.build_caption(date(2026, 8, 5), picked)
    assert text.count("#") <= caption_mod.MAX_HASHTAGS


def test_caption_numbering_starts_at_two_because_slide_one_is_the_cover():
    text = caption_mod.build_caption(date(2026, 8, 5), [_candidate("Only Event")])
    assert "\n2." in text
    assert "\n1." not in text


def test_caption_includes_header_and_link():
    text = caption_mod.build_caption(date(2026, 8, 5), [_candidate("Concert")], site="epchisme.com")
    assert "TODAY IN EL PASO" in text
    assert "epchisme.com" in text


def test_caption_handles_empty_selection():
    text = caption_mod.build_caption(date(2026, 8, 5), [])
    assert "TODAY IN EL PASO" in text
    assert len(text) <= caption_mod.MAX_CAPTION


# ── text fitting ───────────────────────────────────────────────────────────────
def _draw():
    return Image.new("RGB", render.CANVAS), None


def test_wrap_to_fit_respects_max_lines_at_every_length():
    from PIL import ImageDraw

    img = Image.new("RGB", render.CANVAS)
    draw = ImageDraw.Draw(img)
    box = render.CANVAS[0] - render.SAFE_X * 2
    titles = (
        "Short",
        "A moderately long event title here",
        "Word " * 60,
        "Supercalifragilistic" * 6,
    )
    for title in titles:
        _, lines = render.wrap_to_fit(draw, title, "display", box, 3, 88, 56)
        assert 1 <= len(lines) <= 3, title


def test_wrap_to_fit_ellipsizes_overflow():
    from PIL import ImageDraw

    img = Image.new("RGB", render.CANVAS)
    draw = ImageDraw.Draw(img)
    box = render.CANVAS[0] - render.SAFE_X * 2
    _, lines = render.wrap_to_fit(draw, "word " * 200, "display", box, 3, 88, 56)
    assert lines[-1].endswith("…")


# ── rendering ──────────────────────────────────────────────────────────────────
def _assert_valid_slide(blob: bytes):
    assert len(blob) < 8 * 1024 * 1024
    img = Image.open(BytesIO(blob))
    assert img.size == render.CANVAS
    # JPEG is the ONLY format Instagram's publishing API accepts.
    assert img.format == "JPEG"


def test_cover_slide_is_a_valid_jpeg():
    _assert_valid_slide(render.render_cover(date(2026, 8, 5), 9))


def test_event_slide_without_photo_still_renders():
    """A missing photo must degrade to the branded panel, never crash."""
    cand = _candidate("Some Event With A Fairly Long Name To Wrap")
    _assert_valid_slide(render.render_event_slide(2, 10, cand.row, None, cand.start_local))


def test_event_slide_with_photo_renders():
    from scraper.social.imaging import SourcePhoto

    src = Image.new("RGB", (1600, 900), (10, 120, 200))
    photo = SourcePhoto(image=src, width=1600, height=900)
    cand = _candidate("Concert At The Plaza")
    _assert_valid_slide(render.render_event_slide(3, 10, cand.row, photo, cand.start_local))


def test_event_slide_survives_missing_venue_and_categories():
    row = {"id": "x", "title": "Bare Minimum Event", "categories": [], "location": ""}
    _assert_valid_slide(render.render_event_slide(2, 5, row, None, None))


def test_cover_branding_fits_inside_the_profile_grid_crop():
    """Instagram crops slide 1 to a centre square on the profile grid; content
    outside that band is invisible where discovery happens."""
    blob = render.render_cover(date(2026, 8, 5), 9)
    img = Image.open(BytesIO(blob)).convert("RGB")
    square = img.crop((0, render.GRID_SAFE_TOP, render.CANVAS[0], render.GRID_SAFE_BOTTOM))
    # The crop must contain real ink, not just the paper background.
    assert len(square.getcolors(maxcolors=100000) or []) > 3
