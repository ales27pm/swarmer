"""Public projections of persisted memory receipts, never model reasoning or vectors."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

MemoryIdentifier = Annotated[
    str, StringConstraints(min_length=1, max_length=200, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")
]
MemoryTimestamp = Annotated[str, StringConstraints(min_length=1, max_length=64)]


class MemoryPublicModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class MemoryUsageRetrieval(MemoryPublicModel):
    mode: Literal["lexical", "semantic", "hybrid", "unknown"]
    reason: str | None = Field(max_length=100)
    provider_fingerprint: str | None = Field(pattern=r"^[a-f0-9]{64}$")


class MemoryUsageItem(MemoryPublicModel):
    id: MemoryIdentifier
    source_id: MemoryIdentifier | None
    source_kind: Literal["general_memory", "message", "project_plan", "episode", "unknown"]
    scope: Literal["general", "project"]
    source_goal_id: MemoryIdentifier | None
    source_revision_id: MemoryIdentifier | None
    source_at: MemoryTimestamp | None
    source_state: Literal["available", "changed", "missing", "redacted", "unknown"]
    verification: Literal["user_asserted", "assistant_claim", "recorded_outcome", "unknown"]
    summary: str | None = Field(max_length=800)


class MemoryUsageEntry(MemoryPublicModel):
    id: MemoryIdentifier
    evidence_stage: Literal["retrieved", "attached_to_model_call", "included_in_worker_job"]
    recorded_at: MemoryTimestamp
    completed_at: MemoryTimestamp | None
    status: str = Field(min_length=1, max_length=50)
    purpose: str | None = Field(max_length=100)
    conversation_revision: int | None = Field(ge=0, le=2**53 - 1)
    task_id: MemoryIdentifier | None
    node_id: MemoryIdentifier | None
    model_call_id: MemoryIdentifier | None
    worker_job_id: MemoryIdentifier | None
    context_id: MemoryIdentifier | None
    model_id: str | None = Field(max_length=200)
    retrieval: MemoryUsageRetrieval
    items: list[MemoryUsageItem] = Field(max_length=100)
    omitted_item_count: int = Field(ge=0, le=2**53 - 1)


class MemoryUsagePage(MemoryPublicModel):
    schema_version: Literal["1.0"]
    goal_id: MemoryIdentifier
    project_id: MemoryIdentifier | None
    current_conversation_revision: int = Field(ge=0, le=2**53 - 1)
    observed_at: MemoryTimestamp
    availability: Literal["available", "no_records"]
    history_coverage: Literal["recorded_receipts_only"]
    entries: list[MemoryUsageEntry] = Field(max_length=50)
    next_cursor: str | None = Field(max_length=512, pattern=r"^[A-Za-z0-9_-]+$")
