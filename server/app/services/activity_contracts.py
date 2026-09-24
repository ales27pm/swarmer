"""Explicit public metadata for persisted activity; never a raw audit/result payload."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

Identifier = Annotated[
    str, StringConstraints(min_length=1, max_length=200, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")
]
ActivityKind = Literal["model_call", "worker_job", "tool_call", "project_revision", "project_check"]
ActivityStatus = Literal[
    "queued", "running", "waiting", "completed", "failed", "cancelled", "skipped", "recorded"
]


class PublicModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ActivityDetail(PublicModel):
    revision_id: Identifier | None = None
    check_index: int | None = Field(default=None, ge=0, le=11)
    exit_code: int | None = Field(default=None, ge=-255, le=255)
    file_count: int | None = Field(default=None, ge=0, le=80)
    command: list[str] | None = Field(default=None, max_length=8)


class ActivityItem(PublicModel):
    id: str = Field(min_length=1, max_length=260, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")
    kind: ActivityKind
    status: ActivityStatus
    title: str = Field(min_length=1, max_length=160)
    recorded_at: str = Field(min_length=1, max_length=64)
    started_at: str | None = None
    completed_at: str | None = None
    duration_ms: int | None = Field(default=None, ge=0, le=2**53 - 1)
    goal_run_id: Identifier | None = None
    task_id: Identifier | None = None
    node_id: Identifier | None = None
    agent_id: Identifier | None = None
    model_id: str | None = Field(default=None, max_length=200)
    role: Literal["planner", "evaluator", "summarizer", "synthesizer"] | None = None
    tool_name: str | None = Field(default=None, max_length=200)
    detail: ActivityDetail = Field(default_factory=ActivityDetail)


class ActivityScope(PublicModel):
    type: Literal["task", "goal"]
    id: Identifier
    goal_run_id: Identifier | None = None
    root_task_id: Identifier | None = None


class ActivityCoverage(PublicModel):
    mode: Literal["persisted_records"] = "persisted_records"
    live_operations: Literal[False] = False
    notice: str = (
        "Preuves enregistrées, relues à chaque actualisation. Les opérations internes en direct "
        "non enregistrées restent absentes. Les durées inconnues ne sont pas estimées."
    )


class ActivityPage(PublicModel):
    schema_version: Literal["1.0"] = "1.0"
    scope: ActivityScope
    items: list[ActivityItem] = Field(max_length=100)
    next_cursor: str | None
    has_more: bool
    coverage: ActivityCoverage = Field(default_factory=ActivityCoverage)
