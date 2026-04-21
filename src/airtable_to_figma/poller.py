"""
poller.py
─────────
Entry point. Polls Airtable every N seconds and processes records
where the trigger field is set.

Run:  python -m airtable_to_figma.poller
  or: airtable-to-figma  (if installed via pyproject.toml scripts)
"""

from __future__ import annotations

import logging
import time

import colorlog
import requests

from airtable_to_figma.pipeline import run_pipeline
from airtable_to_figma.settings import Settings, get_settings

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


def get_pending_records(settings: Settings) -> list[dict]:
    """Return records where the trigger field is checked."""
    at = settings.airtable
    formula = f"{{{at.trigger_field}}}=1"
    base_url = (
        f"https://api.airtable.com/v0/{at.base_id}"
        f"/{requests.utils.quote(at.table_name)}"
    )
    resp = requests.get(
        base_url,
        headers={"Authorization": f"Bearer {at.api_key}"},
        params={"filterByFormula": formula, "maxRecords": 50},
    )
    resp.raise_for_status()
    return resp.json().get("records", [])


def run_poller() -> None:
    _setup_logging()

    try:
        settings = get_settings()
    except Exception as e:
        log.error("Configuration error: %s", e)
        log.error("Copy .env.example to .env and fill in your values.")
        raise SystemExit(1)

    interval = settings.server.poll_interval

    log.info("=" * 55)
    log.info("  Airtable → Figma Poller  (every %ds)", interval)
    log.info("=" * 55)
    log.info("Table:    %s", settings.airtable.table_name)
    log.info("Trigger:  %s", settings.airtable.trigger_field)
    log.info("Output:   %s", settings.airtable.attachment_field)
    log.info("Press Ctrl+C to stop.\n")

    while True:
        try:
            records = get_pending_records(settings)
            if records:
                log.info("Found %d record(s) to process", len(records))
                for record in records:
                    rid = record.get("id")
                    if rid:
                        try:
                            run_pipeline(settings, rid)
                        except Exception as exc:
                            log.error("Failed to process %s: %s", rid, exc)
            else:
                log.info("No pending records. Next check in %ds…", interval)
        except Exception as exc:
            log.error("Poll error: %s  (retrying in %ds)", exc, interval)

        time.sleep(interval)


if __name__ == "__main__":
    run_poller()
