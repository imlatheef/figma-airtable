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

# Sentinel used when no font path is resolved at all — replaced with a
# size-aware call inside _load_font() so text isn't rendered at bitmap size.
_FALLBACK_FONT = None

# Fonts directory: <project_root>/fonts/
_FONTS_DIR = Path(__file__).parent.parent.parent / "fonts"

# Maps Figma's fontWeight number → the word Figma appends to the family name
# e.g. fontFamily="Space Grotesk", fontWeight=700 → "Space Grotesk Bold"
_WEIGHT_LABELS: dict[int, str] = {
    100: "Thin",
    200: "ExtraLight",
    300: "Light",
    400: "",          # Regular — Figma usually omits the suffix
    500: "Medium",
    600: "SemiBold",
    700: "Bold",
    800: "ExtraBold",
    900: "Black",
}


def _weight_label(weight: int) -> str:
    """Return the display-name suffix for a CSS font-weight number."""
    if weight in _WEIGHT_LABELS:
        return _WEIGHT_LABELS[weight]
    # Round to nearest 100
    rounded = round(weight / 100) * 100
    return _WEIGHT_LABELS.get(rounded, "")


_SYSTEM_FONTS = [
    "/Library/Fonts/Arial.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "C:/Windows/Fonts/arial.ttf",
]


def _resolve_font_path(font_path: str) -> Path | None:
    """Resolve a font path — absolute, relative-to-fonts-dir, or relative-to-module."""
    p = Path(font_path)
    if p.is_absolute():
        return p if p.exists() else None
    # Try relative to fonts/ directory first (the preferred location)
    in_fonts = _FONTS_DIR / font_path
    if in_fonts.exists():
        return in_fonts
    # Try relative to the module file itself (legacy behaviour)
    in_module = Path(__file__).parent / font_path
    if in_module.exists():
        return in_module
    return None


def _load_font(font_path: str | None, size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    """
    Load a TrueType font at the given size.

    Resolution order:
      1. font_path if provided (absolute, relative to fonts/, or relative to module)
      2. System fonts (Arial / Helvetica / DejaVu)
      3. Pillow built-in default (last resort — renders correctly but looks basic)
    """
    if font_path:
        resolved = _resolve_font_path(font_path)
        if resolved:
            try:
                return ImageFont.truetype(str(resolved), size)
            except Exception as exc:
                log.warning("Could not load font %s: %s", resolved, exc)
        else:
            log.warning("Font file not found: %s (looked in fonts/ and module dir)", font_path)

    for sf in _SYSTEM_FONTS:
        if os.path.exists(sf):
            try:
                return ImageFont.truetype(sf, size)
            except Exception:
                continue

    # Last resort: Pillow's built-in default font.
    # load_default(size=N) is supported in Pillow ≥ 10.1 and renders at the
    # correct pixel size.  Older Pillow returns a fixed tiny bitmap — still
    # better than nothing, and warns us clearly in the log.
    log.warning(
        "No usable font found for size=%dpx — falling back to Pillow default. "
        "Check that font files in fonts/ are static (non-variable) TTF/OTF.",
        size,
    )
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        # Pillow < 10.1 doesn't accept the size argument
        return ImageFont.load_default()


class ImageRenderer:
    """
    Overlays Airtable field values onto a PIL Image exported from Figma.

    Parameters
    ----------
    text_nodes : list[dict]
        Output of FigmaClient.get_text_nodes() – positions, sizes, styles.
    field_mappings : dict[str, str]
        { "Airtable Field Name": "Figma Layer Name" }
    font_map : dict[str, str]
        { "Figma font family name": "path/to/font.ttf" }
        Looked up using the font_family read from each Figma text node.
        Paths are relative to the fonts/ directory.
        Example: { "Space Grotesk Bold": "SpaceGrotesk-Bold.ttf" }
    font_overrides : dict[str, dict]
        { "Figma Layer Name": { "font_path": ..., "font_size": ..., "color": [...] } }
        Per-layer overrides — take priority over font_map.
    scale : float
        The export scale used when the base image was exported from Figma.
    """

    def __init__(
        self,
        text_nodes: list[dict[str, Any]],
        image_nodes: list[dict[str, Any]],
        field_mappings: dict[str, str],
        image_field_mappings: dict[str, str] | None = None,
            font_map: dict[str, str] | None = None,
        font_overrides: dict[str, dict] | None = None,
        erase_placeholders: bool = True,
        scale: float = 2.0,
    ):
        self.text_nodes  = {n["name"]: n for n in text_nodes}   # keyed by layer name
        self.image_nodes = {n["name"]: n for n in image_nodes}
        self.field_mappings       = field_mappings
        self.image_field_mappings = image_field_mappings or {}
        self.font_map           = font_map or {}
        self.font_overrides     = font_overrides or {}
        self.erase_placeholders = erase_placeholders
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

        # Apply a rounded-rectangle (or full circle) mask when cornerRadius > 0
        corner_radius = node.get("corner_radius", 0) or 0
        if node.get("shape") == "ELLIPSE":
            # Full circle/ellipse
            corner_radius = min(pw, ph) / 2
        if corner_radius > 0:
            # Scale the corner radius to match the exported image resolution
            scaled_r = int(corner_radius * self.scale)
            # Clamp so it never exceeds half the shortest side
            scaled_r = min(scaled_r, pw // 2, ph // 2)
            mask = Image.new("L", (pw, ph), 0)
            ImageDraw.Draw(mask).rounded_rectangle(
                [0, 0, pw - 1, ph - 1], radius=scaled_r, fill=255
            )
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

        # Font size
        font_size = int(overrides.get("font_size", node["font_size"]) * self.scale)

        # Font path — resolution order:
        #   1. Explicit font_path in font_overrides  (highest priority)
        #   2. weight shorthand in font_overrides — looks up "{family} Bold" etc. in font_map
        #   3. font_map lookup by Figma font family name (e.g. "Space Grotesk Bold")
        #   4. font_map lookup by PostScript name (e.g. "SpaceGrotesk-Bold")
        #   5. System font / Pillow fallback
        font_path: str | None = overrides.get("font_path")

        if not font_path and "weight" in overrides:
            font_path = self._font_path_for_weight(
                base_family=node.get("font_family", ""),
                weight=overrides["weight"],
            )

        if not font_path:
            family      = node.get("font_family", "")
            post_script = node.get("font_post_script", "")
            weight_num  = node.get("font_weight", 400)

            # Build the combined key Figma-style: "Space Grotesk Bold"
            # Figma returns fontFamily="Space Grotesk" and fontWeight=700 separately,
            # so we reconstruct the display name to match font_map keys.
            weight_label = _weight_label(weight_num)
            family_with_weight = f"{family} {weight_label}".strip() if weight_label else family

            font_path = (
                self.font_map.get(family_with_weight)   # "Space Grotesk Bold"
                or self.font_map.get(post_script)        # "SpaceGrotesk-Bold"
                or self.font_map.get(family)             # "Space Grotesk" (fallback)
            )
            if font_path:
                log.info(
                    "Layer '%s': font '%s' (weight=%d, size=%dpx) → %s",
                    layer_name, family_with_weight, weight_num, font_size, font_path,
                )
            else:
                log.warning(
                    "Layer '%s': no font_map entry for '%s' / '%s' / '%s' (size=%dpx) — using system fallback. "
                    "Add an entry to font_map in templates.yaml.",
                    layer_name, family_with_weight, post_script, family, font_size,
                )

        font = _load_font(font_path, font_size)

        # Color
        color = tuple(overrides.get("color", node["color"]))

        # Position (convert Figma canvas coords → image pixels)
        px = (node["x"] - node["frame_x"]) * self.scale
        py = (node["y"] - node["frame_y"]) * self.scale
        max_w = node["width"] * self.scale

        # ── Erase placeholder text (only when erase_placeholders=True) ──────────
        # Skip this when Figma template text layers are set to 0% opacity —
        # that's the recommended approach as it avoids background colour bleeding.
        if self.erase_placeholders:
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

    def _font_path_for_weight(self, base_family: str, weight: str) -> str | None:
        """
        Look up a font file from font_map using a weight shorthand.

        weight values: "bold", "semibold", "medium", "regular", "light"

        Tries variations like "Space Grotesk Bold", "Space Grotesk SemiBold", etc.
        Falls back to the base family name if no weighted variant is found.
        """
        weight_labels: dict[str, list[str]] = {
            "bold":     ["Bold"],
            "semibold": ["SemiBold", "Semi Bold", "DemiBold"],
            "medium":   ["Medium"],
            "regular":  ["Regular", ""],
            "light":    ["Light"],
            "thin":     ["Thin", "ExtraLight"],
            "black":    ["Black", "ExtraBold"],
            "italic":   ["Italic"],
        }
        # Strip any existing weight suffix from the base family to get the root name
        # e.g. "Space Grotesk Bold" → "Space Grotesk"
        weight_suffixes = [label for labels in weight_labels.values() for label in labels if label]
        root_family = base_family
        for suffix in sorted(weight_suffixes, key=len, reverse=True):
            if base_family.endswith(f" {suffix}"):
                root_family = base_family[: -len(suffix) - 1].strip()
                break

        for label in weight_labels.get(weight.lower(), [weight.capitalize()]):
            candidate = f"{root_family} {label}".strip() if label else root_family
            if candidate in self.font_map:
                log.debug("Weight '%s' → font_map key '%s'", weight, candidate)
                return self.font_map[candidate]

        # Last resort: return whatever the base family maps to
        return self.font_map.get(base_family)

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
    font_map: dict[str, str] | None = None,
    font_overrides: dict[str, dict] | None = None,
    erase_placeholders: bool = True,
    scale: float = 2.0,
) -> bytes:
    """Convenience function – create an ImageRenderer and render in one call."""
    renderer = ImageRenderer(
        text_nodes=text_nodes,
        image_nodes=image_nodes,
        field_mappings=field_mappings,
        image_field_mappings=image_field_mappings,
        font_map=font_map,
        font_overrides=font_overrides,
        erase_placeholders=erase_placeholders,
        scale=scale,
    )
    return renderer.render(base_image, record_fields, photo_images=photo_images)
