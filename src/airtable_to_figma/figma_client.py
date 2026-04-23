"""
figma_client.py
───────────────
Figma REST API wrapper.

Capabilities used:
  • GET /v1/files/:key/nodes  – read text-node positions & styles from a frame
  • GET /v1/images/:key       – export a frame as JPG/PNG bytes

The Figma REST API is read-only for file content; we export the template as a
base image and separately read text-node metadata (position, size, font) so the
image_renderer can overlay live data from Airtable.
"""

from __future__ import annotations

import io
import logging
from typing import Any

import requests
from PIL import Image

log = logging.getLogger(__name__)

# ── Figma fills: type SOLID ────────────────────────────────────────────────────
def _rgba_from_fill(fills: list[dict]) -> tuple[int, int, int]:
    """Return (R, G, B) from the first SOLID fill, default black."""
    for fill in fills:
        if fill.get("type") == "SOLID":
            c = fill.get("color", {})
            r = round(c.get("r", 0) * 255)
            g = round(c.get("g", 0) * 255)
            b = round(c.get("b", 0) * 255)
            return (r, g, b)
    return (0, 0, 0)


class FigmaClient:
    BASE = "https://api.figma.com/v1"

    def __init__(self, api_key: str, file_key: str):
        self.file_key = file_key
        self.session = requests.Session()
        self.session.headers.update({"X-Figma-Token": api_key})

    # ── Internal helpers ────────────────────────────────────────────────────────

    def _get(self, path: str, **params) -> dict:
        url = f"{self.BASE}{path}"
        resp = self.session.get(url, params=params)
        resp.raise_for_status()
        return resp.json()

    def _node_data(self, node_id: str) -> dict:
        """Return the document dict for a single node."""
        data = self._get(f"/files/{self.file_key}/nodes", ids=node_id)
        nodes = data.get("nodes", {})
        # Figma may return the id with ':' or '-' as separator
        for key in (node_id, node_id.replace(":", "-"), node_id.replace("-", ":")):
            if key in nodes and nodes[key]:
                return nodes[key].get("document", {})
        # Fall back to first available
        for v in nodes.values():
            if v:
                return v.get("document", {})
        return {}

    # ── Public API ──────────────────────────────────────────────────────────────

    def get_frame_size(self, frame_node_id: str) -> tuple[float, float]:
        """Return (width, height) of the template frame in Figma units."""
        doc = self._node_data(frame_node_id)
        box = doc.get("absoluteBoundingBox", {})
        return box.get("width", 800), box.get("height", 600)

    def get_image_nodes(self, frame_node_id: str) -> list[dict[str, Any]]:
        """
        Walk the frame tree and return a list of non-text nodes (FRAME, RECTANGLE,
        ELLIPSE, COMPONENT, etc.) that can act as image placeholders.

        Each entry:
        {
            "id":      str,
            "name":    str,    # Figma layer name  e.g. "Photo"
            "x":       float,
            "y":       float,
            "frame_x": float,
            "frame_y": float,
            "width":   float,
            "height":  float,
            "shape":   str,    # "ELLIPSE" or "RECTANGLE"
        }
        """
        doc = self._node_data(frame_node_id)
        frame_box = doc.get("absoluteBoundingBox", {})
        frame_x = frame_box.get("x", 0)
        frame_y = frame_box.get("y", 0)

        image_nodes: list[dict] = []
        self._walk_images(doc, image_nodes, frame_x, frame_y)
        log.info("Found %d image node(s) in frame %s", len(image_nodes), frame_node_id)
        return image_nodes

    def _walk_images(self, node: dict, result: list, frame_x: float, frame_y: float):
        IMAGE_TYPES = {"FRAME", "RECTANGLE", "ELLIPSE", "COMPONENT", "INSTANCE", "VECTOR"}
        node_type = node.get("type", "")
        # Skip the root frame itself and text nodes
        if node_type in IMAGE_TYPES and node_type != "TEXT":
            box = node.get("absoluteBoundingBox", {})
            result.append({
                "id":      node.get("id", ""),
                "name":    node.get("name", ""),
                "x":       box.get("x", 0),
                "y":       box.get("y", 0),
                "frame_x": frame_x,
                "frame_y": frame_y,
                "width":   box.get("width", 100),
                "height":  box.get("height", 100),
                "shape":   node_type,
            })
        for child in node.get("children", []):
            self._walk_images(child, result, frame_x, frame_y)

    def get_text_nodes(self, frame_node_id: str) -> list[dict[str, Any]]:
        """
        Walk the frame tree and return a list of text-node descriptors:

        {
            "id":       str,
            "name":     str,          # Figma layer name
            "text":     str,          # current placeholder text
            "x":        float,        # absolute x (pixels in Figma)
            "y":        float,
            "frame_x":  float,        # frame's own absolute x (for offset calc)
            "frame_y":  float,
            "width":    float,
            "height":   float,
            "font_size": float,
            "color":    (R, G, B),
            "align":    str,          # "LEFT" | "CENTER" | "RIGHT"
        }
        """
        doc = self._node_data(frame_node_id)
        frame_box = doc.get("absoluteBoundingBox", {})
        frame_x = frame_box.get("x", 0)
        frame_y = frame_box.get("y", 0)

        text_nodes: list[dict] = []
        self._walk(doc, text_nodes, frame_x, frame_y)
        log.info("Found %d text node(s) in frame %s", len(text_nodes), frame_node_id)
        return text_nodes

    def _walk(self, node: dict, result: list, frame_x: float, frame_y: float):
        if node.get("type") == "TEXT":
            box = node.get("absoluteBoundingBox", {})
            style = node.get("style", {})
            fills = node.get("fills", [])
            result.append(
                {
                    "id": node.get("id", ""),
                    "name": node.get("name", ""),
                    "text": node.get("characters", ""),
                    "x": box.get("x", 0),
                    "y": box.get("y", 0),
                    "frame_x": frame_x,
                    "frame_y": frame_y,
                    "width": box.get("width", 200),
                    "height": box.get("height", 40),
                    "font_size": style.get("fontSize", 16),
                    "font_family": style.get("fontFamily", ""),
                    "font_post_script": style.get("fontPostScriptName", ""),
                    "font_weight": style.get("fontWeight", 400),
                    "color": _rgba_from_fill(fills),
                    "align": style.get("textAlignHorizontal", "LEFT"),
                }
            )
        for child in node.get("children", []):
            self._walk(child, result, frame_x, frame_y)

    def export_frame_image(
        self,
        frame_node_id: str,
        scale: float = 2.0,
        fmt: str = "jpg",
    ) -> Image.Image:
        """
        Export the Figma template frame as a PIL Image.

        scale=2 → double resolution (good for retina/print).
        """
        data = self._get(
            f"/images/{self.file_key}",
            ids=frame_node_id,
            format=fmt,
            scale=scale,
        )
        images: dict = data.get("images", {})

        # Figma returns URLs keyed by node-id (may use ':' or '-')
        url: str | None = None
        for key in (frame_node_id, frame_node_id.replace(":", "-"), frame_node_id.replace("-", ":")):
            url = images.get(key)
            if url:
                break
        if not url and images:
            url = next(iter(images.values()))

        if not url:
            raise RuntimeError(
                f"Figma returned no image URL for node {frame_node_id}. "
                "Check that the node-id is correct and the frame is visible."
            )

        log.debug("Downloading Figma export from %s", url[:60])
        resp = requests.get(url, timeout=60)
        resp.raise_for_status()

        img = Image.open(io.BytesIO(resp.content)).convert("RGBA")
        log.info("Exported frame %s  size=%s", frame_node_id, img.size)
        return img
