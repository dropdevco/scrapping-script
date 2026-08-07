"""_fit_photo: cover-crop for normal photos, never-crop composite for
edge-to-edge graphics.

Regression coverage for a real bug caught by eyeballing a rendered preview: a
930x560 promotional flyer whose own designer ran the title text edge-to-edge
got center-cropped into 1080x860 and lost 24% of its width, slicing straight
through "VIDA SANA MIDDAY MEDITATION".
"""

from __future__ import annotations

from PIL import Image

from scraper.social.render import CANVAS, PHOTO_H, _fit_photo

TARGET = (CANVAS[0], PHOTO_H)


def test_fit_photo_always_returns_target_size():
    for size in [(930, 560), (1920, 1080), (400, 1600), (1080, 860), (2000, 300)]:
        out = _fit_photo(Image.new("RGB", size, (10, 20, 30)), TARGET)
        assert out.size == TARGET


def test_modest_crop_uses_plain_cover_fit():
    """A photo close to the target's own aspect ratio should get the
    full-bleed treatment — no reason to letterbox something that barely
    needs cropping."""
    # 1080x860 target, aspect 1.256. A 1200x950 source (aspect 1.263) needs a
    # negligible crop.
    photo = Image.new("RGB", (1200, 950), (200, 0, 0))
    out = _fit_photo(photo, TARGET)
    # A cover-fit result is uniformly the source color edge-to-edge; a
    # composite would show the darker blurred backdrop peeking through.
    corner = out.getpixel((2, 2))
    assert corner[0] > 150  # still close to the pure red source, not darkened


def test_extreme_aspect_ratio_is_never_cropped():
    """The exact shape that caused the bug: a wide banner with content near
    both edges. The composite must preserve the FULL source, uncropped —
    verified by checking that distinct colors placed at the extreme left and
    right edges both survive somewhere in the output.
    """
    w, h = 930, 560
    photo = Image.new("RGB", (w, h), (250, 250, 40))  # background: yellow
    # Paint a solid blue block at the far left edge and a solid green block at
    # the far right edge — analogous to text running to both margins.
    for x in range(0, 40):
        for y in range(h):
            photo.putpixel((x, y), (0, 0, 255))
    for x in range(w - 40, w):
        for y in range(h):
            photo.putpixel((x, y), (0, 255, 0))

    out = _fit_photo(photo, TARGET)
    colors = {c for _, c in (out.getcolors(maxcolors=1_000_000) or [])}

    def _has_close(target, tol=40):
        return any(all(abs(c[i] - target[i]) <= tol for i in range(3)) for c in colors)

    assert _has_close((0, 0, 255)), "left-edge content was cropped away"
    assert _has_close((0, 255, 0)), "right-edge content was cropped away"


def test_event_slide_renders_correctly_with_extreme_aspect_photo():
    """End-to-end: the full slide render must still be a valid, correctly
    -sized JPEG when handed a photo that triggers the composite path."""
    from io import BytesIO

    from scraper.social.imaging import SourcePhoto
    from scraper.social.render import render_event_slide

    src = Image.new("RGB", (2000, 300), (80, 40, 160))
    photo = SourcePhoto(image=src, width=2000, height=300)
    row = {"id": "x", "title": "Wide Banner Event", "categories": ["Music"], "venue": "Some Venue"}

    blob = render_event_slide(2, 10, row, photo, None)
    img = Image.open(BytesIO(blob))
    assert img.size == CANVAS
    assert img.format == "JPEG"
