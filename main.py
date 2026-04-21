"""
main.py
───────
Entry point. Run this file to start the automation.

Usage
─────
  python main.py                     # start the webhook server
  python main.py --backfill          # process all records without a design
  python main.py --record rec123     # process a single record by ID
  python main.py --config my.yaml    # use a custom config file
"""

from __future__ import annotations

import argparse
import logging
import sys

import colorlog
import yaml


def _setup_logging():
    handler = colorlog.StreamHandler()
    handler.setFormatter(
        colorlog.ColoredFormatter(
            "%(log_color)s%(levelname)-8s%(reset)s %(blue)s%(name)s%(reset)s  %(message)s",
            log_colors={
                "DEBUG": "cyan",
                "INFO": "green",
                "WARNING": "yellow",
                "ERROR": "red",
                "CRITICAL": "red,bg_white",
            },
        )
    )
    logging.basicConfig(level=logging.INFO, handlers=[handler])
    # Quieten noisy libraries
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("werkzeug").setLevel(logging.WARNING)


def main():
    _setup_logging()
    log = logging.getLogger("main")

    parser = argparse.ArgumentParser(
        description="Airtable → Figma → Airtable design automation"
    )
    parser.add_argument(
        "--config", default="config.yaml", help="Path to config.yaml (default: config.yaml)"
    )
    parser.add_argument(
        "--backfill",
        action="store_true",
        help="Process all records that don't yet have a generated design, then exit.",
    )
    parser.add_argument(
        "--record",
        metavar="RECORD_ID",
        help="Process a single record by ID, then exit.",
    )
    args = parser.parse_args()

    # Load config
    try:
        with open(args.config) as f:
            config = yaml.safe_load(f)
    except FileNotFoundError:
        log.error("Config file not found: %s", args.config)
        log.error("Copy config.yaml and fill in your API keys, then re-run.")
        sys.exit(1)

    # ── Single record mode ───────────────────────────────────────────────────────
    if args.record:
        from pipeline import run_pipeline
        log.info("Processing single record: %s", args.record)
        run_pipeline(config, args.record)
        log.info("Done.")
        return

    # ── Backfill mode ────────────────────────────────────────────────────────────
    if args.backfill:
        from airtable_client import AirtableClient
        from pipeline import run_pipeline

        cfg_at = config["airtable"]
        client = AirtableClient(
            api_key=cfg_at["api_key"],
            base_id=cfg_at["base_id"],
            table_name=cfg_at["table_name"],
        )
        records = client.list_records_without_design(cfg_at["attachment_field"])
        log.info("Backfill: %d record(s) to process", len(records))
        for record in records:
            rid = record.get("id")
            if rid:
                try:
                    run_pipeline(config, rid)
                except Exception as exc:
                    log.error("Failed to process %s: %s", rid, exc)
        log.info("Backfill complete.")
        return

    # ── Webhook server mode (default) ────────────────────────────────────────────
    log.info("=" * 60)
    log.info("  Airtable → Figma → Airtable  |  Webhook Server")
    log.info("=" * 60)
    log.info("")
    log.info("Expose this server to the internet with ngrok:")
    log.info("  ngrok http %s", config.get("server", {}).get("port", 5000))
    log.info("")
    log.info("Then paste the ngrok URL into Airtable Automation:")
    log.info("  Trigger  : When a record is created")
    log.info("  Action   : Send a webhook  →  POST  <ngrok-url>/webhook")
    log.info('  Body     : { "record_id": "{{record_id}}" }')
    log.info("")

    from webhook_server import start_server
    start_server(args.config)


if __name__ == "__main__":
    main()
