"""
settings.py
───────────
Global API credentials loaded from .env (or environment variables on Fly.io).

Templates (which tables, which Figma frames, which field mappings) live in
templates.yaml — not here. Only secrets belong in this file.
"""

from __future__ import annotations

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # ── Airtable ──────────────────────────────────────────────────────────────
    airtable_api_key: str = Field(..., description="Airtable personal access token")
    airtable_imgbb_api_key: str = Field("", description="imgbb.com API key for image hosting")

    # ── Figma ─────────────────────────────────────────────────────────────────
    figma_api_key: str = Field(..., description="Figma personal access token")

    # ── Poller ────────────────────────────────────────────────────────────────
    poll_interval: int = Field(30, description="Seconds between Airtable polls")

    @field_validator("airtable_api_key")
    @classmethod
    def validate_airtable_key(cls, v: str) -> str:
        if not v.startswith("pat"):
            raise ValueError(
                "AIRTABLE_API_KEY must start with 'pat' — "
                "create a personal access token at airtable.com/create/tokens"
            )
        return v

    @field_validator("figma_api_key")
    @classmethod
    def validate_figma_key(cls, v: str) -> str:
        if not v.startswith("figd_"):
            raise ValueError(
                "FIGMA_API_KEY must start with 'figd_' — "
                "create one at figma.com → Account Settings → Personal Access Tokens"
            )
        return v


def get_settings() -> Settings:
    """Load and validate credentials. Fails fast with a clear error if anything is missing."""
    return Settings()
