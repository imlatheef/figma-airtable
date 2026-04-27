"""
background_remover.py
─────────────────────
Removes the background from speaker photos using the rembg library (U2Net model).

The model (~170MB) is downloaded once at build time via the Dockerfile and cached
at /app/.u2net/ so it never needs to download at runtime.

Detection logic
───────────────
Before running the (relatively slow) AI removal, we check whether the photo
actually needs it:
  1. If the image already has significant transparency (alpha channel with
     more than 10% transparent pixels), skip — it's already been processed.
  2. If the corners of the image are all very similar in colour, it's likely
     a solid/uniform background — remove it.
  3. Otherwise, remove anyway (headshots almost always benefit from it).

Usage
─────
    from airtable_to_figma.background_remover import remove_background_if_needed
    photo = remove_background_if_needed(photo)   # returns RGBA PIL Image
"""

from __future__ import annotations

import logging
from collections import Counter

from PIL import Image

log = logging.getLogger(__name__)

# Lazy-import rembg so the module loads fine even if rembg isn't installed
_rembg_session = None


def _get_session():
    """Return a cached rembg InferenceSession (loaded once per process)."""
    global _rembg_session
    if _rembg_session is None:
        try:
            from rembg import new_session
            _rembg_session = new_session("u2netp")
            log.info("rembg: U2Net model loaded")
        except Exception as exc:
            log.warning("rembg not available — background removal disabled: %s", exc)
            _rembg_session = False   # sentinel: don't try again
    return _rembg_session if _rembg_session else None


def _already_transparent(img: Image.Image, threshold: float = 0.10) -> bool:
    """Return True if more than `threshold` fraction of pixels are transparent."""
    if img.mode != "RGBA":
        return False
    alpha = img.split()[3]
    pixels = list(alpha.getdata())
    transparent = sum(1 for p in pixels if p < 10)
    return (transparent / len(pixels)) > threshold


def _has_uniform_background(img: Image.Image, tolerance: int = 30) -> bool:
    """
    Sample the four corners of the image. If they're all similar in colour,
    assume the photo has a solid/uniform background worth removing.
    """
    rgb = img.convert("RGB")
    w, h = rgb.size
    margin = max(5, min(w, h) // 20)   # sample patch size relative to image
    corners = [
        rgb.getpixel((margin, margin)),
        rgb.getpixel((w - margin, margin)),
        rgb.getpixel((margin, h - margin)),
        rgb.getpixel((w - margin, h - margin)),
    ]
    for i, c1 in enumerate(corners):
        for c2 in corners[i + 1:]:
            if any(abs(a - b) > tolerance for a, b in zip(c1, c2)):
                return False
    return True


def remove_background_if_needed(img: Image.Image) -> Image.Image:
    """
    Remove the background from `img` if it appears to have one.

    Always returns an RGBA image. If removal is skipped or fails,
    the original image is returned (converted to RGBA).
    """
    session = _get_session()
    if session is None:
        log.debug("rembg unavailable — returning original photo")
        return img.convert("RGBA")

    rgba = img.convert("RGBA")

    # Skip if already transparent
    if _already_transparent(rgba):
        log.debug("Photo already has transparency — skipping background removal")
        return rgba

    # Run removal (always — headshots almost always benefit)
    try:
        from rembg import remove as rembg_remove
        log.info("Removing background from photo…")
        result = rembg_remove(rgba, session=session)
        log.info("Background removed successfully")
        return result
    except Exception as exc:
        log.warning("Background removal failed — using original photo: %s", exc)
        return rgba
