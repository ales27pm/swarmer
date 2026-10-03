"""Bounded generation requests and server-verified binary references."""

from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

MEDIA_SKILLS = frozenset({"image.generate", "audio.synthesize"})
MAX_MEDIA_BYTES = 8 * 1024 * 1024
MEDIA_FAILURE_CODES = frozenset(
    {
        "invalid_arguments",
        "unsupported_steps",
        "resource_busy",
        "model_unavailable",
        "model_integrity_error",
        "duration_limit",
        "storage_limit",
        "runtime_error",
        "wall_timeout",
        "invalid_output",
    }
)


class MediaModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ImageArguments(MediaModel):
    """Legacy SDXL requests keep their original payload when profile is omitted."""

    prompt: str = Field(min_length=1, max_length=2_000)
    width: Literal[512, 768]
    height: Literal[512, 768]
    steps: int = Field(ge=4, le=4)
    seed: int = Field(ge=0, le=2_147_483_647)
    model_profile: Literal["sdxl-lightning-4step"] = "sdxl-lightning-4step"

    @field_validator("width", "height", mode="before")
    @classmethod
    def integer_dimensions(cls, value: object) -> int:
        # Literal validation alone accepts 512.0; the worker's contract does not.
        if type(value) is not int:
            raise ValueError("image dimensions must be integers")
        return value


class ChromaImageArguments(ImageArguments):
    """Only the qualified Chroma recipe is exposed, never runtime paths or flags."""

    width: Literal[512]
    height: Literal[512]
    steps: int = Field(ge=40, le=40)
    model_profile: Literal["chroma1-hd-q4"] = Field(...)


class AudioArguments(MediaModel):
    text: str = Field(min_length=1, max_length=1_000)
    language: Literal["fr-FR"]
    voice: Literal["ff_siwis"]
    max_duration_seconds: int = Field(ge=1, le=30)


def media_arguments(skill: str, value: object) -> dict[str, Any]:
    if skill not in MEDIA_SKILLS:
        raise ValueError("unsupported media skill")
    cls = (
        ChromaImageArguments
        if skill == "image.generate"
        and isinstance(value, dict)
        and value.get("model_profile") == "chroma1-hd-q4"
        else ImageArguments
        if skill == "image.generate"
        else AudioArguments
    )
    result = cls.model_validate(value).model_dump(exclude_unset=True)
    text = result.get("prompt", result.get("text"))
    if not isinstance(text, str) or not text.strip() or "\0" in text:
        raise ValueError("media text must be nonempty")
    return result


def media_argument_schema(skill: str) -> dict[str, Any]:
    if skill == "image.generate":
        return {
            "anyOf": [ImageArguments.model_json_schema(), ChromaImageArguments.model_json_schema()]
        }
    if skill == "audio.synthesize":
        return AudioArguments.model_json_schema()
    raise ValueError("unsupported media skill")


def media_payload(skill: str, value: object) -> dict[str, Any]:
    if skill not in MEDIA_SKILLS or not isinstance(value, dict):
        raise ValueError("unsupported media payload")
    fields = {k: v for k, v in value.items() if k != "context"}
    result = media_arguments(skill, fields)
    if "context" in value:
        context = value["context"]
        if not isinstance(context, dict) or set(context) != {
            "goal_id",
            "objective",
            "completion_criteria",
            "step_objective",
            "conversation_revision",
            "durable_context",
        }:
            raise ValueError("invalid media context")
        if not all(isinstance(context[k], str) for k in ("goal_id", "objective", "step_objective")):
            raise ValueError("invalid media context")
        if (
            not isinstance(context["completion_criteria"], list)
            or not all(isinstance(item, str) for item in context["completion_criteria"])
            or type(context["conversation_revision"]) is not int
        ):
            raise ValueError("invalid media context")
        if context["durable_context"] is not None:
            from app.services.agent_capsule import validate_agent_capsule

            validate_agent_capsule(context["durable_context"])
        result["context"] = context
    if len(json.dumps(result, ensure_ascii=False, allow_nan=False).encode()) > 64_000:
        raise ValueError("media requirements exceed input budget")
    return result


class MediaArtifact(MediaModel):
    artifact_id: str = Field(pattern=r"^media_[a-f0-9]{40}$")
    job_id: str = Field(pattern=r"^job_[a-f0-9]{32}$")
    goal_id: str = Field(pattern=r"^goal_[a-f0-9]{32}$")
    media_type: Literal["image/png", "audio/wav"]
    size_bytes: int = Field(ge=1, le=MAX_MEDIA_BYTES)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    width: int | None
    height: int | None
    duration_ms: int | None
    sample_rate: int | None
    channels: int | None

    @model_validator(mode="after")
    def dimensions(self) -> MediaArtifact:
        if self.media_type == "image/png":
            if (
                self.width not in (512, 768)
                or self.height not in (512, 768)
                or any(x is not None for x in (self.duration_ms, self.sample_rate, self.channels))
            ):
                raise ValueError("invalid image metadata")
        elif (
            self.width is not None
            or self.height is not None
            or self.sample_rate != 24_000
            or self.channels != 1
            or self.duration_ms is None
            or not 1 <= self.duration_ms <= 30_000
        ):
            raise ValueError("invalid audio metadata")
        return self


class MediaResult(MediaModel):
    schema_version: Literal["1.0"]
    content_trust: Literal["untrusted"]
    artifact: MediaArtifact


def valid_media_result(skill: str, result: object) -> bool:
    try:
        artifact = MediaResult.model_validate(result).artifact
    except (TypeError, ValueError):
        return False
    return artifact.media_type == ("image/png" if skill == "image.generate" else "audio/wav")
