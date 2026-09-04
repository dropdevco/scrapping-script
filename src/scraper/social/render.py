"""Render carousel slides as JPEG with Pillow.

JPEG is not a preference — it is the only format Instagram's Content Publishing
API accepts. That single constraint is why rendering lives here in Python rather
than reusing the web app's Next.js `ImageResponse`, which emits PNG.

Every slide is rendered at exactly CANVAS. Instagram crops all carousel items to
the first item's aspect ratio, so identical dimensions make that rule a no-op —
the assertion at the end of each render is what keeps it that way.

Visual language is a gossip-tabloid / newspaper-clipping collage: torn-paper
seams, a taped-down corner, halftone dots — using ONLY the brand's own four
colors (paper, ink, cosmo pink, pop yellow), never an outside hue. Event
slides rotate through three layouts AND two accents independently (3x2 = 6
combinations before repeating) so a carousel doesn't read as one template
stamped out N times — see `_variant_for`/`_accent_for` below.
"""

from __future__ import annotations

import hashlib
import logging
import random
import re
from datetime import date
from pathlib import Path
from typing import Any, Optional

from .imaging import SourcePhoto

log = logging.getLogger("scraper.social.render")

CANVAS = (1080, 1350)  # 4:5 — the tallest portrait Instagram allows, max feed real estate
SAFE_X = 72
# The nominal photo band. No layout uses it as a fixed seam any more — each one
# sizes its band from the source image and the title (see photo_band_height /
# _seam_for) — but it remains the REFERENCE target that imaging.py measures a
# photo's required upscale against, so the quality gate keeps judging every
# source on the same yardstick instead of a per-slide one.
PHOTO_H = 860

# Instagram's PROFILE GRID crops slide 1 to a centered square. Anything outside
# this band is invisible on the profile, which is where discovery happens, so
# all cover branding must sit inside it.
GRID_SAFE_TOP = (CANVAS[1] - CANVAS[0]) // 2          # 135
GRID_SAFE_BOTTOM = GRID_SAFE_TOP + CANVAS[0]          # 1215

# Brand tokens, mirrored from web/src/app/globals.css. ONLY these four colors
# (plus their neutral tints below) are allowed anywhere in this module — no
# outside hue, ever, even for a single accent. That constraint is deliberate,
# not a limitation: it's what keeps a collage of torn paper and tape reading
# as "this brand's gossip board" instead of generic clip-art.
PAPER = (251, 246, 236)
CARD = (255, 252, 245)
INK = (20, 17, 24)
INK_SOFT = (74, 69, 80)
COSMO = (230, 17, 127)
POP_YELLOW = (255, 210, 30)
LINE = (224, 213, 192)
# A muted paper/ink blend for the "tape" accent — a tint, not a new hue.
TAPE = (214, 204, 184)

# Event slides pick a LAYOUT and an ACCENT independently (see _variant_for /
# _accent_for) — 3 layouts x 2 accents = 6 combinations before the cycle
# repeats, real variety across a 6-10 slide carousel from only 2 accent colors.
_ACCENTS = (COSMO, POP_YELLOW)

_FONT_DIR = Path(__file__).resolve().parents[3] / "assets" / "fonts"

# Static instances, NOT the variable files Google Fonts serves by default:
# ImageFont.truetype() on a variable font silently renders the default (Regular)
# instance, so a "Black Italic" request would come out looking wrong with no
# error. Every role below is a bold-or-heavier weight on purpose — nothing in
# this design reads as "regular" text anywhere.
_FONT_FILES = {
    "display": "Anton-Regular.ttf",                 # headlines — see note below
    "sans_black": "Archivo-Black.ttf",              # venue names — the boldest body text
    "sans_semibold": "Archivo-SemiBold.ttf",        # addresses — still bold, one step down
    "condensed": "Oswald-Bold.ttf",                 # labels, kickers, time stamps, chips
}
# Cosmopolitan's own masthead/headline face is Franklin Gothic Extra Condensed
# (Morris Fuller Benton, ATF, 1902) — a commercial font we can't legally vendor.
# Oswald ("condensed" above) is Google's own reworking of Alternate Gothic,
# Franklin Gothic's direct sibling from the same ATF family, so it was already
# the right lineage. Anton is that same grotesque-condensed gothic style at a
# true black weight — Oswald tops out at 700 (Bold), and "a more bold, thick
# font for most of the text" needed heavier than that for headlines.

_warned_fonts: set[str] = set()


def _variant_for(index: int, builder_count: Optional[int] = None) -> int:
    """Slide 2 -> 0, 3 -> 1, 4 -> 2, 5 -> 0, ... — deterministic so re-rendering
    the same day produces the same carousel, not a different one each time.

    Modulo the ACTUAL builder count, not a hardcoded 3. It was hardcoded while
    _accent_for correctly used len(_ACCENTS), which made adding a fourth layout
    a silent no-op: the new builder would sit in the tuple and never be
    reached, with nothing failing to say so.

    Resolved inside the body, not as a default argument: _SLIDE_BUILDERS is
    defined below (it needs the builder functions), and a default is evaluated
    at def time."""
    return (index - 2) % (builder_count or len(_SLIDE_BUILDERS))


def _accent_for(index: int) -> tuple[int, int, int]:
    """Cycles independently of the layout (period 2 vs period 3), so the
    (layout, accent) pair only repeats every 6 slides, not every 2 or 3."""
    return _ACCENTS[(index - 2) % len(_ACCENTS)]


def _on_accent(accent: tuple[int, int, int]) -> tuple[int, int, int]:
    """Readable text color for text sitting ON a solid accent background.
    POP_YELLOW is light enough that paper-colored text nearly disappears;
    COSMO is dark enough that ink-colored text nearly disappears — never
    hardcode one or the other, always ask which accent you're on."""
    return INK if accent == POP_YELLOW else PAPER


def _stable_seed(value: str) -> int:
    """A deterministic seed from a stable identity (an event's id, or its
    title as a fallback) — NOT Python's built-in hash(), which is randomized
    per-process and would make the same event tear differently every run."""
    return int(hashlib.sha1(value.encode()).hexdigest()[:8], 16)


def font(role: str, size: int):
    """Load a vendored static font, falling back to Pillow's built-in.

    The fallback keeps rendering (and the test suite) working before the TTFs
    are vendored, but warns once per role so a CI run can't quietly ship
    off-brand slides.
    """
    from PIL import ImageFont

    path = _FONT_DIR / _FONT_FILES[role]
    if path.exists():
        return ImageFont.truetype(str(path), size)
    if role not in _warned_fonts:
        _warned_fonts.add(role)
        log.warning(
            "font %s missing at %s — falling back to Pillow default; "
            "see assets/fonts/README.md",
            _FONT_FILES[role],
            path,
        )
    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # Pillow < 10.1 has no size arg
        return ImageFont.load_default()


def _text_width(draw, text: str, fnt) -> int:
    return int(draw.textlength(text, font=fnt))


def wrap_to_fit(
    draw,
    text: str,
    role: str,
    box_w: int,
    max_lines: int,
    size_hi: int,
    size_lo: int,
    step: int = 4,
):
    """Largest size in [size_lo, size_hi] whose greedy wrap fits max_lines.

    Pillow has no text layout engine — no wrapping, no auto-fit — so this is the
    piece that keeps a 12-character title and a 200-character one both looking
    deliberate in the same slot. Returns (font, lines); the last line is
    ellipsized if even size_lo overflows.
    """
    words = text.split()
    chosen_font = font(role, size_lo)
    chosen_lines = [text]

    for size in range(size_hi, size_lo - 1, -step):
        fnt = font(role, size)
        lines: list[str] = []
        current = ""
        overflow = False
        for word in words:
            trial = f"{current} {word}".strip()
            if _text_width(draw, trial, fnt) <= box_w:
                current = trial
                continue
            if current:
                lines.append(current)
            # A single word wider than the box (rare: long hyphenless names)
            # falls back to character wrapping so it can't loop forever.
            if _text_width(draw, word, fnt) > box_w:
                chunk = ""
                for ch in word:
                    if _text_width(draw, chunk + ch, fnt) <= box_w:
                        chunk += ch
                    else:
                        lines.append(chunk)
                        chunk = ch
                current = chunk
            else:
                current = word
            if len(lines) > max_lines:
                overflow = True
                break
        if current:
            lines.append(current)
        if not overflow and len(lines) <= max_lines:
            return fnt, lines
        chosen_font, chosen_lines = fnt, lines

    lines = chosen_lines[:max_lines]
    if lines:
        last = lines[-1]
        while last and _text_width(draw, last + "…", chosen_font) > box_w:
            last = last[:-1]
        lines[-1] = last.rstrip() + "…"
    return chosen_font, lines


# The photo band is elastic, not a fixed slab. Its height is chosen per slide so
# that a source image can be shown WHOLE and edge-to-edge whenever its own
# proportions allow — a 16:9 flyer gets a short wide band it fills exactly, a
# squarer photo gets a tall one. Only a source too tall to fill the width falls
# through to the paper mount.
#
# The bounds are what keeps the carousel coherent: below PHOTO_H_MIN the slide
# stops reading as photo-led, above PHOTO_H_MAX the text panel gets too cramped
# for a long title. Everything in between is fair game, and the variation
# between slides is itself part of why the carousel doesn't read as one template
# stamped out N times.
PHOTO_H_MIN = 540
PHOTO_H_MAX = 900


# A slide with no usable photo shows the halftone placeholder instead, and there
# is no reason to give 900px of the canvas to a dot pattern. Shrinking its band
# turns a weak slide into a deliberately typographic one — the title gets the
# room the missing photo would have had.
PHOTO_H_NO_PHOTO = 560

# ...and the title takes the room the photo would have had. Without this the
# type stayed at its photo-slide size and the slide read as mostly empty:
# a band of dots on top, a band of flat accent below, and two lines of text
# floating between them.
_NO_PHOTO_TITLE_SCALE = 1.5


def title_size_hi(photo, base: int) -> int:
    """Largest display size a title may use on this slide.

    `fit_block` only ever shrinks from this ceiling, so raising it for a
    photoless slide is what lets the title actually fill the space rather than
    sitting at the size that suited a slide with a photo in it."""
    return base if photo is not None else int(base * _NO_PHOTO_TITLE_SCALE)


def photo_band_height(photo, *, preferred: int = PHOTO_H_MAX, floor: int = PHOTO_H_MIN) -> int:
    """Band height that lets `photo` fill the canvas width without cropping.

    Clamps to [floor, preferred]. A source taller than `preferred` at full width
    — a portrait poster — clamps to `preferred` and is mounted rather than
    cropped. With no photo at all, see PHOTO_H_NO_PHOTO.
    """
    if photo is None or not photo.width:
        return min(preferred, PHOTO_H_NO_PHOTO)
    natural = round(CANVAS[0] * photo.height / photo.width)
    return max(floor, min(preferred, natural))


def fit_block(
    draw,
    text: str,
    role: str,
    box_w: int,
    max_h: int,
    size_hi: int,
    size_lo: int,
    *,
    line_ratio: float = 1.06,
    step: int = 4,
):
    """Largest size in [size_lo, size_hi] whose wrap fits inside `max_h` px.

    The difference from `wrap_to_fit` is the constraint: a HEIGHT budget rather
    than a line count. That is what stops a title being truncated. Capping lines
    meant a title needing four of them lost its tail to an ellipsis — real
    slides shipped reading "…Cultures in El Paso del…" and "Hueco Tanks 10,000
    b…", which is worse than useless: the reader cannot tell what the event is,
    and the information was there all along.

    Trading type size for completeness is the right way round for this content.
    Returns (font, lines) with the text always complete.
    """
    words = text.split()
    for size in range(size_hi, size_lo - 1, -step):
        fnt = font(role, size)
        lines = _greedy_wrap(draw, words, fnt, box_w)
        if len(lines) * int(size * line_ratio) <= max_h:
            return fnt, lines
    # Even the floor overflows — an extreme outlier. Keep the text whole and let
    # the caller's own bottom guard decide what to do with the overflow, rather
    # than silently amputating the title here.
    fnt = font(role, size_lo)
    return fnt, _greedy_wrap(draw, words, fnt, box_w)


def _greedy_wrap(draw, words: list[str], fnt, box_w: int) -> list[str]:
    """Greedy line breaking, with character wrapping for a single word wider
    than the box (rare: long hyphenless names) so it can't loop forever."""
    lines: list[str] = []
    current = ""
    for word in words:
        trial = f"{current} {word}".strip()
        if _text_width(draw, trial, fnt) <= box_w:
            current = trial
            continue
        if current:
            lines.append(current)
        if _text_width(draw, word, fnt) > box_w:
            chunk = ""
            for ch in word:
                if _text_width(draw, chunk + ch, fnt) <= box_w:
                    chunk += ch
                else:
                    lines.append(chunk)
                    chunk = ch
            current = chunk
        else:
            current = word
    if current:
        lines.append(current)
    return lines or [""]


# Vertical space the venue / address / category stack under a title needs. Sized
# for the worst realistic case — a two-line venue name, a two-line address and a
# category chip — because every one of those blocks may now wrap rather than
# ellipsize, and a reserve that assumed one line each is how a long venue name
# would push the chip off the bottom edge.
_RESERVED_BELOW_TITLE = 265


def _seam_for(draw, photo, title: str, box_w: int, *, size_hi: int, reserved: int,
              size_lo: int = 44, top_pad: int = 100) -> int:
    """Where the photo band should end on a photo-top/panel-bottom layout.

    Two claims on the same 1350px: the photo wants to be tall enough to read as
    the subject, the title wants to be big enough to read at all. Resolved in
    that order — the band starts at the height that shows this particular image
    whole (see `photo_band_height`) and then gives ground, down to PHOTO_H_MIN,
    only as far as the title actually needs.

    The alternative, a fixed seam, is what produced the truncated titles: the
    panel's height was decided before anyone knew how long the title was.
    """
    band = photo_band_height(photo)
    title_font, lines = fit_block(
        draw, title, "display", box_w, CANVAS[1] - band - top_pad - reserved, size_hi, size_lo
    )
    needed = len(lines) * int(title_font.size * 1.06) + top_pad + reserved
    return max(PHOTO_H_MIN, min(band, CANVAS[1] - needed))


def _card_top_for(draw, title: str, box_w: int, *, size_hi: int, reserved: int,
                  size_lo: int = 42, default_top: int = 860, slant: int = 46,
                  top_pad: int = 46) -> int:
    """Where the overlapping text card starts on the full-bleed layout.

    Same bargain as `_seam_for`, but here the card slides UP over the photo
    instead of the photo shrinking — the photo is full-canvas by design, so
    there is nothing to shrink. Never rises past the profile-grid safe band's
    lower edge, which would start eating the part of the image the square crop
    shows.
    """
    title_font, lines = fit_block(
        draw, title, "display", box_w,
        CANVAS[1] - default_top - slant - top_pad - reserved, size_hi, size_lo,
    )
    needed = len(lines) * int(title_font.size * 1.06) + slant + top_pad + reserved
    return max(GRID_SAFE_BOTTOM - CANVAS[0] // 2, min(default_top, CANVAS[1] - needed))


def _photo_for_overlay(img, visible_bottom: int, *, seed: int = 0):
    """Full-canvas composition for a layout whose lower part is covered.

    A croppable photo fills the whole canvas as before. A source that has to be
    mounted instead gets mounted within the VISIBLE region only, then that board
    is placed on a full-canvas paper ground — so the uncropped image the mount
    exists to preserve is actually on screen rather than behind the card.
    """
    cover_scale = max(CANVAS[0] / img.width, CANVAS[1] / img.height)
    crop_fraction = max(
        1 - CANVAS[0] / (img.width * cover_scale), 1 - CANVAS[1] / (img.height * cover_scale)
    )
    if crop_fraction <= _MAX_CROP_FRACTION:
        return _cover_fit(img, CANVAS)

    visible = max(PHOTO_H_MIN, min(CANVAS[1], visible_bottom))
    # The board covers the whole canvas so there is no seam between it and the
    # ground; only the clipping is confined to the visible region.
    return _matte(img, CANVAS, seed=seed, region=(0, visible))


def _cover_fit(img, size: tuple[int, int]):
    """Scale to fill then center-crop — never letterbox, never distort."""
    from PIL import Image

    target_w, target_h = size
    scale = max(target_w / img.width, target_h / img.height)
    new = img.resize((max(1, round(img.width * scale)), max(1, round(img.height * scale))),
                     Image.LANCZOS)
    left = (new.width - target_w) // 2
    top = (new.height - target_h) // 2
    return new.crop((left, top, left + target_w, top + target_h))


# Fraction of the source a plain center-crop would discard, above which we
# stop trusting a crop at all. Genuine event photos (people, a venue, a
# stage) are forgiving of a 10-15% crop — nobody notices a shoulder got cut.
# A PROMOTIONAL FLYER is not: several of these sources hand over graphics
# whose own designer ran the headline text edge-to-edge with zero margin
# (confirmed on a live slide — a wide flyer center-cropped into 1080x860 lost
# 24% of its width and sliced straight through "VIDA SANA MIDDAY
# MEDITATION"). We can't reliably tell "photo" from "flyer" from pixels
# alone, so instead of guessing, this fits the WHOLE image in in all cases
# above the threshold — nothing is ever cropped, at the cost of some
# padding on photos that didn't strictly need it.
_MAX_CROP_FRACTION = 0.12

# How the leftover space around a whole, uncropped image is filled. The first
# version of this used a blurred, darkened copy of the photo — the Instagram
# Stories idiom — and it read as exactly what it was: a smear. It also fought
# the rest of the design, which is flat printed paper with torn edges, tape and
# halftone screens, and has no other soft-focus element anywhere.
#
# So the leftover space is now PAPER instead: the image sits on the board like a
# clipping someone pasted there, with a hard ink rule around it and the same
# hard offset shadow the web app puts on its cards
# (shadow-[3px_3px_0_var(--color-ink)] in globals.css). Nothing is blurred,
# nothing is cropped, and the padding reads as a deliberate mount rather than as
# the renderer having run out of image.
# Below this much cropping the band is effectively the image's own shape, which
# means the source is a graphic being shown whole — darkening its bottom edge
# would undo the point.
_SCRIM_MIN_CROP = 0.02
_MATTE_INSET = 26        # paper visible around the clipping, minimum
_MATTE_SHADOW = 9        # hard offset shadow, matching the web app's card style


def _mount_padding() -> int:
    return 2 * (_MATTE_INSET + _MATTE_SHADOW)


def mount_height(photo, max_h: int) -> int:
    """Board height that shows `photo` whole at the canvas's full width.

    Lets a layout hug a mounted image instead of centering it in a band sized
    for something else — an ultrawide flyer mounted inside an 860px region left
    a quarter of the slide as empty paper above and below it.
    """
    if photo is None or not photo.width:
        return max_h
    pad = _mount_padding()
    natural = round(photo.height * ((CANVAS[0] - pad) / photo.width)) + pad
    return max(PHOTO_H_MIN, min(max_h, natural))


def crop_fraction(photo, size: tuple[int, int]) -> float:
    """How much of `photo` a cover-fit into `size` would discard."""
    if photo is None or not photo.width or not photo.height:
        return 0.0
    target_w, target_h = size
    cover = max(target_w / photo.width, target_h / photo.height)
    return max(1 - target_w / (photo.width * cover), 1 - target_h / (photo.height * cover))


def needs_mount(photo, size: tuple[int, int]) -> bool:
    """Would `_fit_photo` mount this rather than cover-fit it?

    Callers need to know because a mounted board is paper, and the treatments
    that suit a photograph — the scrim fading its lower edge into the seam —
    read as a smudge on paper.
    """
    return crop_fraction(photo, size) > _MAX_CROP_FRACTION


def _matte(img, size: tuple[int, int], *, seed: int = 0, region: Optional[tuple[int, int]] = None):
    """Mount the WHOLE image on a paper board — no crop, no blur.

    Used whenever the source's proportions are too far from the band's for a
    center-crop to be safe. Which axis ends up with visible paper follows from
    the source itself: a tall poster is matted left and right, a wide flyer top
    and bottom, and in both cases every pixel of the original survives.

    `region` centers the clipping within a sub-band of the board (top, bottom)
    while the halftone paper still covers the whole `size` — used by the
    full-bleed layout, where the board runs the full canvas but the part below
    the text card is not visible.
    """
    from PIL import Image, ImageDraw

    target_w, target_h = size
    board = _placeholder_band(size)  # paper + the same halftone dot screen

    top, bottom = region or (0, target_h)
    # Inset enough to leave the mount visible on the tight axis too, so the
    # clipping never looks like it is bleeding off one edge by accident.
    avail_w = target_w - _mount_padding()
    avail_h = (bottom - top) - _mount_padding()
    scale = min(avail_w / img.width, avail_h / img.height)
    fg_w = max(1, round(img.width * scale))
    fg_h = max(1, round(img.height * scale))
    x = (target_w - fg_w) // 2
    y = top + (bottom - top - fg_h) // 2

    draw = ImageDraw.Draw(board)
    draw.rectangle(
        (x + _MATTE_SHADOW, y + _MATTE_SHADOW, x + fg_w + _MATTE_SHADOW, y + fg_h + _MATTE_SHADOW),
        fill=INK,
    )
    board.paste(img.resize((fg_w, fg_h), Image.LANCZOS), (x, y))
    draw.rectangle((x - 2, y - 2, x + fg_w + 1, y + fg_h + 1), outline=INK, width=3)

    # Two short strips of tape on the top corners — the same motif the panels
    # already use, and what makes the mount read as pasted rather than framed.
    _draw_tape(board, cx=x + 14, cy=y + 10, w=96, h=34, angle=-38)
    _draw_tape(board, cx=x + fg_w - 14, cy=y + 10, w=96, h=34, angle=38)
    return board


def _fit_photo(img, size: tuple[int, int], *, seed: int = 0):
    """Fill `size` with `img`, choosing the compositing strategy by how much
    a plain cover-fit crop would have to discard.

    Below the threshold: cover-fit (scale + center-crop) — full-bleed, no dead
    space, right for a normal photo.

    Above it: the whole image, mounted on paper (see `_matte`). Callers that can
    vary their band height should call `photo_band_height` FIRST, which avoids
    reaching this path at all for a landscape source by simply making the band
    the shape of the image.
    """
    target_w, target_h = size
    cover_scale = max(target_w / img.width, target_h / img.height)
    scaled_w, scaled_h = img.width * cover_scale, img.height * cover_scale
    crop_fraction = max(1 - target_w / scaled_w, 1 - target_h / scaled_h)

    if crop_fraction <= _MAX_CROP_FRACTION:
        return _cover_fit(img, size)
    return _matte(img, size, seed=seed)


def _scrim(img, height: int, strength: float = 0.55):
    """Fade the bottom of the photo toward ink so the panel seam reads as
    intentional rather than as an abrupt cut."""
    from PIL import Image

    overlay = Image.new("L", (1, height))
    for y in range(height):
        overlay.putpixel((0, y), int(255 * strength * (y / max(1, height - 1))))
    mask = overlay.resize((img.width, height))
    shade = Image.new("RGB", (img.width, height), INK)
    region = img.crop((0, img.height - height, img.width, img.height))
    img.paste(Image.composite(shade, region, mask), (0, img.height - height))
    return img


def _placeholder_band(size: tuple[int, int]):
    """Branded fallback for a missing photo — halftone dots on paper, matching
    the web app's ImagePlaceholder language rather than a blank rectangle."""
    from PIL import Image, ImageDraw

    block = Image.new("RGB", size, CARD)
    dots = ImageDraw.Draw(block)
    for gy in range(0, size[1], 22):
        for gx in range(0, size[0], 22):
            dots.ellipse((gx, gy, gx + 3, gy + 3), fill=LINE)
    return block


# ── newspaper-clipping motifs ────────────────────────────────────────────────
def _torn_edge(
    x0: int, x1: int, y_base: float, *, slant: float = 0, amplitude: int = 9,
    segment: int = 26, seed: int = 0,
) -> list[tuple[float, float]]:
    """Points along a jagged torn-paper edge from x0 to x1, sloping by
    `slant` px total across the span (0 = horizontal), with small per
    -segment jitter. Seeded so a given event always tears the same way on
    re-render, but different events don't all share one identical rip."""
    rng = random.Random(seed)
    span = max(1, x1 - x0)
    points: list[tuple[float, float]] = []
    x = x0
    while x < x1:
        base_y = y_base + slant * (x - x0) / span
        points.append((x, base_y + rng.randint(-amplitude, amplitude)))
        x += segment
    points.append((x1, y_base + slant + rng.randint(-amplitude, amplitude)))
    return points


def _torn_panel(draw, edge: list[tuple[float, float]], x0: int, x1: int, y_bottom: int, fill) -> None:
    """Fill everything below a torn edge, then trace the tear in ink — the
    line is what sells "ripped paper" instead of just "jagged rectangle"."""
    draw.polygon(list(edge) + [(x1, y_bottom), (x0, y_bottom)], fill=fill)
    draw.line(edge, fill=INK, width=3, joint="curve")


def _draw_tape(img, cx: float, cy: float, w: int, h: int, angle: float) -> None:
    """A short strip of "tape" holding the clipping down — rotated, so it
    reads as physically stuck on rather than a flat UI badge. RGBA + rotate
    + paste-with-self-as-mask is the standard Pillow idiom for pasting a
    non-axis-aligned shape onto an RGB canvas without square corner artifacts."""
    from PIL import Image as PILImage
    from PIL import ImageDraw as PILImageDraw

    tape = PILImage.new("RGBA", (w, h), (*TAPE, 232))
    d = PILImageDraw.Draw(tape)
    d.rectangle((0, 0, w - 1, h - 1), outline=(*INK, 255), width=2)
    tape = tape.rotate(angle, expand=True, resample=PILImage.BICUBIC)
    img.paste(tape, (int(cx - tape.width / 2), int(cy - tape.height / 2)), tape)


def _halftone_circle(draw, cx: float, cy: float, r: float, color, dot_r: int = 5, spacing: int = 15) -> None:
    """A circle built from a dot grid instead of a flat fill — the classic
    cheap-newsprint halftone-screen look, in one of our own two accents."""
    y = cy - r
    row = 0
    while y <= cy + r:
        offset = (spacing / 2) if row % 2 else 0
        x = cx - r - offset
        while x <= cx + r:
            dx, dy = x - cx, y - cy
            if dx * dx + dy * dy <= r * r:
                draw.ellipse((x - dot_r, y - dot_r, x + dot_r, y + dot_r), fill=color)
            x += spacing
        y += spacing
        row += 1


def _encode(img) -> bytes:
    from io import BytesIO

    assert img.size == CANVAS, f"slide must be {CANVAS}, got {img.size}"
    buf = BytesIO()
    # progressive=False deliberately: some server-side fetchers handle
    # progressive JPEG poorly, and Meta cURLs these itself.
    img.save(buf, "JPEG", quality=88, optimize=True, progressive=False)
    return buf.getvalue()


def _venue_label(row: dict[str, Any]) -> str:
    venues = row.get("venues")
    if isinstance(venues, list):
        venues = venues[0] if venues else None
    if isinstance(venues, dict) and venues.get("name"):
        return str(venues["name"])
    return str(row.get("venue") or "")


# Strips a trailing ", TX 79901"-style state+zip (some sources append a
# further ", US"/", USA" country token, e.g. "..., TX 79905, US" — the
# optional group absorbs that too) — the cover slide already establishes
# "El Paso", so repeating state/zip/country on every event slide is noise;
# street + city is the useful part of the address.
_STATE_ZIP_RE = re.compile(r",\s*[A-Z]{2}\s*\d{5}(-\d{4})?(,\s*[A-Za-z]+)?\s*$")


def _address_label(row: dict[str, Any]) -> str:
    """Street + city, with anything the venue line already says removed.

    Several calendars store the venue name INSIDE the address — Visit El Paso
    ships "El Paso County Coliseum - 4100 E Paisano Dr, El Paso, TX 79905" — so
    the slide printed the venue twice, once in Archivo Black and again beside the
    pin, and the long duplicate pushed the useful street text off the line.
    """
    location = str(row.get("location") or "").strip()
    if not location:
        return ""

    venue = _venue_label(row).strip()
    if venue:
        # Only a LEADING duplicate is stripped: "<venue> - <street>" and
        # "<venue>, <street>" are the shapes sources actually produce, whereas a
        # venue name appearing mid-address is usually part of the real address
        # ("Suite 12, Flix Brewhouse Plaza") and is left alone.
        prefix = re.match(re.escape(venue) + r"\s*[-–—,]\s*", location, re.IGNORECASE)
        if prefix:
            location = location[prefix.end() :].strip()

    return _STATE_ZIP_RE.sub("", location).strip() or location


def _draw_pin(draw, x: float, y: float, size: float, color) -> None:
    """A location-pin mark out of two Pillow primitives. There's no icon font
    or SVG rasterizer vendored (see assets/fonts/README.md), so every
    icon-shaped mark on these slides is hand-drawn — same technique the
    cover's swipe arrow already uses."""
    r = size / 2
    cx, cy = x + r, y + r
    draw.ellipse((cx - r, cy - r, cx + r, cy + r), fill=color)
    draw.polygon(
        [(cx - r * 0.55, cy + r * 0.35), (cx + r * 0.55, cy + r * 0.35), (cx, cy + r * 1.7)],
        fill=color,
    )


def _time_stamp(start_local: Optional[Any]) -> Optional[str]:
    # Only stamp a time we actually believe — see selection.has_plausible_time.
    # A wrong time on a public slide is worse than no time.
    from .selection import has_plausible_time

    if not has_plausible_time(start_local):
        return None
    hour = start_local.strftime("%I").lstrip("0") or "12"
    return f"{hour}:{start_local.strftime('%M')} {start_local.strftime('%p')}"


# Kicker lines and the accent the halftone circle is drawn in, per format.
# One layout parameterised rather than four near-duplicate functions: the
# composition is doing its job, and the thing that actually distinguishes
# "tonight" from "book this now" is the words and the colour, not the grid.
_COVER_SPECS: dict[str, tuple[str, str, tuple[int, int, int]]] = {
    "digest": ("TODAY IN", "EL PASO", COSMO),
    "breaking": ("JUST IN", "EL PASO", COSMO),
    "weekend": ("THIS WEEKEND", "IN EL PASO", POP_YELLOW),
    "monthly": ("THIS MONTH IN", "EL PASO", COSMO),
    "horizon": ("SAVE THE DATE", "EL PASO", POP_YELLOW),
}


def _default_period_label(kind: str, day: date) -> str:
    """What the big line says when the caller supplies nothing.

    %-d is glibc-only and %d leaves a leading zero ("AUGUST 05"), so the day
    number is always formatted by hand.
    """
    if kind == "monthly":
        return day.strftime("%B").upper()
    if kind == "horizon":
        return f"{day.strftime('%B')} {day.year}".upper()
    return f"{day.strftime('%B')} {day.day}".upper()


def render_cover(
    day: date,
    event_count: int,
    handle: str = "epchisme.com",
    *,
    kind: str = "digest",
    period_label: Optional[str] = None,
) -> bytes:
    from PIL import Image, ImageDraw

    kicker_top, kicker_bottom, halo = _COVER_SPECS.get(kind, _COVER_SPECS["digest"])
    big_label = period_label or _default_period_label(kind, day)
    # The weekday only means something for a single-day post: a weekend or a
    # month spans several, and printing one of them would be a lie. Non-daily
    # formats drop the line entirely rather than repeating the kicker
    # underneath itself, which read as a duplicated "IN EL PASO".
    sub_label = day.strftime("%A") if kind in ("digest", "breaking") else None

    img = Image.new("RGB", CANVAS, INK)
    draw = ImageDraw.Draw(img)

    # Big offset halftone-dot circle, mostly off-canvas to the upper right —
    # "more design on the background" rendered as a cheap-newsprint dot
    # screen instead of a flat vector shape, without competing with the
    # left-aligned text column.
    _halftone_circle(draw, cx=620 + 470, cy=-160 + 470, r=470, color=halo, dot_r=7, spacing=19)

    kicker = font("condensed", 46)
    count_font = font("sans_black", 40)
    date_font = date_lines = None
    if sub_label:
        date_font, date_lines = wrap_to_fit(
            draw, sub_label, "display", CANVAS[0] - SAFE_X * 2, 1, 150, 84
        )
    # Shrink to fit: a range like "SEP 4-6" or "MARCH 2027" is wider than the
    # single date this line was sized for, and overflowing the safe margin
    # would crop it in the profile grid. Measured BEFORE the block height is
    # computed — sizing the block against a 128pt guess and then drawing a
    # 64pt line left the whole composition floating above a dead gap.
    month_font, month_lines = wrap_to_fit(
        draw, big_label, "display", CANVAS[0] - SAFE_X * 2, 1, 128, 64
    )

    # Measure first, then centre the whole block inside the profile-grid square.
    sub_h = int(date_font.size * 1.05) if date_font else 0
    block_h = 60 + 100 + sub_h + int(month_font.size * 1.15) + 58
    y = GRID_SAFE_TOP + max(40, (CANVAS[0] - block_h) // 2)

    draw.text((SAFE_X, y), kicker_top, font=kicker, fill=POP_YELLOW)
    y += 60
    draw.text((SAFE_X, y), kicker_bottom, font=kicker, fill=POP_YELLOW)
    y += 100

    if date_font and date_lines:
        draw.text((SAFE_X, y), date_lines[0], font=date_font, fill=PAPER)
        y += sub_h

    draw.text((SAFE_X, y), month_lines[0], font=month_font, fill=POP_YELLOW)
    y += int(month_font.size * 1.15)

    # A few drawn dot accents ahead of the count line — the "some icons" ask,
    # kept modest since there's no icon set to draw from beyond Pillow shapes.
    dot_x = SAFE_X
    for c in (COSMO, POP_YELLOW, PAPER):
        draw.ellipse((dot_x, y + 14, dot_x + 14, y + 28), fill=c)
        dot_x += 22
    label = "thing happening" if event_count == 1 else "things happening"
    if kind == "horizon":
        label = "on sale now" if event_count == 1 else "on sale now"
    elif kind in ("weekend", "monthly"):
        label = "thing to do" if event_count == 1 else "things to do"
    draw.text((dot_x + 10, y), f"{event_count} {label}", font=count_font, fill=PAPER)

    # Footer pinned to the bottom of the grid-safe band, not the canvas, so it
    # survives the profile-grid crop.
    foot = font("condensed", 32)
    draw.text((SAFE_X, GRID_SAFE_BOTTOM - 70), handle.upper(), font=foot, fill=PAPER)

    # Swipe hint sits outside the square — only feed viewers can swipe anyway.
    # The triangle is DRAWN rather than typed: "→" is missing from plenty of
    # fonts and renders as a tofu box, which is worse than no arrow at all.
    hint = font("condensed", 30)
    hint_y = CANVAS[1] - 78
    draw.text((SAFE_X, hint_y), "SWIPE FOR MORE", font=hint, fill=POP_YELLOW)
    ax = SAFE_X + _text_width(draw, "SWIPE FOR MORE", hint) + 16
    mid = hint_y + 18
    draw.polygon([(ax, mid - 11), (ax + 18, mid), (ax, mid + 11)], fill=POP_YELLOW)
    return _encode(img)


def _slide_bold_block(row: dict[str, Any], photo, start_local, accent, seed: int):
    """Layout 0 — photo-top/panel-bottom, torn seam instead of a clean cut,
    a taped corner, bold Archivo Black venue text, an accent-color chip."""
    from PIL import Image, ImageDraw

    on_accent = _on_accent(accent)
    img = Image.new("RGB", CANVAS, PAPER)
    box_w = CANVAS[0] - SAFE_X * 2
    title = str(row.get("title") or "Untitled event")

    # The seam moves to suit BOTH the photo and the title: the band starts at
    # whatever height shows this image whole, then gives ground if the title
    # needs more room than what is left. _RESERVED_BELOW_TITLE is the venue,
    # address and chip stack that always follows.
    measure = ImageDraw.Draw(img)
    size_hi = title_size_hi(photo, 96)
    panel_y = _seam_for(measure, photo, title, box_w, size_hi=size_hi,
                        reserved=_RESERVED_BELOW_TITLE)

    if photo is not None:
        band = _fit_photo(photo.image, (CANVAS[0], panel_y), seed=seed)
        # The scrim exists to soften a PHOTOGRAPH's hard lower edge into the
        # torn seam, and it is only ever right when there is spare image to
        # spend on it. On a mounted board it reads as a grey smudge across the
        # paper, and on an image the band was sized to fit exactly — a flyer,
        # whose designer put content at the very bottom edge — it dims the
        # content we just went to the trouble of not cropping.
        if crop_fraction(photo, (CANVAS[0], panel_y)) > _SCRIM_MIN_CROP:
            band = _scrim(band, 170)
        img.paste(band, (0, 0))
    else:
        img.paste(_placeholder_band((CANVAS[0], panel_y)), (0, 0))

    draw = ImageDraw.Draw(img)
    edge = _torn_edge(0, CANVAS[0], panel_y, seed=seed)
    _torn_panel(draw, edge, 0, CANVAS[0], CANVAS[1], PAPER)

    stamp = _time_stamp(start_local)
    if stamp:
        time_font = font("condensed", 44)
        draw.text(
            (CANVAS[0] - SAFE_X, panel_y + 40), stamp, font=time_font, fill=INK, anchor="ra"
        )

    y = panel_y + 100
    title_font, title_lines = fit_block(
        draw, title, "display", box_w, CANVAS[1] - y - _RESERVED_BELOW_TITLE, size_hi, 44
    )
    line_h = int(title_font.size * 1.06)
    for line in title_lines:
        draw.text((SAFE_X, y), line, font=title_font, fill=INK)
        y += line_h

    # Venue, address and chip reflow upward when the title is short, so
    # there's never a mystery gap and never an overflow off the bottom.
    venue = _venue_label(row)
    if venue:
        y += max(18, int(title_font.size * 0.30))
        venue_font, venue_lines = fit_block(draw, venue, "sans_black", box_w, 96, 36, 22,
                                            line_ratio=1.2)
        for line in venue_lines:
            draw.text((SAFE_X, y), line, font=venue_font, fill=INK)
            y += int(venue_font.size * 1.2)
    else:
        # No venue block to carry this gap — without it, the address's pin
        # lands right on the title's last-line descenders (seen on venueless
        # city-calendar events once those started reaching this layout).
        y += max(14, int(title_font.size * 0.18))

    address = _address_label(row)
    if address and y + 30 < CANVAS[1] - 60:
        y += 8
        _draw_pin(draw, SAFE_X, y + 2, 18, accent)
        addr_font, addr_lines = fit_block(draw, address, "sans_semibold", box_w - 26, 68, 26, 18,
                                          line_ratio=1.2)
        for line in addr_lines:
            draw.text((SAFE_X + 26, y), line, font=addr_font, fill=INK_SOFT)
            y += int(addr_font.size * 1.2)

    cats = [c for c in (row.get("categories") or []) if c][:1]
    if cats and y + 56 < CANVAS[1] - 24:
        y += 16
        chip_font = font("condensed", 24)
        label = str(cats[0]).upper()
        w = _text_width(draw, label, chip_font)
        draw.rectangle((SAFE_X, y, SAFE_X + w + 28, y + 44), fill=accent)
        draw.text((SAFE_X + 14, y + 9), label, font=chip_font, fill=on_accent)

    # Left side, mirroring _slide_full_bleed — the time stamp owns the right
    # side of this row (anchor="ra" at CANVAS[0]-SAFE_X); a right-side tape
    # placement collided with it for real on a live render ("1:00 PM" half
    # covered by the tape strip).
    _draw_tape(img, cx=110, cy=panel_y + 34, w=150, h=56, angle=6)
    return img


def _slide_full_bleed(row: dict[str, Any], photo, start_local, accent, seed: int):
    """Layout 1 — the photo fills the ENTIRE canvas; a bold, torn-edge
    accent card overlaps the lower third holding the text, magazine-cover
    style instead of photo-with-caption-strip."""
    from PIL import Image, ImageDraw

    on_accent = _on_accent(accent)
    img = Image.new("RGB", CANVAS, PAPER)
    box_w = CANVAS[0] - SAFE_X * 2
    title = str(row.get("title") or "Untitled event")

    # The card overlaps the photo here, so the card's top edge — not the canvas
    # bottom — is the visible region. Composing the photo into the FULL canvas
    # centered whatever could not be cropped behind the card, which on a matted
    # source hid a third of the very image the mount existed to preserve.
    measure = ImageDraw.Draw(img)
    size_hi = title_size_hi(photo, 86)
    card_top = _card_top_for(measure, title, box_w, size_hi=size_hi,
                             reserved=_RESERVED_BELOW_TITLE)
    if photo is None:
        card_top = min(card_top, PHOTO_H_NO_PHOTO)
    elif needs_mount(photo, CANVAS):
        # A mounted image does not fill the canvas, so let the card climb to
        # meet it rather than leaving a quarter-slide of blank paper between
        # the two. A croppable photo is full-bleed and needs no such help.
        card_top = max(PHOTO_H_MIN, min(card_top, mount_height(photo, card_top)))
    slant = 46

    if photo is not None:
        # Cover-fit still gets the whole canvas (a full-bleed photo behind the
        # card is the point of this layout); only the mounted path is confined
        # to what stays visible.
        img.paste(_photo_for_overlay(photo.image, card_top, seed=seed), (0, 0))
    else:
        img.paste(_placeholder_band(CANVAS), (0, 0))

    draw = ImageDraw.Draw(img)

    edge = _torn_edge(0, CANVAS[0], card_top, slant=slant, seed=seed)
    _torn_panel(draw, edge, 0, CANVAS[0], CANVAS[1], accent)

    y = card_top + slant + 46
    # The time stamp gets its own row, ABOVE the title, rather than sharing a
    # row with the title's first line — a two-line title is exactly as wide
    # as the box and would otherwise run straight into a right-aligned badge
    # sitting at the same height (this collided for real: "Friday Salsa
    # Social with 3:00 PM" on a live render before this fix). The row is
    # reserved UNCONDITIONALLY, even when there's no stamp to draw — an
    # event with no plausible time would otherwise pull the title up into
    # the tape accent's vertical space instead (also caught on a live render).
    time_font = font("condensed", 40)
    stamp = _time_stamp(start_local)
    if stamp:
        draw.text((CANVAS[0] - SAFE_X, y), stamp, font=time_font, fill=on_accent, anchor="ra")
    y += int(time_font.size * 1.3)

    title_font, title_lines = fit_block(
        draw, title, "display", box_w, CANVAS[1] - y - _RESERVED_BELOW_TITLE, size_hi, 42
    )
    line_h = int(title_font.size * 1.06)
    for line in title_lines:
        draw.text((SAFE_X, y), line, font=title_font, fill=on_accent)
        y += line_h

    venue = _venue_label(row)
    if venue:
        y += max(16, int(title_font.size * 0.30))
        venue_font, venue_lines = fit_block(draw, venue, "sans_black", box_w, 92, 34, 22,
                                            line_ratio=1.2)
        for line in venue_lines:
            draw.text((SAFE_X, y), line, font=venue_font, fill=on_accent)
            y += int(venue_font.size * 1.2)
    else:
        # See _slide_bold_block — without this the address collides with the
        # title's last-line descenders when there is no venue block to carry
        # the gap.
        y += max(12, int(title_font.size * 0.18))

    address = _address_label(row)
    if address and y + 30 < CANVAS[1] - 24:
        y += 6
        _draw_pin(draw, SAFE_X, y + 2, 16, on_accent)
        addr_font, addr_lines = fit_block(draw, address, "sans_semibold", box_w - 26, 64, 24, 17,
                                          line_ratio=1.2)
        for line in addr_lines:
            draw.text((SAFE_X + 24, y), line, font=addr_font, fill=on_accent)
            y += int(addr_font.size * 1.2)

    _draw_tape(img, cx=110, cy=card_top + slant / 2, w=140, h=54, angle=-8)
    return img


def _slide_split_panel(row: dict[str, Any], photo, start_local, accent, seed: int):
    """Layout 2 — photo on the top ~65%, but the bottom panel is a SOLID
    accent-color background instead of paper — a strong color-inversion
    beat partway through the carousel, torn seam, taped corner."""
    from PIL import Image, ImageDraw

    on_accent = _on_accent(accent)
    img = Image.new("RGB", CANVAS, accent)
    box_w = CANVAS[0] - SAFE_X * 2
    title = str(row.get("title") or "Untitled event")

    measure = ImageDraw.Draw(img)
    size_hi = title_size_hi(photo, 82)
    split_photo_h = _seam_for(
        measure, photo, title, box_w, size_hi=size_hi, reserved=_RESERVED_BELOW_TITLE, top_pad=48
    )

    if photo is not None:
        img.paste(_fit_photo(photo.image, (CANVAS[0], split_photo_h), seed=seed), (0, 0))
    else:
        img.paste(_placeholder_band((CANVAS[0], split_photo_h)), (0, 0))

    draw = ImageDraw.Draw(img)
    edge = _torn_edge(0, CANVAS[0], split_photo_h, seed=seed)
    _torn_panel(draw, edge, 0, CANVAS[0], CANVAS[1], accent)

    y = split_photo_h + 48
    # Own row above the title, reserved unconditionally — see the identical
    # comment in _slide_full_bleed for why both the collision this avoids
    # (title vs. time badge) and the one this avoids (title vs. tape accent
    # when there's no time to stamp) are real, both caught on live renders.
    time_font = font("condensed", 40)
    stamp = _time_stamp(start_local)
    if stamp:
        draw.text((CANVAS[0] - SAFE_X, y), stamp, font=time_font, fill=on_accent, anchor="ra")
    y += int(time_font.size * 1.3)

    title_font, title_lines = fit_block(
        draw, title, "display", box_w, CANVAS[1] - y - _RESERVED_BELOW_TITLE, size_hi, 42,
        line_ratio=1.08,
    )
    line_h = int(title_font.size * 1.08)
    for line in title_lines:
        draw.text((SAFE_X, y), line, font=title_font, fill=on_accent)
        y += line_h

    venue = _venue_label(row)
    if venue:
        y += max(16, int(title_font.size * 0.30))
        venue_font, venue_lines = fit_block(draw, venue, "sans_black", box_w, 88, 32, 20,
                                            line_ratio=1.2)
        for line in venue_lines:
            draw.text((SAFE_X, y), line, font=venue_font, fill=on_accent)
            y += int(venue_font.size * 1.2)
    else:
        # See _slide_bold_block — without this the address collides with the
        # title's last-line descenders when there is no venue block to carry
        # the gap.
        y += max(12, int(title_font.size * 0.18))

    address = _address_label(row)
    if address and y + 30 < CANVAS[1] - 60:
        y += 6
        _draw_pin(draw, SAFE_X, y + 2, 16, on_accent)
        addr_font, addr_lines = fit_block(draw, address, "sans_semibold", box_w - 26, 64, 24, 17,
                                          line_ratio=1.2)
        for line in addr_lines:
            draw.text((SAFE_X + 24, y), line, font=addr_font, fill=on_accent)
            y += int(addr_font.size * 1.2)

    cats = [c for c in (row.get("categories") or []) if c][:1]
    if cats and y + 56 < CANVAS[1] - 24:
        y += 12
        chip_font = font("condensed", 24)
        label = str(cats[0]).upper()
        w = _text_width(draw, label, chip_font)
        draw.rectangle((SAFE_X, y, SAFE_X + w + 28, y + 44), fill=PAPER)
        draw.text((SAFE_X + 14, y + 9), label, font=chip_font, fill=INK)

    # Left side — same fix as _slide_bold_block, same reason: the time stamp
    # owns the right side of this row.
    _draw_tape(img, cx=110, cy=split_photo_h + 30, w=150, h=56, angle=5)
    return img


_SLIDE_BUILDERS = (_slide_bold_block, _slide_full_bleed, _slide_split_panel)

# Same three layouts, different ORDER per format, so a weekend post does not
# open with the same composition as that morning's daily one. Reordering
# rather than inventing new layouts keeps every format inside the same visual
# system — four unrelated designs would read as four accounts.
_BUILDERS_BY_KIND: dict[str, tuple] = {
    # Identical tuple AND order -> byte-identical output to before this existed.
    "digest": _SLIDE_BUILDERS,
    "breaking": _SLIDE_BUILDERS,
    "weekend": (_slide_full_bleed, _slide_bold_block, _slide_split_panel),
    "monthly": (_slide_split_panel, _slide_bold_block, _slide_full_bleed),
    "horizon": (_slide_full_bleed, _slide_split_panel, _slide_bold_block),
}


def render_event_slide(
    index: int,
    total: int,
    row: dict[str, Any],
    photo: Optional[SourcePhoto],
    start_local: Optional[Any] = None,
    kind: str = "digest",
) -> bytes:
    """`total` is accepted for signature stability but no longer rendered —
    the numbered "N / total" badge was removed; order is still implied by
    chronological position, same as the caption.

    `kind` defaults to "digest" so every existing call site — and the whole
    existing test suite — keeps producing byte-identical slides."""
    builders = _BUILDERS_BY_KIND.get(kind, _SLIDE_BUILDERS)
    variant = _variant_for(index, len(builders))
    accent = _accent_for(index)
    seed = _stable_seed(str(row.get("id") or row.get("title") or index))
    img = builders[variant](row, photo, start_local, accent, seed)
    return _encode(img)
