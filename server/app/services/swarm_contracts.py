from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

SCHEMA_VERSION = "1.0"
MAX_PLAN_NODES = 20
MAX_PLAN_PARALLELISM = 3

StableIdentifier = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    ),
]
ModelIdentifier = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=1,
        max_length=500,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/-]*$",
    ),
]
TemporaryNodeId = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=1,
        max_length=64,
        pattern=r"^[A-Za-z][A-Za-z0-9_-]*$",
    ),
]
ShortText = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=500),
]
LongText = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=4_000),
]
Timestamp = Annotated[
    str,
    StringConstraints(
        min_length=20,
        max_length=40,
        pattern=(
            r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}"
            r"(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$"
        ),
    ),
    Field(json_schema_extra={"format": "date-time"}),
]


class GoalRunStatus(StrEnum):
    PLANNING = "planning"
    RUNNING = "running"
    WAITING_PERMISSION = "waiting_permission"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    BUDGET_EXHAUSTED = "budget_exhausted"


class PlanNodeStatus(StrEnum):
    PLANNED = "planned"
    READY = "ready"
    DISPATCHED = "dispatched"
    RUNNING = "running"
    WAITING_PERMISSION = "waiting_permission"
    WAITING_CAPABILITY = "waiting_capability"
    COMPLETED = "completed"
    FAILED = "failed"
    BLOCKED = "blocked"
    CANCELLED = "cancelled"
    SKIPPED = "skipped"


class PlanNodeType(StrEnum):
    WORKER = "worker"
    SYNTHESIS = "synthesis"


class AutonomyProfile(StrEnum):
    MANUAL = "manual"
    ASSISTED = "assisted"
    AUTONOMOUS = "autonomous"


class PlannerSource(StrEnum):
    IPHONE_LOCAL = "iphone_local"
    UBUNTU_LOCAL = "ubuntu_local"
    MANUAL = "manual"
    TEST = "test"


class EvaluationStatus(StrEnum):
    CONTINUE = "continue"
    REPLAN = "replan"
    DONE = "done"
    FAILED = "failed"
    NEEDS_USER = "needs_user"


class ModelRole(StrEnum):
    PLANNER = "planner"
    EVALUATOR = "evaluator"
    SUMMARIZER = "summarizer"
    SYNTHESIZER = "synthesizer"


class GoalCreateRequest(BaseModel):
    """User-owned goal limits; none of these fields grant execution authority."""

    model_config = ConfigDict(extra="forbid")

    objective: LongText
    autonomy_profile: AutonomyProfile = AutonomyProfile.ASSISTED
    completion_criteria: list[ShortText] = Field(default_factory=list, max_length=MAX_PLAN_NODES)
    max_steps: int | None = Field(default=None, strict=True, ge=1, le=MAX_PLAN_NODES)
    max_parallelism: int | None = Field(default=None, strict=True, ge=1, le=MAX_PLAN_PARALLELISM)
    max_replans: int | None = Field(default=None, strict=True, ge=0, le=10)
    max_runtime_seconds: int | None = Field(default=None, strict=True, ge=30, le=86_400)
    max_model_calls: int | None = Field(default=None, strict=True, ge=1, le=100)


class GoalStartRequest(BaseModel):
    """An optional phone/manual proposal still requires full server validation."""

    model_config = ConfigDict(extra="forbid")

    plan_proposal: SwarmPlanProposal | None = None
    planner_source: Literal["iphone_local", "manual"] | None = None

    @model_validator(mode="after")
    def validate_supplied_plan_source(self) -> GoalStartRequest:
        if (self.plan_proposal is None) != (self.planner_source is None):
            raise ValueError("plan_proposal and planner_source must be supplied together")
        return self


class GoalReplanRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: ShortText | None = None
    plan_proposal: SwarmPlanProposal | None = None
    planner_source: Literal["iphone_local", "manual"] | None = None

    @model_validator(mode="after")
    def validate_supplied_plan_source(self) -> GoalReplanRequest:
        if (self.plan_proposal is None) != (self.planner_source is None):
            raise ValueError("plan_proposal and planner_source must be supplied together")
        return self


class GoalCancelRequest(BaseModel):
    """An intentionally empty, strict cancellation command."""

    model_config = ConfigDict(extra="forbid")


class GoalFeedbackRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    score: float = Field(ge=0, le=5)
    note: str | None = Field(default=None, max_length=4_000)
    corrected_final_answer: str | None = Field(default=None, max_length=32_000)
    corrected_plan_summary: str | None = Field(default=None, max_length=16_000)
    reviewed: bool = False


class GoalResult(BaseModel):
    """Safe mobile-facing result and provenance, never raw model or worker traces."""

    model_config = ConfigDict(extra="forbid")

    goal_run_id: StableIdentifier
    root_task_id: StableIdentifier
    status: GoalRunStatus
    answer: str = Field(max_length=32_000)
    completed_nodes: list[StableIdentifier] = Field(max_length=MAX_PLAN_NODES)
    failed_nodes: list[StableIdentifier] = Field(max_length=MAX_PLAN_NODES)
    agents_used: list[StableIdentifier] = Field(max_length=MAX_PLAN_NODES)
    memory_ids: list[StableIdentifier] = Field(max_length=100)
    episode_ids: list[StableIdentifier] = Field(max_length=100)
    started_at: Timestamp
    completed_at: Timestamp | None
    limitations: list[Annotated[str, StringConstraints(max_length=4_000)]] = Field(
        max_length=MAX_PLAN_NODES
    )


class GoalRecord(BaseModel):
    """Strict public goal projection; internal model state is never accepted."""

    model_config = ConfigDict(extra="forbid")

    id: StableIdentifier
    root_task_id: StableIdentifier
    objective: LongText
    status: GoalRunStatus
    autonomy_profile: AutonomyProfile
    planner_source: PlannerSource
    max_steps: int = Field(strict=True, ge=1, le=MAX_PLAN_NODES)
    max_parallelism: int = Field(strict=True, ge=1, le=MAX_PLAN_PARALLELISM)
    max_replans: int = Field(strict=True, ge=0, le=10)
    max_runtime_seconds: int = Field(strict=True, ge=30, le=86_400)
    max_model_calls: int = Field(strict=True, ge=1, le=100)
    step_count: int = Field(strict=True, ge=0)
    replan_count: int = Field(strict=True, ge=0)
    model_call_count: int = Field(strict=True, ge=0)
    completion_criteria: list[ShortText] = Field(max_length=MAX_PLAN_NODES)
    current_phase: StableIdentifier
    evaluator_status: EvaluationStatus | None
    evaluator_summary: str | None = Field(max_length=4_000)
    created_at: Timestamp
    updated_at: Timestamp
    started_at: Timestamp | None
    completed_at: Timestamp | None
    failure_reason: str | None = Field(max_length=4_000)


class PlanNode(BaseModel):
    """Strict public node projection without planner metadata or raw evidence."""

    model_config = ConfigDict(extra="forbid")

    id: StableIdentifier
    goal_run_id: StableIdentifier
    parent_node_id: StableIdentifier | None
    node_type: PlanNodeType
    title: ShortText
    objective: LongText
    required_skill: StableIdentifier | None
    status: PlanNodeStatus
    priority: int = Field(strict=True, ge=0, le=100)
    depends_on: list[StableIdentifier] = Field(max_length=MAX_PLAN_NODES)
    assigned_agent_id: StableIdentifier | None
    worker_job_id: StableIdentifier | None
    task_id: StableIdentifier | None
    expected_output: LongText
    result_summary: str | None = Field(max_length=4_000)
    error_summary: str | None = Field(max_length=4_000)
    created_at: Timestamp
    updated_at: Timestamp
    completed_at: Timestamp | None


class GoalFeedbackRecord(BaseModel):
    """Strict redacted trajectory-feedback response."""

    model_config = ConfigDict(extra="forbid")

    id: StableIdentifier
    goal_run_id: StableIdentifier
    score: float = Field(ge=0, le=5)
    note: str | None = Field(max_length=4_000)
    corrected_final_answer: str | None = Field(max_length=32_000)
    corrected_plan_summary: str | None = Field(max_length=16_000)
    reviewed: bool
    created_at: Timestamp


class GoalDetail(BaseModel):
    model_config = ConfigDict(extra="forbid")

    goal: GoalRecord
    nodes: list[PlanNode] = Field(max_length=MAX_PLAN_NODES)
    result: GoalResult | None


class PreferredAgentConstraints(BaseModel):
    """Advisory scheduling hints which never override server eligibility."""

    model_config = ConfigDict(extra="forbid")

    agent_ids: list[StableIdentifier] = Field(default_factory=list, max_length=20)
    model_ids: list[ModelIdentifier] = Field(default_factory=list, max_length=20)
    runtime: Literal["python"] | None = None

    @model_validator(mode="after")
    def require_a_constraint(self) -> PreferredAgentConstraints:
        if not self.agent_ids and not self.model_ids and self.runtime is None:
            raise ValueError("preferred_agent_constraints must not be empty")
        if len(set(self.agent_ids)) != len(self.agent_ids):
            raise ValueError("preferred agent ids must be unique")
        if len(set(self.model_ids)) != len(self.model_ids):
            raise ValueError("preferred model ids must be unique")
        return self


class SwarmPlanNodeProposal(BaseModel):
    """A model-proposed node. It contains no execution or completion authority."""

    model_config = ConfigDict(extra="forbid")

    temporary_id: TemporaryNodeId
    node_type: PlanNodeType
    title: ShortText
    objective: LongText
    required_skill: StableIdentifier | None
    dependencies: list[TemporaryNodeId] = Field(max_length=MAX_PLAN_NODES)
    optional_dependencies: list[TemporaryNodeId] = Field(
        default_factory=list,
        max_length=MAX_PLAN_NODES,
    )
    expected_output: LongText
    priority: int = Field(strict=True, ge=0, le=100)
    preferred_agent_constraints: PreferredAgentConstraints | None = None

    @model_validator(mode="after")
    def validate_role_shape(self) -> SwarmPlanNodeProposal:
        if set(self.dependencies) & set(self.optional_dependencies):
            raise ValueError("a dependency cannot be both hard and optional")
        if self.node_type is PlanNodeType.WORKER and self.required_skill is None:
            raise ValueError("worker nodes require a worker skill")
        if self.node_type is PlanNodeType.SYNTHESIS and self.required_skill is not None:
            raise ValueError("synthesis nodes cannot declare a worker skill")
        if (
            self.node_type is PlanNodeType.SYNTHESIS
            and self.preferred_agent_constraints is not None
        ):
            raise ValueError("synthesis nodes cannot declare preferred agent constraints")
        return self


class SwarmPlanProposal(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0"]
    objective: LongText
    rationale_summary: LongText
    nodes: list[SwarmPlanNodeProposal] = Field(min_length=1, max_length=MAX_PLAN_NODES)
    completion_criteria: list[ShortText] = Field(min_length=1, max_length=MAX_PLAN_NODES)
    max_parallelism: int = Field(strict=True, ge=1, le=MAX_PLAN_PARALLELISM)


class EvaluationDecision(BaseModel):
    """An evaluator proposal; authoritative state still decides whether a goal is done."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0"]
    status: EvaluationStatus
    reason_summary: LongText
    missing_requirements: list[ShortText] = Field(max_length=MAX_PLAN_NODES)
    invalid_results: list[ShortText] = Field(max_length=MAX_PLAN_NODES)
    suggested_new_nodes: list[SwarmPlanNodeProposal] = Field(max_length=MAX_PLAN_NODES)
    user_question: ShortText | None = None
    completion_summary: LongText | None = None

    @model_validator(mode="after")
    def validate_status_shape(self) -> EvaluationDecision:
        if self.status is EvaluationStatus.NEEDS_USER:
            if self.user_question is None:
                raise ValueError("needs_user decisions require user_question")
        elif self.user_question is not None:
            raise ValueError("user_question is only valid for needs_user decisions")
        if (
            self.status not in {EvaluationStatus.CONTINUE, EvaluationStatus.REPLAN}
            and self.suggested_new_nodes
        ):
            raise ValueError("terminal and needs_user decisions cannot suggest new nodes")
        return self


class EvaluationNodeResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    node_id: StableIdentifier
    title: ShortText
    status: PlanNodeStatus
    expected_output: LongText
    result_summary: LongText | None = None
    failure_reason: ShortText | None = None


class GoalEvaluationContext(BaseModel):
    """Bounded, non-authoritative input presented to an evaluator model."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0"]
    goal_run_id: StableIdentifier
    objective: LongText
    completion_criteria: list[ShortText] = Field(min_length=1, max_length=MAX_PLAN_NODES)
    node_results: list[EvaluationNodeResult] = Field(max_length=MAX_PLAN_NODES)
    known_node_ids: list[StableIdentifier] = Field(max_length=MAX_PLAN_NODES)
    remaining_step_budget: int = Field(strict=True, ge=0, le=MAX_PLAN_NODES)
    remaining_model_call_budget: int = Field(strict=True, ge=0, le=100)
    elapsed_seconds: int = Field(strict=True, ge=0, le=86_400)
    state_fingerprint: Annotated[
        str,
        StringConstraints(pattern=r"^[a-f0-9]{64}$"),
    ]

    @model_validator(mode="after")
    def validate_node_ids(self) -> GoalEvaluationContext:
        if len(set(self.known_node_ids)) != len(self.known_node_ids):
            raise ValueError("known_node_ids must be unique")
        result_ids = [node.node_id for node in self.node_results]
        if len(set(result_ids)) != len(result_ids):
            raise ValueError("evaluation node results must be unique")
        if not set(result_ids).issubset(self.known_node_ids):
            raise ValueError("evaluation results contain an unknown node")
        return self


class ModelRoleConfig(BaseModel):
    """Safe routing metadata only; it deliberately carries no URL, token, or callable."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    role: ModelRole
    source: PlannerSource
    model_id: ModelIdentifier
