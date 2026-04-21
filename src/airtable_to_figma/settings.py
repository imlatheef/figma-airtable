"""
settings.py
───────────
All configuration via Pydantic BaseSettings.

Local dev  → create a .env file (see .env.example)
Railway    → set environment variables in the dashboard

Pydantic validates every field on startup and raises a clear error
if anything is missing or the wrong type — no silent failures.
"""

from __future__ import annotations

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class AirtableSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AIRTABLE_", env_file=".env", extra="ignore")

    api_key:          str = Field(..., description="Airtable personal access token")
    base_id:          str = Field(..., description="Airtable base ID (appXXXXXXXXXXXXXX)")
    table_name:       str = Field(..., description="Exact table name")
    attachment_field: str = Field(..., description="Attachment field to upload JPG to")
    trigger_field:    str = Field("Ready for Design", description="Checkbox or single-select trigger field")
    imgbb_api_key:    str = Field("", description="imgbb.com API key for image hosting")

    @field_validator("api_key")
    @classmethod
    def validate_api_key(cls, v: str) -> str:
        if not v.startswith("pat"):
            raise ValueError("AIRTABLE_API_KEY must start with 'pat' — check your personal access token")
        return v

    @field_validator("base_id")
    @classmethod
    def validate_base_id(cls, v: str) -> str:
        if not v.startswith("app"):
            raise ValueError("AIRTABLE_BASE_ID must start with 'app'")
        return v


class FigmaSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="FIGMA_", env_file=".env", extra="ignore")

    api_key:       str   = Field(..., description="Figma personal access token")
    file_key:      str   = Field(..., description="Figma file key (from URL)")
    frame_node_id: str   = Field(..., description="Template frame node ID e.g. '1:2'")
    export_scale:  float = Field(2.0, description="Export resolution multiplier")

    @field_validator("api_key")
    @classmethod
    def validate_api_key(cls, v: str) -> str:
        if not v.startswith("figd_"):
            raise ValueError("FIGMA_API_KEY must start with 'figd_'")
        return v

    @field_validator("frame_node_id")
    @classmethod
    def validate_node_id(cls, v: str) -> str:
        # Accept both "1:2" and "1-2" formats, normalise to "1:2"
        v = v.split("&")[0]  # strip any URL junk like &t=...
        return v.replace("-", ":") if ":" not in v else v

    @field_validator("export_scale")
    @classmethod
    def validate_scale(cls, v: float) -> float:
        if not 0.5 <= v <= 4.0:
            raise ValueError("FIGMA_EXPORT_SCALE must be between 0.5 and 4.0")
        return v


class ServerSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SERVER_", env_file=".env", extra="ignore")

    port:           int = Field(5001, description="Webhook server port")
    webhook_secret: str = Field("", description="Optional secret to verify Airtable requests")
    poll_interval:  int = Field(30, description="Polling interval in seconds")


class MappingSettings(BaseSettings):
    """
    Field mappings are stored as comma-separated KEY=VALUE pairs in env vars.
    e.g. FIELD_MAPPINGS="Session Title=Title,Speaker 1 Name=Speaker name"
    """
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    field_mappings:       str = Field(
        "Session Title=Title,Speaker 1 Name=Speaker name,Speaker 1 tagline=Job",
        description="Airtable field=Figma layer pairs, comma-separated",
    )
    image_field_mappings: str = Field(
        "Speaker 1 picture=Photo",
        description="Airtable attachment field=Figma layer pairs, comma-separated",
    )

    @property
    def field_mappings_dict(self) -> dict[str, str]:
        return self._parse(self.field_mappings)

    @property
    def image_field_mappings_dict(self) -> dict[str, str]:
        return self._parse(self.image_field_mappings)

    @staticmethod
    def _parse(raw: str) -> dict[str, str]:
        result = {}
        for pair in raw.split(","):
            if "=" in pair:
                k, v = pair.split("=", 1)
                result[k.strip()] = v.strip()
        return result


class Settings(BaseSettings):
    """Root settings — composes all sub-settings."""
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    airtable: AirtableSettings  = Field(default_factory=AirtableSettings)
    figma:    FigmaSettings      = Field(default_factory=FigmaSettings)
    server:   ServerSettings     = Field(default_factory=ServerSettings)
    mappings: MappingSettings    = Field(default_factory=MappingSettings)

    @model_validator(mode="after")
    def validate_all(self) -> "Settings":
        # Cross-field validation can go here
        return self


def get_settings() -> Settings:
    """Load and validate all settings. Call once at startup."""
    return Settings()
