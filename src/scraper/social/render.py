"""Render carousel slides as JPEG with Pillow.

JPEG is not a preference — it is the only format Instagram's Content Publishing
API accepts. That single constraint is why rendering lives here in Python rather
than reusing the web app's Next.js `ImageResponse`, which emits PNG.

Every slide is rendered at exactly CANVAS. Instagram crops all carousel items to
the first item's aspect ratio, so identical dimensions make that rule a no-op —
the assertion at the end of each render is what keeps it that way.

Event slides rotate through three genuinely different layouts (not just a color
swap) so a carousel doesn't read as one template stamped out N times — see
`_variant_for` and the three `_slide_*` builders below.
"""

from __future__ import annotations

import logging
import re
from datetime import date
from pathlib import Path
from typing import Any, Optional

from .imaging import SourcePhoto

log = logging.getLogger("scraper.social.render")

CANVAS = (1080, 1350)  # 4:5 — the tallest portrait Instagram allows, max feed real estate
SAFE_X = 72
PHOTO_H = 860
PANEL_Y = PHOTO_H

# Instagram's PROFILE GRID crops slide 1 to a centered square. Anything outside
# this band is invisible on the profile, which is where discovery happens, so
# all cover branding must sit inside it.
GRID_SAFE_TOP = (CANVAS[1] - CANVAS[0]) // 2          # 135
GRID_SAFE_BOTTOM = GRID_SAFE_TOP + CANVAS[0]          # 1215

# Brand tokens, mirrored from web/src/app/globals.css.
PAPER = (251, 246, 236)
CARD = (255, 252, 245)
INK = (20, 17, 24)
INK_SOFT = (74, 69, 80)
COSMO = (230, 17, 127)
POP_YELLOW = (255, 210, 30)
LINE = (224, 213, 192)
TEAL = (20, 96, 106)
RUST = (196, 74, 32)

# One accent per event-slide layout variant — see _variant_for. Each variant
# always pairs with the same accent, so a given "look" stays internally
# coherent instead of becoming a combinatorial mess of layout x color.
_ACCENTS = (COSMO, TEAL, RUST)

_FONT_DIR = Path(__file__).resolve().parents[3] / "assets" / "fonts"

# Static instances, NOT the variable files Google Fonts serves by default:
# ImageFont.truetype() on a variable font silently renders the default (Regular)
# instance, so a "Black Italic" request would come out looking wrong with no error.
_FONT_FILES = {
    "display": "Fraunces_144pt-BlackItalic.ttf",
    "sans": "Archivo-Regular.ttf",
    "sans_semibold": "Archivo-SemiBold.ttf",
    "condensed": "Oswald-Bold.ttf",
}

_warned_fonts: set[str] = set()


def _variant_for(index: int) -> int:
    """Slide 2 -> 0, 3 -> 1, 4 -> 2, 5 -> 0, ... — deterministic so re-rendering
    the same day produces the same carousel, not a different one each time."""
    return (index - 2) % len(_ACCENTS)


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


def _fit_photo(img, size: tuple[int, int]):
    """Fill `size` with `img`, choosing the compositing strategy by how much
    a plain cover-fit crop would have to discard.

    Below the threshold: cover-fit (scale + center-crop), same as before —
    full-bleed, no dead space, right for a normal photo.

    Above it: an Instagram Stories-style composite — a blurred, darkened
    cover-fit copy fills the frame as a backdrop with no hard edges, and the
    COMPLETE, uncropped image is layered on top at whatever size fits without
    cropping either axis. The backdrop is what avoids a jarring hard-edged
    letterbox; the foreground is what guarantees no text is ever lost.
    """
    from PIL import Image, ImageFilter

    target_w, target_h = size
    cover_scale = max(target_w / img.width, target_h / img.height)
    scaled_w, scaled_h = img.width * cover_scale, img.height * cover_scale
    crop_fraction = max(1 - target_w / scaled_w, 1 - target_h / scaled_h)

    if crop_fraction <= _MAX_CROP_FRACTION:
        return _cover_fit(img, size)

    backdrop = _cover_fit(img, size).filter(ImageFilter.GaussianBlur(32))
    backdrop = Image.blend(backdrop, Image.new("RGB", size, INK), alpha=0.35)

    contain_scale = min(target_w / img.width, target_h / img.height)
    fg_w = max(1, round(img.width * contain_scale))
    fg_h = max(1, round(img.height * contain_scale))
    foreground = img.resize((fg_w, fg_h), Image.LANCZOS)
    backdrop.paste(foreground, ((target_w - fg_w) // 2, (target_h - fg_h) // 2))
    return backdrop


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
    location = str(row.get("location") or "").strip()
    if not location:
        return ""
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


def render_cover(day: date, event_count: int, handle: str = "epchisme.com") -> bytes:
    from PIL import Image, ImageDraw

    img = Image.new("RGB", CANVAS, INK)
    draw = ImageDraw.Draw(img)

    # Big offset accent circle, mostly off-canvas to the upper right — "more
    # design on the background" without competing with the left-aligned text.
    draw.ellipse((620, -160, 620 + 940, -160 + 940), fill=COSMO)

    kicker = font("condensed", 46)
    date_font, date_lines = wrap_to_fit(
        draw, day.strftime("%A"), "display", CANVAS[0] - SAFE_X * 2, 1, 150, 84
    )
    month_font = font("display", 124)
    count_font = font("sans_semibold", 42)

    # Measure first, then centre the whole block inside the profile-grid square.
    # Laying it out top-down from a fixed offset left a large dead gap above the
    # footer, and the block's height changes with the weekday name's size.
    block_h = 60 + 100 + int(date_font.size * 1.05) + int(month_font.size * 1.15) + 58
    y = GRID_SAFE_TOP + max(40, (CANVAS[0] - block_h) // 2)

    draw.text((SAFE_X, y), "TODAY IN", font=kicker, fill=POP_YELLOW)
    y += 60
    draw.text((SAFE_X, y), "EL PASO", font=kicker, fill=POP_YELLOW)
    y += 100

    draw.text((SAFE_X, y), date_lines[0], font=date_font, fill=PAPER)
    y += int(date_font.size * 1.05)

    # %-d is glibc-only and %d leaves a leading zero ("AUGUST 05"), so format
    # the day number ourselves.
    draw.text(
        (SAFE_X, y), f"{day.strftime('%B')} {day.day}".upper(), font=month_font, fill=POP_YELLOW
    )
    y += int(month_font.size * 1.15)

    # A few drawn dot accents ahead of the count line — the "some icons" ask,
    # kept modest since there's no icon set to draw from beyond Pillow shapes.
    dot_x = SAFE_X
    for c in (COSMO, POP_YELLOW, TEAL):
        draw.ellipse((dot_x, y + 14, dot_x + 14, y + 28), fill=c)
        dot_x += 22
    label = "thing happening" if event_count == 1 else "things happening"
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


def _slide_bold_block(row: dict[str, Any], photo, start_local, accent):
    """Variant 0 — photo-top/panel-bottom, evolved bolder: bigger title type,
    a solid accent block behind the category chip, address line with a
    drawn pin."""
    from PIL import Image, ImageDraw

    img = Image.new("RGB", CANVAS, PAPER)
    if photo is not None:
        band = _fit_photo(photo.image, (CANVAS[0], PHOTO_H))
        band = _scrim(band, 170)
        img.paste(band, (0, 0))
    else:
        img.paste(_placeholder_band((CANVAS[0], PHOTO_H)), (0, 0))

    draw = ImageDraw.Draw(img)
    draw.rectangle((0, PANEL_Y, CANVAS[0], CANVAS[1]), fill=PAPER)
    draw.rectangle((0, PANEL_Y, CANVAS[0], PANEL_Y + 4), fill=accent)

    box_w = CANVAS[0] - SAFE_X * 2
    stamp = _time_stamp(start_local)
    if stamp:
        time_font = font("condensed", 44)
        draw.text(
            (CANVAS[0] - SAFE_X, PANEL_Y + 34), stamp, font=time_font, fill=INK, anchor="ra"
        )

    y = PANEL_Y + 96
    title_font, title_lines = wrap_to_fit(
        draw, str(row.get("title") or "Untitled event"), "display", box_w, 3, 96, 60
    )
    line_h = int(title_font.size * 1.06)
    for line in title_lines:
        draw.text((SAFE_X, y), line, font=title_font, fill=INK)
        y += line_h

    # Venue, address and chip reflow upward when the title is short, so
    # there's never a mystery gap and never an overflow off the bottom.
    venue = _venue_label(row)
    if venue:
        y += 18
        venue_font, venue_lines = wrap_to_fit(draw, venue, "sans_semibold", box_w, 1, 34, 26)
        draw.text((SAFE_X, y), venue_lines[0], font=venue_font, fill=INK_SOFT)
        y += int(venue_font.size * 1.2)

    address = _address_label(row)
    if address and y + 30 < CANVAS[1] - 60:
        y += 8
        _draw_pin(draw, SAFE_X, y + 2, 18, accent)
        addr_font, addr_lines = wrap_to_fit(draw, address, "sans", box_w - 26, 1, 24, 20)
        draw.text((SAFE_X + 26, y), addr_lines[0], font=addr_font, fill=INK_SOFT)
        y += int(addr_font.size * 1.2)

    cats = [c for c in (row.get("categories") or []) if c][:1]
    if cats and y + 56 < CANVAS[1] - 24:
        y += 16
        chip_font = font("condensed", 24)
        label = str(cats[0]).upper()
        w = _text_width(draw, label, chip_font)
        draw.rectangle((SAFE_X, y, SAFE_X + w + 28, y + 44), fill=accent)
        draw.text((SAFE_X + 14, y + 9), label, font=chip_font, fill=PAPER)

    return img


def _slide_full_bleed(row: dict[str, Any], photo, start_local, accent):
    """Variant 1 — the photo fills the ENTIRE canvas; a bold, slanted
    accent-color card overlaps the lower third holding the text, magazine
    -cover style instead of photo-with-caption-strip."""
    from PIL import Image, ImageDraw

    img = Image.new("RGB", CANVAS, PAPER)
    if photo is not None:
        img.paste(_fit_photo(photo.image, CANVAS), (0, 0))
    else:
        img.paste(_placeholder_band(CANVAS), (0, 0))

    draw = ImageDraw.Draw(img)

    card_top = 860
    slant = 46
    draw.polygon(
        [(0, card_top + slant), (CANVAS[0], card_top), (CANVAS[0], CANVAS[1]), (0, CANVAS[1])],
        fill=accent,
    )

    box_w = CANVAS[0] - SAFE_X * 2
    y = card_top + slant + 44
    # The time stamp gets its own row, ABOVE the title, rather than sharing a
    # row with the title's first line — a two-line title is exactly as wide
    # as the box and would otherwise run straight into a right-aligned badge
    # sitting at the same height (this collided for real: "Friday Salsa
    # Social with 3:00 PM" on a live render before this fix).
    stamp = _time_stamp(start_local)
    if stamp:
        time_font = font("condensed", 40)
        draw.text((CANVAS[0] - SAFE_X, y), stamp, font=time_font, fill=PAPER, anchor="ra")
        y += int(time_font.size * 1.3)

    title_font, title_lines = wrap_to_fit(
        draw, str(row.get("title") or "Untitled event"), "display", box_w, 2, 84, 52
    )
    line_h = int(title_font.size * 1.06)
    for line in title_lines:
        draw.text((SAFE_X, y), line, font=title_font, fill=PAPER)
        y += line_h

    venue = _venue_label(row)
    if venue:
        y += 16
        venue_font, venue_lines = wrap_to_fit(draw, venue, "sans_semibold", box_w, 1, 32, 24)
        draw.text((SAFE_X, y), venue_lines[0], font=venue_font, fill=PAPER)
        y += int(venue_font.size * 1.2)

    address = _address_label(row)
    if address and y + 30 < CANVAS[1] - 24:
        y += 6
        _draw_pin(draw, SAFE_X, y + 2, 16, PAPER)
        addr_font, addr_lines = wrap_to_fit(draw, address, "sans", box_w - 26, 1, 22, 18)
        draw.text((SAFE_X + 24, y), addr_lines[0], font=addr_font, fill=PAPER)

    return img


def _slide_split_panel(row: dict[str, Any], photo, start_local, accent):
    """Variant 2 — photo on the top ~65%, but the bottom panel is a SOLID
    accent-color background with paper-colored text instead of paper — a
    strong color-inversion beat partway through the carousel."""
    from PIL import Image, ImageDraw

    split_photo_h = 878
    img = Image.new("RGB", CANVAS, accent)
    if photo is not None:
        img.paste(_fit_photo(photo.image, (CANVAS[0], split_photo_h)), (0, 0))
    else:
        img.paste(_placeholder_band((CANVAS[0], split_photo_h)), (0, 0))

    draw = ImageDraw.Draw(img)
    draw.rectangle((0, split_photo_h, CANVAS[0], CANVAS[1]), fill=accent)

    box_w = CANVAS[0] - SAFE_X * 2
    y = split_photo_h + 44
    # Own row above the title — see the identical comment in _slide_full_bleed
    # for why sharing a row with the title's first line is unsafe.
    stamp = _time_stamp(start_local)
    if stamp:
        time_font = font("condensed", 40)
        draw.text((CANVAS[0] - SAFE_X, y), stamp, font=time_font, fill=PAPER, anchor="ra")
        y += int(time_font.size * 1.3)

    title_font, title_lines = wrap_to_fit(
        draw, str(row.get("title") or "Untitled event"), "display", box_w, 2, 80, 50
    )
    line_h = int(title_font.size * 1.08)
    for line in title_lines:
        draw.text((SAFE_X, y), line, font=title_font, fill=PAPER)
        y += line_h

    venue = _venue_label(row)
    if venue:
        y += 16
        venue_font, venue_lines = wrap_to_fit(draw, venue, "sans_semibold", box_w, 1, 30, 22)
        draw.text((SAFE_X, y), venue_lines[0], font=venue_font, fill=PAPER)
        y += int(venue_font.size * 1.2)

    address = _address_label(row)
    if address and y + 30 < CANVAS[1] - 60:
        y += 6
        _draw_pin(draw, SAFE_X, y + 2, 16, PAPER)
        addr_font, addr_lines = wrap_to_fit(draw, address, "sans", box_w - 26, 1, 22, 18)
        draw.text((SAFE_X + 24, y), addr_lines[0], font=addr_font, fill=PAPER)
        y += int(addr_font.size * 1.2)

    cats = [c for c in (row.get("categories") or []) if c][:1]
    if cats and y + 56 < CANVAS[1] - 24:
        y += 12
        chip_font = font("condensed", 24)
        label = str(cats[0]).upper()
        w = _text_width(draw, label, chip_font)
        draw.rectangle((SAFE_X, y, SAFE_X + w + 28, y + 44), fill=PAPER)
        draw.text((SAFE_X + 14, y + 9), label, font=chip_font, fill=accent)

    return img


_SLIDE_BUILDERS = (_slide_bold_block, _slide_full_bleed, _slide_split_panel)


def render_event_slide(
    index: int,
    total: int,
    row: dict[str, Any],
    photo: Optional[SourcePhoto],
    start_local: Optional[Any] = None,
) -> bytes:
    """`total` is accepted for signature stability but no longer rendered —
    the numbered "N / total" badge was removed; order is still implied by
    chronological position, same as the caption."""
    variant = _variant_for(index)
    accent = _ACCENTS[variant]
    img = _SLIDE_BUILDERS[variant](row, photo, start_local, accent)
    return _encode(img)
