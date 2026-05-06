"""
pipeline.py
───────────
Processes one Airtable record for one template.

Supports two modes transparently:
  - Single output:  one Figma frame → one image → one attachment field
  - Multi-output:   one trigger → multiple frames → multiple attachment fields
                    (e.g. OpenGraph + LinkedIn Social + Speaker Social)
"""

from __future__ import annotations

import io
import logging
from datetime import datetime
from typing import Any

import requests
from PIL import Image

from airtable_to_figma.airtable_client import AirtableClient
from airtable_to_figma.background_remover import remove_background_if_needed
from airtable_to_figma.figma_client import FigmaClient
from airtable_to_figma.image_renderer import build_jpeg
from airtable_to_figma.settings import Settings
from airtable_to_figma.template import TemplateConfig, TemplateOutput, TemplateVariant

log = logging.getLogger(__name__)

# ── Per-variant Figma cache keyed by (file_key, frame_node_id) ──────────────
_figma_cache: dict[tuple[str, str], tuple[Image.Image, list[dict], list[dict]]] = {}


def _get_figma_assets(
    figma: FigmaClient,
    variant: TemplateVariant,
    label: str,
) -> tuple[Image.Image, list[dict], list[dict]]:
    """Return (base_image, text_nodes, image_nodes), cached per variant."""
    key = variant.cache_key
    if key not in _figma_cache:
        log.info("[%s] Fetching Figma assets for frame %s (will be cached)…",
                 label, variant.figma_frame_node_id)
        text_nodes  = figma.get_text_nodes(variant.figma_frame_node_id)
        image_nodes = figma.get_image_nodes(variant.figma_frame_node_id)
        base_image  = figma.export_frame_image(
            variant.figma_frame_node_id, scale=variant.figma_export_scale
        )
        _figma_cache[key] = (base_image, text_nodes, image_nodes)
        log.info(
            "[%s] Cached — %d text, %d image nodes, size=%s",
            label, len(text_nodes), len(image_nodes), base_image.size,
        )
    return _figma_cache[key]


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
    remove_bg: bool = False,
) -> dict[str, Image.Image]:
    photo_images: dict[str, Image.Image] = {}
    for airtable_field, figma_layer in image_field_mappings.items():
        attachments = fields.get(airtable_field)
        if not attachments or not isinstance(attachments, list):
            continue
        photo_url = attachments[0].get("url", "")
        if not photo_url:
            continue
        img = _download_photo(photo_url)
        if img:
            if remove_bg:
                img = remove_background_if_needed(img)
            photo_images[figma_layer] = img
            log.info("Downloaded photo for layer '%s'", figma_layer)
    return photo_images


def _process_output(
    airtable: AirtableClient,
    settings: Settings,
    template: TemplateConfig,
    output: TemplateOutput,
    fields: dict[str, Any],
    record_id: str,
) -> None:
    """Render one output and upload it to its attachment field."""
    label = f"{template.name} › {output.name}"

    # ── Resolve variant ───────────────────────────────────────────────────────
    # Auto-variant takes priority: check if a presence-based field triggers it
    if output.auto_variant_on_field:
        variant = output.resolve_auto_variant(fields, template.name)
    else:
        variant_value = ""
        if output.variant_field:
            raw = fields.get(output.variant_field, "")
            if isinstance(raw, list):
                raw = raw[0] if raw else ""
            variant_value = str(raw).strip()
            log.info("[%s] Variant field '%s' = '%s'", label, output.variant_field, variant_value)
        variant = output.resolve_variant(variant_value, template.name)

    # ── Merge field mappings ──────────────────────────────────────────────────
    field_mappings, image_field_mappings = template.resolve_field_mappings(output)

    # ── Figma assets ──────────────────────────────────────────────────────────
    figma = FigmaClient(
        api_key=settings.figma_api_key,
        file_key=variant.figma_file_key,
    )
    base_image, text_nodes, image_nodes = _get_figma_assets(figma, variant, label)

    # ── Photos ────────────────────────────────────────────────────────────────
    photo_images = _get_photo_images(fields, image_field_mappings, remove_bg=template.remove_background)

    # ── Render ────────────────────────────────────────────────────────────────
    jpeg_bytes = build_jpeg(
        base_image=base_image,
        record_fields=fields,
        text_nodes=text_nodes,
        image_nodes=image_nodes,
        field_mappings=field_mappings,
        image_field_mappings=image_field_mappings,
        photo_images=photo_images,
        font_map=template.font_map,
        font_overrides=template.font_overrides,
        erase_placeholders=template.erase_placeholders,
        scale=variant.figma_export_scale,
    )
    log.info("[%s] Rendered JPEG  size=%d bytes", label, len(jpeg_bytes))

    # ── Clear old attachment ──────────────────────────────────────────────────
    try:
        airtable._session.patch(
            f"{airtable._table_url}/{record_id}",
            json={"fields": {output.attachment_field: []}},
        )
    except Exception as e:
        log.warning("[%s] Could not clear old attachment: %s", label, e)

    # ── Upload ────────────────────────────────────────────────────────────────
    timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    filename  = f"{output.name.lower().replace(' ', '_')}_{record_id}_{timestamp}.jpg"
    airtable.upload_attachment(
        record_id=record_id,
        attachment_field=output.attachment_field,
        image_bytes=jpeg_bytes,
        filename=filename,
    )
    log.info("[%s] ✓ Uploaded  record=%s  field=%s", label, record_id, output.attachment_field)


def _set_status(
    airtable: AirtableClient,
    template: TemplateConfig,
    record_id: str,
    value: str | bool,
) -> None:
    """Write a value to the trigger field — used for status progression."""
    try:
        airtable._session.patch(
            f"{airtable._table_url}/{record_id}",
            json={"fields": {template.airtable_trigger_field: value}},
        )
        log.info("[%s] Status → '%s'", template.name, value)
    except Exception as e:
        log.warning("[%s] Could not update status to '%s': %s", template.name, value, e)


def run_pipeline(settings: Settings, template: TemplateConfig, record_id: str) -> None:
    """End-to-end pipeline for one record — processes all outputs."""
    log.info("[%s] Processing record %s", template.name, record_id)

    # ── 1. Fetch record ───────────────────────────────────────────────────────
    airtable = AirtableClient(
        api_key=settings.airtable_api_key,
        base_id=template.airtable_base_id,
        table_name=template.airtable_table_name,
        imgbb_key=settings.airtable_imgbb_api_key,
    )
    fields = airtable.get_fields(record_id)

    # Skip if trigger not set
    trigger_val = fields.get(template.airtable_trigger_field)
    if template.airtable_trigger_value:
        if trigger_val != template.airtable_trigger_value:
            log.info("[%s] Trigger field is '%s', expected '%s' — skipping",
                     template.name, trigger_val, template.airtable_trigger_value)
            return
    else:
        if not trigger_val:
            log.info("[%s] Trigger not set on %s — skipping", template.name, record_id)
            return

    # ── 2. Mark as Pending (record queued, not yet rendering) ────────────────
    if template.airtable_trigger_pending_value:
        _set_status(airtable, template, record_id, template.airtable_trigger_pending_value)

    # Log field values once (shared across all outputs)
    log.info("[%s] Field values:", template.name)
    for at_field, fig_layer in template.field_mappings.items():
        val = fields.get(at_field)
        if isinstance(val, list):
            val = val[0] if val else ""
        log.info("  %s → %s = %r", at_field, fig_layer, val)

    # ── 3. Determine which outputs to run ────────────────────────────────────
    outputs = template.selected_outputs(fields)
    if not outputs:
        log.info("[%s] No outputs selected for record %s — skipping", template.name, record_id)
        return
    log.info("[%s] Generating %d output(s): %s",
             template.name, len(outputs), [o.name for o in outputs])

    # ── 4. Mark as Working (actively rendering) ───────────────────────────────
    if template.airtable_trigger_working_value:
        _set_status(airtable, template, record_id, template.airtable_trigger_working_value)

    errors = []
    for output in outputs:
        try:
            _process_output(airtable, settings, template, output, fields, record_id)
        except Exception as exc:
            log.error("[%s › %s] Failed: %s", template.name, output.name, exc)
            errors.append((output.name, exc))

    # ── 5. Reset trigger field to final status ────────────────────────────────
    if template.airtable_trigger_value:
        if not template.airtable_trigger_reset_value:
            log.info("[%s] No trigger_reset_value set — leaving status unchanged", template.name)
        else:
            _set_status(airtable, template, record_id, template.airtable_trigger_reset_value)
    else:
        # Checkbox: uncheck it
        _set_status(airtable, template, record_id, False)

    if errors:
        failed = ", ".join(name for name, _ in errors)
        raise RuntimeError(
            f"[{template.name}] {len(errors)} output(s) failed: {failed}"
        )

    log.info("[%s] ✓ All outputs complete for record %s", template.name, record_id)
