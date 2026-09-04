"""Download and decode source event photos.

This is where the messy half of the world gets normalized. Source images are
hotlinked third-party CDN URLs (S3, img.evbuc.com, Wix, ...) that may 404, be
hotlink-protected, be WebP/PNG/AVIF, or be a 120px thumbnail. Everything that
can go wrong with them goes wrong HERE, at build time, so the publish step only
ever deals with JPEGs we produced ourselves.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from io import BytesIO
from typing import Any, Optional

from ..core.http import HttpClient
from ..core.media import clean_image_url

log = logging.getLogger("scraper.social.imaging")

# Quality gate, expressed as how far a photo must be UPSCALED to fill the slide
# rather than as a raw pixel floor. A flat short-side threshold gets this wrong
# in both directions: it rejects Visit El Paso's 930x560 derivatives (which only
# need 1.5x and look fine) while passing a 2000x300 banner whose height would
# need 2.9x. Measuring the actual scale factor is what the eye responds to.
MAX_UPSCALE = 1.75
# Below this on either axis it's an icon or a thumbnail, whatever the ratio.
MIN_ABS_SIDE = 320
# The band a photo fills on an event slide (see render.CANVAS / render.PHOTO_H).
DEFAULT_TARGET = (1080, 860)
# Guard against a decompression bomb or a multi-hundred-MB print-res TIFF.
MAX_DOWNLOAD_BYTES = 25 * 1024 * 1024


@dataclass
class SourcePhoto:
    image: Any  # PIL.Image.Image, already RGB
    width: int
    height: int

    @property
    def short_side(self) -> int:
        return min(self.width, self.height)

    def upscale_for(self, target: tuple[int, int]) -> float:
        """Scale factor needed to cover `target`. <=1.0 means downscaling."""
        return max(target[0] / self.width, target[1] / self.height)


def encode_jpeg(photo: "SourcePhoto", quality: int = 90) -> bytes:
    """Encode a source photo for storage.

    Deliberately not render._encode: that one asserts the image is exactly a
    1080x1350 slide canvas. This is for keeping an ORIGINAL a human sent in,
    at its own size, so a later re-render can lay it out afresh.
    """
    from io import BytesIO

    buf = BytesIO()
    photo.image.save(buf, "JPEG", quality=quality, optimize=True, progressive=False)
    return buf.getvalue()


async def fetch_photo(
    http: HttpClient,
    url: Optional[str],
    target: tuple[int, int] = DEFAULT_TARGET,
) -> Optional[SourcePhoto]:
    """Download + decode one source photo, or None if it's unusable.

    Never raises: an unusable photo just means that event loses its slot to the
    next-highest-scoring candidate, which is not an error condition.
    """
    clean = clean_image_url(url)
    if not clean:
        return None

    try:
        raw = await http.get_bytes(clean)
    except Exception as exc:  # noqa: BLE001
        log.debug("photo fetch failed for %s: %s", clean, exc)
        return None

    if not raw or len(raw) > MAX_DOWNLOAD_BYTES:
        log.debug("photo rejected (empty or oversized): %s", clean)
        return None

    from PIL import Image, ImageFile

    # Some CDN images are served slightly truncated; better a usable photo with
    # a few missing scanlines than dropping the event entirely.
    ImageFile.LOAD_TRUNCATED_IMAGES = True

    try:
        # convert("RGB") is what makes source format irrelevant — WebP, PNG with
        # alpha, CMYK JPEG and AVIF all collapse to the same thing here, and we
        # re-encode to JPEG on the way out.
        img = Image.open(BytesIO(raw))
        img.load()
        img = img.convert("RGB")
    except Exception as exc:  # noqa: BLE001
        log.debug("photo decode failed for %s: %s", clean, exc)
        return None

    photo = SourcePhoto(image=img, width=img.width, height=img.height)
    if min(photo.width, photo.height) < MIN_ABS_SIDE:
        log.debug("photo is a thumbnail (%dx%d): %s", photo.width, photo.height, clean)
        return None
    upscale = photo.upscale_for(target)
    if upscale > MAX_UPSCALE:
        log.debug(
            "photo needs %.2fx upscale to fill %dx%d (%dx%d): %s",
            upscale, target[0], target[1], photo.width, photo.height, clean,
        )
        return None
    return photo
