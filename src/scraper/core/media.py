"""Image URL sanitization for scraped event data."""

from __future__ import annotations

from typing import Optional
from urllib.parse import urlparse

# Substrings seen in known placeholder/fallback graphics some source sites serve
# in place of a real event photo — reject these even if absolute.
_PLACEHOLDER_MARKERS = ("fallback", "placeholder", "default-image", "no-image", "no_image")


def clean_image_url(url: Optional[str]) -> Optional[str]:
    """Return `url` if it's a genuine absolute image URL, else None.

    Meetup (and potentially other sources) embed relative fallback-graphic paths
    (e.g. "/images/fallbacks/redesign/group-cover-4-square.webp") in their
    schema.org JSON-LD when an organizer hasn't uploaded a real photo. Those
    resolve against *our own* domain when rendered client-side and 404. Reject
    them here so `image_url` stays None and the frontend's branded placeholder
    renders directly, instead of round-tripping through a broken <img>.
    """
    if not isinstance(url, str) or not url.strip():
        return None
    url = url.strip()
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return None
    if any(marker in url.lower() for marker in _PLACEHOLDER_MARKERS):
        return None
    return url
