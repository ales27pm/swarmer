"""Explicit human mapping and review of immutable project evidence snapshots."""

from typing import Literal, Self

from pydantic import Field, model_validator

from app.services.activity_contracts import Identifier
from app.services.project_contracts import Digest
from app.services.project_graph_contracts import GraphCheck, GraphFile, GraphModel, GraphRevision
from app.services.swarm_contracts import Timestamp

StaleReason = Literal["goal_changed", "revision_changed", "producer_changed", "evidence_changed"]


class EvidenceMappingRequest(GraphModel):
    request_id: Identifier
    expected_version: int = Field(ge=0, le=2**31 - 1)
    context_sha256: Digest
    conversation_revision: int = Field(ge=0)
    criterion_sha256: Digest
    project_id: Identifier
    node_id: Identifier
    revision_id: Identifier
    revision_sha256: Digest
    file_ids: list[Identifier] = Field(max_length=80)
    check_ids: list[Identifier] = Field(max_length=12)
    review_status: Literal["linked", "reviewed"]
    public_explanation: str = Field(default="", max_length=2_000)

    @model_validator(mode="after")
    def validate_selection(self) -> Self:
        if not self.file_ids and not self.check_ids:
            raise ValueError("select at least one evidence item")
        if len(set(self.file_ids)) != len(self.file_ids) or len(set(self.check_ids)) != len(
            self.check_ids
        ):
            raise ValueError("duplicate evidence selection")
        if self.review_status == "reviewed" and not self.check_ids:
            raise ValueError("explicit review requires a passing recorded check")
        return self


class EvidenceMapping(GraphModel):
    id: Identifier
    version: int = Field(ge=1)
    criterion_id: Identifier
    criterion_index: int = Field(ge=0, le=19)
    criterion_text: str = Field(min_length=1, max_length=500)
    criterion_sha256: Digest
    goal_run_id: Identifier
    project_id: Identifier
    node_id: Identifier
    worker_job_id: Identifier
    revision_id: Identifier
    revision_sha256: Digest
    context_sha256: Digest
    conversation_revision: int = Field(ge=0)
    file_ids: list[Identifier] = Field(max_length=80)
    check_ids: list[Identifier] = Field(max_length=12)
    files: list[GraphFile] = Field(max_length=80)
    checks: list[GraphCheck] = Field(max_length=12)
    review_status: Literal["linked", "reviewed"]
    public_explanation: str = Field(max_length=2_000)
    recorded_at: Timestamp
    reviewed_at: Timestamp | None
    stale_reasons: list[StaleReason] = Field(default_factory=list, max_length=4)


class EvidenceCriterion(GraphModel):
    id: Identifier
    index: int = Field(ge=0, le=19)
    text: str = Field(min_length=1, max_length=500)
    sha256: Digest
    status: Literal["unmapped", "linked", "reviewed", "stale"]
    mapping: EvidenceMapping | None


class RequirementEvidenceView(GraphModel):
    schema_version: Literal["1.0"] = "1.0"
    observed_at: Timestamp
    goal_run_id: Identifier
    project_id: Identifier | None
    context_sha256: Digest
    conversation_revision: int = Field(ge=0)
    current_revision: GraphRevision | None
    criteria: list[EvidenceCriterion] = Field(max_length=20)
    unmatched_mappings: list[EvidenceMapping] = Field(max_length=20)
