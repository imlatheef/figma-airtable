# Airtable → Figma Automation — Product Roadmap

## What it does today
- Polls Airtable for trigger fields (single-select or checkbox)
- Exports Figma frames as images, overlays live data from Airtable
- Supports multi-output templates (one trigger → multiple assets)
- Background removal, location-based variant selection
- PDF generation with text overlay (sponsor recap reports)
- Uploads results back to Airtable attachment fields
- Deployed on Fly.io, polls every 30s

---

## Phase 1 — Reliability & Ops
*Make failures visible and self-healing. No new features, just trust.*

### 1.1 Error writeback to Airtable
When processing fails, write the error message to a dedicated Airtable field (e.g. `Generation Error`) on the record. Team sees failures without checking Fly logs.
- Add optional `error_field` to template/report config in `templates.yaml`
- On exception in `run_pipeline` / `run_pdf_report_pipeline`, patch that field with the error summary
- Clear it on next successful run

### 1.2 Airtable Webhooks (replace polling)
Replace the 30s poll loop with Airtable's webhook API for instant triggers.
- Register a webhook per table on startup pointing to a local HTTP endpoint
- Fly.io app exposes a `/webhook` route (FastAPI or Flask)
- On webhook event, extract record ID and dispatch to the relevant pipeline
- Keep polling as a fallback for missed events
- Expected gain: 0–2s trigger latency vs 0–30s today

### 1.3 Auto-retry queue
Failed records currently sit until someone manually resets the trigger. Add an in-memory retry queue with exponential backoff (e.g. retry after 2m, 10m, 30m before giving up).
- Track `{record_id: (attempt, next_retry_at)}` in memory
- On failure, push to queue instead of logging and moving on
- On final failure, write error to Airtable (see 1.1)

---

## Phase 2 — Output Quality
*Better looking assets, fewer manual fixes.*

### 2.1 Text auto-fit
If a text value is longer than the Figma bounding box width, shrink font size to fit rather than overflow or truncate.
- In `image_renderer.py`: measure text width with PIL's `textbbox`, reduce font size in steps until it fits
- In PDF overlay (`pipeline.py`): do the same with reportlab's `stringWidth`
- Configurable per field mapping: `auto_fit: true`

### 2.2 Conditional layers
Show or hide Figma elements based on an Airtable field value.
- Add `conditions` to template config: `show_if: {field: "Type", value: "Sponsored"}`
- In `image_renderer.py`: check condition before rendering image/text node
- Enables things like: sponsored badge, virtual-only indicators, premium speaker markers

### 2.3 WebP + format options
Output WebP alongside JPEG for web-optimised assets.
- Add `output_format: [jpg, webp]` option to template config
- Generate both and upload both to their respective attachment fields
- Useful for OG images (webp = ~30% smaller, same quality)

### 2.4 Sponsor logo overlay
Pull a logo from an Airtable attachment field and composite it into the generated image or PDF at a configured position.
- Add `logo_field` and `logo_position: {x, y, width, height}` to template/report config
- Download logo bytes from the Airtable attachment URL
- For images: paste onto PIL canvas with transparency preserved
- For PDFs: embed via reportlab `drawImage` (convert SVG→PNG with cairosvg if needed)

---

## Phase 3 — Developer Experience
*Faster iteration, fewer redeploys.*

### 3.1 Figma cache invalidation
Currently Figma frame assets are cached in-memory for the process lifetime. If the Figma design changes, you have to redeploy to pick it up.
- Poll Figma file `last_modified` timestamp every N minutes
- If timestamp changes for a cached frame, evict that entry from `_figma_cache` and `_static_pdf_cache`
- Configurable check interval (default: 10 minutes)

### 3.2 `templates.yaml` hot-reload
Reload config without restarting the process.
- Watch `templates.yaml` mtime; if changed, reload templates and pdf_reports
- Log which templates were added/removed/changed
- Useful during active development of new templates

### 3.3 Dry-run mode
Validate all template config and Figma node IDs without generating anything.
- `--dry-run` CLI flag
- For each template: fetch text nodes from Figma, check all `field_mappings` keys exist as Figma layer names, check all Airtable field names exist on a sample record
- Report mismatches as warnings

---

## Phase 4 — Scale
*Handle more records, more templates, more concurrent users.*

### 4.1 Parallel record processing
Currently records within a template are processed serially. A slow Figma export on record 1 blocks records 2–N.
- Use `concurrent.futures.ThreadPoolExecutor` (or asyncio) to process records in parallel
- Cap concurrency at 3–5 to avoid Figma API rate limits
- Static PDF pages already cached, so second+ records in a batch are fast

### 4.2 CDN / S3 upload option
Instead of Airtable attachments (limited, not publicly linkable), push outputs to S3 or Cloudflare R2 and store the public URL in an Airtable URL field.
- Add `upload_target: s3 | r2 | airtable` to template config
- Better for: embedding OG images in emails, direct linking from websites, avoiding Airtable attachment size limits
- Store bucket config in `.env`

### 4.3 Usage dashboard (lightweight)
A simple web page (can be the same Fly.io app) showing:
- Templates loaded and their last run time
- Per-record processing log (record ID, status, duration, output size)
- Failed records and their error messages
- No auth needed for internal use; add basic auth for external sharing

---

## Backlog (no timeline)
- Multi-language variants (generate same asset in EN/FR/DE from a language field)
- Scheduled regeneration (auto-regenerate all assets on a cron, e.g. weekly)
- Slack/email notification when batch completes
- Self-service sponsor portal (external web UI, sponsors see their own report)
- Template builder UI (map Airtable fields to Figma layers without editing YAML)

---

## Priority order
| # | Item | Impact | Effort |
|---|------|--------|--------|
| 1 | Error writeback (1.1) | High | Low |
| 2 | Sponsor logo overlay (2.4) | High | Medium |
| 3 | Text auto-fit (2.1) | High | Medium |
| 4 | Webhooks (1.2) | High | High |
| 5 | Auto-retry queue (1.3) | Medium | Low |
| 6 | Conditional layers (2.2) | Medium | Medium |
| 7 | Figma cache invalidation (3.1) | Medium | Low |
| 8 | Parallel processing (4.1) | Medium | Medium |
| 9 | CDN upload (4.2) | Medium | High |
| 10 | Dry-run mode (3.3) | Low | Low |
