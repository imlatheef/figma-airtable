# Sponsor Report PDF — Implementation Plan

## Overview

A new template type in `templates.yaml` that exports multiple Figma frames as PDFs, merges them into a single multi-page PDF, and uploads it to one Airtable attachment field. One record = one sponsor = one PDF report.

---

## 1. New `templates.yaml` shape

A report template looks like a normal template but with `format: pdf` at the output level, and instead of a single frame it has a `pages:` list. Each page is its own frame with its own field mappings.

```yaml
- name: "PlatformCon Sponsor Report"
  airtable_base_id: "..."
  airtable_table_name: "Sponsors"
  airtable_trigger_field: "Report status"
  airtable_trigger_value: "Generate report"
  airtable_trigger_reset_value: "Report ready"

  font_map:
    "Space Grotesk Bold": "SpaceGrotesk-Bold.ttf"

  outputs:
    - name: "Sponsor Report PDF"
      format: pdf                        # new flag — triggers PDF path
      attachment_field: "Sponsor Report"
      figma_file_key: "..."
      figma_export_scale: 2.0            # used for image fallback only
      pages:
        - name: "Cover"
          figma_frame_node_id: "..."
          field_mappings:
            "Company Name": "Sponsor name"
          include_always: true
        - name: "Stats"
          figma_frame_node_id: "..."
          field_mappings:
            "Attendee Count": "attendees"
            "Talk Views": "views"
          include_always: true
        - name: "Job Board"
          figma_frame_node_id: "..."
          include_if_field: "Job Board Add-on"   # only included if this field is truthy
```

---

## 2. Changes to `template.py`

- `TemplateOutput` gets a `format` field (`"jpeg"` default, `"pdf"` new option)
- New `TemplatePage` dataclass: `name`, `figma_frame_node_id`, `field_mappings` (overrides), `include_always`, `include_if_field`
- `TemplateOutput.pages` — list of `TemplatePage`, only present when `format == "pdf"`

---

## 3. Changes to `figma_client.py`

Add one new method:

```python
def export_frame_pdf(self, frame_node_id: str) -> bytes:
    """Export a single Figma frame as a PDF and return raw bytes."""
```

This hits the same `/v1/images/{file_key}` endpoint with `format=pdf`. Returns the raw PDF bytes for that frame.

---

## 4. Changes to `pipeline.py`

Add a new `_process_pdf_output()` function (parallel to `_process_output`):

1. Iterate over the output's `pages:` list
2. For each page, check `include_if_field` — skip if falsy
3. Fill text nodes via the existing field mapping logic (same as current, but no image compositing)
4. Call `figma.export_frame_pdf(page.figma_frame_node_id)` → raw PDF bytes per page
5. Merge all pages using `pypdf` (`PdfWriter` + `PdfReader`)
6. Upload the merged PDF bytes to `output.attachment_field`

The existing `_process_output()` (JPEG path) is untouched.

In `run_pipeline()`, the dispatch is a single `if output.format == "pdf"` check.

---

## 5. New dependency

Add `pypdf` to `pyproject.toml` — lightweight, no system deps, actively maintained.

---

## 6. Open question before implementation

The Figma PDF export endpoint renders the frame as Figma sees it — it won't reflect text substitutions we apply locally. The current JPEG pipeline renders the base frame image and composites text on top in Python. For PDF, there are three options:

- **Option A (simpler):** Use Figma's Variables/Plugin API to push data into the live file before export — requires write access and is complex.
- **Option B (recommended):** Export the Figma frame as a PDF for the layout/graphics, then overlay the substituted text as a PDF text layer on top using `pypdf` or `reportlab`. Keeps vector fidelity and still injects real data.
- **Option C (pragmatic fallback):** Render each page as a high-res image via the existing `build_jpeg` path, then wrap into a PDF with `fpdf2` or `img2pdf`. Slightly pixelated at very high zoom but indistinguishable at normal reading size.

Recommendation is **Option B** — export Figma as PDF for the visual layer, overlay text programmatically. Worth confirming once the designs are finalised and it's clear how text-heavy vs graphic-heavy the pages are.

---

## Implementation sequence

1. Figma designs finalised — file key + frame node IDs confirmed
2. Add `pypdf` dependency and `export_frame_pdf()` to `figma_client.py`
3. Extend `template.py` with the new `pages:` schema
4. Add `_process_pdf_output()` to `pipeline.py`
5. Configure `templates.yaml` with the sponsor table and frame IDs
6. Test on one sponsor record
