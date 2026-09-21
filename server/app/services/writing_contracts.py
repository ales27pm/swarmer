"""Bounded textual drafts; producing text grants no execution authority."""

from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

WRITING_SKILL = "writing.draft"
MAX_WRITING_PAYLOAD_BYTES = 32_000
MAX_WRITING_TEXT_BYTES = 24_000
MAX_WRITING_SUMMARY_CHARACTERS = 1_200
MAX_WRITING_CONTEXT_CHARACTERS = 4_000
MAX_WRITING_CONVERSATION_MESSAGES = 12


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


class WritingPayload(_StrictModel):
    schema_version: Literal["1.0"]
    objective: str = Field(min_length=1, max_length=MAX_WRITING_CONTEXT_CHARACTERS)
    conversation: list[WritingConversationMessage] = Field(
        max_length=MAX_WRITING_CONVERSATION_MESSAGES
    )

    @field_validator("objective")
    @classmethod
    def validate_objective(cls, value: str) -> str:
        return _checked_text(value)

    @model_validator(mode="after")
    def validate_payload_size(self) -> WritingPayload:
        encoded = json.dumps(
            self.model_dump(), ensure_ascii=False, allow_nan=False, separators=(",", ":")
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
