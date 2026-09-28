"""Read-only project structure, with reported reasoning separate from receipts."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from app.services.activity_contracts import Identifier
from app.services.project_contracts import Digest
from app.services.swarm_contracts import GoalRecord, PlanNode, Timestamp

ShortText = Annotated[str, StringConstraints(min_length=1, max_length=500)]


class GraphModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class GraphCriterion(GraphModel):
    id: Identifier
    index: int = Field(ge=0, le=19)
    text: ShortText
    coverage: Literal["not_mapped"] = "not_mapped"


class GraphDependency(GraphModel):
    from_node_id: Identifier
    to_node_id: Identifier
    dependency_type: Literal["hard", "optional"]


class GraphFile(GraphModel):
    id: Identifier
    path: str = Field(min_length=1, max_length=240)
    sha256: Digest
    bytes: int = Field(ge=0, le=64_000)


class GraphCheck(GraphModel):
    id: Identifier
    index: int = Field(ge=0, le=11)
    command: list[str] | None = Field(max_length=8)
    status: Literal["passed", "failed", "skipped"]
    exit_code: int | None = Field(ge=-255, le=255)
    duration_ms: int = Field(ge=0, le=900_000)
    provenance: Literal["attached_to_revision"] = "attached_to_revision"


class GraphRevision(GraphModel):
    id: Identifier
    project_id: Identifier
    goal_run_id: Identifier
    node_id: Identifier
    worker_job_id: Identifier
    revision: int = Field(ge=1)
    sha256: Digest
    created_at: Timestamp
    files: list[GraphFile] = Field(max_length=80)
    checks: list[GraphCheck] = Field(max_length=12)


class GraphEvaluation(GraphModel):
    id: Identifier
    goal_run_id: Identifier
    sequence: int = Field(ge=1)
    status: Literal["continue", "replan", "done", "failed", "needs_user"]
    reason_summary: str = Field(min_length=1, max_length=4_000)
    missing_requirements: list[ShortText] = Field(max_length=20)
    invalid_results: list[ShortText] = Field(max_length=20)
    created_at: Timestamp
    authority: Literal["model_report"] = "model_report"


class GraphCoverage(GraphModel):
    mode: Literal["persisted_records"] = "persisted_records"
    scope: Literal["current_goal_and_latest_project_revision"] = (
        "current_goal_and_latest_project_revision"
    )
    criterion_mapping: Literal["not_recorded"] = "not_recorded"
    planner_rationale: Literal["recorded", "not_recorded"] = "not_recorded"
    check_freshness: Literal["not_established"] = "not_established"


class GraphPlanningDecision(GraphModel):
    id: Identifier
    audit_event_id: int = Field(ge=1)
    goal_run_id: Identifier
    event_type: Literal["goal.plan.accepted", "goal.replan.accepted"]
    planner_source: Literal["iphone_local", "ubuntu_local", "manual", "test"]
    rationale_summary: str = Field(min_length=1, max_length=4_000)
    node_ids: list[Identifier] = Field(min_length=1, max_length=20)
    conversation_revision: int = Field(ge=0)
    model_call_id: Identifier | None
    model_id: str | None = Field(max_length=500)
    created_at: Timestamp
    authority: Literal["planner_proposal"] = "planner_proposal"


class ProjectGraph(GraphModel):
    schema_version: Literal["1.0"] = "1.0"
    observed_at: Timestamp
    goal: GoalRecord
    conversation_revision: int = Field(ge=0)
    project_id: Identifier | None
    criteria: list[GraphCriterion] = Field(max_length=20)
    nodes: list[PlanNode] = Field(max_length=20)
    dependencies: list[GraphDependency] = Field(max_length=190)
    latest_revision: GraphRevision | None
    evaluations: list[GraphEvaluation] = Field(max_length=5)
    evaluations_has_more: bool
    planning_decisions: list[GraphPlanningDecision] = Field(max_length=5)
    planning_decisions_has_more: bool
    coverage: GraphCoverage = Field(default_factory=GraphCoverage)
