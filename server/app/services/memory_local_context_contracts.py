"""Server-issued, bounded context for local generation; never an execution grant."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.services.memory_symbolic_contracts import SymbolicContext


class LocalContextModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class LocalContextReceipt(LocalContextModel):
    id: str = Field(pattern=r"^lmctx_[a-f0-9]{32}$")
    context_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class IssuedLocalContextReceipt(LocalContextReceipt):
    expires_at: str


class GoalLocalContextRequest(LocalContextModel):
    purpose: Literal["goal_plan"]
    goal_id: str = Field(min_length=1, max_length=128)
    expected_goal_updated_at: str = Field(min_length=1, max_length=100)
    max_context_bytes: int = Field(default=16384, ge=0, le=16384)


class ToolLocalContextRequest(LocalContextModel):
    purpose: Literal["tool_proposal"]
    intent: str = Field(min_length=1, max_length=32000)
    mode: Literal["normal", "commandant", "review", "autonome"]
    source: Literal["iphone_local"]
    max_context_bytes: int = Field(default=16384, ge=0, le=16384)


LocalContextRequest = Annotated[
    GoalLocalContextRequest | ToolLocalContextRequest, Field(discriminator="purpose")
]


class LocalContextResponse(LocalContextModel):
    schema_version: Literal["local-context-v1"] = "local-context-v1"
    enabled: bool
    purpose: Literal["goal_plan", "tool_proposal"]
    goal_id: str | None
    goal_updated_at: str | None
    project_id: str | None
    input_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    symbolic_context: SymbolicContext | None
    receipt: IssuedLocalContextReceipt | None
