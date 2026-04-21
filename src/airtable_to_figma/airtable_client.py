"""
airtable_client.py
──────────────────
Airtable REST API wrapper (v0 / personal-access-token auth).

Used for:
  • Fetching a single record by ID
  • Listing records that don't yet have a generated design
  • Uploading a JPG as an attachment to an Attachment field
"""

from __future__ import annotations

import io
import logging
import mimetypes
from pathlib import Path
from typing import Any

import requests

log = logging.getLogger(__name__)

AIRTABLE_API_BASE = "https://api.airtable.com/v0"
AIRTABLE_CONTENT_API = "https://content.airtable.com/v0"


class AirtableClient:
    def __init__(self, api_key: str, base_id: str, table_name: str, imgbb_key: str = ""):
        self.base_id = base_id
        self.table_name = table_name
        self._imgbb_key = imgbb_key
        self._table_url = f"{AIRTABLE_API_BASE}/{base_id}/{requests.utils.quote(table_name)}"
        self._session = requests.Session()
        self._session.headers.update(
            {
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            }
        )

    # ── Record helpers ──────────────────────────────────────────────────────────

    def get_record(self, record_id: str) -> dict[str, Any]:
        """Return the full record dict (fields + id + createdTime)."""
        resp = self._session.get(f"{self._table_url}/{record_id}")
        resp.raise_for_status()
        return resp.json()

    def list_records_without_design(
        self,
        attachment_field: str,
        max_records: int = 100,
    ) -> list[dict[str, Any]]:
        """
        Return records where the attachment field is empty (i.e., no design yet).
        Useful for a backfill / manual run mode.
        """
        formula = f"AND({{%s}}=BLANK())" % attachment_field
        params = {
            "filterByFormula": formula,
            "maxRecords": max_records,
        }
        resp = self._session.get(self._table_url, params=params)
        resp.raise_for_status()
        return resp.json().get("records", [])

    def get_fields(self, record_id: str) -> dict[str, Any]:
        """Convenience: return just the 'fields' dict for a record."""
        return self.get_record(record_id).get("fields", {})

    # ── Attachment upload ───────────────────────────────────────────────────────

    def upload_attachment(
        self,
        record_id: str,
        attachment_field: str,
        image_bytes: bytes,
        filename: str = "design.jpg",
    ) -> dict[str, Any]:
        """
        Upload image_bytes as an attachment.

        Strategy (tries in order):
          1. Airtable Content API  (direct multipart upload)
          2. file.io               (free temp host → give Airtable the URL)
          3. 0x0.st                (backup temp host)
        """
        content_type = mimetypes.guess_type(filename)[0] or "image/jpeg"

        upload_url = (
            f"{AIRTABLE_CONTENT_API}/{self.base_id}/{record_id}"
            f"/{requests.utils.quote(attachment_field)}/uploadAttachment"
        )
        upload_headers = {"Authorization": self._session.headers["Authorization"]}

        # ── 1. Airtable Content API with field name "file" ─────────────────────
        try:
            resp = requests.post(
                upload_url, headers=upload_headers,
                files={"file": (filename, image_bytes, content_type)},
                timeout=120,
            )
            log.info("Content API (file) → %s: %s", resp.status_code, resp.text[:200])
            if resp.status_code == 200:
                return self.get_record(record_id)
        except Exception as e:
            log.warning("Content API (file) error: %s", e)

        # ── 2. Airtable Content API with field name "attachment" ───────────────
        try:
            resp = requests.post(
                upload_url, headers=upload_headers,
                files={"attachment": (filename, image_bytes, content_type)},
                timeout=120,
            )
            log.info("Content API (attachment) → %s: %s", resp.status_code, resp.text[:200])
            if resp.status_code == 200:
                return self.get_record(record_id)
        except Exception as e:
            log.warning("Content API (attachment) error: %s", e)

        # ── 3. imgbb (free image host, needs API key in config) ────────────────
        imgbb_key = self._imgbb_key
        if imgbb_key:
            public_url = self._upload_to_imgbb(image_bytes, imgbb_key)
            if public_url:
                return self._patch_with_url(record_id, attachment_field, public_url, filename)

        # ── 4. transfer.sh ─────────────────────────────────────────────────────
        public_url = self._upload_to_transfer_sh(image_bytes, filename)
        if public_url:
            return self._patch_with_url(record_id, attachment_field, public_url, filename)

        raise RuntimeError(
            "All upload methods failed.\n"
            "Quick fix: set AIRTABLE_IMGBB_API_KEY in your .env file.\n"
            "Get a free key at: https://imgbb.com/api  (takes 30 seconds)"
        )

    def _upload_to_imgbb(self, image_bytes: bytes, api_key: str) -> str | None:
        """Upload to imgbb.com (free, permanent, direct URL). Needs free API key."""
        import base64
        try:
            resp = requests.post(
                "https://api.imgbb.com/1/upload",
                params={"key": api_key},
                data={"image": base64.b64encode(image_bytes).decode()},
                timeout=60,
            )
            resp.raise_for_status()
            url = resp.json()["data"]["url"]
            log.info("Uploaded to imgbb: %s", url)
            return url
        except Exception as e:
            log.warning("imgbb upload failed: %s", e)
        return None

    def _upload_to_transfer_sh(self, image_bytes: bytes, filename: str) -> str | None:
        """Upload to transfer.sh (free, 14-day expiry)."""
        try:
            resp = requests.put(
                f"https://transfer.sh/{filename}",
                data=image_bytes,
                headers={"Content-Type": "image/jpeg", "Max-Days": "3"},
                timeout=60,
            )
            resp.raise_for_status()
            url = resp.text.strip()
            if url.startswith("http"):
                log.info("Uploaded to transfer.sh: %s", url)
                return url
        except Exception as e:
            log.warning("transfer.sh upload failed: %s", e)
        return None

    def _upload_to_catbox(self, image_bytes: bytes, filename: str) -> str | None:
        """Upload to catbox.moe (free, permanent)."""
        try:
            resp = requests.post(
                "https://catbox.moe/user/api.php",
                data={"reqtype": "fileupload"},
                files={"fileToUpload": (filename, image_bytes, "image/jpeg")},
                timeout=60,
            )
            resp.raise_for_status()
            url = resp.text.strip()
            if url.startswith("http"):
                log.info("Uploaded to catbox.moe: %s", url)
                return url
        except Exception as e:
            log.warning("catbox.moe upload failed: %s", e)
        return None

    def _upload_to_telegraph(self, image_bytes: bytes, filename: str) -> str | None:
        """Upload to telegra.ph (Telegram's free image host, direct URL, no API key)."""
        try:
            resp = requests.post(
                "https://telegra.ph/upload",
                files={"file": ("image.jpg", image_bytes, "image/jpeg")},
                timeout=60,
            )
            resp.raise_for_status()
            result = resp.json()
            # Returns: [{"src": "/file/XXXXX.jpg"}]
            if isinstance(result, list) and result:
                path = result[0].get("src", "")
                if path:
                    url = f"https://telegra.ph{path}"
                    log.info("Uploaded to telegra.ph: %s", url)
                    return url
        except Exception as e:
            log.warning("telegra.ph upload failed: %s", e)
        return None

    def _patch_with_url(
        self,
        record_id: str,
        attachment_field: str,
        url: str,
        filename: str,
    ) -> dict[str, Any]:
        """PATCH the Airtable record with a publicly accessible image URL."""
        resp = self._session.patch(
            f"{self._table_url}/{record_id}",
            json={"fields": {attachment_field: [{"url": url, "filename": filename}]}},
        )
        resp.raise_for_status()
        log.info("Patched record %s with URL attachment", record_id)
        return resp.json()

    # ── Mark record as processed ────────────────────────────────────────────────

    def mark_processed(self, record_id: str, status_field: str, value: str = "Done") -> None:
        """
        Optionally write a status value to a field so you can filter processed
        records easily.  Only called if status_field is set in config.
        """
        resp = self._session.patch(
            f"{self._table_url}/{record_id}",
            json={"fields": {status_field: value}},
        )
        resp.raise_for_status()
        log.debug("Marked record %s  %s=%s", record_id, status_field, value)
