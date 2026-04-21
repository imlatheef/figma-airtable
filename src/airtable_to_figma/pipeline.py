"""
pipeline.py
───────────
Orchestrates the full flow for a single Airtable record.
"""

from __future__ import annotations

import io
import logging
import threading
from datetime import datetime
from typing import Any

import requests
from PIL import Image

from airtable_to_figma.airtable_client import AirtableClient
from airtable_to_figma.figma_client import FigmaClient
from airtable_to_figma.image_renderer import build_jpeg
from airtable_to_figma.settings import Settings

log = logging.getLogger(__name__)

# ── Per-process cache so we only call Figma once ────────────────────────────────
_cache_lock = threading.Lock()
_base_image_cache: Image.Image | None = None
_text_nodes_cache: list[dict] | None = None
_image_nodes_cache: list[dict] | None = None


def _get_figma_assets(
    figma: FigmaClient,
    frame_node_id: str,
    scale: float,
) -> tuple[Image.Image, list[dict], list[dict]]:
    global _base_image_cache, _text_nodes_cache, _image_nodes_cache
    with _cache_lock:
        if _base_image_cache is None or _text_nodes_cache is None:
            log.info("Fetching Figma template assets (will be cached for this run)…")
            _text_nodes_cache  = figma.get_text_nodes(frame_node_id)
            _image_nodes_cache = figma.get_image_nodes(frame_node_id)
            _base_image_cache  = figma.export_frame_image(frame_node_id, scale=scale)
            log.info(
                "Cached %d text node(s), %d image node(s), image size=%s",
                len(_text_nodes_cache), len(_image_nodes_cache), _base_image_cache.size,
            )
        return _base_image_cache, _text_nodes_cache, _image_nodes_cache


def _download_photo(url: str) -> Image.Image | None:
    try:
        resp = requests.get(url, timeout=30)
        resp.raise_for_status()
        return Image.open(io.BytesIO(resp.content)).convert("RGBA")
    except Exception as exc:
        log.warning("Could not download photo from %s: %s", url[:80], exc)
        return None


def _get_photo_images(
    fields: dict[str, Any],
    image_field_mappings: dict[str, str],
) -> dict[str, Image.Image]:
    photo_images: dict[str, Image.Image] = {}
    for airtable_field, figma_layer in image_field_mappings.items():
        attachments = fields.get(airtable_field)
        if not attachments or not isinstance(attachments, list):
            continue
        photo_url = attachments[0].get("url", "")
        if not photo_url:
            continue
        log.info("Downloading photo for layer '%s'…", figma_layer)
        img = _download_photo(photo_url)
        if img:
            photo_images[figma_layer] = img
    return photo_images


def run_pipeline(settings: Settings, record_id: str) -> None:
    """End-to-end pipeline for one Airtable record."""
    at  = settings.airtable
    fig = settings.figma
    m   = settings.mappings

    field_mappings       = m.field_mappings_dict
    image_field_mappings = m.image_field_mappings_dict

    # ── 1. Fetch record ───────────────────────────────────────────────────────
    airtable = AirtableClient(
        api_key=at.api_key,
        base_id=at.base_id,
        table_name=at.table_name,
        imgbb_key=at.imgbb_api_key,
    )
    fields = airtable.get_fields(record_id)
    log.info("Record %s fields: %s", record_id, list(fields.keys()))

    # ── Debug: show mapped field values ──────────────────────────────────────
    log.info("── Field values for rendering ──")
    for at_field, fig_layer in field_mappings.items():
        val = fields.get(at_field)
        log.info("  [%s] → [%s]  =  %r", at_field, fig_layer, val)

    # Skip if trigger not set
    if not fields.get(at.trigger_field):
        log.info("Record %s trigger not set – skipping.", record_id)
        return

    # ── 2 & 3. Figma assets (cached) ─────────────────────────────────────────
    figma = FigmaClient(api_key=fig.api_key, file_key=fig.file_key)
    base_image, text_nodes, image_nodes = _get_figma_assets(
        figma, fig.frame_node_id, fig.export_scale
    )

    # ── 4. Download photos ────────────────────────────────────────────────────
    photo_images = _get_photo_images(fields, image_field_mappings)

    # ── 5. Render ─────────────────────────────────────────────────────────────
    jpeg_bytes = build_jpeg(
        base_image=base_image,
        record_fields=fields,
        text_nodes=text_nodes,
        image_nodes=image_nodes,
        field_mappings=field_mappings,
        image_field_mappings=image_field_mappings,
        photo_images=photo_images,
        scale=fig.export_scale,
    )
    log.info("Rendered JPEG  size=%d bytes", len(jpeg_bytes))

    # ── 6. Clear old attachment + upload new one ──────────────────────────────
    try:
        airtable._session.patch(
            f"{airtable._table_url}/{record_id}",
            json={"fields": {at.attachment_field: []}},
        )
    except Exception as e:
        log.warning("Could not clear old attachment: %s", e)

    timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    filename  = f"design_{record_id}_{timestamp}.jpg"
    airtable.upload_attachment(
        record_id=record_id,
        attachment_field=at.attachment_field,
        image_bytes=jpeg_bytes,
        filename=filename,
    )
    log.info("✓ Design uploaded  record=%s  file=%s", record_id, filename)

    # ── 7. Uncheck trigger field ──────────────────────────────────────────────
    try:
        airtable._session.patch(
            f"{airtable._table_url}/{record_id}",
            json={"fields": {at.trigger_field: False}},
        )
        log.info("Auto-unchecked '%s'", at.trigger_field)
    except Exception as e:
        log.warning("Could not uncheck trigger field: %s", e)
