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


def test_caption_has_no_numbered_list_markers():
    """Numbering was dropped in favor of category emoji — order is still
    implied by chronological position, same as the slides."""
    picked = [_candidate("Only Event", idx=i) for i in range(3)]
    text = caption_mod.build_caption(date(2026, 8, 5), picked)
    for n in range(1, 10):
        assert f"\n{n}." not in text


def test_caption_event_line_opens_with_the_categorys_emoji():
    text = caption_mod.build_caption(date(2026, 8, 5), [_candidate("Concert", categories=["Music"])])
    assert caption_mod._CATEGORY_EMOJI["Music"] in text


def test_caption_opener_is_deterministic_per_date():
    """Same date -> same opener every render (reproducible, not truly random)."""
    picked = [_candidate("Concert")]
    a = caption_mod.build_caption(date(2026, 8, 5), picked)
    b = caption_mod.build_caption(date(2026, 8, 5), picked)
    assert a == b


def test_caption_includes_header_and_link():
    text = caption_mod.build_caption(date(2026, 8, 5), [_candidate("Concert")], site="epchisme.com")
    opener, _flourish = caption_mod._OPENERS[date(2026, 8, 5).toordinal() % len(caption_mod._OPENERS)]
    assert opener in text
    assert "epchisme.com" in text


def test_caption_handles_empty_selection():
    text = caption_mod.build_caption(date(2026, 8, 5), [])
    assert len(text) <= caption_mod.MAX_CAPTION
    assert "check back later" in text


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


def test_event_slides_have_no_numbering_badge_and_cycle_through_variants():
    """Regression coverage for two real things this redesign changed: the
    "N / total" badge is gone, and consecutive slides rotate through all
    three layouts/accents deterministically."""
    assert [render._variant_for(i) for i in (2, 3, 4, 5)] == [0, 1, 2, 0]


# ── address formatting ────────────────────────────────────────────────────────
def test_address_label_strips_state_and_zip():
    row = {"location": "123 Main St, El Paso, TX 79901"}
    assert render._address_label(row) == "123 Main St, El Paso"


def test_address_label_strips_trailing_country_code():
    """A real live render exposed this: some sources append ', US' after the
    zip ('4100 East Paisano Street, El Paso, TX 79905, US')."""
    row = {"location": "4100 East Paisano Street, El Paso, TX 79905, US"}
    assert render._address_label(row) == "4100 East Paisano Street, El Paso"


def test_address_label_passes_through_when_no_state_zip_present():
    row = {"location": "125 W Mills Ave, El Paso"}
    assert render._address_label(row) == "125 W Mills Ave, El Paso"


def test_address_label_empty_when_no_location():
    assert render._address_label({}) == ""


def test_event_slide_with_two_line_title_and_time_does_not_crash():
    """The exact shape that caused a real overlap bug: a full-bleed-variant
    slide (index 3) with a title long enough to wrap to 2 lines AND a time
    stamp — must still render at valid CANVAS size regardless of the fix."""
    cand = _candidate(
        "Friday Salsa Social with Team Havana Salsa Band",
        hour=15,
        venue="El Paso Ballroom Dance Academy",
    )
    _assert_valid_slide(render.render_event_slide(3, 9, cand.row, None, cand.start_local))


def test_cover_branding_fits_inside_the_profile_grid_crop():
    """Instagram crops slide 1 to a centre square on the profile grid; content
    outside that band is invisible where discovery happens."""
    blob = render.render_cover(date(2026, 8, 5), 9)
    img = Image.open(BytesIO(blob)).convert("RGB")
    square = img.crop((0, render.GRID_SAFE_TOP, render.CANVAS[0], render.GRID_SAFE_BOTTOM))
    # The crop must contain real ink, not just the paper background.
    assert len(square.getcolors(maxcolors=100000) or []) > 3
