"""Publicly typed measurements; these fields grant no trust or authority."""

from __future__ import annotations

from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from app.services.project_execution_receipts import execution_receipt_value

ExecutionHash = Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{64}$")]
ExecutionProfile = Literal["python_build", "python_test", "node_build", "node_test"]
MeasurementError = Literal[
    "source_unavailable", "workspace_unavailable", "environment_unbound", "harness_unavailable"
]
IncompleteReason = Literal[
    "source_unavailable",
    "workspace_unavailable",
    "environment_unbound",
    "harness_unavailable",
    "legacy_harness",
    "invalid_measurement",
    "profile_not_executed",
    "source_mismatch",
    "runner_identity_unavailable",
    "profile_interrupted",
]


class ExecutionModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class ProjectCheckObservation(ExecutionModel):
    schema_version: Literal["project-check-observation-v1"]
    source_before_sha256: ExecutionHash | None
    source_after_sha256: ExecutionHash | None
    workspace_before_sha256: ExecutionHash | None
    workspace_after_sha256: ExecutionHash | None
    dependency_before_sha256: ExecutionHash | None
    dependency_after_sha256: ExecutionHash | None
    harness_sha256: ExecutionHash | None
    source_unchanged: bool | None
    environment_unchanged: bool | None
    errors: list[MeasurementError] = Field(max_length=4)


class ProjectProfileExecution(ExecutionModel):
    profile: ExecutionProfile
    check_index: int = Field(ge=0, le=11)
    exit_code: int | None = Field(ge=-255, le=255)
    tests_executed: int | None = Field(ge=0, le=100_000)
    test_failures: int | None = Field(ge=0, le=100_000)
    duration_ms: int = Field(ge=0, le=900_000)
    observation: ProjectCheckObservation | None
    measurement_error: (
        Literal["legacy_harness", "invalid_measurement", "profile_interrupted"] | None
    )


class ProjectExecutionReceipt(ExecutionModel):
    schema_version: Literal["project-execution-receipt-v1"]
    origin: Literal["worker_reported_measurement"]
    run_id: Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{32}$")]
    runtime: Literal["python", "node", "python_node"]
    source_sha256: ExecutionHash
    runtime_image_id: Annotated[str, StringConstraints(pattern=r"^sha256:[a-f0-9]{64}$")]
    runner_sha256: ExecutionHash | None
    policy_sha256: ExecutionHash
    profiles_expected: list[ExecutionProfile] = Field(min_length=2, max_length=4)
    profiles: list[ProjectProfileExecution] = Field(max_length=4)
    observation_status: Literal["complete", "incomplete"]
    incomplete_reasons: list[IncompleteReason] = Field(max_length=10)

    @model_validator(mode="after")
    def coherent_measurement(self) -> Self:
        execution_receipt_value(self.model_dump())
        return self
