"""
poller.py
─────────
Entry point. Loads all templates from templates.yaml, polls each Airtable
table every N seconds, and processes records where the trigger field is set.

Run:  python -m airtable_to_figma.poller
  or: airtable-to-figma  (if installed via pyproject.toml scripts)
"""

from __future__ import annotations

import argparse
import logging
import time

import colorlog
import requests

from airtable_to_figma.pipeline import run_pipeline
from airtable_to_figma.settings import Settings, get_settings
from airtable_to_figma.template import TemplateConfig, load_templates

log = logging.getLogger(__name__)


def _setup_logging() -> None:
    handler = colorlog.StreamHandler()
    handler.setFormatter(
        colorlog.ColoredFormatter(
            "%(log_color)s%(levelname)-8s%(reset)s %(blue)s%(name)s%(reset)s  %(message)s"
        )
    )
    logging.basicConfig(level=logging.INFO, handlers=[handler])
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def get_pending_records(
    settings: Settings,
    template: TemplateConfig,
) -> list[dict]:
    """Return records where the trigger field is set for a given template.

    Supports both:
      - Checkbox fields:     {field}=1
      - Single-select fields: {field}="Push to publishing"
    """
    if template.airtable_trigger_value:
        formula = f'{{{template.airtable_trigger_field}}}="{template.airtable_trigger_value}"'
    else:
        formula = f"{{{template.airtable_trigger_field}}}=1"
    base_url = (
        f"https://api.airtable.com/v0/{template.airtable_base_id}"
        f"/{requests.utils.quote(template.airtable_table_name)}"
    )
    resp = requests.get(
        base_url,
        headers={"Authorization": f"Bearer {settings.airtable_api_key}"},
        params={"filterByFormula": formula, "maxRecords": 50},
    )
    resp.raise_for_status()
    return resp.json().get("records", [])


def _poll_once(settings: Settings, templates: list[TemplateConfig]) -> None:
    """Check all templates once and process any pending records."""
    for template in templates:
        try:
            records = get_pending_records(settings, template)
            if records:
                log.info("[%s] Found %d record(s) to process", template.name, len(records))
                for record in records:
                    rid = record.get("id")
                    if rid:
                        try:
                            run_pipeline(settings, template, rid)
                        except Exception as exc:
                            log.error("[%s] Failed to process %s: %s", template.name, rid, exc)
            else:
                log.info("[%s] No pending records.", template.name)
        except Exception as exc:
            log.error("[%s] Poll error: %s", template.name, exc)


def run_poller() -> None:
    parser = argparse.ArgumentParser(description="Airtable → Figma poller")
    parser.add_argument(
        "--run-once",
        action="store_true",
        help="Check all templates once and exit (used by GitHub Actions cron)",
    )
    args = parser.parse_args()

    _setup_logging()

    try:
        settings = get_settings()
    except Exception as e:
        log.error("Configuration error: %s", e)
        log.error("Copy .env.example to .env and fill in your API keys.")
        raise SystemExit(1)

    try:
        templates = load_templates()
    except Exception as e:
        log.error("Template config error: %s", e)
        log.error("Copy templates.yaml.example to templates.yaml and fill in your templates.")
        raise SystemExit(1)

    log.info("=" * 60)
    log.info("  Airtable → Figma Poller")
    log.info("=" * 60)
    log.info("Loaded %d template(s):", len(templates))
    for t in templates:
        log.info("  • %s", t)

    if args.run_once:
        log.info("Running once then exiting (--run-once mode).\n")
        _poll_once(settings, templates)
        return

    interval = settings.poll_interval
    log.info("Polling every %ds. Press Ctrl+C to stop.\n", interval)

    while True:
        _poll_once(settings, templates)
        time.sleep(interval)


if __name__ == "__main__":
    run_poller()
