"""Bounded textual drafts; producing text grants no execution authority."""

from __future__ import annotations

import ipaddress
import json
import re
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

WRITING_SKILL = "writing.draft"
MAX_WRITING_PAYLOAD_BYTES = 32_000
MAX_WRITING_TEXT_BYTES = 24_000
MAX_WRITING_SUMMARY_CHARACTERS = 1_200
MAX_WRITING_CONTEXT_CHARACTERS = 4_000
MAX_WRITING_CONVERSATION_MESSAGES = 12
MAX_RESEARCH_SOURCES = 5
MAX_RESEARCH_SOURCE_BYTES = 8_000


def checked_research_url(value: str) -> str:
    """Validate a citation without fetching it or rewriting its destination."""
    value.encode("utf-8")
    if (
        not value
        or len(value) > 1_000
        or any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in value)
    ):
        raise ValueError("research URL is outside its bounds")
    parsed = urlsplit(value)
    host = parsed.hostname
    if (
        parsed.scheme not in {"http", "https"}
        or not host
        or parsed.username is not None
        or parsed.password is not None
        or host.lower() == "localhost"
        or host.lower().endswith((".localhost", ".local", ".internal"))
    ):
        raise ValueError("research URL must be a credential-free public HTTP(S) URL")
    _ = parsed.port
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        if "." not in host or "\\" in host or "%" in host:
            raise ValueError("research URL hostname is invalid") from None
    else:
        if not address.is_global:
            raise ValueError("research URL must not identify a private address")
    return value


def _checked_text(value: str, *, max_bytes: int | None = None) -> str:
    if not value.strip() or "\0" in value:
        raise ValueError("writing text must be nonempty and contain no NUL")
    try:
        encoded = value.encode("utf-8")
    except UnicodeError as exc:
        raise ValueError("writing text must contain valid Unicode") from exc
    if max_bytes is not None and len(encoded) > max_bytes:
        raise ValueError("writing text exceeds its UTF-8 byte limit")
    return value


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class WritingConversationMessage(_StrictModel):
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=MAX_WRITING_CONTEXT_CHARACTERS)

    @field_validator("content")
    @classmethod
    def validate_content(cls, value: str) -> str:
        return _checked_text(value)


class WritingResearchSource(_StrictModel):
    content_trust: Literal["untrusted"]
    worker_job_id: str = Field(min_length=5, max_length=200)
    title: str = Field(min_length=1, max_length=240)
    url: str = Field(min_length=1, max_length=1_000)
    snippet: str = Field(max_length=700)

    @field_validator("worker_job_id")
    @classmethod
    def validate_job_id(cls, value: str) -> str:
        if re.fullmatch(r"job_[A-Za-z0-9._:-]+", value) is None:
            raise ValueError("research source job identity is invalid")
        return value

    @field_validator("title")
    @classmethod
    def validate_title(cls, value: str) -> str:
        return _checked_text(value)

    @field_validator("url")
    @classmethod
    def validate_url(cls, value: str) -> str:
        return checked_research_url(value)

    @field_validator("snippet")
    @classmethod
    def validate_snippet(cls, value: str) -> str:
        if value:
            return _checked_text(value)
        return value


class WritingPayload(_StrictModel):
    schema_version: Literal["1.0"]
    objective: str = Field(min_length=1, max_length=MAX_WRITING_CONTEXT_CHARACTERS)
    conversation: list[WritingConversationMessage] = Field(
        max_length=MAX_WRITING_CONVERSATION_MESSAGES
    )
    research_sources: list[WritingResearchSource] = Field(
        default_factory=list, max_length=MAX_RESEARCH_SOURCES
    )

    @field_validator("objective")
    @classmethod
    def validate_objective(cls, value: str) -> str:
        return _checked_text(value)

    @model_validator(mode="after")
    def validate_payload_size(self) -> WritingPayload:
        if (
            len(
                json.dumps(
                    [s.model_dump() for s in self.research_sources],
                    ensure_ascii=False,
                    separators=(",", ":"),
                ).encode("utf-8")
            )
            > MAX_RESEARCH_SOURCE_BYTES
        ):
            raise ValueError("research sources exceed their UTF-8 byte limit")
        encoded = json.dumps(
            self.model_dump(exclude_unset=True),
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
        if len(encoded) > MAX_WRITING_PAYLOAD_BYTES:
            raise ValueError("writing payload exceeds its UTF-8 byte limit")
        return self


class WritingResult(_StrictModel):
    schema_version: Literal["1.0"]
    content_trust: Literal["untrusted"]
    text: str = Field(min_length=1, max_length=MAX_WRITING_TEXT_BYTES)
    summary: str = Field(min_length=1, max_length=MAX_WRITING_SUMMARY_CHARACTERS)

    @field_validator("text")
    @classmethod
    def validate_text(cls, value: str) -> str:
        return _checked_text(value, max_bytes=MAX_WRITING_TEXT_BYTES)

    @field_validator("summary")
    @classmethod
    def validate_summary(cls, value: str) -> str:
        return _checked_text(value)


def validate_writing_result(value: object) -> dict[str, Any]:
    """Validate the full draft without treating it as proof of external actions."""
    return WritingResult.model_validate(value).model_dump()
