"""Source records for a bounded website capture; no inferred business facts."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, StringConstraints, model_validator

UrlText = Annotated[str, StringConstraints(min_length=1, max_length=2_048)]
Digest = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]


class DossierModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class CaptureLimits(DossierModel):
    max_pages: int = Field(default=12, ge=1, le=30)
    max_discovered_urls: int = Field(default=200, ge=1, le=1_000)
    max_page_bytes: int = Field(default=262_144, ge=1_024, le=1_048_576)
    max_total_bytes: int = Field(default=2_097_152, ge=1_024, le=8_388_608)
    max_seconds: int = Field(default=60, ge=1, le=120)
    max_redirects: int = Field(default=3, ge=0, le=5)
    max_sitemaps: int = Field(default=3, ge=0, le=5)
    max_text_characters: int = Field(default=32_000, ge=100, le=64_000)


class SourceLink(DossierModel):
    url: UrlText
    text: str = Field(max_length=2_000)
    kind: Literal["navigation", "image", "document", "contact"]
    locator: str = Field(min_length=1, max_length=100)


class ObservedForm(DossierModel):
    action: str = Field(max_length=2_048)
    method: str = Field(max_length=20)
    field_names: list[Annotated[str, StringConstraints(max_length=200)]] = Field(max_length=100)
    submitted: Literal[False] = False


class CapturedPage(DossierModel):
    requested_url: UrlText
    final_url: UrlText
    fetched_at: str
    http_status: int = Field(ge=100, le=599)
    html_sha256: Digest
    response_bytes: int = Field(ge=0, le=1_048_576)
    title: str = Field(max_length=1_000)
    language: str = Field(max_length=100)
    text: str = Field(max_length=64_000)
    headings: list[Annotated[str, StringConstraints(max_length=2_000)]] = Field(max_length=200)
    metadata: dict[str, str]
    structured_data: list[JsonValue] = Field(max_length=20)
    links: list[SourceLink] = Field(max_length=300)
    forms: list[ObservedForm] = Field(max_length=30)
    truncated_fields: list[str]
    extraction: Literal["source_html"] = "source_html"
    rendering: Literal["not_available"] = "not_available"


class SourceInventoryItem(DossierModel):
    id: str
    source_url: UrlText
    page_sha256: Digest
    source_locator: str = Field(min_length=1, max_length=100)
    kind: Literal["text", "heading", "navigation", "image", "document", "contact"]
    text: str = Field(max_length=8_000)
    original_url: UrlText | None = None
    authority: Literal["source_site_statement"] = "source_site_statement"
    disposition: Literal[
        "unassigned", "retained", "grouped", "rewritten", "needs_confirmation", "excluded"
    ] = "unassigned"
    destination_url: UrlText | None = None
    reason: str | None = Field(default=None, max_length=1_000)

    @model_validator(mode="after")
    def validate_disposition(self) -> "SourceInventoryItem":
        if self.disposition == "excluded" and not (self.reason and self.reason.strip()):
            raise ValueError("an excluded source item requires a reason")
        if self.disposition == "unassigned" and self.destination_url is not None:
            raise ValueError("unassigned source items cannot invent a destination")
        return self


class CoverageEntry(DossierModel):
    url: UrlText
    discovered_from: UrlText | None
    state: Literal["extracted", "failed", "blocked", "excluded", "not_visited"]
    reason: str | None = None
    final_url: UrlText | None = None
    http_status: int | None = Field(default=None, ge=100, le=599)


class DiscoveryIssue(DossierModel):
    url: UrlText
    reason: str = Field(min_length=1, max_length=100)


class DossierCoverage(DossierModel):
    status: Literal["bounded_scope_exhausted", "partial"]
    entries: list[CoverageEntry] = Field(max_length=1_000)
    discovered: int = Field(ge=1, le=1_000)
    visited: int = Field(ge=0, le=30)
    extracted: int = Field(ge=0, le=30)
    blocked: int = Field(ge=0, le=1_000)
    failed: int = Field(ge=0, le=1_000)
    excluded: int = Field(ge=0, le=1_000)
    not_visited: int = Field(ge=0, le=1_000)
    discovery_limited: bool
    discovery_issues: list[DiscoveryIssue] = Field(max_length=10)
    response_bytes: int = Field(ge=0)
    elapsed_ms: int = Field(ge=0)
    limits: CaptureLimits


class WebsiteDossier(DossierModel):
    schema_version: Literal["1.0"] = "1.0"
    source_url: UrlText
    captured_at: str
    pages: list[CapturedPage] = Field(max_length=30)
    inventory: list[SourceInventoryItem] = Field(max_length=15_000)
    coverage: DossierCoverage
    source_authority: Literal["unverified_site_content"] = "unverified_site_content"
    rendering: Literal["not_available"] = "not_available"
    asset_downloads: Literal["not_performed"] = "not_performed"
    migration_audit: Literal["not_run"] = "not_run"
