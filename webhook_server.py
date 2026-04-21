"""
webhook_server.py
─────────────────
Flask server that receives POST requests from Airtable Automations.

Airtable Automation setup (do this in Airtable):
  Trigger  : "When a record is created" in your table
  Action   : "Send a webhook"  →  POST  http://<your-ngrok-url>/webhook
  Body     : { "record_id": "{{record_id}}" }
  (optionally add Header  X-Webhook-Secret: <value from config.yaml>)

The server hands off processing to the pipeline function so the webhook
responds immediately (202 Accepted) while the image is generated in the
background thread.
"""

from __future__ import annotations

import hashlib
import hmac
import io
import logging
import threading
from datetime import datetime
from typing import Any

import yaml
from flask import Flask, jsonify, request

from pipeline import run_pipeline

log = logging.getLogger(__name__)

app = Flask(__name__)
_config: dict[str, Any] = {}


# ── Config loader ────────────────────────────────────────────────────────────────

def load_config(path: str = "config.yaml") -> dict[str, Any]:
    with open(path, "r") as f:
        return yaml.safe_load(f)


# ── Webhook secret verification ──────────────────────────────────────────────────

def _verify_secret(req) -> bool:
    secret = _config.get("server", {}).get("webhook_secret", "")
    if not secret:
        return True  # disabled
    provided = req.headers.get("X-Webhook-Secret", "")
    return hmac.compare_digest(secret, provided)


# ── Routes ───────────────────────────────────────────────────────────────────────

@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "timestamp": datetime.utcnow().isoformat()})


@app.route("/webhook", methods=["POST"])
def webhook():
    # Security check
    if not _verify_secret(request):
        log.warning("Webhook received with invalid secret")
        return jsonify({"error": "Unauthorized"}), 401

    body = request.get_json(silent=True)
    if not body:
        return jsonify({"error": "Expected JSON body"}), 400

    record_id = body.get("record_id")
    if not record_id:
        return jsonify({"error": "Missing 'record_id' in request body"}), 400

    log.info("Webhook received  record_id=%s", record_id)

    # Process in background so we can respond immediately
    thread = threading.Thread(
        target=_process_record_safe,
        args=(record_id,),
        daemon=True,
    )
    thread.start()

    return jsonify({"status": "accepted", "record_id": record_id}), 202


@app.route("/run-backfill", methods=["POST"])
def run_backfill():
    """
    Optional endpoint: process ALL records that don't yet have a generated design.
    Call this manually once to backfill existing records.
    """
    if not _verify_secret(request):
        return jsonify({"error": "Unauthorized"}), 401

    thread = threading.Thread(target=_backfill_safe, daemon=True)
    thread.start()
    return jsonify({"status": "backfill started"}), 202


# ── Processing helpers ───────────────────────────────────────────────────────────

def _process_record_safe(record_id: str):
    try:
        run_pipeline(_config, record_id)
    except Exception as exc:
        log.exception("Pipeline failed for record %s: %s", record_id, exc)


def _backfill_safe():
    try:
        from airtable_client import AirtableClient

        cfg_at = _config["airtable"]
        client = AirtableClient(
            api_key=cfg_at["api_key"],
            base_id=cfg_at["base_id"],
            table_name=cfg_at["table_name"],
        )
        records = client.list_records_without_design(cfg_at["attachment_field"])
        log.info("Backfill: found %d record(s) to process", len(records))
        for record in records:
            rid = record.get("id")
            if rid:
                log.info("Backfill processing %s", rid)
                run_pipeline(_config, rid)
    except Exception as exc:
        log.exception("Backfill failed: %s", exc)


# ── Entry point ───────────────────────────────────────────────────────────────────

def start_server(config_path: str = "config.yaml"):
    global _config
    _config = load_config(config_path)

    port = _config.get("server", {}).get("port", 5000)
    log.info("Starting webhook server on port %d", port)
    log.info("Webhook endpoint: POST http://localhost:%d/webhook", port)
    log.info("Health check:     GET  http://localhost:%d/health", port)

    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)


if __name__ == "__main__":
    import colorlog

    handler = colorlog.StreamHandler()
    handler.setFormatter(
        colorlog.ColoredFormatter("%(log_color)s%(levelname)-8s%(reset)s %(message)s")
    )
    logging.basicConfig(level=logging.INFO, handlers=[handler])
    start_server()
