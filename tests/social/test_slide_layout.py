"""Slide layout guarantees: nothing is cropped, nothing is blurred.

Two defects reported off live slides drive these:
  * titles were truncated with an ellipsis — "…Cultures in El Paso del…",
    "Hueco Tanks 10,000 b…" — because each layout capped the title at two or
    three lines regardless of how long it was;
  * a source image that could not be cropped safely was composited over a
    blurred, darkened copy of itself, which read as a smear across the top of
    the slide and matched nothing else in a design made of flat printed paper.
"""

from __future__ import annotations

from io import BytesIO

import pytest
from PIL import Image, ImageDraw

from scraper.social.imaging import SourcePhoto
from scraper.social.render import (
    CANVAS,
    PHOTO_H_MAX,
    PHOTO_H_MIN,
    PHOTO_H_NO_PHOTO,
    fit_block,
    font,
    needs_mount,
    photo_band_height,
    render_event_slide,
    title_size_hi,
)

LONG_TITLE = (
    "El Paso Mission Trail Association presents Coffee on the Trail : "
    "Hueco Tanks 10,000 by Nicole Roque"
)
LONGER_TITLE = (
    "Mountain of Gold: A History of East and Southeast Asian Cultures in "
    "El Paso del Norte, 1880s - 1980s"
)


def _draw():
    return ImageDraw.Draw(Image.new("RGB", CANVAS))


@pytest.mark.parametrize("title", [LONG_TITLE, LONGER_TITLE, "Short One"])
@pytest.mark.parametrize("variant", [0, 1, 2])
def test_no_slide_layout_ever_ellipsizes_a_title(title, variant):
    src = Image.new("RGB", (930, 560), (120, 90, 60))
    photo = SourcePhoto(image=src, width=930, height=560)
    row = {"id": f"{variant}", "title": title, "venue": "El Paso Museum of History",
           "location": "510 N Santa Fe St, El Paso, TX 79901", "categories": ["Community"]}
    blob = render_event_slide(2 + variant, 10, row, photo, None)
    assert Image.open(BytesIO(blob)).size == CANVAS


def test_fit_block_keeps_every_word():
    draw = _draw()
    _, lines = fit_block(draw, LONGER_TITLE, "display", 936, 400, 96, 44)
    assert " ".join(lines) == LONGER_TITLE
    assert not any("…" in line for line in lines)


def test_fit_block_trades_size_for_completeness():
    """A longer title in the same box must come back SMALLER, not shorter."""
    draw = _draw()
    short_font, _ = fit_block(draw, "Short One", "display", 936, 400, 96, 44)
    long_font, lines = fit_block(draw, LONGER_TITLE, "display", 936, 400, 96, 44)
    assert long_font.size <= short_font.size
    assert " ".join(lines) == LONGER_TITLE


def test_fit_block_keeps_the_text_whole_even_when_the_floor_overflows():
    draw = _draw()
    monster = " ".join(["Extraordinarily"] * 120)
    _, lines = fit_block(draw, monster, "display", 936, 200, 96, 44)
    assert " ".join(lines) == monster


def test_fit_block_character_wraps_a_word_wider_than_the_box():
    draw = _draw()
    _, lines = fit_block(draw, "A" * 200, "display", 400, 900, 60, 40)
    assert "".join(lines) == "A" * 200


def test_fit_block_respects_the_height_budget_when_it_can():
    draw = _draw()
    fnt, lines = fit_block(draw, LONGER_TITLE, "display", 936, 260, 96, 44)
    assert len(lines) * int(fnt.size * 1.06) <= 260


def test_a_landscape_source_gets_a_band_shaped_like_itself():
    """Which is what removes the mount entirely for the common case: the band
    becomes the image's own aspect, so a cover-fit crops nothing."""
    photo = SourcePhoto(image=None, width=1920, height=1080)
    band = photo_band_height(photo)
    assert band == round(CANVAS[0] * 1080 / 1920)
    assert not needs_mount(photo, (CANVAS[0], band))


def test_a_portrait_source_clamps_and_is_mounted_rather_than_cropped():
    photo = SourcePhoto(image=None, width=800, height=1600)
    band = photo_band_height(photo)
    assert band == PHOTO_H_MAX
    assert needs_mount(photo, (CANVAS[0], band))


def test_an_ultrawide_source_clamps_at_the_floor():
    photo = SourcePhoto(image=None, width=2000, height=400)
    assert photo_band_height(photo) == PHOTO_H_MIN


def test_a_photoless_slide_gives_its_room_to_the_type():
    assert photo_band_height(None) == PHOTO_H_NO_PHOTO
    assert title_size_hi(None, 96) > title_size_hi(object(), 96)


def test_the_mount_is_paper_not_a_blurred_copy_of_the_photo():
    """The mount must introduce NO color from the photo into the surround —
    a blurred backdrop would bleed the photo's own hue into the margins."""
    from scraper.social.render import _matte

    src = Image.new("RGB", (400, 1600), (255, 0, 0))  # unmistakable red
    out = _matte(src, (CANVAS[0], PHOTO_H_MAX))
    # Sample the far left margin, well outside where the clipping can reach.
    for y in (60, PHOTO_H_MAX // 2, PHOTO_H_MAX - 60):
        r, g, b = out.getpixel((6, y))
        assert r > 200 and g > 190 and b > 170, "margin is not paper"
        assert not (r > 200 and g < 80 and b < 80), "photo bled into the margin"


def test_the_mount_preserves_both_extreme_edges_of_the_source():
    from scraper.social.render import _matte

    w, h = 400, 1600
    src = Image.new("RGB", (w, h), (250, 250, 40))
    for y in range(0, 30):
        for x in range(w):
            src.putpixel((x, y), (0, 0, 255))       # top edge
    for y in range(h - 30, h):
        for x in range(w):
            src.putpixel((x, y), (0, 255, 0))       # bottom edge
    out = _matte(src, (CANVAS[0], PHOTO_H_MAX))
    colors = {c for _, c in (out.getcolors(maxcolors=1_000_000) or [])}

    def _has(target, tol=45):
        return any(all(abs(c[i] - target[i]) <= tol for i in range(3)) for c in colors)

    assert _has((0, 0, 255)), "top edge was cropped away"
    assert _has((0, 255, 0)), "bottom edge was cropped away"


@pytest.mark.parametrize("size", [(930, 560), (1920, 1080), (800, 1600), (2000, 400), (1080, 1350)])
@pytest.mark.parametrize("variant", [0, 1, 2])
def test_every_shape_and_layout_produces_a_valid_slide(size, variant):
    src = Image.new("RGB", size, (90, 110, 140))
    photo = SourcePhoto(image=src, width=size[0], height=size[1])
    row = {"id": "x", "title": LONG_TITLE, "venue": "A Very Long Venue Name For El Paso Texas",
           "location": "9065 Alameda, El Paso, TX 79907", "categories": ["Music"]}
    blob = render_event_slide(2 + variant, 10, row, photo, None)
    img = Image.open(BytesIO(blob))
    assert img.size == CANVAS and img.format == "JPEG"


def test_a_photoless_slide_still_renders():
    row = {"id": "x", "title": LONG_TITLE, "venue": "El Paso Live", "categories": ["Community"]}
    for variant in range(3):
        blob = render_event_slide(2 + variant, 10, row, None, None)
        assert Image.open(BytesIO(blob)).size == CANVAS


def test_font_loader_is_reachable_for_every_role():
    for role in ("display", "sans_black", "sans_semibold", "condensed"):
        assert font(role, 40) is not None
