"""Conditional reported observations, never permissions or attested lessons."""

from __future__ import annotations

from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from app.services.project_execution_contracts import ExecutionHash, ExecutionProfile

LessonId = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_.:-]{1,200}$")]
Runtime = Literal["python", "node", "python_node"]


def expected_profiles(runtime: str) -> list[str]:
    return (["python_build", "python_test"] if runtime != "node" else []) + (
        ["node_build", "node_test"] if runtime != "python" else []
    )


class LessonModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class VersionedRequest(LessonModel):
    @model_validator(mode="after")
    def byte_budget(self) -> Self:
        if len(self.model_dump_json().encode("utf-8")) > 32768:
            raise ValueError("lesson request exceeds byte budget")
        return self

    request_id: LessonId
    expected_version: int = Field(ge=0, le=2**31 - 1)


class ProfileBinding(LessonModel):
    profile: ExecutionProfile
    harness_sha256: ExecutionHash
    dependency_sha256: ExecutionHash


class OracleFile(LessonModel):
    path: str = Field(min_length=1, max_length=500)
    sha256: ExecutionHash

    @model_validator(mode="after")
    def safe_path(self) -> Self:
        from pathlib import PurePosixPath

        path = PurePosixPath(self.path)
        if (
            self.path == "."
            or "\x00" in self.path
            or path.is_absolute()
            or ".." in path.parts
            or str(path) != self.path
            or "\\" in self.path
        ):
            raise ValueError("oracle path must be a normalized relative path")
        return self


class CriterionOracle(LessonModel):
    criterion_sha256: ExecutionHash
    oracle_files: list[OracleFile] = Field(min_length=1, max_length=20)
    test_profiles: list[Literal["python_test", "node_test"]] = Field(min_length=1, max_length=2)
    minimum_tests: int = Field(ge=1, le=100_000)

    @model_validator(mode="after")
    def unique(self) -> Self:
        if len({f.path for f in self.oracle_files}) != len(self.oracle_files) or len(
            set(self.test_profiles)
        ) != len(self.test_profiles):
            raise ValueError("duplicate oracle selection")
        return self


class ProfileApproval(VersionedRequest):
    runtime: Runtime
    runtime_image_id: Annotated[str, StringConstraints(pattern=r"^sha256:[a-f0-9]{64}$")]
    runner_sha256: ExecutionHash
    policy_sha256: ExecutionHash
    profiles: list[ProfileBinding] = Field(min_length=2, max_length=4)
    producer_agent_ids: list[LessonId] = Field(min_length=1, max_length=20)
    qualification_kind: Literal["synthetic_only", "isolated_runtime"]
    qualification_receipt_sha256: ExecutionHash
    criteria: list[CriterionOracle] = Field(min_length=1, max_length=20)

    @model_validator(mode="after")
    def coherent(self) -> Self:
        profiles = [p.profile for p in self.profiles]
        if profiles != expected_profiles(self.runtime):
            raise ValueError("profile must cover the full fixed runtime recipe")
        if len(set(self.producer_agent_ids)) != len(self.producer_agent_ids) or len(
            {c.criterion_sha256 for c in self.criteria}
        ) != len(self.criteria):
            raise ValueError("duplicate profile binding")
        if any(not set(c.test_profiles) <= set(profiles) for c in self.criteria):
            raise ValueError("oracle references an unavailable profile")
        return self


class ProfileWithdrawal(VersionedRequest):
    pass


class ProfileView(LessonModel):
    profile_id: LessonId
    version: int
    status: Literal["approved", "revoked"]
    approval: ProfileApproval
    approval_basis: Literal["operator_declared_qualification"] = "operator_declared_qualification"
    execution_attested: Literal[False] = False


class LessonNoteRef(LessonModel):
    memory_id: LessonId
    revision: int = Field(ge=1)


class LessonProposal(VersionedRequest):
    goal_id: LessonId
    conversation_revision: int = Field(ge=0)
    criterion_index: int = Field(ge=0, le=19)
    criterion_sha256: ExecutionHash
    revision_id: LessonId
    source_sha256: ExecutionHash
    profile_id: LessonId
    profile_version: int = Field(ge=1)
    runtime: Runtime
    profiles: list[ExecutionProfile] = Field(min_length=2, max_length=4)
    note_ref: LessonNoteRef | None = None

    @model_validator(mode="after")
    def recipe(self) -> Self:
        if len(self.model_dump_json().encode("utf-8")) > 16384:
            raise ValueError("candidate exceeds byte budget")
        if self.profiles != expected_profiles(self.runtime):
            raise ValueError("candidate must retain the full fixed recipe")
        return self


class LessonAssessment(VersionedRequest):
    acceptance_ids: list[LessonId] = Field(min_length=1, max_length=8)

    @model_validator(mode="after")
    def unique(self) -> Self:
        if len(set(self.acceptance_ids)) != len(self.acceptance_ids):
            raise ValueError("duplicate execution evidence")
        return self


class LessonWithdrawal(VersionedRequest):
    pass


class LessonNote(LessonModel):
    memory_id: LessonId
    revision: int
    original_content: str
    canonical_content: str
    canonical_language: Literal["en"] = "en"
    content_trust: Literal["untrusted"] = "untrusted"
    purpose: Literal["unvalidated_note"] = "unvalidated_note"


class LessonEvidence(LessonModel):
    acceptance_id: LessonId
    outcome: Literal["passed", "failed", "incomplete", "unknown"]
    reasons: list[str]


class LessonView(LessonModel):
    schema_version: Literal["procedure-lesson-v1"] = "procedure-lesson-v1"
    lesson_id: LessonId
    project_id: LessonId
    version: int
    lifecycle: Literal["candidate", "assessed", "quarantined", "withdrawn"]
    observation: Literal["passed", "failed", "incomplete", "unknown"]
    applicability: Literal["reported_conditions_match", "needs_revalidation", "withdrawn"]
    reasons: list[str]
    candidate: LessonProposal
    evidence: list[LessonEvidence]
    evidence_scope: Literal["explicit_selection"] = "explicit_selection"
    note: LessonNote | None
    producer_assurance: Literal["authenticated_lease_only"] = "authenticated_lease_only"
    origin: Literal["worker_reported_measurement"] = "worker_reported_measurement"
    grants_authority: Literal[False] = False
    promotion: Literal["none"] = "none"


class LessonPage(LessonModel):
    consistency: Literal["page_snapshot"] = "page_snapshot"
    items: list[LessonView]
    has_more: bool
    next_after_id: str | None
