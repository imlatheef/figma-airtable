"""
template.py
───────────
TemplateConfig model and loader.

Each template represents one design automation workflow:
  - Which Airtable base + table to watch
  - Which Figma file + frame to use as the template
  - How Airtable fields map to Figma layer names

Templates support two advanced features:

  1. Outputs — one trigger produces multiple images, each uploaded to its own
     Airtable attachment field. Define an `outputs` list; each output has its
     own Figma frame and attachment field while sharing the field_mappings.

  2. Variants — a single Airtable field (e.g. "Event Location") selects which
     Figma frame to use (e.g. Virtual / London / New York). Works both at the
     template level (single-output) and per-output.

Templates are defined in templates.yaml (no secrets — safe to commit).
API keys stay in .env only.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, field_validator

log = logging.getLogger(__name__)

TEMPLATES_FILE = Path(__file__).parent.parent.parent / "templates.yaml"


# ── Variant ────────────────────────────────────────────────────────────────────

class TemplateVariant(BaseModel):
    """
    A single visual variant — different Figma frame, same layer names.
    e.g. the "London" variant uses a different frame than "Virtual".
    """
    figma_file_key:      str
    figma_frame_node_id: str
    figma_export_scale:  float = 2.0

    @field_validator("figma_frame_node_id")
    @classmethod
    def normalise_node_id(cls, v: str) -> str:
        if not v:
            return v
        v = v.split("&")[0].strip()
        return v.replace("-", ":") if ":" not in v else v

    @field_validator("figma_export_scale")
    @classmethod
    def validate_scale(cls, v: float) -> float:
        if not 0.5 <= v <= 4.0:
            raise ValueError("figma_export_scale must be between 0.5 and 4.0")
        return v

    @property
    def cache_key(self) -> tuple[str, str]:
        return (self.figma_file_key, self.figma_frame_node_id)


# ── Output ─────────────────────────────────────────────────────────────────────

class TemplateOutput(BaseModel):
    """
    One image output within a multi-output template.

    Each output has its own Figma frame and uploads to its own Airtable
    attachment field. Field mappings are inherited from the parent
    TemplateConfig but can be overridden here if needed.

    Variants are supported per-output — useful when e.g. the LinkedIn card
    also has Virtual / London / New York versions.
    """
    name:              str   # human label, e.g. "OpenGraph", "LinkedIn Social"
    attachment_field:  str   # Airtable field to upload the image to

    figma_file_key:      str
    figma_frame_node_id: str
    figma_export_scale:  float = 2.0

    # If set, this output only runs when its value appears in the template's
    # selection_field (a multi-select in Airtable). If blank, always runs.
    selection_value: str = ""

    # Optional variant selection for this output
    variant_field: str = ""
    variants: dict[str, TemplateVariant] = {}

    # Auto-variant: if this Airtable field has content (e.g. a second speaker
    # photo), switch to the named variant automatically.
    # e.g. auto_variant_on_field: "Pic speaker 2"
    #      auto_variant_name: "two_speakers"
    auto_variant_on_field: str = ""
    auto_variant_name:     str = "two_speakers"

    # Boolean-variant: if this Airtable checkbox/boolean field is truthy,
    # switch to the named variant. Checked before auto_variant_on_field.
    # e.g. boolean_variant_field: "Is Sponsored"
    #      boolean_variant_name: "sponsored"
    boolean_variant_field: str = ""
    boolean_variant_name:  str = "sponsored"

    # Field-value variant: maps an Airtable field value to a base variant name
    # via field_variant_map, then combines with auto_variant_name when
    # auto_variant_on_field also has content.
    # e.g. field_variant_field: "Location type"
    #      field_variant_map: { "LiveDay LDN": "liveday_ldn", "LiveDay NYC": "liveday_nyc" }
    # Variant keys: "liveday_ldn", "liveday_ldn_two_speakers", etc.
    field_variant_field: str = ""
    field_variant_map:   dict[str, str] = {}

    # Optional field mapping overrides (merged over parent mappings)
    field_mapping_overrides:       dict[str, str] = {}
    image_field_mapping_overrides: dict[str, str] = {}

    @field_validator("figma_frame_node_id")
    @classmethod
    def normalise_node_id(cls, v: str) -> str:
        if not v:
            return v
        v = v.split("&")[0].strip()
        return v.replace("-", ":") if ":" not in v else v

    @field_validator("figma_export_scale")
    @classmethod
    def validate_scale(cls, v: float) -> float:
        if not 0.5 <= v <= 4.0:
            raise ValueError("figma_export_scale must be between 0.5 and 4.0")
        return v

    def resolve_boolean_variant(self, fields: dict, template_name: str) -> TemplateVariant | None:
        """
        Check if boolean_variant_field is truthy in the record.
        If auto_variant_on_field also has content, tries the combined variant
        name first (e.g. "sponsored_two_speakers") before falling back to the
        boolean-only variant (e.g. "sponsored").
        Returns None when the field is not checked or no variant can be resolved.
        """
        if not self.boolean_variant_field or not self.variants:
            return None
        field_val = fields.get(self.boolean_variant_field)
        if not bool(field_val):
            return None

        # Check for combined variant when auto_variant_on_field is also active
        if self.auto_variant_on_field:
            auto_val = fields.get(self.auto_variant_on_field)
            has_auto = bool(auto_val and (
                (isinstance(auto_val, list) and len(auto_val) > 0) or
                (isinstance(auto_val, str) and auto_val.strip())
            ))
            if has_auto:
                combined = f"{self.boolean_variant_name}_{self.auto_variant_name}"
                if combined in self.variants:
                    log.info(
                        "[%s › %s] '%s' checked + '%s' has content — using combined variant '%s'",
                        template_name, self.name, self.boolean_variant_field,
                        self.auto_variant_on_field, combined,
                    )
                    return self.variants[combined]
                log.warning(
                    "[%s › %s] Combined variant '%s' not found — falling back to '%s'",
                    template_name, self.name, combined, self.boolean_variant_name,
                )

        # Boolean field checked, no combined variant needed (or not found)
        if self.boolean_variant_name in self.variants:
            log.info(
                "[%s › %s] Field '%s' is checked — using boolean variant '%s'",
                template_name, self.name, self.boolean_variant_field, self.boolean_variant_name,
            )
            return self.variants[self.boolean_variant_name]
        log.warning(
            "[%s › %s] Field '%s' is checked but variant '%s' not found — using default. Available: %s",
            template_name, self.name, self.boolean_variant_field, self.boolean_variant_name,
            list(self.variants.keys()),
        )
        return None

    def resolve_field_variant(self, fields: dict, template_name: str) -> TemplateVariant | None:
        """
        Read field_variant_field, map its value to a base variant name via
        field_variant_map, then try the combined name with auto_variant_name if
        auto_variant_on_field has content.  Returns None when the field is
        missing or unmapped.
        """
        if not self.field_variant_field or not self.field_variant_map or not self.variants:
            return None
        raw = fields.get(self.field_variant_field, "")
        if isinstance(raw, list):
            raw = raw[0] if raw else ""
        value = str(raw).strip()
        base_name = self.field_variant_map.get(value)
        if not base_name:
            if value:
                log.warning(
                    "[%s › %s] field_variant_field '%s' = '%s' not in field_variant_map %s — using default",
                    template_name, self.name, self.field_variant_field, value,
                    list(self.field_variant_map.keys()),
                )
            return None

        if self.auto_variant_on_field:
            auto_val = fields.get(self.auto_variant_on_field)
            has_auto = bool(auto_val and (
                (isinstance(auto_val, list) and len(auto_val) > 0) or
                (isinstance(auto_val, str) and auto_val.strip())
            ))
            if has_auto:
                combined = f"{base_name}_{self.auto_variant_name}"
                if combined in self.variants:
                    log.info(
                        "[%s › %s] '%s' = '%s' + '%s' has content — using combined variant '%s'",
                        template_name, self.name, self.field_variant_field, value,
                        self.auto_variant_on_field, combined,
                    )
                    return self.variants[combined]
                log.warning(
                    "[%s › %s] Combined variant '%s' not found — falling back to '%s'",
                    template_name, self.name, combined, base_name,
                )

        if base_name in self.variants:
            log.info(
                "[%s › %s] Field '%s' = '%s' — using variant '%s'",
                template_name, self.name, self.field_variant_field, value, base_name,
            )
            return self.variants[base_name]
        log.warning(
            "[%s › %s] Variant '%s' not found — using default. Available: %s",
            template_name, self.name, base_name, list(self.variants.keys()),
        )
        return None

    def resolve_variant(self, field_value: str, template_name: str) -> TemplateVariant:
        """Return the TemplateVariant to use, falling back to the output's default frame."""
        if self.variants and field_value in self.variants:
            log.info("[%s › %s] Using variant '%s'", template_name, self.name, field_value)
            return self.variants[field_value]
        if self.variants and field_value:
            log.warning(
                "[%s › %s] Variant '%s' not found — using default frame. Available: %s",
                template_name, self.name, field_value, list(self.variants.keys()),
            )
        return TemplateVariant(
            figma_file_key=self.figma_file_key,
            figma_frame_node_id=self.figma_frame_node_id,
            figma_export_scale=self.figma_export_scale,
        )

    def resolve_auto_variant(self, fields: dict, template_name: str) -> TemplateVariant:
        """
        Check if auto_variant_on_field has content in the record.
        If yes and the named variant exists, use it. Otherwise use the default frame.
        This runs before variant_field resolution so it takes priority.
        """
        if self.auto_variant_on_field and self.variants:
            field_val = fields.get(self.auto_variant_on_field)
            has_content = bool(field_val and (
                (isinstance(field_val, list) and len(field_val) > 0) or
                (isinstance(field_val, str) and field_val.strip())
            ))
            if has_content and self.auto_variant_name in self.variants:
                log.info(
                    "[%s › %s] Field '%s' has content — using auto variant '%s'",
                    template_name, self.name, self.auto_variant_on_field, self.auto_variant_name,
                )
                return self.variants[self.auto_variant_name]
            elif has_content:
                log.warning(
                    "[%s › %s] Field '%s' has content but variant '%s' not found — using default",
                    template_name, self.name, self.auto_variant_on_field, self.auto_variant_name,
                )
        return TemplateVariant(
            figma_file_key=self.figma_file_key,
            figma_frame_node_id=self.figma_frame_node_id,
            figma_export_scale=self.figma_export_scale,
        )

    @property
    def cache_key(self) -> tuple[str, str]:
        return (self.figma_file_key, self.figma_frame_node_id)


# ── Template ───────────────────────────────────────────────────────────────────

class TemplateConfig(BaseModel):
    # ── Identity ──────────────────────────────────────────────────────────────
    name: str

    # ── Airtable ──────────────────────────────────────────────────────────────
    airtable_base_id:          str
    airtable_table_name:       str
    airtable_trigger_field:    str = "Ready for Design"

    # If set, the trigger fires when the field equals this value (single-select).
    # If blank, the trigger fires when the field is truthy (checkbox = checked).
    airtable_trigger_value: str = ""

    # Intermediate statuses written during processing (optional).
    # Set these to match your Airtable single-select options.
    # e.g. airtable_trigger_pending_value: "Pending"
    #      airtable_trigger_working_value: "Working"
    airtable_trigger_pending_value: str = ""
    airtable_trigger_working_value: str = ""

    # After processing, set the trigger field to this value.
    # For checkboxes: leave blank (will be unchecked automatically).
    # For single-select: set to the status you want after generation,
    #   e.g. "Ready for publishing" or leave blank to not change it.
    airtable_trigger_reset_value: str = ""

    airtable_attachment_field: str = ""   # used only when no `outputs` defined

    # ── Figma default (used when no `outputs` defined) ────────────────────────
    figma_file_key:      str = ""
    figma_frame_node_id: str = ""
    figma_export_scale:  float = 2.0

    # ── Variant selection (single-output mode only) ───────────────────────────
    variant_field: str = ""
    variants: dict[str, TemplateVariant] = {}

    # ── Multi-output (one trigger → multiple images) ──────────────────────────
    # When defined, each output is rendered and uploaded independently.
    # airtable_attachment_field / figma_* above are ignored.
    outputs: list[TemplateOutput] = []

    # Optional Airtable multi-select field that controls which outputs to run.
    # e.g. "Generate" with options ["OpenGraph", "LinkedIn Social", "Speaker Social"]
    # If blank, all outputs run every time.
    selection_field: str = ""

    # ── Mappings (shared across all outputs) ──────────────────────────────────
    field_mappings:       dict[str, str] = {}
    image_field_mappings: dict[str, str] = {}

    # ── Rendering ─────────────────────────────────────────────────────────────
    # Set erase_placeholders to False when your Figma template text layers are
    # set to 0% opacity (recommended). When True, the renderer paints a
    # background-coloured rectangle behind each text layer to erase placeholder
    # text — this causes visible boxes if the background is non-uniform.
    erase_placeholders: bool = True

    # Set remove_background to True to automatically remove the background
    # from speaker/profile photos before compositing onto the template.
    # Uses the rembg U2Net AI model. Skips photos that are already transparent.
    remove_background: bool = False

    # ── Fonts ─────────────────────────────────────────────────────────────────
    # font_map: maps Figma font family names → font file paths (relative to fonts/)
    #   e.g. { "Space Grotesk Bold": "SpaceGrotesk-Bold.ttf" }
    # font_overrides: per-layer overrides (font_path, font_size, color)
    #   e.g. { "Title": { "font_path": "SpaceGrotesk-Bold.ttf", "font_size": 48 } }
    font_map:       dict[str, str]           = {}
    font_overrides: dict[str, dict[str, Any]] = {}

    @field_validator("figma_frame_node_id")
    @classmethod
    def normalise_node_id(cls, v: str) -> str:
        if not v:
            return v
        v = v.split("&")[0].strip()
        return v.replace("-", ":") if ":" not in v else v

    @field_validator("figma_export_scale")
    @classmethod
    def validate_scale(cls, v: float) -> float:
        if not 0.5 <= v <= 4.0:
            raise ValueError("figma_export_scale must be between 0.5 and 4.0")
        return v

    def resolved_outputs(self) -> list[TemplateOutput]:
        """
        Return the list of outputs to process.

        If `outputs` is explicitly defined, return those.
        Otherwise wrap the legacy single-frame config into a TemplateOutput
        so the pipeline can always iterate a list.
        """
        if self.outputs:
            return self.outputs

        if not self.figma_file_key or not self.figma_frame_node_id:
            raise ValueError(
                f"[{self.name}] No outputs defined and no default "
                "figma_file_key / figma_frame_node_id set."
            )
        return [
            TemplateOutput(
                name=self.name,
                attachment_field=self.airtable_attachment_field,
                figma_file_key=self.figma_file_key,
                figma_frame_node_id=self.figma_frame_node_id,
                figma_export_scale=self.figma_export_scale,
                variant_field=self.variant_field,
                variants=self.variants,
            )
        ]

    def selected_outputs(self, fields: dict) -> list[TemplateOutput]:
        """
        Return the outputs that should run for this record.

        If selection_field is set, reads the multi-select value from the record
        and filters to outputs whose selection_value appears in it.
        If selection_field is not set, all outputs run.
        """
        all_outputs = self.resolved_outputs()

        if not self.selection_field:
            return all_outputs

        # Airtable multi-select returns a comma-separated string or a list
        raw = fields.get(self.selection_field, "")
        if isinstance(raw, list):
            selected = {str(v).strip() for v in raw}
        else:
            selected = {v.strip() for v in str(raw).split(",")} if raw else set()

        if not selected:
            log.warning(
                "[%s] selection_field '%s' is empty — no outputs will run. "
                "Select at least one option in Airtable.",
                self.name, self.selection_field,
            )
            return []

        filtered = [
            o for o in all_outputs
            if not o.selection_value or o.selection_value in selected
        ]
        skipped = [o.name for o in all_outputs if o not in filtered]
        if skipped:
            log.info("[%s] Skipping outputs not selected: %s", self.name, skipped)

        return filtered

    def resolve_field_mappings(self, output: TemplateOutput) -> tuple[dict[str, str], dict[str, str]]:
        """
        Merge parent field_mappings with any output-level overrides.
        Returns (field_mappings, image_field_mappings).
        """
        fm  = {**self.field_mappings,       **output.field_mapping_overrides}
        ifm = {**self.image_field_mappings,  **output.image_field_mapping_overrides}
        return fm, ifm

    @property
    def cache_key(self) -> tuple[str, str]:
        return (self.figma_file_key, self.figma_frame_node_id)

    def __str__(self) -> str:
        if self.outputs:
            names = [o.name for o in self.outputs]
            return (
                f"{self.name} "
                f"[{self.airtable_table_name} → {len(names)} outputs: {', '.join(names)}]"
            )
        if self.variants:
            return (
                f"{self.name} "
                f"[{self.airtable_table_name} → {len(self.variants)} variants: "
                f"{', '.join(self.variants.keys())}]"
            )
        return (
            f"{self.name} "
            f"[{self.airtable_table_name} → {self.figma_file_key}/{self.figma_frame_node_id}]"
        )


# ── PDF Report ─────────────────────────────────────────────────────────────────

class PdfPage(BaseModel):
    """One page in a PDF report — a Figma frame with an optional data overlay."""
    figma_frame_node_id: str
    field_mappings: dict[str, str] = {}
    # If any of these Airtable fields are non-empty/non-zero, include this page.
    # Leave empty to always include.
    field_presence_fields: list[str] = []

    @field_validator("figma_frame_node_id")
    @classmethod
    def normalise_node_id(cls, v: str) -> str:
        if not v:
            return v
        v = v.split("&")[0].strip()
        return v.replace("-", ":") if ":" not in v else v


class ComboPage(BaseModel):
    """A sponsor-specific page selected by field-group combination logic."""
    figma_frame_node_id: str
    require: list[str] = []   # group names; ALL must be active to include this page
    exclude: list[str] = []   # group names; ALL must be inactive to include this page
    field_mappings: dict[str, str] = {}

    @field_validator("figma_frame_node_id")
    @classmethod
    def normalise_node_id(cls, v: str) -> str:
        if not v:
            return v
        v = v.split("&")[0].strip()
        return v.replace("-", ":") if ":" not in v else v


class PdfReportConfig(BaseModel):
    name: str
    airtable_base_id: str
    airtable_table_name: str
    airtable_trigger_field: str
    airtable_trigger_value: str = ""
    airtable_trigger_reset_value: str = ""
    airtable_trigger_pending_value: str = ""
    airtable_trigger_working_value: str = ""
    attachment_field: str
    figma_file_key: str
    static_page_ids: list[str]
    closing_page_id: str = ""
    # Named groups of Airtable fields; a group is "active" if any field in it is non-empty.
    field_groups: dict[str, list[str]] = {}
    # Pages selected by which groups are active/inactive.
    combo_pages: list[ComboPage] = []
    font_map: dict[str, str] = {}
    logo_attachment_field: str = ""
    logo_layer_name: str = "Logo"


def load_pdf_reports(path: Path | str | None = None) -> list[PdfReportConfig]:
    """Load PDF report configs from the pdf_reports section of templates.yaml."""
    resolved = Path(path) if path else TEMPLATES_FILE
    if not resolved.exists():
        return []
    with open(resolved) as f:
        data = yaml.safe_load(f)
    raw = data.get("pdf_reports", [])
    if not raw:
        return []
    reports = [PdfReportConfig(**r) for r in raw]
    log.info("Loaded %d PDF report(s): %s", len(reports), [r.name for r in reports])
    return reports


def load_templates(path: Path | str | None = None) -> list[TemplateConfig]:
    """
    Load all templates from templates.yaml.
    Raises clearly if the file is missing or malformed.
    """
    resolved = Path(path) if path else TEMPLATES_FILE

    if not resolved.exists():
        raise FileNotFoundError(
            f"templates.yaml not found at {resolved}.\n"
            "Copy templates.yaml.example to templates.yaml and fill in your templates."
        )

    with open(resolved) as f:
        data = yaml.safe_load(f)

    raw_templates = data.get("templates", [])
    if not raw_templates:
        raise ValueError("templates.yaml has no templates defined.")

    templates = [TemplateConfig(**t) for t in raw_templates]
    log.info("Loaded %d template(s): %s", len(templates), [t.name for t in templates])
    return templates
