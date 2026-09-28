"""Versioned native website project commands; no filesystem paths from clients."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.services.website_dossier_contracts import CaptureLimits


class WebsiteModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class WebsiteCreate(WebsiteModel):
    request_id: str = Field(pattern=r"^[A-Za-z0-9_-]{8,100}$")
    source_url: str = Field(min_length=8, max_length=2048)
    objective: str = Field(min_length=1, max_length=4000)
    limits: CaptureLimits = Field(default_factory=CaptureLimits)


class WebsiteCommand(WebsiteModel):
    request_id: str = Field(pattern=r"^[A-Za-z0-9_-]{8,100}$")
    expected_version: int = Field(ge=1)
    action: Literal["capture", "branding", "build"]
    palette_id: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9-]{0,39}$")
    direction_id: Literal["editorial", "studio", "catalog"] | None = None


class WebsiteReview(WebsiteModel):
    expected_version: int = Field(ge=1)
    build_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class WebsitePublish(WebsiteReview):
    approval_token: str = Field(pattern=r"^[A-Za-z0-9_-]{32,100}$")
    confirm_publication: Literal[True]
