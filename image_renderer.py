"""
image_renderer.py
─────────────────
Takes the exported Figma template image and overlays dynamic text from
Airtable using Pillow.

How it works
────────────
1. Export the template frame from Figma (done once per run or cached).
2. For each Airtable record, clone the base image, iterate over field_mappings,
   find the matching Figma text node, and redraw the text at the correct
   position using the node's font-size and color as read from the Figma file.
3. Return the final composite as JPEG bytes.

Text-node coordinate system
────────────────────────────
Figma's absoluteBoundingBox uses the canvas coordinate system.
When we export a frame at scale S, the image origin (0,0) = frame's top-left.
So the pixel position of a text node inside the image is:

    px = (node.x - frame.x) * S
    py = (node.y - frame.y) * S

We draw the text starting at (px, py) and wrap within node.width * S.
"""

from __future__ import annotations

import io
import logging
import os
import textwrap
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont, ImageOps

log = logging.getLogger(__name__)

# Bundled fallback font (always available in Python ≥ 3.x via Pillow)
_DEFAULT_FONT_SIZE = 16
_FALLBACK_FONT = ImageFont.load_default()


def _load_font(font_path: str | None, size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    """Load a TrueType font, falling back to Pillow's built-in default."""
    if font_path:
        # Resolve relative paths from the script's own directory
        resolved = font_path if os.path.isabs(font_path) else os.path.join(
            os.path.dirname(os.path.abspath(__file__)), font_path
        )
        if os.path.exists(resolved):
            try:
                return ImageFont.truetype(resolved, size)
            except Exception as exc:
                log.warning("Could not load font %s: %s – using default", resolved, exc)
        else:
            log.warning("Font file not found: %s", resolved)
    # Try common system fonts as secondary fallback
    system_fonts = [
        "/Library/Fonts/Arial.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "C:/Windows/Fonts/arial.ttf",
    ]
    for sf in system_fonts:
        if os.path.exists(sf):
            try:
                return ImageFont.truetype(sf, size)
            except Exception:
                continue
    return _FALLBACK_FONT


class ImageRenderer:
    """
    Overlays Airtable field values onto a PIL Image exported from Figma.

    Parameters
    ----------
    text_nodes : list[dict]
        Output of FigmaClient.get_text_nodes() – positions, sizes, styles.
    field_mappings : dict[str, str]
        { "Airtable Field Name": "Figma Layer Name" }
    font_overrides : dict[str, dict]
        { "Figma Layer Name": { "font_path": ..., "font_size": ..., "color": [...] } }
    scale : float
        The export scale used when the base image was exported from Figma.
    """

    def __init__(
        self,
        text_nodes: list[dict[str, Any]],
        image_nodes: list[dict[str, Any]],
        field_mappings: dict[str, str],
        image_field_mappings: dict[str, str] | None = None,
        font_overrides: dict[str, dict] | None = None,
        scale: float = 2.0,
    ):
        self.text_nodes  = {n["name"]: n for n in text_nodes}   # keyed by layer name
        self.image_nodes = {n["name"]: n for n in image_nodes}
        self.field_mappings       = field_mappings
        self.image_field_mappings = image_field_mappings or {}
        self.font_overrides = font_overrides or {}
        self.scale = scale

    # ── Public ──────────────────────────────────────────────────────────────────

    def render(
        self,
        base_image: Image.Image,
        record_fields: dict[str, Any],
        photo_images: dict[str, Image.Image] | None = None,
    ) -> bytes:
        """
        Composite text and photos from record_fields onto base_image.
        photo_images: { "Figma layer name": PIL.Image }
        Returns JPEG bytes.
        """
        # Work on a flattened RGB copy (JPEG doesn't support alpha)
        img = Image.new("RGB", base_image.size, (255, 255, 255))
        img.paste(base_image, mask=base_image.split()[3] if base_image.mode == "RGBA" else None)

        # ── Photo overlays first (go underneath text) ────────────────────────────
        if photo_images:
            for figma_layer, photo in photo_images.items():
                node = self.image_nodes.get(figma_layer)
                if node is None:
                    log.warning("Image layer '%s' not found in Figma frame – skipping", figma_layer)
                    continue
                self._paste_photo(img, photo, node)

        draw = ImageDraw.Draw(img)

        # ── Text overlays ────────────────────────────────────────────────────────
        for airtable_field, figma_layer in self.field_mappings.items():
            value = record_fields.get(airtable_field)
            if value is None:
                log.debug("Field '%s' not in record – skipping", airtable_field)
                continue
            node = self.text_nodes.get(figma_layer)
            if node is None:
                log.warning(
                    "Figma text layer '%s' not found – check layer name is exact.",
                    figma_layer,
                )
                continue
            # Airtable lookup/rollup fields return lists — extract the first item
            if isinstance(value, list):
                value = value[0] if value else ""
            self._draw_text(draw, str(value), node, figma_layer)

        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=95)
        return buf.getvalue()

    # ── Image compositing ─────────────────────────────────────────────────────────

    def _paste_photo(self, canvas: Image.Image, photo: Image.Image, node: dict[str, Any]):
        """Resize & crop photo to fit the node bounds, then paste onto canvas."""
        px = int((node["x"] - node["frame_x"]) * self.scale)
        py = int((node["y"] - node["frame_y"]) * self.scale)
        pw = int(node["width"]  * self.scale)
        ph = int(node["height"] * self.scale)

        if pw <= 0 or ph <= 0:
            return

        # Centre-crop to target aspect ratio then resize
        photo_rgb = photo.convert("RGBA")
        photo_rgb = ImageOps.fit(photo_rgb, (pw, ph), method=Image.LANCZOS, centering=(0.5, 0.5))

        # If the Figma node is an ELLIPSE, apply a circular mask
        if node.get("shape") == "ELLIPSE":
            mask = Image.new("L", (pw, ph), 0)
            ImageDraw.Draw(mask).ellipse([0, 0, pw - 1, ph - 1], fill=255)
            photo_rgb.putalpha(mask)

        canvas.paste(photo_rgb, (px, py), mask=photo_rgb.split()[3])
        log.info("Pasted photo onto layer '%s' at (%d,%d) size=%dx%d", node["name"], px, py, pw, ph)

    # ── Internal ─────────────────────────────────────────────────────────────────

    def _draw_text(
        self,
        draw: ImageDraw.ImageDraw,
        text: str,
        node: dict[str, Any],
        layer_name: str,
    ):
        overrides = self.font_overrides.get(layer_name, {})

        # Font
        font_size = int(overrides.get("font_size", node["font_size"]) * self.scale)
        font_path = overrides.get("font_path")
        font = _load_font(font_path, font_size)

        # Color
        color = tuple(overrides.get("color", node["color"]))

        # Position (convert Figma canvas coords → image pixels)
        px = (node["x"] - node["frame_x"]) * self.scale
        py = (node["y"] - node["frame_y"]) * self.scale
        max_w = node["width"] * self.scale

        # ── Erase the old template text by painting a background rect ───────────
        # We paint a slightly expanded rectangle in white-ish to cover placeholder.
        # If your template has a non-white background behind text, adjust this.
        # A better approach: keep Figma template text layers empty / invisible.
        bg_color = self._sample_background(draw._image, px, py, max_w, node["height"] * self.scale)
        draw.rectangle(
            [px, py, px + max_w, py + node["height"] * self.scale],
            fill=bg_color,
        )

        # ── Word-wrap ────────────────────────────────────────────────────────────
        avg_char_px = font_size * 0.55  # rough estimate
        wrap_chars = max(1, int(max_w / avg_char_px))
        lines = textwrap.wrap(text, width=wrap_chars) or [text]

        # Alignment
        align = node.get("align", "LEFT").upper()

        y_cursor = py
        line_h = font_size * 1.2
        for line in lines:
            if y_cursor + line_h > py + node["height"] * self.scale + line_h:
                break  # clip to node bounds
            x_cursor = px
            if align == "CENTER":
                try:
                    tw = draw.textlength(line, font=font)
                except AttributeError:
                    tw = font.getsize(line)[0]
                x_cursor = px + (max_w - tw) / 2
            elif align == "RIGHT":
                try:
                    tw = draw.textlength(line, font=font)
                except AttributeError:
                    tw = font.getsize(line)[0]
                x_cursor = px + max_w - tw

            draw.text((x_cursor, y_cursor), line, font=font, fill=color)
            y_cursor += line_h

    @staticmethod
    def _sample_background(
        img: Image.Image,
        x: float,
        y: float,
        w: float,
        h: float,
    ) -> tuple[int, ...]:
        """
        Sample the modal (most common) color in the text bounding box to use as
        the erase-background color.  Falls back to white.
        """
        try:
            # Sample a small area just outside the text box (likely the BG color)
            sx = max(0, int(x))
            sy = max(0, int(y))
            ex = min(img.width, int(x + w))
            ey = min(img.height, int(y + h))
            if sx >= ex or sy >= ey:
                return (255, 255, 255)
            crop = img.crop((sx, sy, ex, ey)).convert("RGB")
            # Get all pixel colors and find the most common one
            pixels = list(crop.getdata())
            if not pixels:
                return (255, 255, 255)
            from collections import Counter
            most_common = Counter(pixels).most_common(1)[0][0]
            return most_common
        except Exception:
            return (255, 255, 255)


def build_jpeg(
    base_image: Image.Image,
    record_fields: dict[str, Any],
    text_nodes: list[dict[str, Any]],
    image_nodes: list[dict[str, Any]],
    field_mappings: dict[str, str],
    image_field_mappings: dict[str, str] | None = None,
    photo_images: dict[str, Image.Image] | None = None,
    font_overrides: dict[str, dict] | None = None,
    scale: float = 2.0,
) -> bytes:
    """Convenience function – create an ImageRenderer and render in one call."""
    renderer = ImageRenderer(
        text_nodes=text_nodes,
        image_nodes=image_nodes,
        field_mappings=field_mappings,
        image_field_mappings=image_field_mappings,
        font_overrides=font_overrides,
        scale=scale,
    )
    return renderer.render(base_image, record_fields, photo_images=photo_images)
