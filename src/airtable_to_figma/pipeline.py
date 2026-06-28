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
import re
from datetime import datetime
from pathlib import Path
from typing import Any

import requests
from PIL import Image

from airtable_to_figma.airtable_client import AirtableClient
from airtable_to_figma.background_remover import remove_background_if_needed
from airtable_to_figma.figma_client import FigmaClient
from airtable_to_figma.image_renderer import build_jpeg
from airtable_to_figma.settings import Settings
from airtable_to_figma.template import PdfReportConfig, TemplateConfig, TemplateOutput, TemplateVariant

FONTS_DIR = Path(__file__).parent.parent.parent / "fonts"
_registered_pdf_fonts: set[str] = set()

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
    # Priority: boolean_variant_field → field_variant_field → auto_variant_on_field → variant_field → default
    boolean_variant = output.resolve_boolean_variant(fields, template.name)
    if boolean_variant is not None:
        variant = boolean_variant
    elif output.field_variant_field:
        field_variant = output.resolve_field_variant(fields, template.name)
        variant = field_variant if field_variant is not None else output.resolve_variant("", template.name)
    elif output.auto_variant_on_field:
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


# ── PDF report pipeline ────────────────────────────────────────────────────────

def _parse_layer_key(key: str) -> tuple[str, int]:
    """Parse 'LayerName[2]' → ('LayerName', 2). Plain name → (name, 0)."""
    m = re.match(r"^(.*)\[(\d+)\]$", key)
    if m:
        return m.group(1), int(m.group(2))
    return key, 0


def _register_pdf_font(font_file: str) -> str:
    """Register a TTF font with reportlab and return its registered name."""
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    font_name = Path(font_file).stem
    if font_name not in _registered_pdf_fonts:
        abs_path = FONTS_DIR / font_file
        if abs_path.exists():
            pdfmetrics.registerFont(TTFont(font_name, str(abs_path)))
            _registered_pdf_fonts.add(font_name)
            log.debug("Registered PDF font '%s' from %s", font_name, abs_path)
        else:
            log.warning("Font file not found: %s — falling back to Helvetica", abs_path)
            return "Helvetica"
    return font_name


def _resolve_pdf_font(node: dict, font_map: dict[str, str]) -> str:
    """Return a reportlab-registered font name for a text node."""
    family = node.get("font_family", "")
    weight = node.get("font_weight", 400)

    if weight >= 700:
        candidates = [f"{family} Bold", f"{family} SemiBold", family]
    elif weight >= 600:
        candidates = [f"{family} SemiBold", f"{family} Bold", family]
    elif weight >= 500:
        candidates = [f"{family} Medium", family]
    elif weight <= 300:
        candidates = [f"{family} Light", family]
    else:
        candidates = [family]

    for candidate in candidates:
        if candidate in font_map:
            return _register_pdf_font(font_map[candidate])

    return "Helvetica"


def _merge_pdfs(pdf_list: list[bytes]) -> bytes:
    """Merge a list of single-page PDF byte strings into one multi-page PDF."""
    from pypdf import PdfReader, PdfWriter

    writer = PdfWriter()
    for pdf_bytes in pdf_list:
        reader = PdfReader(io.BytesIO(pdf_bytes))
        for page in reader.pages:
            writer.add_page(page)
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def _overlay_text_on_pdf(
    pdf_bytes: bytes,
    fields: dict[str, Any],
    field_mappings: dict[str, str],
    text_nodes: list[dict],
    frame_width: float,
    frame_height: float,
    font_map: dict[str, str],
) -> bytes:
    """
    Overlay Airtable data onto a Figma-exported PDF page.

    field_mappings keys are Figma layer names (optionally with [index] suffix
    to disambiguate duplicate layer names, e.g. "#Leads scanned at booth[1]").
    Values are Airtable field names.

    Positions are read from the Figma text_nodes and scaled to match the actual
    PDF page dimensions (handles Figma's variable PDF export DPI).
    """
    from pypdf import PdfReader, PdfWriter
    from reportlab.lib.units import pt
    from reportlab.pdfgen import canvas as rl_canvas

    # Read actual PDF page size (Figma may export at a different scale than frame units)
    reader = PdfReader(io.BytesIO(pdf_bytes))
    page = reader.pages[0]
    pdf_w = float(page.mediabox.width)
    pdf_h = float(page.mediabox.height)
    scale_x = pdf_w / frame_width
    scale_y = pdf_h / frame_height

    # Build index-aware lookup: layer_name → [node, node, ...]
    nodes_by_name: dict[str, list[dict]] = {}
    for n in text_nodes:
        nodes_by_name.setdefault(n["name"], []).append(n)

    buf = io.BytesIO()
    c = rl_canvas.Canvas(buf, pagesize=(pdf_w, pdf_h))

    for layer_key, airtable_field in field_mappings.items():
        layer_name, idx = _parse_layer_key(layer_key)
        node_list = nodes_by_name.get(layer_name, [])
        if idx >= len(node_list):
            log.warning("PDF overlay: layer '%s'[%d] not found in frame", layer_name, idx)
            continue

        raw_val = fields.get(airtable_field)
        if raw_val is None:
            log.warning("PDF overlay: Airtable field '%s' is empty — skipping", airtable_field)
            continue
        text = str(int(raw_val)) if isinstance(raw_val, float) and raw_val == int(raw_val) else str(raw_val)

        node = node_list[idx]
        rel_x = (node["x"] - node["frame_x"]) * scale_x
        rel_y = (node["y"] - node["frame_y"]) * scale_y
        node_w = node["width"] * scale_x
        node_h = node["height"] * scale_y
        font_size = node["font_size"] * scale_y

        # Vertical baseline: centre within the node box
        baseline_y = pdf_h - rel_y - node_h / 2 - font_size * 0.3

        font_name = _resolve_pdf_font(node, font_map)
        c.setFont(font_name, font_size)

        color = node.get("color", (0, 0, 0))
        c.setFillColorRGB(color[0] / 255, color[1] / 255, color[2] / 255)

        align = node.get("align", "LEFT")
        if align == "CENTER":
            c.drawCentredString(rel_x + node_w / 2, baseline_y, text)
        elif align == "RIGHT":
            c.drawRightString(rel_x + node_w, baseline_y, text)
        else:
            c.drawString(rel_x, baseline_y, text)

        log.debug("PDF overlay: '%s' = %r  at (%.1f, %.1f) font=%s %.1fpt",
                  layer_name, text, rel_x, baseline_y, font_name, font_size)

    c.save()
    overlay_bytes = buf.getvalue()

    # Merge Figma PDF + text overlay
    overlay_reader = PdfReader(io.BytesIO(overlay_bytes))
    page.merge_page(overlay_reader.pages[0])

    writer = PdfWriter()
    writer.add_page(page)
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def run_pdf_report_pipeline(
    settings: Settings,
    report: PdfReportConfig,
    record_id: str,
) -> None:
    """End-to-end pipeline for one sponsor PDF report record."""
    log.info("[%s] Processing PDF report for record %s", report.name, record_id)

    airtable = AirtableClient(
        api_key=settings.airtable_api_key,
        base_id=report.airtable_base_id,
        table_name=report.airtable_table_name,
        imgbb_key=settings.airtable_imgbb_api_key,
    )
    fields = airtable.get_fields(record_id)

    # Check trigger
    trigger_val = fields.get(report.airtable_trigger_field)
    if report.airtable_trigger_value:
        if trigger_val != report.airtable_trigger_value:
            log.info("[%s] Trigger is '%s', expected '%s' — skipping",
                     report.name, trigger_val, report.airtable_trigger_value)
            return
    else:
        if not trigger_val:
            log.info("[%s] Trigger not set on %s — skipping", report.name, record_id)
            return

    if report.airtable_trigger_pending_value:
        _set_status(
            airtable,
            TemplateConfig(  # type: ignore[call-arg]
                name=report.name,
                airtable_base_id=report.airtable_base_id,
                airtable_table_name=report.airtable_table_name,
                airtable_trigger_field=report.airtable_trigger_field,
            ),
            record_id,
            report.airtable_trigger_pending_value,
        )

    figma = FigmaClient(api_key=settings.figma_api_key, file_key=report.figma_file_key)
    pdf_pages: list[bytes] = []

    # Static pages — export as PDF, no overlay
    for frame_id in report.static_page_ids:
        log.info("[%s] Static page %s", report.name, frame_id)
        pdf_pages.append(figma.export_frame_pdf(frame_id))

    # Resolve location combo
    raw_loc = fields.get(report.location_field, [])
    if isinstance(raw_loc, list):
        locations = frozenset(str(v).strip() for v in raw_loc)
    else:
        locations = frozenset(v.strip() for v in str(raw_loc).split(",")) if raw_loc else frozenset()

    log.info("[%s] Location values: %s", report.name, sorted(locations))

    matching = next(
        (c for c in report.combos if frozenset(c.locations) == locations),
        None,
    )
    if not matching:
        log.warning("[%s] No combo matches %s — PDF will have only static pages", report.name, locations)
    else:
        for page in matching.pages:
            log.info("[%s] Sponsor page %s", report.name, page.figma_frame_node_id)
            pdf_bytes = figma.export_frame_pdf(page.figma_frame_node_id)

            if page.field_mappings:
                text_nodes = figma.get_text_nodes(page.figma_frame_node_id)
                frame_w, frame_h = figma.get_frame_size(page.figma_frame_node_id)
                pdf_bytes = _overlay_text_on_pdf(
                    pdf_bytes,
                    fields,
                    page.field_mappings,
                    text_nodes,
                    frame_w,
                    frame_h,
                    report.font_map,
                )
            pdf_pages.append(pdf_bytes)

    merged = _merge_pdfs(pdf_pages)
    log.info("[%s] Merged PDF  pages=%d  size=%d bytes", report.name, len(pdf_pages), len(merged))

    # Clear old attachment
    try:
        airtable._session.patch(
            f"{airtable._table_url}/{record_id}",
            json={"fields": {report.attachment_field: []}},
        )
    except Exception as e:
        log.warning("[%s] Could not clear old attachment: %s", report.name, e)

    timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    filename = f"sponsor_report_{record_id}_{timestamp}.pdf"
    airtable.upload_attachment(
        record_id=record_id,
        attachment_field=report.attachment_field,
        image_bytes=merged,
        filename=filename,
    )
    log.info("[%s] ✓ Uploaded  record=%s", report.name, record_id)

    if report.airtable_trigger_reset_value:
        try:
            airtable._session.patch(
                f"{airtable._table_url}/{record_id}",
                json={"fields": {report.airtable_trigger_field: report.airtable_trigger_reset_value}},
            )
            log.info("[%s] Status → '%s'", report.name, report.airtable_trigger_reset_value)
        except Exception as e:
            log.warning("[%s] Could not reset trigger: %s", report.name, e)
