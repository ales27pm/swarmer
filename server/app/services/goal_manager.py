from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

import aiosqlite

from app.models import TaskCreate, TaskMode, TaskRecord, TaskStatus
from app.services.agent_card import SUPPORTED_AGENT_PROTOCOL, SUPPORTED_AGENT_SKILLS
from app.services.agent_dispatcher import AgentDispatchConflict, AgentDispatcher
from app.services.audit_log import append_audit_event
from app.services.context_builder import (
    ContextBuilder,
    ContextCard,
    safe_context_text,
)
from app.services.evaluator_provider import EvaluatorProvider, EvaluatorProviderError
from app.services.execution_engine import ExecutionEngine
from app.services.feedback_dataset import redact_dataset_text
from app.services.goal_code_application import (
    CODE_PROPOSAL_SKILL,
    GoalCodeApplicationConflict,
    GoalCodeApplicationService,
)
from app.services.goal_conversation import GoalConversationConflict, GoalConversationService
from app.services.goal_limits import (
    RESUME_RUNTIME_SQL,
    active_runtime_seconds,
    runtime_expired,
    runtime_remaining_seconds,
)
from app.services.goal_memory_execution import GoalMemoryExecutor
from app.services.goal_project import GoalProjectConflict, GoalProjectService
from app.services.goal_state import (
    GoalStateConflict,
    GoalStateService,
    public_goal,
    public_plan_node,
)
from app.services.maintenance_lease import MaintenanceLeaseGuard
from app.services.media_contracts import MEDIA_SKILLS
from app.services.media_store import MediaConflict, media_root, verify_media_result
from app.services.memory_normalization import MemoryNormalizationError
from app.services.memory_symbolic_contracts import (
    SymbolicCatalog,
    SymbolicContext,
    SymbolicEvidence,
)
from app.services.memory_symbolic_search import revalidate_symbolic_evidence
from app.services.model_request_execution import (
    ModelExecutionControlError,
    ModelRequestBudgetUnavailable,
)
from app.services.model_resource_admission import (
    active_local_model_work_locked,
    model_admission_connection,
)
from app.services.permission_policy import PermissionPolicy
from app.services.plan_validation import (
    PlanValidationError,
    validate_evaluation_decision,
    validate_swarm_plan,
    validate_worker_capabilities,
)
from app.services.planner_continuation_context import continuation_cards
from app.services.planner_diagnostics import (
    DIAGNOSTICS,
    known_diagnostic,
    rejection_diagnostic,
    rejection_reason,
)
from app.services.planner_provider import (
    SwarmPlannerProvider,
    SwarmPlannerProviderError,
    advertised_worker_skills,
)
from app.services.project_contracts import ProjectMemoryContext, ProjectPayload, ProjectResult
from app.services.project_identity import create_project_locked
from app.services.project_memory import ProjectMemoryConflict, ProjectMemoryService
from app.services.project_progress import has_project_progress
from app.services.project_validation import (
    NATIVE_VALIDATION_DIAGNOSTIC,
    native_authoring,
    native_project,
    pause_native_validation_locked,
)
from app.services.remote_job_policy import RemoteJobPolicyError, validate_remote_job
from app.services.research_contracts import valid_research_collect_receipt
from app.services.research_source_requirements import (
    ResearchSourceRequirementError,
    bind_research_sources,
    completed_read_urls,
)
from app.services.result_aggregator import (
    ResultAggregator,
    summarize_untrusted_worker_output,
    validate_worker_evidence,
)
from app.services.state_service import StateService
from app.services.strategy_retrieval import StrategyRetrieval
from app.services.swarm_contracts import (
    AutonomyProfile,
    EvaluationConversationMessage,
    EvaluationNodeResult,
    EvaluationStatus,
    GoalCreateRequest,
    GoalEvaluationContext,
    GoalFeedbackRequest,
    GoalMessageRequest,
    GoalReplanRequest,
    GoalStartRequest,
    PlannerSource,
    PlanNodeStatus,
    PlanNodeType,
    SwarmPlanNodeProposal,
    SwarmPlanProposal,
)
from app.services.swift_contracts import SWIFT_SKILLS, valid_swift_receipt
from app.services.worker_context import attach_symbolic_worker_context, read_worker_context
from app.services.writing_contracts import (
    WRITING_SKILL,
    validate_writing_non_delivery_result,
    validate_writing_result,
)
from app.services.writing_drafts import (
    attach_writing_retry_feedback,
    read_writing_draft,
    writing_completion_failure_locked,
    writing_payload,
    writing_retry_provenance_locked,
)

logger = logging.getLogger(__name__)
_PLANNER_RETRY_COOLDOWN_SECONDS = 60
_MEMORY_RETRIEVAL_FAILURE_PHASES = frozenset(
    {
        "memory_retrieval_invalid",
        "memory_retrieval_uncertain",
        "memory_retrieval_unavailable",
        "memory_retrieval_source_conflict",
    }
)
_EVALUATOR_RETRY_COOLDOWN_SECONDS = 60
_MAX_INVALID_EVALUATOR_ATTEMPTS = 3
_MAX_UNPRODUCTIVE_PROJECT_ITERATIONS = 3
_PROJECT_STALLED_REASON = (
    "Le projet est en pause après trois tentatives sans modification de fichier "
    "ni nouveau contrôle réussi. Les lectures intermédiaires ne remettent pas "
    "ce compteur à zéro. Les fichiers et les résultats de vérification sont conservés. "
    "Envoyez un message au projet pour reprendre avec de nouvelles instructions."
)
_EVALUATOR_FAILURE_DETAILS = {
    "transport_unavailable": ("evaluator_unavailable", "Evaluator transport is unavailable."),
    "request_rejected": (
        "evaluator_request_rejected",
        "The model provider rejected the evaluator request.",
    ),
    "invalid_response": (
        "evaluator_invalid_response",
        "The evaluator response did not pass server validation.",
    ),
    "invalid_context": (
        "evaluator_invalid_context",
        "The evaluator context could not be prepared.",
    ),
}
_EVALUATOR_RETRY_REASON = (
    "Evaluation is paused after repeated invalid responses. "
    "Retry evaluation or send new instructions to continue."
)
_EVALUATOR_TRUNCATED_REASON = (
    "The evaluator response reached its output or context limit and was cut short. "
    "No evaluation was accepted."
)
PROJECT_SKILL = "code.build_project"
_PLANNER_FAILURE_DETAILS = {
    "transport_unavailable": ("planner_unavailable", "Planner transport is unavailable."),
    "request_rejected": (
        "planner_request_rejected",
        "The model provider rejected the planner request.",
    ),
    "invalid_response": (
        "planner_invalid_response",
        "The planner response did not pass server validation.",
    ),
    "invalid_context": (
        "planner_invalid_context",
        "The planner context could not be prepared.",
    ),
}


class GoalManagerConflict(ModelExecutionControlError):
    """An authoritative goal invariant rejected a requested transition."""


class _LocalModelResourceBusy(GoalManagerConflict):
    """Admission deferred before an inference attempt or budget debit exists."""


class _PlannerProposalRejected(GoalManagerConflict):
    """A returned proposal failed validation before any graph mutation."""

    def __init__(self, message: str, *, diagnostic_code: str | None = None) -> None:
        super().__init__(message)
        self.diagnostic_code = known_diagnostic(diagnostic_code)


class _ProjectMemoryContextChanged(GoalManagerConflict):
    """Retrieval is in flight or its authoritative context changed; defer work."""


class GoalManager:
    """Code-controlled goal/DAG runtime.

    Models can return proposals, but only this service validates/persists graph
    state and creates bounded child tasks. Every worker node owns a distinct
    child task, preserving the one-active-remote-job-per-task invariant.
    """

    ACTIVE_NODE_STATUSES = frozenset(
        {"dispatched", "running", "waiting_permission", "waiting_capability"}
    )

    def __init__(
        self,
        db_path: Path,
        *,
        state_service: StateService,
        agent_dispatcher: AgentDispatcher,
        planner: SwarmPlannerProvider,
        evaluator: EvaluatorProvider,
        permission_policy: PermissionPolicy,
        research_evaluator: EvaluatorProvider | None = None,
        context_builder: Any | None = None,
        strategy_retrieval: Any | None = None,
        project_memory: ProjectMemoryService | None = None,
        episode_memory: Any | None = None,
        result_aggregator: Any | None = None,
        default_max_steps: int = 20,
        default_max_parallelism: int = 3,
        default_max_replans: int = 3,
        default_max_runtime_seconds: int = 1_800,
        default_max_model_calls: int = 100,
        auto_continue_on_model_budget_exhausted: bool = False,
        instance_id: str | None = None,
        model_call_lease_seconds: int = 120,
        require_execution_workers: bool = False,
        execution_engine: ExecutionEngine | None = None,
    ) -> None:
        self.db_path = db_path
        self.state_service = state_service
        self.agent_dispatcher = agent_dispatcher
        self.planner = planner
        self.evaluator = evaluator
        self.research_evaluator = research_evaluator
        self.permission_policy = permission_policy
        self.context_builder = context_builder
        self.strategy_retrieval = strategy_retrieval
        self.project_memory = project_memory
        self.episode_memory = episode_memory
        self.result_aggregator = result_aggregator or ResultAggregator(db_path)
        self.instance_id = instance_id or f"goal-manager-{uuid4().hex}"
        if not 30 <= model_call_lease_seconds <= 900:
            raise ValueError("model_call_lease_seconds must be between 30 and 900")
        self.model_call_lease_seconds = model_call_lease_seconds
        self.require_execution_workers = require_execution_workers
        self.auto_continue_on_model_budget_exhausted = auto_continue_on_model_budget_exhausted
        self.code_applications = (
            GoalCodeApplicationService(db_path, execution_engine)
            if execution_engine is not None
            else None
        )
        self.project_applications = (
            GoalProjectService(db_path, execution_engine) if execution_engine is not None else None
        )
        self.conversations = GoalConversationService(db_path)
        self.defaults = {
            "max_steps": default_max_steps,
            "max_parallelism": default_max_parallelism,
            "max_replans": default_max_replans,
            "max_runtime_seconds": default_max_runtime_seconds,
            "max_model_calls": default_max_model_calls,
        }
        self.graph = GoalStateService(db_path)
        self._locks: dict[str, asyncio.Lock] = {}

    def _lock(self, goal_run_id: str) -> asyncio.Lock:
        return self._locks.setdefault(goal_run_id, asyncio.Lock())

    @staticmethod
    def _now() -> str:
        return datetime.now(UTC).isoformat()

    @staticmethod
    def _model_output_digest(value: Mapping[str, Any]) -> str:
        encoded = json.dumps(
            dict(value),
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    @staticmethod
    def _bind_plan_to_goal(
        goal: Mapping[str, Any],
        proposal: SwarmPlanProposal,
        *,
        model_call_id: str | None,
    ) -> SwarmPlanProposal:
        """Resolve a model's goal-card reference without revealing the raw objective.

        The reference must identify this call's goal. Explicit phone/manual
        proposals keep their existing exact-objective contract; they cannot
        substitute a reference for user-supplied objective text.
        """

        if sum(node.required_skill == PROJECT_SKILL for node in proposal.nodes) > 1:
            raise _PlannerProposalRejected(
                "a project plan requires one sequential project worker",
                diagnostic_code="project_plan_shape",
            )
        if proposal.objective == goal["objective"]:
            return proposal
        if model_call_id is not None and proposal.objective == f"goal:{goal['id']}":
            return proposal.model_copy(update={"objective": str(goal["objective"])})
        raise _PlannerProposalRejected(
            "planner changed the authoritative goal objective", diagnostic_code="objective_mismatch"
        )

    @staticmethod
    def _runtime_expired(goal: Mapping[str, Any]) -> bool:
        return runtime_expired(goal)

    async def _bind_research_source_requirements(
        self, goal: Mapping[str, Any], proposal: SwarmPlanProposal, *, initial: bool = True
    ) -> SwarmPlanProposal:
        try:
            return bind_research_sources(
                proposal,
                str(goal["objective"]),
                await self.recent_conversation(str(goal["id"]), include_timestamps=True),
                already_read=(
                    {} if initial else await completed_read_urls(self.db_path, str(goal["id"]))
                ),
            )
        except ResearchSourceRequirementError as exc:
            raise _PlannerProposalRejected(
                "plan does not preserve explicit source requirements",
                diagnostic_code=exc.diagnostic_code,
            ) from exc

    @staticmethod
    def _remaining_runtime_seconds(goal: Mapping[str, Any]) -> float:
        return runtime_remaining_seconds(goal)

    async def initialize(self) -> None:
        for service in (self.context_builder, self.episode_memory):
            initializer = getattr(service, "initialize", None)
            if initializer is not None:
                await initializer()

        now = self._now()
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """UPDATE goal_model_calls SET status='failed',completed_at=?,
                error_category='lease_expired'
                WHERE status='started'
                  AND (lease_expires_at IS NULL OR lease_expires_at<=?)""",
                (now, now),
            )
            await db.commit()

    async def conversation_messages(self, goal_id: str, limit: int = 100) -> dict[str, Any]:
        try:
            return await self.conversations.messages(goal_id, limit=limit)
        except GoalConversationConflict as exc:
            raise GoalManagerConflict(str(exc)) from exc

    async def recent_conversation(
        self, goal_id: str, limit: int = 40, *, include_timestamps: bool = False
    ) -> list[dict[str, str]]:
        # The UI may show a shared continuation history. Model inputs instead
        # require each message's own goal/conversation and exact project scope.
        # This read snapshot does not lease source scope after returning;
        # model-call admission independently checks goal/conversation revision.
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("PRAGMA query_only=ON")
            await db.execute("BEGIN")
            target = await (
                await db.execute(
                    """SELECT c.conversation_id,p.project_id FROM goal_conversation_links c
                    LEFT JOIN goal_project_links p ON p.goal_run_id=c.goal_run_id
                    WHERE c.goal_run_id=?""",
                    (goal_id,),
                )
            ).fetchone()
            if target is None:
                raise GoalManagerConflict("goal not found")
            rows = await (
                await db.execute(
                    """SELECT m.role,m.content,m.created_at FROM goal_messages m
                    JOIN goal_conversation_links source
                      ON source.goal_run_id=m.goal_run_id
                     AND source.conversation_id=m.conversation_id
                    LEFT JOIN goal_project_links p ON p.goal_run_id=m.goal_run_id
                    WHERE m.conversation_id=? AND
                      (p.project_id=? OR (? IS NULL AND p.project_id IS NULL))
                    ORDER BY m.rowid DESC LIMIT ?""",
                    (
                        target["conversation_id"],
                        target["project_id"],
                        target["project_id"],
                        max(1, min(limit, 100)),
                    ),
                )
            ).fetchall()
            await db.rollback()
        return [
            {
                "role": item["role"],
                "content": safe_context_text(item["content"], max_chars=4_000),
                **({"created_at": item["created_at"]} if include_timestamps else {}),
            }
            for item in reversed(list(rows))
        ]

    async def reply_goal(
        self, goal_id: str, request: GoalMessageRequest, *, actor_id: str
    ) -> dict[str, Any]:
        # Never hold a model-call lock while accepting an independently durable reply.
        try:
            active_id = await self.conversations.append(
                goal_id,
                message=request.message,
                client_message_id=request.client_message_id,
                reply_to_message_id=request.reply_to_message_id,
                actor_id=actor_id,
                planning_mode=request.planning_mode,
            )
        except GoalConversationConflict as exc:
            raise GoalManagerConflict(str(exc)) from exc
        detail = await self.get_goal(active_id)
        if detail is None:
            raise GoalManagerConflict("goal not found")
        return detail

    async def _worker_payload(
        self, goal: Mapping[str, Any], node: Mapping[str, Any]
    ) -> dict[str, Any]:
        payload = await self._worker_payload_base(goal, node)
        catalogs = getattr(self.strategy_retrieval, "symbolic_catalogs", ())
        if not catalogs or not node.get("id"):
            return payload
        try:
            return await attach_symbolic_worker_context(
                self.db_path,
                payload,
                goal_id=str(goal["id"]),
                node_id=str(node["id"]),
                conversation_revision=int(goal.get("conversation_revision") or 0),
                catalogs=tuple(catalogs),
            )
        except (ValueError, TypeError) as exc:
            raise GoalManagerConflict("Symbolic worker context is unavailable or changed.") from exc

    async def _worker_payload_base(
        self, goal: Mapping[str, Any], node: Mapping[str, Any]
    ) -> dict[str, Any]:
        dependency_context: list[dict[str, str]] = []
        sources: list[dict[str, str]] = []
        if node["required_skill"] in {WRITING_SKILL, PROJECT_SKILL} and node.get("id"):
            try:
                dependency_context, sources = await read_worker_context(
                    self.db_path, str(node["goal_run_id"]), str(node["id"])
                )
            except (ValueError, TypeError) as exc:
                raise GoalManagerConflict("Completed dependency evidence is unavailable.") from exc
        if node["required_skill"] == WRITING_SKILL:
            from app.services.project_context import ProjectContextConflict

            goal_id = str(node["goal_run_id"])
            original = await self.graph.get_goal(goal_id)
            if original is None:
                raise GoalManagerConflict("The writing goal is unavailable.")
            try:
                durable_context = None
                if (
                    self.project_applications is not None
                    and self.project_applications.context is not None
                ):
                    durable = await self.project_applications.context.refresh(goal_id)
                    durable_context = self.project_applications.context.prompt_state(durable)
                payload = writing_payload(
                    str(original["objective"]),
                    await self.recent_conversation(goal_id, limit=100),
                    research_sources=sources,
                    dependency_context=dependency_context,
                    step_objective=str(node["objective"]) if node.get("id") else None,
                    durable_context=durable_context,
                )
            except (ProjectContextConflict, ValueError, TypeError) as exc:
                raise GoalManagerConflict(
                    "Writing context is unavailable or exceeds its required input budget."
                ) from exc
            if not node.get("id"):
                return payload
            try:
                return await attach_writing_retry_feedback(
                    self.db_path, goal_id, str(node["id"]), payload
                )
            except (ValueError, TypeError) as exc:
                raise GoalManagerConflict("Writing repair feedback is unavailable.") from exc
        if node["required_skill"] in MEDIA_SKILLS:
            from app.services.project_context import ProjectContextConflict

            payload = self._payload_for_node(node)
            try:
                media_context = None
                if (
                    self.project_applications is not None
                    and self.project_applications.context is not None
                ):
                    state = await self.project_applications.context.refresh(str(goal["id"]))
                    media_context = self.project_applications.context.prompt_state(state)
                payload["context"] = {
                    "goal_id": str(goal["id"]),
                    "objective": str(goal["objective"]),
                    "completion_criteria": goal.get("completion_criteria", []),
                    "step_objective": str(node["objective"]),
                    "conversation_revision": int(goal.get("conversation_revision") or 0),
                    "durable_context": media_context,
                }
                return validate_remote_job(str(node["required_skill"]), payload)
            except (ProjectContextConflict, ValueError, TypeError) as exc:
                raise GoalManagerConflict(
                    "Media requirements exceed the input budget or are unavailable."
                ) from exc
        if node["required_skill"] == CODE_PROPOSAL_SKILL:
            from app.services.project_context import ProjectContextConflict

            try:
                payload = self._payload_for_node(node)
                if (
                    self.project_applications is not None
                    and self.project_applications.context is not None
                ):
                    durable = await self.project_applications.context.refresh(str(goal["id"]))
                    payload["durable_context"] = self.project_applications.context.prompt_state(
                        durable
                    )
                return validate_remote_job(CODE_PROPOSAL_SKILL, payload)
            except (ProjectContextConflict, ValueError, TypeError) as exc:
                raise GoalManagerConflict(
                    "Python generation context is unavailable or exceeds its required input budget."
                ) from exc
        if node["required_skill"] != PROJECT_SKILL:
            return self._payload_for_node(node)
        if self.project_applications is None:
            raise GoalManagerConflict("The project application gateway is unavailable.")
        goal_id = str(node["goal_run_id"])
        from app.services.project_context import ProjectContextConflict

        try:
            payload = await self.project_applications.payload(
                goal_id,
                dict(node),
                await self.recent_conversation(goal_id),
                dependency_context=dependency_context,
                research_sources=sources,
            )
            return ProjectPayload.model_validate(payload).model_dump(exclude_unset=True)
        except ProjectContextConflict as exc:
            raise GoalManagerConflict(str(exc)) from exc

    async def _resume_pending_conversation(
        self, goal_id: str, *, maintenance_guard: MaintenanceLeaseGuard | None = None
    ) -> None:
        """Prepare the next iteration only after previous work and grants settle."""
        goal = await self.graph.get_goal(goal_id)
        if goal is None or goal["status"] in self.graph.GOAL_TERMINAL:
            return
        if (
            goal["status"] == "planning"
            and goal["started_at"] is None
            and goal["current_phase"] == "awaiting_local_plan"
        ):
            return
        pending = int(goal.get("pending_message_revision") or 0) > 0
        if not pending and goal["current_phase"] != "project_continue":
            return
        if pending and goal["current_phase"] == "evaluator_retry_required":
            await self._resume_evaluator_retry(goal_id, maintenance_guard=maintenance_guard)
            goal = await self.graph.get_goal(goal_id)
            if goal is None:
                return
        if pending:
            await self.agent_dispatcher.cancel_stale_goal_jobs(
                goal_id, maintenance_guard=maintenance_guard
            )
        nodes = await self.graph.list_nodes(goal_id)
        if any(
            node["status"] in {"dispatched", "running", "waiting_capability"}
            and (pending or node["required_skill"] == PROJECT_SKILL)
            for node in nodes
        ):
            return
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            if maintenance_guard is not None:
                await maintenance_guard.require_current_locked(db)
            current = await (
                await db.execute("SELECT * FROM goal_runs WHERE id=?", (goal_id,))
            ).fetchone()
            if current is None or current["status"] in self.graph.GOAL_TERMINAL:
                return
            if any(
                current[key] != goal[key]
                for key in ("conversation_revision", "pending_message_revision", "current_phase")
            ):
                return
            current_nodes = await (
                await db.execute("SELECT id,status FROM plan_nodes WHERE goal_run_id=?", (goal_id,))
            ).fetchall()
            if {(row["id"], row["status"]) for row in current_nodes} != {
                (node["id"], node["status"]) for node in nodes
            }:
                return
            # A real approval is never implicitly denied, replaced or approved
            # by text. A child committed before call creation is not a grant.
            prepared = await (
                await db.execute(
                    """SELECT 1 FROM plan_nodes n LEFT JOIN project_revisions r ON r.node_id=n.id
                    WHERE n.goal_run_id=? AND n.status='waiting_permission'
                    AND (n.required_skill<>? OR (r.apply_task_id IS NOT NULL AND (
                        EXISTS (SELECT 1 FROM tool_calls c WHERE c.task_id=r.apply_task_id)
                        OR NOT EXISTS (SELECT 1 FROM tasks t WHERE t.id=r.apply_task_id
                                       AND t.status IN ('created','planned'))))) LIMIT 1""",
                    (goal_id, PROJECT_SKILL),
                )
            ).fetchone()
            if prepared:
                return
            orphaned = await (
                await db.execute(
                    """SELECT r.id,r.node_id,r.apply_task_id FROM project_revisions r
                    JOIN plan_nodes n ON n.id=r.node_id JOIN tasks t ON t.id=r.apply_task_id
                    WHERE r.goal_run_id=? AND n.status='waiting_permission'
                    AND n.required_skill=? AND t.status IN ('created','planned')
                    AND NOT EXISTS (SELECT 1 FROM tool_calls c WHERE c.task_id=t.id)""",
                    (goal_id, PROJECT_SKILL),
                )
            ).fetchall()
            for orphan in orphaned:
                now = self._now()
                # This same lock fences a delayed create_tool_call: it can no
                # longer attach an approval to this cancelled historical task.
                await db.execute(
                    "UPDATE tasks SET status='cancelled',updated_at=?,completed_at=? WHERE id=?",
                    (now, now, orphan["apply_task_id"]),
                )
                await db.execute(
                    "UPDATE project_revisions SET apply_task_id=NULL WHERE id=?",
                    (orphan["id"],),
                )
                await append_audit_event(
                    db,
                    "goal.project.review_superseded",
                    {
                        "goal_run_id": goal_id,
                        "revision_id": orphan["id"],
                        "node_id": orphan["node_id"],
                        "apply_task_id": orphan["apply_task_id"],
                        "conversation_revision": int(current["conversation_revision"]),
                    },
                    actor_type="control-plane",
                    actor_id="goal-manager",
                    task_id=orphan["apply_task_id"],
                    trace_id=goal_id,
                    created_at=now,
                )
            linked = await (
                await db.execute(
                    """SELECT 1 FROM goal_project_links l JOIN project_revisions r
                    ON r.project_id=l.project_id WHERE l.goal_run_id=? LIMIT 1""",
                    (goal_id,),
                )
            ).fetchone()
            project = linked is not None or any(n["required_skill"] == PROJECT_SKILL for n in nodes)
            if project and self.project_applications is not None and not pending:
                latest = await (
                    await db.execute(
                        "SELECT node_id FROM project_revisions WHERE goal_run_id=? ORDER BY revision DESC LIMIT 1",
                        (goal_id,),
                    )
                ).fetchone()
                previous = next(
                    (
                        n
                        for n in nodes
                        if latest
                        and n["id"] == latest[0]
                        and n["status"] == "completed"
                        and int(n["conversation_revision"]) == int(current["conversation_revision"])
                    ),
                    None,
                )
                if previous is None:
                    return
                await self._enqueue_project_successor_locked(
                    db,
                    dict(current),
                    previous,
                    now=self._now(),
                    maintenance_guard=maintenance_guard,
                )
                await db.commit()
                return
            # Persist only the orphaned, never-proposed application cleanup
            # before releasing this lock for the planner's network call.
            await db.commit()
        goal = await self.graph.get_goal(goal_id)
        if goal is None or (not pending and (not nodes or goal["status"] == "planning")):
            return
        if any(n["status"] in {"dispatched", "running", "waiting_capability"} for n in nodes):
            return
        initial_continuation = not nodes and goal["status"] == "planning"
        if not initial_continuation and int(goal["replan_count"]) >= int(goal["max_replans"]):
            await self._terminate_goal(
                goal_id, status="budget_exhausted", reason="goal replan budget exhausted"
            )
            return
        async with aiosqlite.connect(self.db_path) as db:
            if await self._memory_retrieval_cooling_down_locked(db, goal, now=self._now()):
                return
        # Failed routing remains recoverable, but reconciliation must not consume
        # a fresh model call on every tick while older ready nodes still exist.
        async with aiosqlite.connect(self.db_path) as db:
            cooling = await (
                await db.execute(
                    """SELECT 1 FROM goal_model_calls WHERE goal_run_id=? AND role='planner'
                AND conversation_revision=? AND status='failed'
                AND julianday(completed_at)+?/86400.0>julianday(?) LIMIT 1""",
                    (
                        goal_id,
                        goal["conversation_revision"],
                        _PLANNER_RETRY_COOLDOWN_SECONDS,
                        self._now(),
                    ),
                )
            ).fetchone()
        if cooling:
            return
        proposal, source, call_id = await self._obtain_plan(
            goal,
            GoalStartRequest(),
            user_guidance=(
                "Route the latest user request using available worker skills, even when this goal "
                "already has a code project. Preserve its saved files. Include code.build_project "
                "for implementation, including continuing unfinished saved work; it resumes the "
                "existing snapshot. Connect "
                "workers to the dependencies they need. Independent work may run in parallel "
                "within the goal limits. Do not repeat completed work."
            )
            if pending
            else None,
            maintenance_guard=maintenance_guard,
        )
        try:
            if initial_continuation:
                await self._persist_initial_plan(
                    goal,
                    proposal,
                    source=source,
                    model_call_id=call_id,
                    maintenance_guard=maintenance_guard,
                )
            else:
                await self._append_replan_nodes(
                    goal,
                    proposal,
                    source=source,
                    model_call_id=call_id,
                    maintenance_guard=maintenance_guard,
                )
        except (PlanValidationError, _PlannerProposalRejected) as exc:
            if call_id is not None:
                await self._record_planner_failure(
                    goal_id,
                    call_id,
                    category="invalid_response",
                    diagnostic_code=rejection_diagnostic(exc),
                    maintenance_guard=maintenance_guard,
                )
            raise GoalManagerConflict(str(exc)) from exc
        except GoalManagerConflict:
            if call_id is not None:
                await self._finish_model_call(
                    call_id,
                    status="failed",
                    error_category="state_changed",
                    maintenance_guard=maintenance_guard,
                )
            raise

    async def _enqueue_project_successor_locked(
        self,
        db: aiosqlite.Connection,
        goal: Mapping[str, Any],
        previous: Mapping[str, Any],
        *,
        now: str,
        maintenance_guard: MaintenanceLeaseGuard | None = None,
    ) -> None:
        """Advance a partial snapshot and its DAG edges in the same transaction."""
        goal_id = str(goal["id"])
        count = await (
            await db.execute("SELECT COUNT(*) FROM plan_nodes WHERE goal_run_id=?", (goal_id,))
        ).fetchone()
        if (count is not None and int(count[0]) >= int(goal["max_steps"])) or int(
            goal["model_call_count"]
        ) >= int(goal["max_model_calls"]):
            await self._terminate_goal_locked(
                db,
                goal,
                status="budget_exhausted",
                reason="goal project iteration budget exhausted",
                now=now,
                maintenance_guard=maintenance_guard,
            )
            return
        successor = f"node_{uuid4().hex}"
        await db.execute(
            """INSERT INTO plan_nodes(id,goal_run_id,parent_node_id,node_type,title,objective,
            required_skill,status,priority,depends_on_json,expected_output,created_at,updated_at,conversation_revision)
            VALUES(?,?,?,'worker','Continue project implementation',?,?,'planned',50,?,?,?,?,?)""",
            (
                successor,
                goal_id,
                previous["id"],
                previous["objective"],
                PROJECT_SKILL,
                json.dumps(previous["depends_on"]),
                "A cumulative project snapshot with actual build and test receipts.",
                now,
                now,
                int(goal["conversation_revision"]),
            ),
        )
        await self._carry_project_dependencies_locked(
            db, goal_id, str(previous["id"]), successor, now
        )
        await db.execute(
            f"""UPDATE goal_runs SET status='running',current_phase='project_building',
            updated_at=?,{RESUME_RUNTIME_SQL} WHERE id=?""",  # nosec B608
            (now, now, goal_id),
        )
        await db.execute(
            "UPDATE tasks SET status='running',updated_at=? WHERE id=?", (now, goal["root_task_id"])
        )

    @staticmethod
    async def _carry_project_dependencies_locked(
        db: aiosqlite.Connection, goal_id: str, previous: str, successor: str, now: str
    ) -> None:
        """Keep upstream context and make downstream work await the final iteration."""
        await db.execute(
            """INSERT INTO plan_edges(goal_run_id,from_node_id,to_node_id,dependency_type)
            SELECT goal_run_id,from_node_id,?,dependency_type FROM plan_edges
            WHERE goal_run_id=? AND to_node_id=?""",
            (successor, goal_id, previous),
        )
        rows = await (
            await db.execute(
                "SELECT id,depends_on_json FROM plan_nodes WHERE goal_run_id=? AND status IN ('planned','ready') AND id<>?",
                (goal_id, successor),
            )
        ).fetchall()
        for row in rows:
            dependencies = json.loads(row["depends_on_json"])
            if previous not in dependencies:
                continue
            await db.execute(
                "UPDATE plan_nodes SET depends_on_json=?,status='planned',updated_at=? WHERE id=?",
                (
                    json.dumps([successor if item == previous else item for item in dependencies]),
                    now,
                    row["id"],
                ),
            )
            await db.execute(
                "UPDATE plan_edges SET from_node_id=? WHERE goal_run_id=? AND from_node_id=? AND to_node_id=?",
                (successor, goal_id, previous, row["id"]),
            )

    @staticmethod
    async def _project_stalled_locked(
        db: aiosqlite.Connection, goal_id: str, conversation_revision: int
    ) -> str | None:
        """Count accepted no-progress receipts, never free-text model claims.

        A new user revision or real file/check progress resets the streak.
        Inspection has no repeat cutoff and does not erase failed attempts.
        Read iterations remain subject to the goal's overall budgets.
        The bounded history is confined to this goal and instruction revision.
        """
        rows = await (
            await db.execute(
                """SELECT r.snapshot_json,j.payload_json FROM project_revisions r
                JOIN plan_nodes n ON n.id=r.node_id
                JOIN agent_jobs j ON j.id=r.worker_job_id
                WHERE r.goal_run_id=? AND n.goal_run_id=? AND n.conversation_revision=?
                ORDER BY r.revision DESC LIMIT 100""",
                (goal_id, goal_id, conversation_revision),
            )
        ).fetchall()
        attempts = 0
        for row in rows:
            result = ProjectResult.model_validate_json(str(row[0]))
            payload = ProjectPayload.model_validate_json(str(row[1]))
            if result.action != "continue" or has_project_progress(payload, result):
                return None
            if result.focus_paths:
                continue
            attempts += 1
            if attempts >= _MAX_UNPRODUCTIVE_PROJECT_ITERATIONS:
                return _PROJECT_STALLED_REASON
        return None

    async def _accept_project_result(
        self,
        goal: Mapping[str, Any],
        node: Mapping[str, Any],
        job: Mapping[str, Any],
        *,
        maintenance_guard: MaintenanceLeaseGuard | None = None,
    ) -> None:
        if self.project_applications is None:
            raise GoalManagerConflict("The project application gateway is unavailable.")
        goal_id = str(goal["id"])
        result = await self.project_applications.capture_result(
            goal_id,
            str(node["id"]),
            str(job["id"]),
            maintenance_guard=maintenance_guard,
        )
        now = self._now()
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            if maintenance_guard is not None:
                await maintenance_guard.require_current_locked(db)
            current = await (
                await db.execute("SELECT * FROM goal_runs WHERE id=?", (goal_id,))
            ).fetchone()
            current_node = await (
                await db.execute("SELECT * FROM plan_nodes WHERE id=?", (node["id"],))
            ).fetchone()
            if (
                current is None
                or current["status"] in self.graph.GOAL_TERMINAL
                or current_node is None
            ):
                return
            if current_node["status"] not in {"dispatched", "running"}:
                return
            if self._runtime_expired(dict(current)):
                await self._terminate_goal_locked(
                    db,
                    dict(current),
                    status="budget_exhausted",
                    reason="goal runtime budget exhausted",
                    now=now,
                    maintenance_guard=maintenance_guard,
                )
                await db.commit()
                return
            stale = int(current_node["conversation_revision"]) != int(
                current["conversation_revision"]
            )
            # Use the authenticated persisted input as well as the output: a
            # model cannot evade the lane's coverage limit by deleting Swift.
            job_input = await (
                await db.execute("SELECT payload_json FROM agent_jobs WHERE id=?", (job["id"],))
            ).fetchone()
            unsupported_native = (
                not stale
                and not native_authoring(result)
                and (
                    native_project(result)
                    or (job_input is not None and native_project(json.loads(str(job_input[0]))))
                )
            )
            action = "continue" if stale or unsupported_native else str(result["action"])
            message = (
                "The previous iteration was retained. Continuing with your latest instructions."
                if stale
                else str(result["message"])
            )
            stall_reason = (
                await self._project_stalled_locked(
                    db, goal_id, int(current["conversation_revision"])
                )
                if not stale and not unsupported_native and action == "continue"
                else None
            )
            stalled = stall_reason is not None
            if unsupported_native:
                message = NATIVE_VALIDATION_DIAGNOSTIC
            elif stalled:
                message = str(stall_reason) + "\n\n" + message[:3_000]
            await GoalConversationService.assistant_locked(
                db, goal_id, message, question=action == "clarify", now=now
            )
            waiting = unsupported_native or stalled or action in {"clarify", "complete"}
            await db.execute(
                """UPDATE plan_nodes SET status=?,result_summary=?,updated_at=?,completed_at=? WHERE id=?""",
                (
                    "waiting_permission" if action == "complete" else "completed",
                    "Project snapshot is ready for review."
                    if action == "complete"
                    else "Project iteration recorded; further work is required.",
                    now,
                    None if action == "complete" else now,
                    node["id"],
                ),
            )
            await db.execute(
                """UPDATE goal_runs SET status=?,current_phase=?,evaluator_summary=?,
                paused_at=CASE WHEN ? THEN COALESCE(paused_at,?) ELSE paused_at END,updated_at=? WHERE id=?""",
                (
                    "waiting_permission" if waiting else "running",
                    "needs_user"
                    if action == "clarify" or stalled or unsupported_native
                    else "project_ready"
                    if action == "complete"
                    else "project_continue",
                    NATIVE_VALIDATION_DIAGNOSTIC
                    if unsupported_native
                    else stall_reason
                    if stalled
                    else "Project needs your clarification."
                    if action == "clarify"
                    else "Project iteration recorded.",
                    waiting,
                    now,
                    now,
                    goal_id,
                ),
            )
            await db.execute(
                "UPDATE tasks SET status=?,updated_at=? WHERE id=?",
                ("waiting_permission" if waiting else "running", now, current["root_task_id"]),
            )
            if not waiting and not stale and not int(current["pending_message_revision"] or 0):
                await self._enqueue_project_successor_locked(
                    db, dict(current), node, now=now, maintenance_guard=maintenance_guard
                )
            if maintenance_guard is not None:
                await maintenance_guard.require_current_locked(db)
            await db.commit()
        if action == "continue" and not stalled and not unsupported_native:
            await self._resume_pending_conversation(goal_id, maintenance_guard=maintenance_guard)
            await self._advance_ready(
                goal_id, explicit_user_action=False, maintenance_guard=maintenance_guard
            )

    async def create_goal(
        self,
        request: GoalCreateRequest,
        *,
        actor_id: str,
    ) -> dict[str, Any]:
        now = datetime.now(UTC)
        goal_run_id = f"goal_{uuid4().hex}"
        root = TaskRecord.new(
            TaskCreate(
                input=request.objective,
                conversation_id=request.conversation_id,
                mode=(
                    TaskMode.AUTONOME
                    if request.autonomy_profile is AutonomyProfile.AUTONOMOUS
                    else TaskMode.NORMAL
                ),
            ),
            source=actor_id,
        ).model_copy(update={"status": TaskStatus.PLANNED})
        criteria = request.completion_criteria or [
            (
                "Fulfill the user's requested outcome, preserving the requested deliverables and "
                "actions, with evidence for each claimed result."
            )
        ]
        limits = {
            name: getattr(request, name) if getattr(request, name) is not None else default
            for name, default in self.defaults.items()
        }
        if int(limits["max_parallelism"]) > int(limits["max_steps"]):
            limits["max_parallelism"] = int(limits["max_steps"])
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            request_key = (
                f"goal.create:{request.client_request_id}" if request.client_request_id else None
            )
            request_digest = hashlib.sha256(
                json.dumps(
                    request.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
                ).encode()
            ).hexdigest()
            if request_key is not None:
                receipt = await (
                    await db.execute(
                        "SELECT operation,request_digest,response_json FROM idempotency_receipts WHERE actor_id=? AND idempotency_key=?",
                        (actor_id, request_key),
                    )
                ).fetchone()
                if receipt is not None:
                    if (
                        receipt["operation"] != "goal.create"
                        or receipt["request_digest"] != request_digest
                    ):
                        raise GoalManagerConflict(
                            "request identifier already belongs to a different request"
                        )
                    saved = json.loads(receipt["response_json"])
                    previous = await (
                        await db.execute(
                            "SELECT * FROM goal_runs WHERE id=?", (saved["goal_run_id"],)
                        )
                    ).fetchone()
                    if previous is None:
                        raise GoalManagerConflict("previously created goal is unavailable")
                    return self.graph._goal_from_row(previous)
            chat_history: list[dict[str, Any]] = []
            if request.conversation_id is not None:
                chat = await (
                    await db.execute(
                        "SELECT id FROM conversations WHERE id=?", (request.conversation_id,)
                    )
                ).fetchone()
                if chat is None:
                    raise GoalManagerConflict("conversation not found")
                chat_history = [
                    dict(row)
                    for row in await (
                        await db.execute(
                            """SELECT * FROM (SELECT rowid AS sequence,id,role,content,created_at
                    FROM messages WHERE conversation_id=? AND role IN ('user','assistant','agent')
                    ORDER BY rowid DESC LIMIT 40) ORDER BY sequence ASC""",
                            (request.conversation_id,),
                        )
                    ).fetchall()
                ]
            await StateService._insert_task(db, root)
            await db.execute(
                """
                INSERT INTO goal_runs(
                    id,root_task_id,objective,status,autonomy_profile,planner_source,
                    max_steps,max_parallelism,max_replans,max_runtime_seconds,max_model_calls,
                    step_count,replan_count,model_call_count,completion_criteria_json,
                    current_phase,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    goal_run_id,
                    root.id,
                    request.objective,
                    "planning",
                    request.autonomy_profile.value,
                    self.planner.source.value,
                    int(limits["max_steps"]),
                    int(limits["max_parallelism"]),
                    int(limits["max_replans"]),
                    int(limits["max_runtime_seconds"]),
                    int(limits["max_model_calls"]),
                    0,
                    0,
                    0,
                    json.dumps(criteria, ensure_ascii=False, separators=(",", ":")),
                    "planning",
                    now.isoformat(),
                    now.isoformat(),
                ),
            )
            await create_project_locked(db, goal_run_id, now.isoformat())
            await GoalConversationService.create_locked(
                db, goal_run_id, request.objective, now.isoformat(), history=chat_history
            )
            if request.conversation_id is not None:
                await db.execute(
                    """INSERT INTO messages(id,conversation_id,task_id,role,content,metadata_json,created_at)
                    VALUES(?,?,?,'user',?,?,?)""",
                    (
                        f"msg_{uuid4().hex}",
                        request.conversation_id,
                        root.id,
                        request.objective,
                        json.dumps({"goal_run_id": goal_run_id}),
                        now.isoformat(),
                    ),
                )
                await db.execute(
                    "UPDATE conversations SET updated_at=? WHERE id=?",
                    (now.isoformat(), request.conversation_id),
                )
                await append_audit_event(
                    db,
                    "goal.chat_context_captured",
                    {
                        "goal_run_id": goal_run_id,
                        "conversation_id": request.conversation_id,
                        "source_message_ids": [message["id"] for message in chat_history],
                        "limit": 40,
                    },
                    actor_type="device",
                    actor_id=actor_id,
                    task_id=root.id,
                    trace_id=goal_run_id,
                    created_at=now.isoformat(),
                )
            if request_key is not None:
                await db.execute(
                    """INSERT INTO idempotency_receipts(actor_id,idempotency_key,operation,request_digest,response_json,created_at,completed_at)
                    VALUES(?,?,'goal.create',?,?,?,?)""",
                    (
                        actor_id,
                        request_key,
                        request_digest,
                        json.dumps({"goal_run_id": goal_run_id}),
                        now.isoformat(),
                        now.isoformat(),
                    ),
                )
            await append_audit_event(
                db,
                "task.created",
                {"task_id": root.id, "source": actor_id, "mode": root.mode.value},
                actor_type="device",
                actor_id=actor_id,
                task_id=root.id,
                trace_id=goal_run_id,
                created_at=now.isoformat(),
            )
            await append_audit_event(
                db,
                "goal.created",
                {
                    "goal_run_id": goal_run_id,
                    "autonomy_profile": request.autonomy_profile.value,
                    "max_steps": int(limits["max_steps"]),
                    "max_parallelism": int(limits["max_parallelism"]),
                },
                actor_type="device",
                actor_id=actor_id,
                task_id=root.id,
                trace_id=goal_run_id,
                created_at=now.isoformat(),
            )
            await db.commit()
        record = await self.graph.get_goal(goal_run_id)
        if record is None:
            raise RuntimeError("created goal disappeared")
        return record

    async def list_goals(self, *, limit: int = 100) -> list[dict[str, Any]]:
        return [public_goal(goal) for goal in await self.graph.list_goals(limit=limit)]

    async def get_goal(self, goal_run_id: str) -> dict[str, Any] | None:
        goal = await self.graph.get_goal(goal_run_id)
        if goal is None:
            return None
        result = await self.graph.get_result(goal_run_id)
        if result is None and goal["status"] in self.graph.GOAL_TERMINAL:
            try:
                await self.result_aggregator.aggregate_goal(goal_run_id)
                result = await self.graph.get_result(goal_run_id)
            except (OSError, RuntimeError, TypeError, ValueError, aiosqlite.Error):
                logger.exception("could not rebuild terminal goal result: %s", goal_run_id)
        return {
            "goal": public_goal(goal),
            "nodes": [public_plan_node(node) for node in await self.graph.list_nodes(goal_run_id)],
            "result": result,
        }

    async def start_goal(
        self,
        goal_run_id: str,
        request: GoalStartRequest,
        *,
        maintenance_guard: MaintenanceLeaseGuard | None = None,
    ) -> dict[str, Any]:
        async with self._lock(goal_run_id):
            goal = await self.graph.get_goal(goal_run_id)
            if goal is None:
                raise GoalManagerConflict("goal not found")
            if goal["status"] in self.graph.GOAL_TERMINAL:
                raise GoalManagerConflict("terminal goal cannot be started")
            memory_fingerprint = request.memory_context_fingerprint
            initial_manual_revision = None
            if goal["current_phase"] == "awaiting_local_plan" and (
                request.planner_source != "iphone_local"
                or request.plan_proposal is None
                or memory_fingerprint is None
            ):
                raise GoalManagerConflict("this continuation requires its reviewed iPhone plan")
            if memory_fingerprint is not None:
                await self._assert_local_memory_current(goal_run_id, memory_fingerprint)
            else:
                await self._resume_evaluator_retry(goal_run_id, maintenance_guard=maintenance_guard)
                if (
                    request.plan_proposal is not None
                    and request.planner_source == PlannerSource.MANUAL.value
                    and goal["status"] == "planning"
                    and not await self.graph.list_nodes(goal_run_id)
                ):
                    # An explicit initial plan already supplies the routing decision.
                    # Do not invoke the automatic planner for its pending reply first.
                    # Fence this decision to the conversation observed at admission.
                    initial_manual_revision = int(goal["conversation_revision"])
                else:
                    try:
                        await self._resume_pending_conversation(
                            goal_run_id, maintenance_guard=maintenance_guard
                        )
                    except _LocalModelResourceBusy:
                        waiting = await self.get_goal(goal_run_id)
                        if waiting is None:
                            raise GoalManagerConflict(
                                "goal disappeared while waiting for local model"
                            )
                        return waiting
            goal = await self.graph.get_goal(goal_run_id)
            if goal is None:
                raise GoalManagerConflict("goal not found")
            nodes = await self.graph.list_nodes(goal_run_id)
            if initial_manual_revision is not None and (
                nodes
                or goal["status"] != "planning"
                or int(goal["conversation_revision"]) != initial_manual_revision
            ):
                raise GoalManagerConflict("goal changed before its manual plan could start")
            if memory_fingerprint is not None and (
                nodes or goal["status"] != "planning" or goal["started_at"] is not None
            ):
                raise GoalManagerConflict("goal changed before its local plan could start")
            if (
                not nodes
                and goal["status"] == "planning"
                and memory_fingerprint is None
                and request.plan_proposal is None
            ):
                goal = await self._mark_start_requested(
                    goal_run_id,
                    maintenance_guard=maintenance_guard,
                )
            if memory_fingerprint is None and self._runtime_expired(goal):
                await self._terminate_goal(
                    goal_run_id,
                    status="budget_exhausted",
                    reason="goal runtime budget exhausted",
                    maintenance_guard=maintenance_guard,
                )
                expired = await self.get_goal(goal_run_id)
                if expired is None:
                    raise RuntimeError("runtime-expired goal disappeared")
                return expired
            if not nodes:
                if goal["status"] != "planning":
                    raise GoalManagerConflict("goal plan is unavailable")
                if request.plan_proposal is None and await self._wait_for_execution_workers(
                    goal_run_id,
                    maintenance_guard=maintenance_guard,
                ):
                    waiting = await self.get_goal(goal_run_id)
                    if waiting is None:
                        raise GoalManagerConflict("goal disappeared while waiting for workers")
                    return waiting
                try:
                    proposal, source, call_id = await self._obtain_plan(
                        goal,
                        request,
                        maintenance_guard=maintenance_guard,
                    )
                except _LocalModelResourceBusy:
                    waiting = await self.get_goal(goal_run_id)
                    if waiting is None:
                        raise GoalManagerConflict("goal disappeared while waiting for local model")
                    return waiting
                refreshed_goal = await self.graph.get_goal(goal_run_id)
                if refreshed_goal is None:
                    raise GoalManagerConflict("goal disappeared while its plan was generated")
                if memory_fingerprint is None and self._runtime_expired(refreshed_goal):
                    if call_id is not None:
                        await self._finish_model_call(
                            call_id,
                            status="failed",
                            maintenance_guard=maintenance_guard,
                        )
                    await self._terminate_goal(
                        goal_run_id,
                        status="budget_exhausted",
                        reason="goal runtime budget exhausted",
                        maintenance_guard=maintenance_guard,
                    )
                    expired = await self.get_goal(goal_run_id)
                    if expired is None:
                        raise RuntimeError("runtime-expired goal disappeared")
                    return expired
                try:
                    await self._persist_initial_plan(
                        refreshed_goal,
                        proposal,
                        source=source,
                        model_call_id=call_id,
                        memory_context_fingerprint=memory_fingerprint,
                        expected_conversation_revision=initial_manual_revision,
                        maintenance_guard=maintenance_guard,
                    )
                except _PlannerProposalRejected as exc:
                    if call_id is not None:
                        await self._record_planner_failure(
                            goal_run_id,
                            call_id,
                            category="invalid_response",
                            diagnostic_code=rejection_diagnostic(exc),
                            maintenance_guard=maintenance_guard,
                        )
                    raise
                except BaseException:
                    if call_id is not None:
                        await self._finish_model_call(
                            call_id,
                            status="failed",
                            maintenance_guard=maintenance_guard,
                        )
                    raise
            elif goal["status"] not in {"running", "waiting_permission"}:
                raise GoalManagerConflict("goal cannot advance from its current state")
            await self._advance_ready(
                goal_run_id,
                explicit_user_action=True,
                maintenance_guard=maintenance_guard,
            )
            await self._drain_synthesis_nodes(
                goal_run_id,
                maintenance_guard=maintenance_guard,
            )
            await self._evaluate_if_quiescent(
                goal_run_id,
                explicit_user_action=True,
                maintenance_guard=maintenance_guard,
            )
            detail = await self.get_goal(goal_run_id)
            if detail is None:
                raise RuntimeError("started goal disappeared")
            return detail

    async def _assert_local_memory_current(
        self,
        goal_id: str,
        fingerprint: str,
        *,
        db: aiosqlite.Connection | None = None,
    ) -> None:
        if self.project_memory is None:
            raise GoalManagerConflict("project memory is unavailable")
        try:
            await self.project_memory.assert_context_current(goal_id, "planner", fingerprint, db=db)
        except ProjectMemoryConflict as exc:
            raise GoalManagerConflict(str(exc)) from exc

    async def _shared_project_memory(
        self, goal: Mapping[str, Any], purpose: Literal["planner", "evaluator"]
    ) -> tuple[dict[str, Any], ProjectMemoryContext | None]:
        if self.project_memory is None:
            return dict(goal), None
        if self.project_applications is not None and self.project_applications.context is not None:
            await self.project_applications.ensure_project(str(goal["id"]))
        try:
            receipt = await self.project_memory.retrieve_for_goal(
                str(goal["id"]), purpose, expected_goal_updated_at=str(goal["updated_at"])
            )
        except ProjectMemoryConflict as exc:
            raise _ProjectMemoryContextChanged(str(exc)) from exc
        # Retrieval may reserve one embedding call. Generation must see the
        # resulting budget, and must not consume a different conversation.
        refreshed = await self.graph.get_goal(str(goal["id"]))
        if refreshed is None or int(refreshed.get("conversation_revision") or 0) != int(
            receipt["conversation_revision"]
        ):
            raise _ProjectMemoryContextChanged("goal changed while project memory was retrieved")
        memory = ProjectMemoryContext.model_validate(
            {key: receipt[key] for key in ("mode", "reason", "items")}
        )
        return refreshed, memory

    async def _has_online_worker_locked(self, db: aiosqlite.Connection) -> bool:
        now = self.agent_dispatcher.clock()
        async with db.execute(
            "SELECT last_seen_at FROM agents WHERE status='online' "
            "AND json_array_length(skills_json)>0"
        ) as cursor:
            async for row in cursor:
                if self.agent_dispatcher.scheduler.is_fresh({"last_seen_at": row[0]}, now=now):
                    return True
        return False

    async def _wait_for_execution_workers(
        self,
        goal_run_id: str,
        *,
        maintenance_guard: MaintenanceLeaseGuard | None = None,
    ) -> bool:
        if not self.require_execution_workers:
            return False
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            if maintenance_guard is not None:
                await maintenance_guard.require_current_locked(db)
            if await self._has_online_worker_locked(db):
                await db.rollback()
                return False
            await db.execute(
                """UPDATE goal_runs SET current_phase='waiting_for_workers',
                failure_reason=?,updated_at=? WHERE id=? AND status='planning'
                AND current_phase<>'waiting_for_workers'""",
                (
                    "No execution worker is online. Waiting for a worker to connect.",
                    self._now(),
                    goal_run_id,
                ),
            )
            if maintenance_guard is not None:
                await maintenance_guard.require_current_locked(db)
            await db.commit()
        return True

    async def _mark_start_requested(
        self,
        goal_run_id: str,
        *,
        maintenance_guard: MaintenanceLeaseGuard | None = None,
    ) -> dict[str, Any]:
        """Persist the explicit start boundary before any model call.

        This marker lets crash recovery distinguish a user-created, idle goal
        from a started planning run that is safe to resume. It also starts the
        runtime budget at the command boundary, never at goal creation time.
        """

        now = self._now()
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            if maintenance_guard is not None:
                await maintenance_guard.require_current_locked(db)
            cursor = await db.execute(
                """UPDATE goal_runs
                SET started_at=COALESCE(started_at,?),
                    current_phase=CASE WHEN started_at IS NULL
                        THEN 'start_requested' ELSE current_phase END,
                    updated_at=?
                WHERE id=? AND status='planning'
                  AND NOT EXISTS (
                    SELECT 1 FROM plan_nodes WHERE goal_run_id=goal_runs.id
                  )""",
                (now, now, goal_run_id),
            )
            if cursor.rowcount != 1:
                await db.rollback()
                raise GoalManagerConflict("goal changed while start was requested")
            if maintenance_guard is not None:
                await maintenance_guard.require_current_locked(db)
            await db.commit()
        goal = await self.graph.get_goal(goal_run_id)
        if goal is None:
            raise RuntimeError("started goal disappeared")
        return goal

    async def _obtain_plan(
        self,
        goal: Mapping[str, Any],
        request: GoalStartRequest,
        *,
        user_guidance: str | None = None,
        maintenance_guard: MaintenanceLeaseGuard | None = None,
    ) -> tuple[SwarmPlanProposal, PlannerSource, str | None]:
        if request.plan_proposal is not None:
            if request.planner_source is None:  # protected by the strict request model
                raise GoalManagerConflict("manual plan source is missing")
            return request.plan_proposal, PlannerSource(request.planner_source), None
        goal_id = str(goal["id"])
        goal, project_memory = await self._shared_project_memory(goal, "planner")
        try:
            protected_cards = await continuation_cards(self.db_path, goal_id)
        except ValueError as exc:
            raise GoalManagerConflict("planner context could not preserve required input") from exc
        additional_cards: list[ContextCard] = []
        recent = [
            message
            for message in await self.recent_conversation(goal_id)
            if not (
                message["role"] == "user"
                and message["content"] == safe_context_text(str(goal["objective"]), max_chars=4_000)
            )
        ]
        if recent:
            # Reserve room for capabilities and budgets; workers receive the full 40-message window.
            summary = "\n".join(
                f"{message['role']}: {safe_context_text(message['content'], max_chars=500)}"
                for message in reversed(recent[-6:])
            )
            additional_cards.append(
                ContextCard(
                    card_id=f"conversation:{goal_id}",
                    kind="user_guidance",
                    summary=summary,
                    provenance_ids=(goal_id,),
                )
            )
        if user_guidance is not None:
            guidance = safe_context_text(user_guidance, max_chars=500)
            if guidance:
                additional_cards.append(
                    ContextCard(
                        card_id=f"user-guidance:{goal_id}",
                        kind="user_guidance",
                        summary=guidance,
                        provenance_ids=(goal_id,),
                    )
                )
        memory_executor = (
            GoalMemoryExecutor(
                self, goal_id, int(goal.get("conversation_revision") or 0), maintenance_guard
            )
            if isinstance(self.strategy_retrieval, StrategyRetrieval)
            else None
        )
        retrieval_status = "completed"
        raw_hints: dict[str, Any] = {}
        if self.strategy_retrieval is not None:
            try:
                hints = await self.strategy_retrieval.retrieve(
                    str(goal["objective"]),
                    goal_run_id=goal_id,
                    **({"model_executor": memory_executor} if memory_executor is not None else {}),
                )
                raw_hints = hints.as_dict()
            except ModelRequestBudgetUnavailable:
                # Lessons are optional, but absence must be explicit and the
                # planner still gets its reserved credit and mandatory inputs.
                retrieval_status = "budget_unavailable"
                raw_hints = {"successful": [], "failures": [], "memory": []}
                additional_cards.append(
                    ContextCard(
                        card_id="strategy:availability",
                        kind="memory_retrieval_status",
                        summary="Memory retrieval unavailable: remaining model budget is reserved for planning. No retrieved lesson is asserted.",
                        provenance_ids=(goal_id,),
                    )
                )
            except MemoryNormalizationError as exc:
                await self._record_recoverable_error(
                    goal_id,
                    f"memory_retrieval_{exc.category}",
                    conversation_revision=int(goal.get("conversation_revision") or 0),
                    maintenance_guard=maintenance_guard,
                )
                raise GoalManagerConflict(
                    f"memory retrieval {exc.category}: {exc.reason}; goal remains recoverable"
                ) from exc
            except ModelExecutionControlError as exc:
                if isinstance(exc, GoalManagerConflict):
                    raise
                raise GoalManagerConflict(
                    "memory retrieval was fenced; goal remains recoverable"
                ) from exc
            refreshed = await self.graph.get_goal(goal_id)
            if (
                refreshed is None
                or refreshed["status"] in self.graph.GOAL_TERMINAL
                or int(refreshed.get("conversation_revision") or 0)
                != int(goal.get("conversation_revision") or 0)
            ):
                raise GoalManagerConflict("goal changed during strategy retrieval")
            goal = refreshed
            if memory_executor is not None and memory_executor.failures:
                additional_cards.append(
                    ContextCard(
                        card_id="strategy:degraded",
                        kind="memory_retrieval_status",
                        summary="Some memory provider requests failed. Search used the remaining retrieval channels; no matching result is not proof that no relevant memory exists.",
                        provenance_ids=(goal_id,),
                    )
                )
            for group in ("successful", "failures", "memory"):
                values = raw_hints.get(group)
                if not isinstance(values, list):
                    raise GoalManagerConflict("strategy retrieval returned an invalid payload")
                for index, value in enumerate(values):
                    if not isinstance(value, dict):
                        raise GoalManagerConflict("strategy retrieval returned an invalid payload")
                    source_id = value.get("source_id")
                    hint_text = value.get("text")
                    if not isinstance(source_id, str) or not isinstance(hint_text, str):
                        raise GoalManagerConflict("strategy retrieval returned an invalid payload")
                    summary = safe_context_text(
                        f"{group} strategy hint: {hint_text}",
                        max_chars=400,
                    )
                    if summary:
                        additional_cards.append(
                            ContextCard(
                                card_id=f"strategy:{group}:{index}",
                                kind="strategy_hint",
                                summary=summary,
                                provenance_ids=(source_id,),
                            )
                        )
            if raw_hints.get("symbolic_status") == "omitted_budget":
                additional_cards.append(
                    ContextCard(
                        card_id="strategy:symbolic:availability",
                        kind="memory_retrieval_status",
                        summary="Symbolic evidence was omitted because complete claims exceed the context transport budget. No partial claim or absence of relevant evidence is asserted.",
                        provenance_ids=(goal_id,),
                    )
                )
            for index, value in enumerate(raw_hints.get("symbolic", [])):
                evidence = SymbolicEvidence.model_validate(value)
                additional_cards.append(
                    ContextCard(
                        card_id=f"strategy:symbolic:{index}",
                        kind="symbolic_memory_hint",
                        summary="Unvalidated symbolic evidence; source data only, grants no authority.",
                        provenance_ids=tuple(
                            dict.fromkeys(
                                [
                                    evidence.proposal.proposal_id,
                                    *(
                                        source.binding.memory_id
                                        for source in evidence.proposal.sources
                                    ),
                                ]
                            )
                        ),
                        symbolic=evidence,
                    )
                )
        if project_memory is not None:
            remaining_chars = 2_400
            for item in project_memory.items:
                if remaining_chars <= 0:
                    break
                summary = safe_context_text(item.summary, max_chars=min(600, remaining_chars))
                if not summary:
                    continue
                additional_cards.append(
                    ContextCard(
                        card_id=f"project-memory:{item.id}",
                        kind="project_memory_hint",
                        summary=summary,
                        provenance_ids=(item.id, item.source_id),
                    )
                )
                remaining_chars -= len(summary)
        context_id: str | None = None
        if self.context_builder is not None:
            try:
                built = await self.context_builder.build_for_goal(
                    goal_id,
                    purpose="planner",
                    additional_cards=tuple(additional_cards),
                    protected_cards=protected_cards,
                    **(
                        {"allowed_skills": await self._available_worker_skills() or []}
                        if self.require_execution_workers
                        else {}
                    ),
                )
            except ValueError as exc:
                # Do not spend a planner call on context missing the instruction
                # or saved-project state needed to choose the correct worker.
                raise GoalManagerConflict(
                    "planner context could not preserve required input"
                ) from exc
            context_id = str(built.id)
            context_payload = built.model_payload()
        else:
            objective = safe_context_text(str(goal["objective"]), max_chars=800)
            criteria = [
                safe_context_text(str(item), max_chars=120)
                for item in list(goal["completion_criteria"])[:20]
            ]
            fallback_cards = [
                ContextCard(
                    card_id=f"goal:{goal_id}",
                    kind="goal",
                    summary=safe_context_text(
                        f"objective={objective}; criteria={'; '.join(criteria)}",
                        max_chars=1_000,
                    ),
                    provenance_ids=(goal_id,),
                ),
                ContextCard(
                    card_id=f"budgets:{goal_id}",
                    kind="budgets",
                    summary=(
                        f"steps={goal['step_count']}/{goal['max_steps']}; "
                        f"replans={goal['replan_count']}/{goal['max_replans']}; "
                        f"model_calls={goal['model_call_count']}/{goal['max_model_calls']}; "
                        f"parallelism={goal['max_parallelism']}"
                    ),
                    provenance_ids=(goal_id,),
                ),
                *additional_cards,
                *protected_cards,
            ]
            context_payload = {
                "schema_version": "1.0",
                "purpose": "planner",
                "cards": [card.as_model_dict() for card in fallback_cards],
            }
        if memory_executor is not None:
            presented_cards = context_payload.get("cards")
            selected_card_ids = (
                {
                    str(card["card_id"])
                    for card in presented_cards
                    if isinstance(card, dict) and "card_id" in card
                }
                if isinstance(presented_cards, list)
                else set()
            )
            try:
                await memory_executor.bind_context(
                    context_id,
                    hints=raw_hints,
                    status=retrieval_status,
                    selected_card_ids=selected_card_ids,
                )
            except ModelExecutionControlError as exc:
                raise GoalManagerConflict(
                    "memory context was fenced; goal remains recoverable"
                ) from exc
        feedback = await self._planner_validation_feedback(goal)
        if feedback is not None:
            context_payload["planner_validation_feedback"] = feedback
        if self.project_applications is not None and self.project_applications.context is not None:
            durable = await self.project_applications.context.refresh(goal_id)
            context_payload["durable_project_requirements"] = (
                self.project_applications.context.prompt_state(durable)
            )
            if len(json.dumps(context_payload, ensure_ascii=False).encode()) > 22000:
                raise GoalManagerConflict(
                    "durable project requirements exceed planner context budget"
                )
        input_digest = hashlib.sha256(
            json.dumps(
                context_payload,
                allow_nan=False,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()
        try:
            call_id = await self._reserve_model_call(
                str(goal["id"]),
                role="planner",
                conversation_revision=int(goal.get("conversation_revision") or 0),
                context_id=context_id,
                input_digest=input_digest,
                provider_source=self.planner.source.value,
                model_id=getattr(self.planner, "model", None),
                maintenance_guard=maintenance_guard,
            )
        except GoalManagerConflict as exc:
            if str(exc) == "goal model call budget exhausted":
                await self._terminate_goal(
                    str(goal["id"]),
                    status="budget_exhausted",
                    reason="goal model call budget exhausted",
                    maintenance_guard=maintenance_guard,
                )
            raise
        try:
            presented_skills = (
                advertised_worker_skills(context_payload)
                if self.require_execution_workers
                else None
            )
            remaining = self._remaining_runtime_seconds(goal)
            if remaining <= 0:
                raise TimeoutError("goal runtime budget exhausted")
            if (
                isinstance(self.strategy_retrieval, StrategyRetrieval)
                and "symbolic_catalogs" in raw_hints
            ):
                selected_evidence = [
                    SymbolicEvidence.model_validate(card["symbolic"])
                    for card in context_payload.get("cards", [])
                    if "symbolic" in card
                ]
                if [
                    item.model_dump() for item in self.strategy_retrieval.symbolic_catalogs
                ] != raw_hints[
                    "symbolic_catalogs"
                ] or not await self.strategy_retrieval.revalidate_symbolic(
                    selected_evidence,
                    goal_run_id=goal_id,
                    project_id=raw_hints.get("symbolic_project_id"),
                ):
                    raise ValueError("symbolic context changed before planner admission")
            async with asyncio.timeout(remaining):
                proposal = await self.planner.propose(context_payload)
            try:
                validate_worker_capabilities(proposal.nodes, available_skills=presented_skills)
            except PlanValidationError as exc:
                raise SwarmPlannerProviderError(
                    "planner proposed a worker absent from its available capabilities",
                    category="invalid_response",
                ) from exc
        except TimeoutError as exc:
            changed = await self._record_model_timeout(
                str(goal["id"]), call_id, maintenance_guard=maintenance_guard
            )
            raise GoalManagerConflict(
                "goal runtime budget exhausted"
                if changed
                else "model call was fenced by newer input"
            ) from exc
        except SwarmPlannerProviderError as exc:
            diagnostic_code = (
                rejection_diagnostic(exc) if exc.category == "invalid_response" else None
            )
            await self._record_planner_failure(
                str(goal["id"]),
                call_id,
                category=exc.category,
                diagnostic_code=diagnostic_code,
                maintenance_guard=maintenance_guard,
            )
            reason = rejection_reason(_PLANNER_FAILURE_DETAILS[exc.category][1], diagnostic_code)
            if exc.category == "transport_unavailable":
                raise GoalManagerConflict("planner unavailable; goal remains recoverable") from exc
            raise GoalManagerConflict(f"{reason} Goal remains recoverable.") from exc
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            await self._record_planner_failure(
                str(goal["id"]),
                call_id,
                category="invalid_context",
                maintenance_guard=maintenance_guard,
            )
            raise GoalManagerConflict(
                "The planner failed internally. Goal remains recoverable."
            ) from exc
        return proposal, self.planner.source, call_id

    async def _reserve_model_call(
        self,
        goal_run_id: str,
        *,
        role: str,
        context_id: str | None,
        input_digest: str,
        provider_source: str,
        model_id: str | None = None,
        conversation_revision: int | None = None,
        evaluator_state_fingerprint: str | None = None,
        explicit_user_action: bool = False,
        reserved_followup_calls: int = 0,
        maintenance_guard: MaintenanceLeaseGuard | None = None,
    ) -> str:
        if type(reserved_followup_calls) is not int or reserved_followup_calls not in (0, 1):
            raise ValueError("invalid model followup reservation")
        call_id = f"gmc_{uuid4().hex}"
        async with model_admission_connection(
            self.db_path, self.agent_dispatcher.local_model_gpu_lock_path
        ) as (db, gpu_admission):
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            # Resource/lease expiry must be checked against time observed after
            # write-lock contention, never a timestamp sampled before it.
            now_dt = datetime.now(UTC)
            now = now_dt.isoformat()
            lease_expires_at = (
                now_dt + timedelta(seconds=self.model_call_lease_seconds)
            ).isoformat()
            if maintenance_guard is not None:
                await maintenance_guard.require_current_locked(db)
            row = await (
                await db.execute(
                    "SELECT * FROM goal_runs WHERE id=?",
                    (goal_run_id,),
                )
            ).fetchone()
            if row is None:
                await db.rollback()
                raise GoalManagerConflict("goal not found")
            if str(row["status"]) in self.graph.GOAL_TERMINAL:
                await db.rollback()
                raise GoalManagerConflict("terminal goal cannot call a model")
            if (
                conversation_revision is not None
                and int(row["conversation_revision"]) != conversation_revision
            ):
                raise GoalManagerConflict("goal conversation changed before model reservation")
            if evaluator_state_fingerprint is not None:
                if (
                    row["status"] != "running"
                    or int(row["pending_message_revision"])
                    or not await self._evaluation_state_matches_locked(
                        db, goal_run_id, evaluator_state_fingerprint
                    )
                ):
                    raise GoalManagerConflict("goal changed before evaluation reservation")
                if not explicit_user_action and await self._evaluator_cooling_down_locked(
                    db, dict(row), evaluator_state_fingerprint, now=now
                ):
                    raise GoalManagerConflict("evaluator retry cooldown is active")
            if int(row["model_call_count"]) >= int(row["max_model_calls"]):
                await db.rollback()
                if reserved_followup_calls:
                    raise ModelRequestBudgetUnavailable("memory budget preserves planner credit")
                raise GoalManagerConflict("goal model call budget exhausted")
            if reserved_followup_calls and (
                int(row["model_call_count"]) + 1 + reserved_followup_calls
                > int(row["max_model_calls"])
            ):
                raise ModelRequestBudgetUnavailable("memory budget preserves planner credit")
            if reserved_followup_calls and self._runtime_expired(dict(row)):
                raise GoalManagerConflict("goal runtime budget exhausted")
            pending = await (
                await db.execute(
                    """SELECT id,lease_expires_at FROM goal_model_calls
                    WHERE goal_run_id=? AND status='started'""",
                    (goal_run_id,),
                )
            ).fetchone()
            if pending is not None:
                pending_expiry = pending["lease_expires_at"]
                if pending_expiry is not None and str(pending_expiry) > now:
                    await db.rollback()
                    raise GoalManagerConflict("goal already has a model call in progress")
                await db.execute(
                    """UPDATE goal_model_calls SET status='failed',completed_at=?,
                    error_category='lease_expired' WHERE id=? AND status='started'""",
                    (now, str(pending["id"])),
                )
            if provider_source == PlannerSource.UBUNTU_LOCAL.value and (
                not gpu_admission.try_acquire()
                or await active_local_model_work_locked(db, now=now_dt)
            ):
                # No model call or budget credit exists yet. Reconciliation may
                # try admission later without classifying contention as failure.
                await db.rollback()
                raise _LocalModelResourceBusy("local model resource is busy")
            generation_row = await (
                await db.execute(
                    "SELECT COALESCE(MAX(lease_generation),0)+1 FROM goal_model_calls WHERE goal_run_id=?",
                    (goal_run_id,),
                )
            ).fetchone()
            generation = int(generation_row[0]) if generation_row else 1
            await db.execute(
                """
                INSERT INTO goal_model_calls(
                    id,goal_run_id,role,provider_source,model_id,context_id,input_digest,
                    output_digest,status,created_at,completed_at,error_category,
                    owner_instance_id,lease_expires_at,lease_generation,conversation_revision
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    call_id,
                    goal_run_id,
                    role,
                    provider_source,
                    model_id,
                    context_id,
                    input_digest,
                    None,
                    "started",
                    now,
                    None,
                    None,
                    self.instance_id,
                    lease_expires_at,
                    generation,
                    int(row["conversation_revision"]),
                ),
            )
            await db.execute(
                """UPDATE goal_runs SET model_call_count=model_call_count+1,
                current_phase=?,updated_at=? WHERE id=?""",
                (f"{role}_model", now, goal_run_id),
            )
            if maintenance_guard is not None:
                await maintenance_guard.require_current_locked(db)
            await db.commit()
        return call_id

    async def _finish_model_call(
        self,
        call_id: str,
        *,
        status: str,
        output_digest: str | None = None,
        error_category: str | None = None,
        maintenance_guard: MaintenanceLeaseGuard | None = None,
    ) -> bool:
        if status not in {"completed", "failed"}:
            raise ValueError("model call status must be completed or failed")
        now = self._now()
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            if maintenance_guard is not None:
                await maintenance_guard.require_current_locked(db)
            changed = await self._finish_model_call_locked(
                db,
                call_id,
                status=status,
                now=now,
                output_digest=output_digest,
                error_category=error_category,
            )
            if maintenance_guard is not None:
                await maintenance_guard.require_current_locked(db)
            await db.commit()
        return changed

    async def _finish_model_call_locked(
        self,
        db: aiosqlite.Connection,
        call_id: str,
        *,
        status: str,
        now: str,
        output_digest: str | None = None,
        error_category: str | None = None,
    ) -> bool:
        cursor = await db.execute(
            """UPDATE goal_model_calls SET status=?,completed_at=?,error_category=?,
                output_digest=COALESCE(?,output_digest),
                latency_ms=CAST(MAX(0,
                    (julianday(?) - julianday(created_at)) * 86400000
                ) AS INTEGER)
            WHERE id=? AND status='started' AND owner_instance_id=?
              AND lease_expires_at>?""",
            (
                status,
                now,
                (error_category or "provider_unavailable") if status == "failed" else None,
                output_digest,
                now,
                call_id,
                self.instance_id,
                now,
            ),
        )
        return cursor.rowcount == 1

    async def _planner_validation_feedback(self, goal: Mapping[str, Any]) -> dict[str, str] | None:
        """Return a server-authored hint for the latest failure of this conversation.

        Does not retry or modify budgets. Old-conversation, successful, unclassified
        and transport failures cannot supply guidance for a later planning request.
        """
        async with aiosqlite.connect(self.db_path) as db:
            call = await (
                await db.execute(
                    """SELECT id,status,error_category FROM goal_model_calls
                WHERE goal_run_id=? AND role='planner' AND conversation_revision=?
                ORDER BY lease_generation DESC,created_at DESC LIMIT 1""",
                    (goal["id"], int(goal.get("conversation_revision") or 0)),
                )
            ).fetchone()
            if call is None or tuple(call[1:]) != ("failed", "invalid_response"):
                return None
            row = await (
                await db.execute(
                    """SELECT payload_json FROM audit_events WHERE trace_id=?
                AND event_type='goal.plan.rejected'
                AND json_extract(payload_json,'$.model_call_id')=? ORDER BY id DESC LIMIT 1""",
                    (goal["id"], call[0]),
                )
            ).fetchone()
        if row is None:
            return None
        try:
            payload = json.loads(row[0])
        except (TypeError, ValueError):
            return None
        if not isinstance(payload, dict):
            return None
        code = known_diagnostic(payload.get("validation_code"))
        if code is None:
            return None
        return {"code": code, "instruction": DIAGNOSTICS[code][1]}

    async def _record_planner_failure(
        self,
        goal_run_id: str,
        call_id: str,
        *,
        category: str,
        diagnostic_code: str | None = None,
        maintenance_guard: MaintenanceLeaseGuard | None = None,
    ) -> bool:
        phase, reason = _PLANNER_FAILURE_DETAILS[category]
        code = known_diagnostic(diagnostic_code) if category == "invalid_response" else None
        reason = rejection_reason(reason, code)
        now = self._now()
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            if maintenance_guard is not None:
                await maintenance_guard.require_current_locked(db)
            call = await (
                await db.execute(
                    """SELECT c.conversation_revision,g.root_task_id,g.conversation_revision,g.status
                    FROM goal_model_calls c
                    JOIN goal_runs g ON g.id=c.goal_run_id
                    WHERE c.id=? AND c.goal_run_id=? AND c.role='planner'""",
                    (call_id, goal_run_id),
                )
            ).fetchone()
            if call is None:
                await db.rollback()
                raise GoalManagerConflict("planner call does not belong to this goal")
            if call[0] != call[2] or call[3] in self.graph.GOAL_TERMINAL:
                await db.rollback()
                return False
            changed = await self._finish_model_call_locked(
                db,
                call_id,
                status="failed",
                now=now,
                error_category=category,
            )
            if changed:
                await db.execute(
                    """UPDATE goal_runs SET current_phase=?,failure_reason=?,updated_at=?
                    WHERE id=? AND status NOT IN
                        ('completed','failed','cancelled','budget_exhausted')""",
                    (phase, reason, now, goal_run_id),
                )
                await append_audit_event(
                    db,
                    "goal.plan.rejected",
                    {
                        "goal_run_id": goal_run_id,
                        "model_call_id": call_id,
                        "conversation_revision": call[0],
                        "category": category,
                        "validation_code": code,
                    },
                    actor_type="control-plane",
                    actor_id="goal-manager",
                    task_id=call[1],
                    trace_id=goal_run_id,
                    created_at=now,
                )
            if maintenance_guard is not None:
                await maintenance_guard.require_current_locked(db)
            await db.commit()
        return changed

    async def _require_current_model_call_locked(
        self,
        db: aiosqlite.Connection,
        call_id: str,
        *,
        now: str,
    ) -> None:
        current = await (
            await db.execute(
                """SELECT 1 FROM goal_model_calls c JOIN goal_runs g ON g.id=c.goal_run_id
                WHERE c.id=? AND c.status='started' AND c.owner_instance_id=? AND c.lease_expires_at>?
                AND c.conversation_revision=g.conversation_revision""",
                (call_id, self.instance_id, now),
            )
        ).fetchone()
        if current is None:
            raise GoalManagerConflict("model call lease expired or was fenced")

    @staticmethod
    async def _memory_retrieval_cooling_down_locked(
        db: aiosqlite.Connection, goal: Mapping[str, Any], *, now: str
    ) -> bool:
        if goal.get("current_phase") not in _MEMORY_RETRIEVAL_FAILURE_PHASES:
            return False
        row = await (
            await db.execute(
                """SELECT 1 FROM audit_events WHERE trace_id=?
            AND event_type='goal.memory.retrieval.failed'
            AND json_extract(payload_json,'$.conversation_revision')=?
            AND julianday(created_at)+?/86400.0>julianday(?) LIMIT 1""",
                (
                    goal["id"],
                    int(goal.get("conversation_revision") or 0),
                    _PLANNER_RETRY_COOLDOWN_SECONDS,
                    now,
                ),
            )
        ).fetchone()
        return row is not None

    async def _record_recoverable_error(
        self,
        goal_run_id: str,
        phase: str,
        *,
        conversation_revision: int | None = None,
        maintenance_guard: MaintenanceLeaseGuard | None = None,
    ) -> None:
        now = self._now()
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            if maintenance_guard is not None:
                await maintenance_guard.require_current_locked(db)
            changed = await db.execute(
                """UPDATE goal_runs SET current_phase=?,failure_reason=?,updated_at=?
                WHERE id=? AND status NOT IN ('completed','failed','cancelled','budget_exhausted')
                AND (? IS NULL OR conversation_revision=?)""",
                (
                    phase,
                    phase.replace("_", " "),
                    now,
                    goal_run_id,
                    conversation_revision,
                    conversation_revision,
                ),
            )
            if changed.rowcount == 1 and phase in _MEMORY_RETRIEVAL_FAILURE_PHASES:
                await append_audit_event(
                    db,
                    "goal.memory.retrieval.failed",
                    {
                        "goal_run_id": goal_run_id,
                        "conversation_revision": conversation_revision,
                        "phase": phase,
                    },
                    trace_id=goal_run_id,
                    created_at=now,
                )
            if maintenance_guard is not None:
                await maintenance_guard.require_current_locked(db)
            await db.commit()

    async def _record_model_timeout(
        self,
        goal_id: str,
        call_id: str,
        *,
        maintenance_guard: MaintenanceLeaseGuard | None = None,
    ) -> bool:
        """A timed-out older conversation cannot terminate its replacement."""
        now = self._now()
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            if maintenance_guard is not None:
                await maintenance_guard.require_current_locked(db)
            current = await (
                await db.execute(
                    """SELECT g.* FROM goal_runs g JOIN goal_model_calls c ON c.goal_run_id=g.id
                WHERE g.id=? AND c.id=? AND c.status='started' AND c.owner_instance_id=?
                AND c.conversation_revision=g.conversation_revision
                AND g.status NOT IN ('completed','failed','cancelled','budget_exhausted')""",
                    (goal_id, call_id, self.instance_id),
                )
            ).fetchone()
            if current is None:
                return False
            await db.execute(
                "UPDATE goal_model_calls SET status='failed',completed_at=?,error_category='runtime_exhausted' WHERE id=?",
                (now, call_id),
            )
            await self._terminate_goal_locked(
                db,
                dict(current),
                status="budget_exhausted",
                reason="goal runtime budget exhausted",
                now=now,
                maintenance_guard=maintenance_guard,
            )
            await db.commit()
        await self._finalize_terminal_goal(
            goal_id, status="budget_exhausted", maintenance_guard=maintenance_guard
        )
        return True

    async def _persist_initial_plan(
        self,
        goal: Mapping[str, Any],
        proposal: SwarmPlanProposal,
        *,
        source: PlannerSource,
        model_call_id: str | None,
        memory_context_fingerprint: str | None = None,
        expected_conversation_revision: int | None = None,
        maintenance_guard: MaintenanceLeaseGuard | None = None,
    ) -> None:
        output_digest = self._model_output_digest(proposal.model_dump(mode="json"))
        proposal = self._bind_plan_to_goal(goal, proposal, model_call_id=model_call_id)
        proposal = await self._bind_research_source_requirements(goal, proposal)
        if len(proposal.nodes) > int(goal["max_steps"]):
            raise _PlannerProposalRejected(
                "plan exceeds the goal step budget", diagnostic_code="step_budget"
            )
        try:
            validated = validate_swarm_plan(
                proposal,
                policy=self.permission_policy,
                max_nodes=int(goal["max_steps"]),
                max_parallelism=int(goal["max_parallelism"]),
            )
        except PlanValidationError as exc:
            raise _PlannerProposalRejected(str(exc)) from exc
        by_temp = {node.temporary_id: f"node_{uuid4().hex}" for node in proposal.nodes}
        now = self._now()
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            if maintenance_guard is not None:
                await maintenance_guard.require_current_locked(db)
            if memory_context_fingerprint is not None:
                await self._assert_local_memory_current(
                    str(goal["id"]), memory_context_fingerprint, db=db
                )
            await self._require_current_worker_capabilities_locked(db, proposal.nodes)
            current = await (
                await db.execute("SELECT * FROM goal_runs WHERE id=?", (goal["id"],))
            ).fetchone()
            if current is None or str(current["status"]) != "planning":
                await db.rollback()
                raise GoalManagerConflict("goal changed while its plan was generated")
            if (
                expected_conversation_revision is not None
                and int(current["conversation_revision"]) != expected_conversation_revision
            ):
                await db.rollback()
                raise GoalManagerConflict(
                    "conversation changed while its manual plan was validated"
                )
            if self._runtime_expired(dict(current)):
                await db.rollback()
                raise GoalManagerConflict("goal runtime budget exhausted")
            if model_call_id is not None:
                await self._require_current_model_call_locked(db, model_call_id, now=now)
            existing = await (
                await db.execute(
                    "SELECT 1 FROM plan_nodes WHERE goal_run_id=? LIMIT 1", (goal["id"],)
                )
            ).fetchone()
            if existing is not None:
                await db.rollback()
                raise GoalManagerConflict("goal already has a persisted plan")
            previous = current["plan_fingerprint"]
            if previous is not None and str(previous) == validated.fingerprint:
                await db.rollback()
                raise GoalManagerConflict("planner proposed an equivalent plan")
            for node in proposal.nodes:
                node_id = by_temp[node.temporary_id]
                hard_dependencies = sorted(by_temp[item] for item in node.dependencies)
                optional_dependencies = sorted(by_temp[item] for item in node.optional_dependencies)
                dependencies = sorted(set(hard_dependencies + optional_dependencies))
                metadata = {
                    "worker_arguments": node.worker_arguments,
                    "schema_version": proposal.schema_version,
                    "temporary_id": node.temporary_id,
                    "preferred_agent_constraints": (
                        node.preferred_agent_constraints.model_dump(mode="json")
                        if node.preferred_agent_constraints is not None
                        else None
                    ),
                }
                await db.execute(
                    """
                    INSERT INTO plan_nodes(
                        id,goal_run_id,parent_node_id,node_type,title,objective,required_skill,
                        status,priority,depends_on_json,expected_output,planner_metadata_json,
                        created_at,updated_at,conversation_revision
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        node_id,
                        goal["id"],
                        None,
                        node.node_type.value,
                        node.title,
                        node.objective,
                        node.required_skill,
                        "planned",
                        node.priority,
                        json.dumps(dependencies, separators=(",", ":")),
                        node.expected_output,
                        json.dumps(metadata, separators=(",", ":"), sort_keys=True),
                        now,
                        now,
                        int(current["conversation_revision"] or 0),
                    ),
                )
                for dependency in hard_dependencies:
                    await db.execute(
                        """INSERT INTO plan_edges(
                        goal_run_id,from_node_id,to_node_id,dependency_type
                        ) VALUES(?,?,?,'hard')""",
                        (goal["id"], dependency, node_id),
                    )
                for dependency in optional_dependencies:
                    await db.execute(
                        """INSERT INTO plan_edges(
                        goal_run_id,from_node_id,to_node_id,dependency_type
                        ) VALUES(?,?,?,'optional')""",
                        (goal["id"], dependency, node_id),
                    )
            await db.execute(
                """
                UPDATE goal_runs SET status='running',planner_source=?,plan_fingerprint=?,
                    max_parallelism=MIN(max_parallelism,?),
                    current_phase='dispatching',started_at=COALESCE(started_at,?),
                    failure_reason=NULL,pending_message_revision=0,updated_at=?
                WHERE id=? AND status='planning'
                """,
                (
                    source.value,
                    validated.fingerprint,
                    proposal.max_parallelism,
                    now,
                    now,
                    goal["id"],
                ),
            )
            root_task_id = str(current["root_task_id"])
            root = await (
                await db.execute("SELECT status FROM tasks WHERE id=?", (root_task_id,))
            ).fetchone()
            if root is None or str(root["status"]) != "planned":
                await db.rollback()
                raise GoalManagerConflict("goal root task is not dispatchable")
            await db.execute(
                """UPDATE tasks SET status='running',updated_at=?
                WHERE id=? AND status='planned'""",
                (now, root_task_id),
            )
            await append_audit_event(
                db,
                "goal.plan.accepted",
                {
                    "goal_run_id": goal["id"],
                    "planner_source": source.value,
                    "node_count": len(proposal.nodes),
                    "plan_fingerprint": validated.fingerprint,
                    "memory_context_fingerprint": memory_context_fingerprint,
                    "rationale_summary": (redact_dataset_text(proposal.rationale_summary) or "")[
                        :4_000
                    ],
                    "node_ids": list(by_temp.values()),
                    "model_call_id": model_call_id,
                    "conversation_revision": int(current["conversation_revision"]),
                },
                actor_type="control-plane",
                actor_id="goal-manager",
                task_id=root_task_id,
                trace_id=str(goal["id"]),
                created_at=now,
            )
            if model_call_id is not None:
                cursor = await db.execute(
                    """UPDATE goal_model_calls SET status='completed',completed_at=?,
                    output_digest=?,latency_ms=CAST(MAX(0,
                        (julianday(?) - julianday(created_at)) * 86400000
                    ) AS INTEGER)
                    WHERE id=? AND status='started' AND owner_instance_id=?
                      AND lease_expires_at>?""",
                    (
                        now,
                        output_digest,
                        now,
                        model_call_id,
                        self.instance_id,
                        now,
                    ),
                )
                if cursor.rowcount != 1:
                    await db.rollback()
                    raise GoalManagerConflict("planner model call lease was fenced")
            if maintenance_guard is not None:
                await maintenance_guard.require_current_locked(db)
            await db.commit()
        await self.graph.refresh_ready_nodes(
            str(goal["id"]),
            maintenance_guard=maintenance_guard,
        )

    async def _advance_ready(
        self,
        goal_run_id: str,
        *,
        explicit_user_action: bool,
        maintenance_guard: MaintenanceLeaseGuard | None = None,
    ) -> None:
        await self._resume_pending_conversation(goal_run_id, maintenance_guard=maintenance_guard)
        await self.graph.refresh_ready_nodes(
            goal_run_id,
            maintenance_guard=maintenance_guard,
        )
        goal = await self.graph.get_goal(goal_run_id)
        if goal is None or goal["status"] != "running":
            return
        if int(goal.get("pending_message_revision") or 0):
            # A failed/cooling-down routing attempt must not silently execute an
            # older ready plan and consume the user's pending instruction.
            return
        if self._runtime_expired(goal):
            await self._terminate_goal(
                goal_run_id,
                status="budget_exhausted",
                reason="goal runtime budget exhausted",
                maintenance_guard=maintenance_guard,
            )
            return
        nodes = await self.graph.list_nodes(goal_run_id)
        active_count = sum(node["status"] in self.ACTIVE_NODE_STATUSES for node in nodes)
        available = max(0, int(goal["max_parallelism"]) - active_count)
        if goal["autonomy_profile"] == AutonomyProfile.MANUAL.value:
            available = min(
                available, 1 if explicit_user_action or goal.get("reply_dispatch_credit") else 0
            )
        if available <= 0:
            return
        ready = [node for node in nodes if node["status"] == "ready"]
        for node in ready[:available]:
            if node["node_type"] == PlanNodeType.SYNTHESIS.value:
                await self._complete_deterministic_synthesis(
                    node,
                    maintenance_guard=maintenance_guard,
                )
                continue
            await self._dispatch_worker_node(
                goal,
                node,
                maintenance_guard=maintenance_guard,
            )
        await self.graph.refresh_ready_nodes(
            goal_run_id,
            maintenance_guard=maintenance_guard,
        )

    @staticmethod
    def _completed_input_summary(record: Mapping[str, Any]) -> str:
        if record["status"] != "completed":
            return ""
        # Older releases persisted this diagnostic as successful output.
        # It must not become evidence through a dependent synthesis.
        return "\n".join(
            line
            for line in str(record.get("result_summary") or "").splitlines()
            if line.strip() != "No evidence summary was available for synthesis."
        ).strip()

    async def _complete_deterministic_synthesis(
        self,
        node: Mapping[str, Any],
        *,
        maintenance_guard: MaintenanceLeaseGuard | None = None,
    ) -> None:
        dependencies = list(node.get("depends_on") or [])
        summaries: list[str] = []
        for dependency in dependencies:
            record = await self.graph.get_node(str(dependency))
            if record is not None:
                evidence = self._completed_input_summary(record)
                if evidence:
                    summaries.append(evidence[:2_000])
        summary = "\n".join(summaries)[:4_000]
        if not summary:
            await self.graph.transition_node(
                str(node["id"]),
                expected="ready",
                target="skipped",
                error_summary="Synthesis skipped because no completed input provided evidence.",
                maintenance_guard=maintenance_guard,
            )
            return
        try:
            await self.graph.complete_synthesis_node(
                str(node["id"]),
                result_summary=summary,
                maintenance_guard=maintenance_guard,
            )
        except GoalStateConflict as exc:
            if "step budget" in str(exc):
                raise GoalManagerConflict("goal step budget exhausted") from exc
            raise

    @staticmethod
    def _payload_for_node(node: Mapping[str, Any]) -> dict[str, Any]:
        skill = str(node["required_skill"])
        metadata = node.get("planner_metadata")
        if metadata is None:
            metadata = json.loads(str(node.get("planner_metadata_json") or "{}"))
        if isinstance(metadata, dict) and metadata.get("worker_arguments") is not None:
            try:
                return validate_remote_job(skill, metadata["worker_arguments"])
            except RemoteJobPolicyError as exc:
                raise GoalManagerConflict("specialist arguments rejected before dispatch") from exc
        objective = str(node["objective"])
        if skill == "workspace.list_dir":
            payload: dict[str, Any] = {"path": "."}
        elif skill == "research.query":
            payload = {"query": objective[:2_000], "max_results": 5}
        elif skill == WRITING_SKILL:
            payload = writing_payload(objective, [])
        elif skill == CODE_PROPOSAL_SKILL:
            payload = {"objective": safe_context_text(objective, max_chars=4_000)}
        elif skill == "code_review.git_status":
            payload = {}
        elif skill == "code_review.git_diff":
            payload = {"paths": [], "staged": False, "context_lines": 3}
        elif skill == "code_review.git_show":
            payload = {"revision": "HEAD", "paths": [], "context_lines": 3}
        else:
            raise GoalManagerConflict(
                f"skill {skill} requires explicit bounded parameters before dispatch"
            )
        try:
            return validate_remote_job(skill, payload)
        except RemoteJobPolicyError as exc:
            raise GoalManagerConflict("planner node could not produce a policy-safe job") from exc

    async def _dispatch_worker_node(
        self,
        goal: Mapping[str, Any],
        node: Mapping[str, Any],
        *,
        maintenance_guard: MaintenanceLeaseGuard | None = None,
    ) -> None:
        if node["required_skill"] == CODE_PROPOSAL_SKILL and self.code_applications is None:
            await self.graph.transition_node(
                str(node["id"]),
                expected="ready",
                target="blocked",
                error_summary="The local code application gateway is unavailable.",
                maintenance_guard=maintenance_guard,
            )
            return
        try:
            payload = await self._worker_payload(goal, node)
        except GoalManagerConflict as exc:
            await self.graph.transition_node(
                str(node["id"]),
                expected="ready",
                target="blocked",
                error_summary=str(exc),
                maintenance_guard=maintenance_guard,
            )
            return
        child = TaskRecord.new(
            TaskCreate(input=str(node["objective"]), mode=TaskMode.REVIEW),
            source=f"goal:{goal['id']}",
        )
        now = self._now()
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            if maintenance_guard is not None:
                await maintenance_guard.require_current_locked(db)
            current = await (
                await db.execute(
                    "SELECT status FROM plan_nodes WHERE id=? AND goal_run_id=?",
                    (node["id"], goal["id"]),
                )
            ).fetchone()
            if current is None or str(current[0]) != "ready":
                await db.rollback()
                return
            usage = await (
                await db.execute(
                    """SELECT g.step_count,g.max_steps,g.max_parallelism,
                        (SELECT COUNT(*) FROM plan_nodes AS active
                         WHERE active.goal_run_id=g.id AND active.status IN
                           ('dispatched','running','waiting_permission','waiting_capability'))
                           AS active_count,g.model_call_count,g.max_model_calls,g.conversation_revision
                    FROM goal_runs AS g WHERE g.id=? AND g.status='running'""",
                    (goal["id"],),
                )
            ).fetchone()
            if usage is None:
                await db.rollback()
                return
            if int(usage[0]) >= int(usage[1]):
                await db.rollback()
                raise GoalManagerConflict("goal step budget exhausted")
            if int(usage[3]) >= int(usage[2]):
                await db.rollback()
                return
            if int(usage[6]) != int(goal.get("conversation_revision") or 0):
                await db.rollback()
                return
            if (
                node["required_skill"]
                in {CODE_PROPOSAL_SKILL, PROJECT_SKILL, WRITING_SKILL} | MEDIA_SKILLS
            ):
                if int(usage[4]) >= int(usage[5]):
                    await db.rollback()
                    await self._terminate_goal(
                        str(goal["id"]),
                        status="budget_exhausted",
                        reason="goal model call budget exhausted",
                        maintenance_guard=maintenance_guard,
                    )
                    return
                await db.execute(
                    "UPDATE goal_runs SET model_call_count=model_call_count+1 WHERE id=?",
                    (goal["id"],),
                )
                await append_audit_event(
                    db,
                    "goal.writing.reserved"
                    if node["required_skill"] == WRITING_SKILL
                    else "goal.codegen.reserved",
                    {
                        "goal_run_id": goal["id"],
                        "node_id": node["id"],
                        "required_skill": node["required_skill"],
                        "model_calls_reserved": 1,
                    },
                    actor_type="control-plane",
                    actor_id="goal-manager",
                    task_id=child.id,
                    trace_id=str(goal["id"]),
                    created_at=now,
                )
            await StateService._insert_task(db, child)
            cursor = await db.execute(
                """UPDATE plan_nodes SET status='dispatched',task_id=?,updated_at=?,conversation_revision=?
                WHERE id=? AND status='ready'""",
                (child.id, now, int(usage[6]), node["id"]),
            )
            if cursor.rowcount != 1:
                await db.rollback()
                return
            await db.execute(
                """UPDATE goal_runs SET step_count=step_count+1,updated_at=?,
                pending_message_revision=0,reply_dispatch_credit=0 WHERE id=?""",
                (now, goal["id"]),
            )
            await append_audit_event(
                db,
                "goal.node.dispatched",
                {
                    "goal_run_id": goal["id"],
                    "node_id": node["id"],
                    "required_skill": node["required_skill"],
                },
                actor_type="control-plane",
                actor_id="goal-manager",
                task_id=child.id,
                trace_id=str(goal["id"]),
                created_at=now,
            )
            if maintenance_guard is not None:
                await maintenance_guard.require_current_locked(db)
            await db.commit()
        try:
            job = await self.agent_dispatcher.queue_job(
                child.id,
                str(node["required_skill"]),
                payload,
                maintenance_guard=maintenance_guard,
            )
        except AgentDispatchConflict as exc:
            await self._fail_dispatched_node(
                str(node["id"]),
                child.id,
                str(exc),
                maintenance_guard=maintenance_guard,
            )
            return
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            if maintenance_guard is not None:
                await maintenance_guard.require_current_locked(db)
            # Recovery may already have linked or claimed this same active job.
            # Accept that identical binding while still fencing cancellation or replacement.
            cursor = await db.execute(
                """UPDATE plan_nodes SET worker_job_id=?,updated_at=?
                WHERE id=? AND goal_run_id=? AND task_id=?
                  AND status IN ('dispatched','running')
                  AND (worker_job_id IS NULL OR worker_job_id=?)
                  AND EXISTS (SELECT 1 FROM goal_runs g WHERE g.id=plan_nodes.goal_run_id
                              AND g.status IN ('running','waiting_permission'))
                  AND EXISTS (SELECT 1 FROM agent_jobs j JOIN tasks t ON t.id=j.task_id
                              WHERE j.id=? AND j.task_id=?
                                AND j.status IN ('queued','claimed','running')
                                AND t.status IN ('queued','running'))""",
                (
                    job["id"],
                    self._now(),
                    node["id"],
                    goal["id"],
                    child.id,
                    job["id"],
                    job["id"],
                    child.id,
                ),
            )
            if maintenance_guard is not None:
                await maintenance_guard.require_current_locked(db)
            await db.commit()
        if cursor.rowcount != 1:
            # Cancellation may have won after queue_job committed. The child
            # lease/job is authoritative, so fence it immediately rather than
            # leaving work detached from the now-terminal plan node.
            task = await self.state_service.get_task(child.id)
            if task is not None and task.status.value not in {"completed", "failed", "cancelled"}:
                try:
                    await self.state_service.cancel_task(
                        child.id,
                        actor_id="goal-manager",
                        maintenance_guard=maintenance_guard,
                    )
                except RuntimeError:
                    refreshed = await self.state_service.get_task(child.id)
                    if refreshed is None or refreshed.status.value not in {
                        "completed",
                        "failed",
                        "cancelled",
                    }:
                        raise

    async def _fail_dispatched_node(
        self,
        node_id: str,
        task_id: str,
        error: str,
        *,
        maintenance_guard: MaintenanceLeaseGuard | None = None,
    ) -> None:
        now = self._now()
        safe_error = (redact_dataset_text(error) or "remote dispatch failed")[:500]
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            if maintenance_guard is not None:
                await maintenance_guard.require_current_locked(db)
            await db.execute(
                """UPDATE plan_nodes SET status='failed',error_summary=?,updated_at=?,completed_at=?
                WHERE id=? AND status='dispatched'""",
                (safe_error, now, now, node_id),
            )
            await db.execute(
                """UPDATE tasks SET status='failed',error_json=?,updated_at=?,completed_at=?
                WHERE id=? AND status='created'""",
                (json.dumps({"message": "remote dispatch rejected"}), now, now, task_id),
            )
            if maintenance_guard is not None:
                await maintenance_guard.require_current_locked(db)
            await db.commit()

    async def on_job_claimed(
        self,
        job: Mapping[str, Any],
        *,
        maintenance_guard: MaintenanceLeaseGuard | None = None,
    ) -> dict[str, Any] | None:
        """Project a server-observed lease claim into its plan node."""

        node = await self.graph.node_for_job(str(job["id"]))
        if node is None:
            return None
        goal = await self.graph.get_goal(str(node["goal_run_id"]))
        if goal is None or goal["status"] in self.graph.GOAL_TERMINAL:
            return None
        if node["status"] == "dispatched":
            try:
                await self.graph.transition_node(
                    str(node["id"]),
                    expected="dispatched",
                    target="running",
                    assigned_agent_id=(str(job["claimed_by"]) if job.get("claimed_by") else None),
                    maintenance_guard=maintenance_guard,
                )
            except GoalStateConflict:
                return None
        detail = await self.get_goal(str(node["goal_run_id"]))
        return detail

    async def on_job_result(
        self,
        job: Mapping[str, Any],
        *,
        maintenance_guard: MaintenanceLeaseGuard | None = None,
    ) -> dict[str, Any] | None:
        """Accept only the authoritative terminal job recorded by AgentDispatcher."""

        if job.get("status") not in {"completed", "failed", "cancelled", "quarantined"}:
            raise GoalManagerConflict("agent job result is not terminal")
        node = await self.graph.node_for_job(str(job["id"]))
        if node is None:
            return None
        goal_run_id = str(node["goal_run_id"])
        async with self._lock(goal_run_id):
            goal = await self.graph.get_goal(goal_run_id)
            if goal is None or goal["status"] in self.graph.GOAL_TERMINAL:
                return await self.get_goal(goal_run_id)
            node = await self.graph.get_node(str(node["id"]))
            if node is None:
                return None
            if node["status"] == "waiting_permission" and node["required_skill"] in {
                CODE_PROPOSAL_SKILL,
                PROJECT_SKILL,
            }:
                return await self.get_goal(goal_run_id)
            if node["status"] not in {"dispatched", "running", "waiting_capability"}:
                if node["status"] in self.graph.NODE_TERMINAL:
                    return await self.get_goal(goal_run_id)
                raise GoalManagerConflict("plan node is not accepting a worker result")
            if (
                node["required_skill"] == PROJECT_SKILL
                and job["status"] == "completed"
                and self.project_applications
            ):
                try:
                    await self._accept_project_result(
                        goal, node, job, maintenance_guard=maintenance_guard
                    )
                except (GoalProjectConflict, ValueError):
                    await self.graph.transition_node(
                        str(node["id"]),
                        expected=str(node["status"]),
                        target="failed",
                        error_summary="The project result did not pass server validation.",
                        maintenance_guard=maintenance_guard,
                    )
                    await self._evaluate_if_quiescent(
                        goal_run_id, maintenance_guard=maintenance_guard
                    )
                return await self.get_goal(goal_run_id)
            if (
                node["required_skill"] in SWIFT_SKILLS | MEDIA_SKILLS
                or node["required_skill"] == WRITING_SKILL
            ):
                recorded_job = await self.agent_dispatcher.get_job(str(job["id"]))
                if (
                    recorded_job is None
                    or recorded_job["status"]
                    not in {"completed", "failed", "cancelled", "quarantined"}
                    or recorded_job["task_id"] != node["task_id"]
                    or recorded_job["required_skill"] != node["required_skill"]
                ):
                    return await self.get_goal(goal_run_id)
                job = recorded_job
            if (
                node["required_skill"] == WRITING_SKILL
                and job["status"] == "failed"
                and isinstance(job.get("result"), dict)
                and isinstance(job["result"].get("outcome"), str)
                and job["result"].get("outcome")
                in {"declined", "needs_clarification", "insufficient_sources"}
            ):
                await self._accept_writing_non_delivery_result(
                    goal_run_id,
                    str(node["id"]),
                    str(job["id"]),
                    maintenance_guard=maintenance_guard,
                )
                return await self.get_goal(goal_run_id)
            valid_evidence = validate_worker_evidence(
                node.get("required_skill"),
                job.get("result"),
            )
            if node["required_skill"] in MEDIA_SKILLS and job["status"] == "completed":
                try:
                    verify_media_result(media_root(self.db_path), dict(job), job.get("result"))
                except MediaConflict:
                    valid_evidence = False
            if node["required_skill"] == WRITING_SKILL and job["status"] == "completed":
                try:
                    validate_writing_result(job.get("result"), payload=job.get("payload"))
                except (TypeError, ValueError):
                    valid_evidence = False
            if node["required_skill"] in SWIFT_SKILLS:
                valid_evidence = valid_evidence and valid_swift_receipt(
                    str(node["required_skill"]), job.get("result"), job.get("payload")
                )
            if node["required_skill"] == "research.collect":
                valid_evidence = valid_evidence and valid_research_collect_receipt(
                    job.get("result"), job.get("payload")
                )
            if (
                node["required_skill"] == CODE_PROPOSAL_SKILL
                and job["status"] == "completed"
                and valid_evidence
                and self.code_applications is not None
            ):
                try:
                    await self.code_applications.capture_result(
                        goal_run_id,
                        str(node["id"]),
                        str(job["id"]),
                        maintenance_guard=maintenance_guard,
                    )
                except GoalCodeApplicationConflict:
                    valid_evidence = False
                else:
                    return await self.get_goal(goal_run_id)
            target = "completed" if job["status"] == "completed" and valid_evidence else "failed"
            result_summary = (
                summarize_untrusted_worker_output(job.get("result"))
                if target == "completed"
                else None
            )
            error_summary = (
                (
                    "worker completed without valid skill evidence"
                    if job["status"] == "completed" and not valid_evidence
                    else str(job.get("error") or job.get("last_failure_reason") or "worker failed")[
                        :500
                    ]
                )
                if target == "failed"
                else None
            )
            await self.graph.transition_node(
                str(node["id"]),
                expected=str(node["status"]),
                target=target,
                result_summary=result_summary,
                error_summary=error_summary,
                assigned_agent_id=(
                    str(job["claimed_by"] or job.get("last_agent_id"))
                    if job.get("claimed_by") or job.get("last_agent_id")
                    else None
                ),
                maintenance_guard=maintenance_guard,
            )
            await self._advance_ready(
                goal_run_id,
                explicit_user_action=False,
                maintenance_guard=maintenance_guard,
            )
            await self._drain_synthesis_nodes(
                goal_run_id,
                maintenance_guard=maintenance_guard,
            )
            await self._evaluate_if_quiescent(
                goal_run_id,
                maintenance_guard=maintenance_guard,
            )
            return await self.get_goal(goal_run_id)

    async def _accept_writing_non_delivery_result(
        self,
        goal_id: str,
        node_id: str,
        job_id: str,
        *,
        maintenance_guard: MaintenanceLeaseGuard | None = None,
    ) -> None:
        """Project a durable non-delivery without evaluating it as a document."""
        terminated = False
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            if maintenance_guard is not None:
                await maintenance_guard.require_current_locked(db)
            goal = await (
                await db.execute("SELECT * FROM goal_runs WHERE id=?", (goal_id,))
            ).fetchone()
            if goal is None or goal["status"] in self.graph.GOAL_TERMINAL:
                return
            row = await (
                await db.execute(
                    """SELECT n.status,n.conversation_revision,j.result_json,j.payload_json,j.error,
                              j.claimed_by,j.last_agent_id
                    FROM plan_nodes n JOIN agent_jobs j
                      ON j.id=n.worker_job_id AND j.task_id=n.task_id
                    JOIN tasks t ON t.id=j.task_id AND t.source=? AND t.status='failed'
                    WHERE n.id=? AND n.goal_run_id=? AND n.required_skill=?
                      AND j.id=? AND j.required_skill=? AND j.status='failed'
                      AND j.error IN ('model_declined','writing_needs_clarification','writing_insufficient_sources')""",
                    (f"goal:{goal_id}", node_id, goal_id, WRITING_SKILL, job_id, WRITING_SKILL),
                )
            ).fetchone()
            if row is None:
                raise GoalManagerConflict("writing refusal evidence is unavailable")
            if row["status"] not in {"dispatched", "running", "waiting_capability"}:
                return
            try:
                result = validate_writing_non_delivery_result(
                    json.loads(str(row["result_json"])),
                    payload=json.loads(str(row["payload_json"])),
                )
            except (TypeError, ValueError) as exc:
                raise GoalManagerConflict("writing refusal evidence is invalid") from exc
            outcome = result["outcome"]
            reason = {
                "declined": "model_declined",
                "needs_clarification": "writing_needs_clarification",
                "insufficient_sources": "writing_insufficient_sources",
            }[outcome]
            if row["error"] != reason:
                raise GoalManagerConflict("writing non-delivery evidence is inconsistent")
            now = self._now()
            await db.execute(
                """UPDATE plan_nodes SET status='failed',error_summary=?,
                   result_summary=NULL,assigned_agent_id=?,updated_at=?,completed_at=?
                   WHERE id=?""",
                (reason, row["claimed_by"] or row["last_agent_id"], now, now, node_id),
            )
            stale = int(row["conversation_revision"]) != int(goal["conversation_revision"])
            if not stale and not int(goal["pending_message_revision"] or 0):
                model_id = safe_context_text(str(result["model_id"]), max_chars=500)
                excerpt = safe_context_text(str(result["text"]), max_chars=2_800)
                if outcome == "needs_clarification":
                    question = safe_context_text(str(result["question"]), max_chars=800)
                    summary = f"Le modèle {model_id} demande une précision.\n\n{question}"
                    await GoalConversationService.assistant_locked(
                        db, goal_id, summary, question=True, now=now
                    )
                    await db.execute(
                        """UPDATE goal_runs SET status='waiting_permission',current_phase='needs_user',
                        evaluator_summary=?,failure_reason=NULL,paused_at=COALESCE(paused_at,?),updated_at=? WHERE id=?""",
                        (summary, now, now, goal_id),
                    )
                    await db.execute(
                        "UPDATE tasks SET status='waiting_permission',updated_at=? WHERE id=?",
                        (now, goal["root_task_id"]),
                    )
                else:
                    message = (
                        f"Le modèle {model_id} a déclaré un refus de produire le document demandé. "
                        "Le texte ci-dessous est sa réponse, pas une conclusion vérifiée du serveur. "
                        "Le but est arrêté sans nouvelle tentative automatique. "
                        f"Les résultats enregistrés sont conservés.\n\n{excerpt}"
                        if outcome == "declined"
                        else f"Le modèle {model_id} signale des sources insuffisantes pour produire le document. "
                        "Aucun document conforme n’est livré. Les sources et résultats sont conservés. "
                        "Précisez les sources à utiliser dans la conversation du projet pour poursuivre.\n\n"
                        f"{excerpt}"
                    )
                    await GoalConversationService.assistant_locked(
                        db, goal_id, message, question=False, now=now
                    )
                    await self._terminate_goal_locked(
                        db,
                        dict(goal),
                        status="failed",
                        reason=reason,
                        now=now,
                        maintenance_guard=maintenance_guard,
                    )
                    terminated = True
                await append_audit_event(
                    db,
                    "goal.writing.non_delivery",
                    {
                        "goal_run_id": goal_id,
                        "node_id": node_id,
                        "job_id": job_id,
                        "outcome": outcome,
                        "model_id": model_id,
                    },
                    actor_type="control-plane",
                    actor_id="goal-manager",
                    task_id=str(goal["root_task_id"]),
                    trace_id=goal_id,
                    created_at=now,
                )
            if maintenance_guard is not None:
                await maintenance_guard.require_current_locked(db)
            await db.commit()
        if terminated:
            await self._finalize_terminal_goal(
                goal_id, status="failed", maintenance_guard=maintenance_guard
            )

    async def on_tool_call_updated(self, tool_call_id: str) -> dict[str, Any] | None:
        changed: set[str] = set()
        if self.code_applications is not None:
            changed.update(await self.code_applications.synchronize(tool_call_id))
        if self.project_applications is not None:
            changed.update(await self.project_applications.synchronize(tool_call_id))
        detail = None
        for goal_run_id in changed:
            async with self._lock(goal_run_id):
                await self._advance_ready(goal_run_id, explicit_user_action=False)
                await self._drain_synthesis_nodes(goal_run_id)
                await self._evaluate_if_quiescent(goal_run_id)
                detail = await self.get_goal(goal_run_id)
        return detail

    async def on_capability_requested(self, job_id: str) -> dict[str, Any] | None:
        node = await self.graph.node_for_job(job_id)
        if node is None or node["status"] != "running":
            return None
        try:
            await self.graph.transition_node(
                str(node["id"]),
                expected="running",
                target="waiting_capability",
            )
        except GoalStateConflict:
            return None
        return await self.get_goal(str(node["goal_run_id"]))

    async def on_capability_resolved(self, job_id: str) -> dict[str, Any] | None:
        node = await self.graph.node_for_job(job_id)
        if node is None or node["status"] != "waiting_capability":
            return None
        try:
            await self.graph.transition_node(
                str(node["id"]),
                expected="waiting_capability",
                target="running",
            )
        except GoalStateConflict:
            return None
        return await self.get_goal(str(node["goal_run_id"]))

    async def _drain_synthesis_nodes(
        self,
        goal_run_id: str,
        *,
        maintenance_guard: MaintenanceLeaseGuard | None = None,
    ) -> None:
        """Complete ready synthesis nodes, bounded by the persisted step budget."""

        while True:
            await self.graph.refresh_ready_nodes(
                goal_run_id,
                maintenance_guard=maintenance_guard,
            )
            goal = await self.graph.get_goal(goal_run_id)
            if goal is None or goal["status"] != "running":
                return
            nodes = await self.graph.list_nodes(goal_run_id)
            ready_synthesis = [
                node
                for node in nodes
                if node["status"] == "ready" and node["node_type"] == "synthesis"
            ]
            if not ready_synthesis:
                return
            for node in ready_synthesis:
                await self._complete_deterministic_synthesis(
                    node,
                    maintenance_guard=maintenance_guard,
                )

    @staticmethod
    def _state_fingerprint(nodes: list[dict[str, Any]]) -> str:
        state = [
            {
                "id": node["id"],
                "status": node["status"],
                "result_summary": node.get("result_summary"),
                "error_summary": node.get("error_summary"),
            }
            for node in sorted(nodes, key=lambda item: str(item["id"]))
        ]
        return hashlib.sha256(
            json.dumps(
                state,
                allow_nan=False,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()

    async def _evaluation_state_matches_locked(
        self, db: aiosqlite.Connection, goal_id: str, state_fingerprint: str
    ) -> bool:
        rows = await (
            await db.execute(
                """SELECT id,status,result_summary,error_summary FROM plan_nodes
                WHERE goal_run_id=? ORDER BY id""",
                (goal_id,),
            )
        ).fetchall()
        return (
            bool(rows)
            and all(row["status"] in self.graph.NODE_TERMINAL for row in rows)
            and (self._state_fingerprint([dict(row) for row in rows]) == state_fingerprint)
        )

    @staticmethod
    async def _evaluator_attempts_locked(
        db: aiosqlite.Connection, goal: Mapping[str, Any]
    ) -> list[aiosqlite.Row]:
        # A goal has at most 100 model credits. Retain the exact request digest;
        # retry identity uses its persisted state, excluding volatile time/budgets.
        return list(
            await (
                await db.execute(
                    """SELECT c.status,c.error_category,c.completed_at,
                    json_extract(x.context_json,'$.state_fingerprint') AS state_fingerprint
                    FROM goal_model_calls c LEFT JOIN goal_contexts x ON x.id=c.context_id
                    WHERE c.goal_run_id=? AND c.role='evaluator' AND c.conversation_revision=?
                    ORDER BY c.lease_generation DESC LIMIT 100""",
                    (goal["id"], goal["conversation_revision"]),
                )
            ).fetchall()
        )

    async def _evaluator_cooling_down_locked(
        self,
        db: aiosqlite.Connection,
        goal: Mapping[str, Any],
        state_fingerprint: str,
        *,
        now: str,
    ) -> bool:
        attempts = await self._evaluator_attempts_locked(db, goal)
        if not attempts:
            return False
        latest = attempts[0]
        return bool(
            latest["status"] == "failed"
            and (
                latest["error_category"] in _EVALUATOR_FAILURE_DETAILS
                or latest["error_category"] == "provider_unavailable"
            )
            and latest["state_fingerprint"] == state_fingerprint
            and latest["completed_at"]
            and datetime.fromisoformat(latest["completed_at"])
            > datetime.fromisoformat(now) - timedelta(seconds=_EVALUATOR_RETRY_COOLDOWN_SECONDS)
        )

    async def _resume_evaluator_retry(
        self, goal_id: str, *, maintenance_guard: MaintenanceLeaseGuard | None = None
    ) -> None:
        """Only an explicit start or a durable new reply releases this human wait."""
        now = self._now()
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            if maintenance_guard is not None:
                await maintenance_guard.require_current_locked(db)
            # This fixed fragment resumes accounting; all values remain bound.
            updated = await db.execute(
                f"""UPDATE goal_runs SET status='running',current_phase='evaluator_retrying',
                failure_reason=NULL,updated_at=?,{RESUME_RUNTIME_SQL}
                WHERE id=? AND status='waiting_permission' AND current_phase='evaluator_retry_required'
                AND NOT EXISTS (SELECT 1 FROM plan_nodes WHERE goal_run_id=goal_runs.id
                    AND status NOT IN ('completed','failed','blocked','cancelled','skipped'))""",  # nosec B608
                (now, now, goal_id),
            )
            if updated.rowcount:
                await append_audit_event(
                    db,
                    "goal.evaluator.retry_requested",
                    {"goal_run_id": goal_id},
                    actor_type="control-plane",
                    actor_id="goal-manager",
                    trace_id=goal_id,
                    created_at=now,
                )
            await db.commit()

    async def _record_evaluator_failure(
        self,
        goal_id: str,
        error: EvaluatorProviderError,
        *,
        conversation_revision: int,
        state_fingerprint: str,
        call_id: str | None = None,
        maintenance_guard: MaintenanceLeaseGuard | None = None,
    ) -> bool:
        phase, reason = _EVALUATOR_FAILURE_DETAILS[error.category]
        truncated = error.category == "invalid_response" and error.diagnostic == "truncated"
        if truncated:
            reason = _EVALUATOR_TRUNCATED_REASON
        now = self._now()
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            if maintenance_guard is not None:
                await maintenance_guard.require_current_locked(db)
            goal = await (
                await db.execute(
                    """SELECT * FROM goal_runs WHERE id=? AND status='running'
                    AND conversation_revision=? AND pending_message_revision=0""",
                    (goal_id, conversation_revision),
                )
            ).fetchone()
            if goal is None:
                return False
            if call_id is not None:
                try:
                    await self._require_current_model_call_locked(db, call_id, now=now)
                except GoalManagerConflict:
                    return False
            if not await self._evaluation_state_matches_locked(db, goal_id, state_fingerprint):
                if call_id is not None:
                    await self._finish_model_call_locked(
                        db, call_id, status="failed", now=now, error_category="state_changed"
                    )
                    await db.commit()
                return False
            if call_id is not None and not await self._finish_model_call_locked(
                db,
                call_id,
                status="failed",
                now=now,
                error_category=error.category,
                output_digest=error.output_digest,
            ):
                return False
            invalid_attempts = 0
            for attempt in await self._evaluator_attempts_locked(db, dict(goal)):
                if (
                    attempt["status"] != "failed"
                    or attempt["state_fingerprint"] != state_fingerprint
                ):
                    break
                if attempt["error_category"] in {"invalid_response", "request_rejected"}:
                    invalid_attempts += 1
            pause = error.category == "invalid_context" or (
                error.category in {"invalid_response", "request_rejected"}
                and invalid_attempts >= _MAX_INVALID_EVALUATOR_ATTEMPTS
            )
            if pause:
                phase = "evaluator_retry_required"
                reason = (
                    f"{reason} Retry evaluation or send new instructions to continue."
                    if error.category == "invalid_context" or truncated
                    else _EVALUATOR_RETRY_REASON
                )
            await db.execute(
                """UPDATE goal_runs SET status=?,current_phase=?,failure_reason=?,updated_at=?,
                paused_at=CASE WHEN ? THEN COALESCE(paused_at,?) ELSE paused_at END,
                evaluator_summary=CASE WHEN ? THEN ? ELSE evaluator_summary END WHERE id=?""",
                (
                    "waiting_permission" if pause else "running",
                    phase,
                    reason,
                    now,
                    pause,
                    now,
                    pause or truncated,
                    reason,
                    goal_id,
                ),
            )
            if pause:
                await GoalConversationService.assistant_locked(
                    db, goal_id, reason, question=False, now=now
                )
            await append_audit_event(
                db,
                "goal.evaluator.failed",
                {
                    "goal_run_id": goal_id,
                    "model_call_id": call_id,
                    "category": error.category,
                    "diagnostic": error.diagnostic,
                    "retry_required": pause,
                },
                actor_type="control-plane",
                actor_id="goal-manager",
                trace_id=goal_id,
                created_at=now,
            )
            if maintenance_guard is not None:
                await maintenance_guard.require_current_locked(db)
            await db.commit()
        return True

    async def _available_worker_skills(self) -> list[str] | None:
        """Current permitted capabilities, independent of temporarily occupied slots."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN")
            skills = await self._available_worker_skills_locked(db)
            # Context collection must not persist even an optional policy bootstrap.
            await db.rollback()
        return skills

    async def _available_worker_skills_locked(self, db: aiosqlite.Connection) -> list[str] | None:
        """Read after the caller acquires its lock; never finish its transaction."""
        scheduler = self.agent_dispatcher.scheduler
        now = self.agent_dispatcher.clock()
        snapshot = await self.agent_dispatcher.worker_skill_policy.load_locked(
            db, now=now.isoformat()
        )
        if snapshot is None:
            return None
        rows = await (
            await db.execute(
                """SELECT status,skills_json,last_seen_at,supported_protocol_version
                FROM agents WHERE status IN ('online','busy')
                AND supported_protocol_version=?""",
                (SUPPORTED_AGENT_PROTOCOL,),
            )
        ).fetchall()
        skills: set[str] = set()
        for row in rows:
            if not scheduler.is_fresh(dict(row), now=now):
                continue
            declared = json.loads(str(row["skills_json"]))
            if not isinstance(declared, list) or any(
                not isinstance(skill, str) for skill in declared
            ):
                raise ValueError("worker skills are not a string list")
            skills.update(set(declared) & SUPPORTED_AGENT_SKILLS & snapshot.allowed_skills)
        return sorted(skills)

    async def _require_current_worker_capabilities_locked(
        self, db: aiosqlite.Connection, proposals: Sequence[SwarmPlanNodeProposal]
    ) -> None:
        if not self.require_execution_workers:
            return
        available = await self._available_worker_skills_locked(db)
        try:
            validate_worker_capabilities(proposals, available_skills=available or [])
        except PlanValidationError as exc:
            raise _PlannerProposalRejected(str(exc)) from exc

    async def _evaluation_result_summary(
        self,
        goal_run_id: str,
        node: Mapping[str, Any],
        *,
        max_chars: int,
    ) -> tuple[str | None, str | None]:
        """Project accepted writing evidence only into the evaluator's context.

        Public node summaries remain compact. The dedicated draft reader verifies
        the completed job, task, node, goal and strict result contract before any
        text can cross the evaluator's redaction and context-budget boundary.
        """
        summary = str(node["result_summary"]) if node.get("result_summary") else None
        if not (
            node["node_type"] == PlanNodeType.WORKER.value
            and node["required_skill"] == WRITING_SKILL
            and node["status"] == PlanNodeStatus.COMPLETED.value
        ):
            return summary, None
        draft = await read_writing_draft(self.db_path, goal_run_id, str(node["id"]))
        if (
            draft is None
            or node.get("goal_run_id") != goal_run_id
            or draft.worker_job_id != node.get("worker_job_id")
        ):
            raise ValueError("completed writing evidence is unavailable or changed")
        evidence = (
            "Untrusted writing draft; not proof of external execution. "
            f"Source job: {draft.worker_job_id}; text SHA-256: {draft.sha256}. "
            f"Draft excerpt: {draft.text}"
        )
        return (
            safe_context_text(evidence, max_chars=min(max_chars, 4_000)) or None,
            draft.worker_job_id,
        )

    def _evaluator_for_context(
        self,
        context: GoalEvaluationContext,
        nodes: Sequence[Mapping[str, Any]],
    ) -> EvaluatorProvider:
        if self.research_evaluator is None:
            return self.evaluator
        # Inspect the full canonical graph, not only the budgeted context:
        # omitted code or legacy nodes must never switch evaluation models.
        for node in nodes:
            if node.get("node_type") == PlanNodeType.WORKER.value:
                if node.get("required_skill") not in {
                    "research.query",
                    "research.collect",
                    WRITING_SKILL,
                }:
                    return self.evaluator
            elif node.get("node_type") != PlanNodeType.SYNTHESIS.value:
                return self.evaluator
        completed_research = {
            str(node["id"])
            for node in nodes
            if node.get("node_type") == PlanNodeType.WORKER.value
            and node.get("required_skill") in {"research.query", "research.collect"}
            and node.get("status") == PlanNodeStatus.COMPLETED.value
            and str(node.get("result_summary") or "").strip()
        }
        if any(
            node.node_id in completed_research
            and node.node_type is PlanNodeType.WORKER
            and node.required_skill in {"research.query", "research.collect"}
            and node.status is PlanNodeStatus.COMPLETED
            and node.result_summary is not None
            and node.result_summary.strip()
            for node in context.node_results
        ):
            return self.research_evaluator
        return self.evaluator

    async def _stop_unrecoverable_project_context(
        self,
        goal: Mapping[str, Any],
        nodes: Sequence[Mapping[str, Any]],
        *,
        maintenance_guard: MaintenanceLeaseGuard | None = None,
    ) -> bool:
        """A model cannot repair an input that failed deterministic preparation.

        Fence the observation transactionally, preserve revisions, and do not
        spend another evaluator/worker call repeating the same admission error.
        Historical blocks from earlier user revisions do not stop new work.
        """
        reason = "project context exceeds input budget; pinned requirements preserved"
        if not any(
            node["required_skill"] == PROJECT_SKILL
            and node["status"] == "blocked"
            and node.get("error_summary") == reason
            for node in nodes
        ):
            return False
        goal_id = str(goal["id"])
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            if maintenance_guard is not None:
                await maintenance_guard.require_current_locked(db)
            current = await (
                await db.execute("SELECT * FROM goal_runs WHERE id=?", (goal_id,))
            ).fetchone()
            rows = list(
                await (
                    await db.execute(
                        "SELECT * FROM plan_nodes WHERE goal_run_id=? ORDER BY rowid", (goal_id,)
                    )
                ).fetchall()
            )
            latest = next(
                (row for row in reversed(rows) if row["required_skill"] == PROJECT_SKILL), None
            )
            if (
                current is None
                or current["status"] != "running"
                or current["pending_message_revision"]
                or current["conversation_revision"] != goal["conversation_revision"]
                or any(row["status"] not in self.graph.NODE_TERMINAL for row in rows)
                or latest is None
                or latest["conversation_revision"] != current["conversation_revision"]
                or latest["status"] != "blocked"
                or latest["error_summary"] != reason
                or self._state_fingerprint([dict(row) for row in rows])
                != self._state_fingerprint([dict(node) for node in nodes])
            ):
                await db.rollback()
                return False
            await self._terminate_goal_locked(
                db,
                dict(current),
                status="failed",
                reason=reason,
                now=self._now(),
                maintenance_guard=maintenance_guard,
            )
            if maintenance_guard is not None:
                await maintenance_guard.require_current_locked(db)
            await db.commit()
        await self._finalize_terminal_goal(
            goal_id, status="failed", maintenance_guard=maintenance_guard
        )
        return True

    async def _evaluate_if_quiescent(
        self,
        goal_run_id: str,
        *,
        explicit_user_action: bool = False,
        maintenance_guard: MaintenanceLeaseGuard | None = None,
    ) -> None:
        goal = await self.graph.get_goal(goal_run_id)
        if goal is None or goal["status"] != "running":
            return
        if self._runtime_expired(goal):
            await self._terminate_goal(
                goal_run_id,
                status="budget_exhausted",
                reason="goal runtime budget exhausted",
                maintenance_guard=maintenance_guard,
            )
            return
        nodes = await self.graph.list_nodes(goal_run_id)
        if int(goal.get("pending_message_revision") or 0):
            return
        if not nodes or any(node["status"] not in self.graph.NODE_TERMINAL for node in nodes):
            return
        if await self._stop_unrecoverable_project_context(
            goal, nodes, maintenance_guard=maintenance_guard
        ):
            return
        state_fingerprint = self._state_fingerprint(nodes)
        if not explicit_user_action:
            async with aiosqlite.connect(self.db_path) as db:
                db.row_factory = aiosqlite.Row
                if await self._evaluator_cooling_down_locked(
                    db, goal, state_fingerprint, now=self._now()
                ):
                    return
        elapsed = int(active_runtime_seconds(goal))
        try:
            goal, project_memory = await self._shared_project_memory(goal, "evaluator")
            durable_context = None
            if (
                self.project_applications is not None
                and self.project_applications.context is not None
            ):
                durable = await self.project_applications.context.refresh(goal_run_id)
                if int(durable["conversation_revision"]) != int(
                    goal.get("conversation_revision") or 0
                ):
                    raise _ProjectMemoryContextChanged("goal changed while its context was read")
                durable_context = self.project_applications.context.prompt_state(durable)
            builder = self.context_builder or ContextBuilder(self.db_path, max_tokens=2_048)
            result_summaries: dict[str, str | None] = {}
            writing_provenance: list[str] = []
            for node in nodes:
                summary, source_id = await self._evaluation_result_summary(
                    goal_run_id,
                    node,
                    max_chars=getattr(builder, "max_result_chars_per_node", 2_000),
                )
                result_summaries[str(node["id"])] = summary
                if source_id is not None:
                    writing_provenance.append(source_id)
            symbolic_context = None
            symbolic_project_id = None
            symbolic_catalogs: tuple[SymbolicCatalog, ...] = ()
            if isinstance(self.strategy_retrieval, StrategyRetrieval):
                (
                    symbolic_context,
                    symbolic_project_id,
                    symbolic_catalogs,
                ) = await self.strategy_retrieval.retrieve_symbolic_context(
                    str(goal["objective"]), goal_run_id=goal_run_id
                )
            context = GoalEvaluationContext(
                schema_version="1.0",
                goal_run_id=goal_run_id,
                objective=str(goal["objective"]),
                completion_criteria=list(goal["completion_criteria"]),
                node_results=[
                    EvaluationNodeResult(
                        node_id=str(node["id"]),
                        title=str(node["title"]),
                        status=PlanNodeStatus(str(node["status"])),
                        expected_output=str(node["expected_output"]),
                        result_summary=result_summaries[str(node["id"])],
                        failure_reason=(
                            str(node["error_summary"]) if node.get("error_summary") else None
                        ),
                        node_type=PlanNodeType(str(node["node_type"])),
                        required_skill=node.get("required_skill"),
                    )
                    for node in nodes
                    # Deterministic synthesis without hard or optional inputs
                    # produced no evidence; retain its ID and stored history.
                    if not (
                        node["node_type"] == PlanNodeType.SYNTHESIS.value
                        and node["status"] == PlanNodeStatus.COMPLETED.value
                        and (
                            node.get("depends_on") == [] or not self._completed_input_summary(node)
                        )
                    )
                ],
                known_node_ids=[str(node["id"]) for node in nodes],
                available_skills=await self._available_worker_skills(),
                conversation_revision=int(goal.get("conversation_revision") or 0),
                project_memory=project_memory,
                symbolic_context=symbolic_context,
                durable_context=durable_context,
                conversation=[
                    EvaluationConversationMessage.model_validate(message)
                    for message in await self.recent_conversation(goal_run_id)
                ],
                remaining_step_budget=max(0, int(goal["max_steps"]) - int(goal["step_count"])),
                remaining_model_call_budget=max(
                    0, int(goal["max_model_calls"]) - int(goal["model_call_count"])
                ),
                elapsed_seconds=min(elapsed, 86_400),
                state_fingerprint=state_fingerprint,
            )
            context, recorded = await builder.build_evaluation_context(
                context,
                symbolic_catalogs=symbolic_catalogs,
                symbolic_project_id=symbolic_project_id,
                provenance_ids=(
                    goal_run_id,
                    *(str(node["id"]) for node in nodes),
                    *writing_provenance,
                ),
            )
            context_id = str(recorded.id)
        except _ProjectMemoryContextChanged:
            # A concurrent retrieval or reply is not a malformed model context.
            # Maintenance will retry the current state without a manual pause.
            return
        except (TypeError, ValueError, RuntimeError, aiosqlite.Error):
            await self._record_evaluator_failure(
                goal_run_id,
                EvaluatorProviderError(
                    "evaluator context unavailable",
                    category="invalid_context",
                    diagnostic="context",
                ),
                conversation_revision=int(goal.get("conversation_revision") or 0),
                state_fingerprint=state_fingerprint,
                maintenance_guard=maintenance_guard,
            )
            return
        context_payload = context.model_dump(mode="json")
        input_digest = hashlib.sha256(
            json.dumps(
                context_payload,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()
        evaluator = self._evaluator_for_context(context, nodes)

        async def revalidate_symbolic_context() -> None:
            if symbolic_context is None:
                return
            retrieval = self.strategy_retrieval
            if (
                not isinstance(retrieval, StrategyRetrieval)
                or retrieval.symbolic_catalogs != symbolic_catalogs
                or not await retrieval.revalidate_symbolic(
                    context.symbolic_context.evidence if context.symbolic_context else (),
                    goal_run_id=goal_run_id,
                    project_id=symbolic_project_id,
                )
            ):
                raise ValueError("symbolic evaluator context changed")

        try:
            call_id = await self._reserve_model_call(
                goal_run_id,
                role="evaluator",
                conversation_revision=int(goal.get("conversation_revision") or 0),
                evaluator_state_fingerprint=state_fingerprint,
                explicit_user_action=explicit_user_action,
                context_id=context_id,
                input_digest=input_digest,
                provider_source=evaluator.source.value,
                model_id=getattr(evaluator, "model", None),
                maintenance_guard=maintenance_guard,
            )
        except GoalManagerConflict as exc:
            if "budget exhausted" in str(exc):
                await self._terminate_goal(
                    goal_run_id,
                    status="budget_exhausted",
                    reason="goal model call budget exhausted",
                    maintenance_guard=maintenance_guard,
                )
            return
        try:
            remaining = self._remaining_runtime_seconds(goal)
            if remaining <= 0:
                raise TimeoutError("goal runtime budget exhausted")
            await revalidate_symbolic_context()
            async with asyncio.timeout(remaining):
                decision = await evaluator.evaluate(context)
            await revalidate_symbolic_context()
            validated = validate_evaluation_decision(
                decision,
                policy=self.permission_policy,
                known_node_ids=context.known_node_ids,
                available_skills=context.available_skills
                if self.require_execution_workers
                else None,
            )
        except TimeoutError:
            await self._record_model_timeout(
                goal_run_id, call_id, maintenance_guard=maintenance_guard
            )
            return
        except (EvaluatorProviderError, OSError, RuntimeError, TypeError, ValueError) as exc:
            if isinstance(exc, EvaluatorProviderError):
                failure = exc
            elif isinstance(exc, PlanValidationError):
                failure = EvaluatorProviderError(
                    "invalid evaluator proposal", category="invalid_response", diagnostic="graph"
                )
            elif isinstance(exc, OSError):
                failure = EvaluatorProviderError("evaluator transport unavailable")
            else:
                failure = EvaluatorProviderError(
                    "evaluator failed internally", category="invalid_context", diagnostic="context"
                )
            await self._record_evaluator_failure(
                goal_run_id,
                failure,
                call_id=call_id,
                conversation_revision=int(goal.get("conversation_revision") or 0),
                state_fingerprint=state_fingerprint,
                maintenance_guard=maintenance_guard,
            )
            return
        refreshed_goal = await self.graph.get_goal(goal_run_id)
        if refreshed_goal is None or refreshed_goal["status"] != "running":
            await self._finish_model_call(
                call_id,
                status="failed",
                maintenance_guard=maintenance_guard,
            )
            return
        if self._runtime_expired(refreshed_goal):
            await self._finish_model_call(
                call_id,
                status="failed",
                maintenance_guard=maintenance_guard,
            )
            await self._terminate_goal(
                goal_run_id,
                status="budget_exhausted",
                reason="goal runtime budget exhausted",
                maintenance_guard=maintenance_guard,
            )
            return
        try:
            await self._apply_evaluation(
                refreshed_goal,
                nodes,
                validated.decision,
                decision_fingerprint=validated.fingerprint,
                state_fingerprint=state_fingerprint,
                model_call_id=call_id,
                symbolic_context=context.symbolic_context,
                symbolic_project_id=symbolic_project_id,
                symbolic_catalogs=symbolic_catalogs,
                maintenance_guard=maintenance_guard,
            )
        except _PlannerProposalRejected:
            await self._record_evaluator_failure(
                goal_run_id,
                EvaluatorProviderError(
                    "evaluator worker capabilities changed before acceptance",
                    category="invalid_response",
                    diagnostic="graph",
                ),
                call_id=call_id,
                conversation_revision=int(context.conversation_revision),
                state_fingerprint=state_fingerprint,
                maintenance_guard=maintenance_guard,
            )
            return
        except (GoalManagerConflict, GoalStateConflict) as exc:
            await self._finish_model_call(
                call_id,
                status="failed",
                maintenance_guard=maintenance_guard,
            )
            if str(exc) == "symbolic evaluator context changed":
                return
            if "runtime budget exhausted" in str(exc):
                await self._terminate_goal(
                    goal_run_id,
                    status="budget_exhausted",
                    reason="goal runtime budget exhausted",
                    maintenance_guard=maintenance_guard,
                )
                return
            raise

    async def _apply_evaluation(
        self,
        goal: Mapping[str, Any],
        nodes: list[dict[str, Any]],
        decision: Any,
        *,
        decision_fingerprint: str,
        state_fingerprint: str,
        model_call_id: str,
        symbolic_context: SymbolicContext | None = None,
        symbolic_project_id: str | None = None,
        symbolic_catalogs: tuple[SymbolicCatalog, ...] = (),
        maintenance_guard: MaintenanceLeaseGuard | None = None,
    ) -> None:
        goal_run_id = str(goal["id"])
        safe_reason_summary = (
            redact_dataset_text(str(decision.reason_summary))
            or "Evaluator returned no safe reason summary."
        )[:4_000]
        now = self._now()
        terminal_status: str | None = None
        terminal_reason: str | None = None
        should_advance = False
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            if maintenance_guard is not None:
                await maintenance_guard.require_current_locked(db)
            await self._require_current_model_call_locked(db, model_call_id, now=now)
            current_goal = await (
                await db.execute("SELECT * FROM goal_runs WHERE id=?", (goal_run_id,))
            ).fetchone()
            if current_goal is None or str(current_goal["status"]) != "running":
                await db.rollback()
                raise GoalManagerConflict("goal changed while evaluation was generated")
            current_goal_record = dict(current_goal)
            if self._runtime_expired(current_goal_record):
                await db.rollback()
                raise GoalManagerConflict("goal runtime budget exhausted")
            current_node_rows = await (
                await db.execute(
                    """SELECT id,node_type,status,result_summary,error_summary FROM plan_nodes
                    WHERE goal_run_id=? ORDER BY id ASC""",
                    (goal_run_id,),
                )
            ).fetchall()
            if (
                self._state_fingerprint([dict(row) for row in current_node_rows])
                != state_fingerprint
            ):
                await db.rollback()
                raise GoalManagerConflict("goal node state changed while evaluation was generated")
            if symbolic_context is not None:
                retrieval = self.strategy_retrieval
                project = await (
                    await db.execute(
                        "SELECT project_id FROM goal_project_links WHERE goal_run_id=?",
                        (goal_run_id,),
                    )
                ).fetchone()
                current_project = str(project[0]) if project else None
                if (
                    not isinstance(retrieval, StrategyRetrieval)
                    or retrieval.symbolic_catalogs != symbolic_catalogs
                    or current_project != symbolic_project_id
                ):
                    raise GoalManagerConflict("symbolic evaluator context changed")
                scopes = ("general",) + ((f"project:{current_project}",) if current_project else ())
                for evidence in symbolic_context.evidence:
                    if not await revalidate_symbolic_evidence(
                        db, evidence, allowed_scopes=scopes, catalogs=symbolic_catalogs
                    ):
                        raise GoalManagerConflict("symbolic evaluator context changed")
            repeated = (
                current_goal["evaluation_fingerprint"] == decision_fingerprint
                and current_goal["last_state_fingerprint"] == state_fingerprint
            )
            sequence_row = await (
                await db.execute(
                    "SELECT COALESCE(MAX(sequence),0)+1 FROM goal_evaluations WHERE goal_run_id=?",
                    (goal_run_id,),
                )
            ).fetchone()
            sequence = int(sequence_row[0]) if sequence_row else 1
            await db.execute(
                """
                INSERT INTO goal_evaluations(
                    id,goal_run_id,sequence,status,reason_summary,decision_json,
                    state_fingerprint,decision_fingerprint,created_at
                ) VALUES(?,?,?,?,?,?,?,?,?)
                """,
                (
                    f"eval_{uuid4().hex}",
                    goal_run_id,
                    sequence,
                    decision.status.value,
                    safe_reason_summary,
                    json.dumps(
                        decision.model_dump(mode="json"),
                        ensure_ascii=False,
                        allow_nan=False,
                        separators=(",", ":"),
                        sort_keys=True,
                    ),
                    state_fingerprint,
                    decision_fingerprint,
                    now,
                ),
            )
            updated = await db.execute(
                """UPDATE goal_runs SET evaluator_status=?,evaluator_summary=?,
                    evaluation_fingerprint=?,last_state_fingerprint=?,updated_at=?
                WHERE id=? AND status='running'""",
                (
                    decision.status.value,
                    safe_reason_summary,
                    decision_fingerprint,
                    state_fingerprint,
                    now,
                    goal_run_id,
                ),
            )
            if updated.rowcount != 1:
                await db.rollback()
                raise GoalManagerConflict("goal changed while evaluation was persisted")

            current_nodes = [dict(row) for row in current_node_rows]
            if repeated:
                terminal_status = "failed"
                terminal_reason = "evaluator repeated an equivalent decision without state change"
            elif decision.status is EvaluationStatus.DONE:
                latest_project = await (
                    await db.execute(
                        """SELECT snapshot_json FROM project_revisions WHERE goal_run_id=?
                        ORDER BY revision DESC LIMIT 1""",
                        (goal_run_id,),
                    )
                ).fetchone()
                unsupported_native = latest_project is not None and native_project(
                    ProjectResult.model_validate_json(str(latest_project[0]))
                )
                completed_evidence = any(
                    node["node_type"] == "worker" and node["status"] == "completed"
                    for node in current_nodes
                )
                failed_required_evidence = any(
                    node["node_type"] == "worker"
                    and node["status"] in {"failed", "blocked", "cancelled"}
                    for node in current_nodes
                )
                writing_failure = await writing_completion_failure_locked(db, goal_run_id)
                if unsupported_native:
                    await pause_native_validation_locked(db, goal_run_id, now=now)
                elif writing_failure is not None:
                    terminal_status = "failed"
                    terminal_reason = writing_failure
                elif (
                    not completed_evidence
                    or failed_required_evidence
                    or decision.missing_requirements
                    or decision.invalid_results
                ):
                    terminal_status = "failed"
                    terminal_reason = "evaluator completion lacked acceptable worker evidence"
                else:
                    terminal_status = "completed"
            elif decision.status is EvaluationStatus.FAILED:
                terminal_status = "failed"
                terminal_reason = safe_reason_summary
            elif decision.status is EvaluationStatus.NEEDS_USER:
                safe_question = (
                    redact_dataset_text(str(decision.user_question)) or "Clarify the goal."
                )
                user_summary = f"{safe_reason_summary} Question: {safe_question}"[:4_000]
                waiting = await db.execute(
                    """UPDATE goal_runs SET status='waiting_permission',
                    current_phase='needs_user',evaluator_summary=?,updated_at=?,paused_at=COALESCE(paused_at,?)
                    WHERE id=? AND status='running'""",
                    (user_summary, now, now, goal_run_id),
                )
                await GoalConversationService.assistant_locked(
                    db, goal_run_id, safe_question, question=True, now=now
                )
                if waiting.rowcount != 1:
                    await db.rollback()
                    raise GoalManagerConflict("goal changed while waiting for user input")
            elif decision.suggested_new_nodes:
                count_replan = decision.status is EvaluationStatus.REPLAN
                existing_count = len(current_nodes)
                exceeds_replans = count_replan and int(current_goal["replan_count"]) >= int(
                    current_goal["max_replans"]
                )
                exceeds_steps = existing_count + len(decision.suggested_new_nodes) > int(
                    current_goal["max_steps"]
                )
                if exceeds_replans or exceeds_steps:
                    terminal_status = "budget_exhausted"
                    terminal_reason = (
                        "goal replan budget exhausted"
                        if exceeds_replans
                        else "goal step budget exhausted"
                    )
                else:
                    await self._insert_evaluator_nodes_locked(
                        db,
                        current_goal_record,
                        current_nodes,
                        decision.suggested_new_nodes,
                        count_replan=count_replan,
                        now=now,
                    )
                    should_advance = True
            else:
                await db.execute(
                    """UPDATE goal_runs SET current_phase='evaluator_continue_no_work',
                    failure_reason='evaluator continue no work',updated_at=?
                    WHERE id=? AND status='running'""",
                    (now, goal_run_id),
                )

            if terminal_status is not None:
                await self._terminate_goal_locked(
                    db,
                    current_goal_record,
                    status=terminal_status,
                    reason=terminal_reason,
                    now=now,
                    maintenance_guard=maintenance_guard,
                )
            finished = await self._finish_model_call_locked(
                db,
                model_call_id,
                status="completed",
                now=now,
                output_digest=self._model_output_digest(decision.model_dump(mode="json")),
            )
            if not finished:
                await db.rollback()
                raise GoalManagerConflict("model call lease expired or was fenced")
            if maintenance_guard is not None:
                await maintenance_guard.require_current_locked(db)
            if symbolic_context is not None and (
                not isinstance(self.strategy_retrieval, StrategyRetrieval)
                or self.strategy_retrieval.symbolic_catalogs != symbolic_catalogs
            ):
                raise GoalManagerConflict("symbolic evaluator context changed")
            await db.commit()
        if terminal_status is not None:
            await self._finalize_terminal_goal(
                goal_run_id,
                status=terminal_status,
                maintenance_guard=maintenance_guard,
            )
            return
        if should_advance:
            await self.graph.refresh_ready_nodes(
                goal_run_id,
                maintenance_guard=maintenance_guard,
            )
            await self._advance_ready(
                goal_run_id,
                explicit_user_action=False,
                maintenance_guard=maintenance_guard,
            )
            return

    async def _insert_evaluator_nodes_locked(
        self,
        db: aiosqlite.Connection,
        goal: Mapping[str, Any],
        existing: list[dict[str, Any]],
        proposals: list[SwarmPlanNodeProposal],
        *,
        count_replan: bool,
        now: str,
    ) -> None:
        await self._require_current_worker_capabilities_locked(db, proposals)
        by_temp = {proposal.temporary_id: f"node_{uuid4().hex}" for proposal in proposals}
        known = {str(node["id"]) for node in existing}
        for proposal in proposals:
            node_id = by_temp[proposal.temporary_id]
            retry_provenance = None
            if proposal.retry_of_node_id is not None:
                if proposal.required_skill != WRITING_SKILL:
                    raise GoalManagerConflict("Only writing nodes may repair an attempt.")
                try:
                    retry_provenance = await writing_retry_provenance_locked(
                        db,
                        str(goal["id"]),
                        int(goal.get("conversation_revision") or 0),
                        proposal.retry_of_node_id,
                    )
                except (ValueError, TypeError) as exc:
                    raise GoalManagerConflict("Writing repair lineage is invalid.") from exc
            hard_dependencies = sorted(by_temp.get(item, item) for item in proposal.dependencies)
            optional_dependencies = sorted(
                by_temp.get(item, item) for item in proposal.optional_dependencies
            )
            dependencies = sorted(set(hard_dependencies + optional_dependencies))
            if not set(dependencies).issubset(known | set(by_temp.values())):
                raise GoalManagerConflict("evaluator extension has an unknown dependency")
            await db.execute(
                """INSERT INTO plan_nodes(
                id,goal_run_id,node_type,title,objective,required_skill,status,priority,
                depends_on_json,expected_output,planner_metadata_json,created_at,updated_at,
                conversation_revision
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    node_id,
                    goal["id"],
                    proposal.node_type.value,
                    proposal.title,
                    proposal.objective,
                    proposal.required_skill,
                    "planned",
                    proposal.priority,
                    json.dumps(dependencies, separators=(",", ":")),
                    proposal.expected_output,
                    json.dumps(
                        {
                            "source": "evaluator",
                            "temporary_id": proposal.temporary_id,
                            "worker_arguments": proposal.worker_arguments,
                            **(
                                {
                                    "retry_of_node_id": proposal.retry_of_node_id,
                                    "writing_retry": retry_provenance,
                                }
                                if retry_provenance is not None
                                else {}
                            ),
                        },
                        separators=(",", ":"),
                    ),
                    now,
                    now,
                    int(goal.get("conversation_revision") or 0),
                ),
            )
            for dependency in hard_dependencies:
                await db.execute(
                    "INSERT INTO plan_edges VALUES(?,?,?,'hard')",
                    (goal["id"], dependency, node_id),
                )
            for dependency in optional_dependencies:
                await db.execute(
                    "INSERT INTO plan_edges VALUES(?,?,?,'optional')",
                    (goal["id"], dependency, node_id),
                )
        if count_replan:
            await db.execute(
                """UPDATE goal_runs SET replan_count=replan_count+1,updated_at=?
                WHERE id=? AND status='running'""",
                (now, goal["id"]),
            )

    async def _terminate_goal(
        self,
        goal_run_id: str,
        *,
        status: str,
        reason: str | None,
        maintenance_guard: MaintenanceLeaseGuard | None = None,
    ) -> None:
        if status not in {"completed", "failed", "cancelled", "budget_exhausted"}:
            raise ValueError("goal terminal status is invalid")
        now = self._now()
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            if maintenance_guard is not None:
                await maintenance_guard.require_current_locked(db)
            goal = await (
                await db.execute("SELECT * FROM goal_runs WHERE id=?", (goal_run_id,))
            ).fetchone()
            if goal is None:
                await db.rollback()
                raise GoalManagerConflict("goal not found")
            if str(goal["status"]) in self.graph.GOAL_TERMINAL:
                await db.rollback()
                return
            await self._terminate_goal_locked(
                db,
                dict(goal),
                status=status,
                reason=reason,
                now=now,
                maintenance_guard=maintenance_guard,
            )
            if maintenance_guard is not None:
                await maintenance_guard.require_current_locked(db)
            await db.commit()
        await self._finalize_terminal_goal(
            goal_run_id,
            status=status,
            maintenance_guard=maintenance_guard,
        )

    async def _terminate_goal_locked(
        self,
        db: aiosqlite.Connection,
        goal: Mapping[str, Any],
        *,
        status: str,
        reason: str | None,
        now: str,
        maintenance_guard: MaintenanceLeaseGuard | None = None,
    ) -> None:
        """Apply the authoritative terminal transition inside the caller's transaction."""

        if status not in {"completed", "failed", "cancelled", "budget_exhausted"}:
            raise ValueError("goal terminal status is invalid")
        if maintenance_guard is not None:
            await maintenance_guard.require_current_locked(db)
        safe_reason = redact_dataset_text(reason) if reason else None
        if reason and not safe_reason:
            safe_reason = "goal terminated"
        updated = await db.execute(
            """UPDATE goal_runs SET status=?,current_phase=?,failure_reason=?,
            completed_at=?,updated_at=? WHERE id=?
            AND status NOT IN ('completed','failed','cancelled','budget_exhausted')""",
            (
                status,
                (
                    "auto_continuation_pending"
                    if status == "budget_exhausted"
                    and self.auto_continue_on_model_budget_exhausted
                    and safe_reason == "goal model call budget exhausted"
                    else status
                ),
                safe_reason[:4_000] if safe_reason else None,
                now,
                now,
                str(goal["id"]),
            ),
        )
        if updated.rowcount != 1:
            raise GoalManagerConflict("goal changed during terminal transition")
        root_status = (
            "completed"
            if status == "completed"
            else ("cancelled" if status == "cancelled" else "failed")
        )
        await db.execute(
            """UPDATE tasks SET status=?,updated_at=?,completed_at=?,error_json=?
            WHERE id=? AND status NOT IN ('completed','failed','cancelled')""",
            (
                root_status,
                now,
                now,
                json.dumps({"message": safe_reason}) if safe_reason else None,
                str(goal["root_task_id"]),
            ),
        )
        if status != "completed":
            await db.execute(
                """UPDATE plan_nodes SET status='cancelled',updated_at=?,completed_at=?,
                error_summary=COALESCE(error_summary,?)
                WHERE goal_run_id=? AND status NOT IN
                ('completed','failed','blocked','cancelled','skipped')""",
                (
                    now,
                    now,
                    safe_reason[:500] if safe_reason else status,
                    str(goal["id"]),
                ),
            )
            # Fence reviewed local writes in the same transaction as cancellation.
            # A write already claimed by the executor keeps its truthful outcome.
            await db.execute(
                """UPDATE approvals SET status='cancelled',decided_at=?
                WHERE status='pending' AND task_id IN (
                    SELECT apply_task_id FROM goal_code_proposals WHERE goal_run_id=?
                    UNION SELECT apply_task_id FROM project_revisions WHERE goal_run_id=?
                )""",
                (now, goal["id"], goal["id"]),
            )
            await db.execute(
                """UPDATE tool_calls SET status='cancelled',updated_at=?
                WHERE status IN ('proposed','waiting_permission','queued') AND task_id IN (
                    SELECT apply_task_id FROM goal_code_proposals WHERE goal_run_id=?
                    UNION SELECT apply_task_id FROM project_revisions WHERE goal_run_id=?
                )""",
                (now, goal["id"], goal["id"]),
            )
            await db.execute(
                """UPDATE tasks SET status='cancelled',updated_at=?,completed_at=?
                WHERE status IN ('created','planned','waiting_permission','queued','blocked')
                  AND id IN (SELECT apply_task_id FROM goal_code_proposals WHERE goal_run_id=?
                    UNION SELECT apply_task_id FROM project_revisions WHERE goal_run_id=?)""",
                (now, now, goal["id"], goal["id"]),
            )
        await append_audit_event(
            db,
            f"goal.{status}",
            {
                "goal_run_id": str(goal["id"]),
                "reason": safe_reason[:500] if safe_reason else None,
            },
            actor_type="control-plane",
            actor_id="goal-manager",
            task_id=str(goal["root_task_id"]),
            trace_id=str(goal["id"]),
            created_at=now,
        )

    async def _finalize_terminal_goal(
        self,
        goal_run_id: str,
        *,
        status: str,
        maintenance_guard: MaintenanceLeaseGuard | None = None,
    ) -> None:
        """Project non-authoritative terminal artifacts after the atomic transition."""

        if status != "completed":
            await self._cancel_child_tasks(
                goal_run_id,
                actor_id="goal-manager",
                maintenance_guard=maintenance_guard,
            )
        try:
            await self.result_aggregator.aggregate_goal(goal_run_id)
        except (OSError, RuntimeError, TypeError, ValueError, aiosqlite.Error):
            logger.exception("terminal goal result projection deferred: %s", goal_run_id)
        try:
            await self._record_episode(goal_run_id)
        except (OSError, RuntimeError, TypeError, ValueError, aiosqlite.Error):
            logger.exception("terminal goal episode projection deferred: %s", goal_run_id)
        if status == "budget_exhausted":
            try:
                await self._auto_continue_model_budget_exhausted(
                    goal_run_id, maintenance_guard=maintenance_guard
                )
            except (
                GoalManagerConflict,
                OSError,
                RuntimeError,
                TypeError,
                ValueError,
                aiosqlite.Error,
            ):
                logger.exception("automatic budget continuation deferred: %s", goal_run_id)

    async def _auto_continue_model_budget_exhausted(
        self, goal_run_id: str, *, maintenance_guard: MaintenanceLeaseGuard | None = None
    ) -> None:
        if not self.auto_continue_on_model_budget_exhausted:
            return
        goal = await self.graph.get_goal(goal_run_id)
        if goal is None or goal.get("status") != "budget_exhausted":
            return
        if goal.get("current_phase") != "auto_continuation_pending":
            return
        if str(goal.get("failure_reason") or "") != "goal model call budget exhausted":
            return
        if int(goal.get("model_call_count") or 0) < int(goal.get("max_model_calls") or 0):
            return
        if maintenance_guard is not None:
            await maintenance_guard.renew_now()
        if await self._auto_continuation_stalled(goal_run_id):
            await self._stop_auto_continuation(
                goal_run_id,
                "automatic continuation stopped after three runs without completed work",
            )
            return
        checkpoint = await self._auto_continuation_checkpoint(goal_run_id)
        if checkpoint is None:
            await self._stop_auto_continuation(
                goal_run_id, "automatic continuation cannot preserve all user instructions"
            )
            return
        request = GoalMessageRequest(
            message=checkpoint,
            client_message_id=f"auto-model-budget:{goal_run_id}",
        )
        active_id = await self.conversations.append(
            goal_run_id,
            message=request.message,
            client_message_id=request.client_message_id,
            reply_to_message_id=request.reply_to_message_id,
            actor_id="goal-manager",
            planning_mode=request.planning_mode,
            internal_checkpoint=True,
        )
        await self._resume_pending_conversation(active_id, maintenance_guard=maintenance_guard)
        await self._advance_ready(
            active_id, explicit_user_action=False, maintenance_guard=maintenance_guard
        )

    async def _stop_auto_continuation(self, goal_run_id: str, reason: str) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """UPDATE goal_runs SET current_phase='auto_continuation_stopped',
                failure_reason=? WHERE id=? AND status='budget_exhausted'
                AND current_phase='auto_continuation_pending'""",
                (reason, goal_run_id),
            )
            await db.commit()

    async def _auto_continuation_stalled(self, goal_run_id: str) -> bool:
        """Stop a chain that repeatedly spends its entire budget without finished work."""
        async with aiosqlite.connect(self.db_path) as db:
            rows = await (
                await db.execute(
                    """SELECT g.status,COUNT(n.id) AS completed_nodes
                    FROM goal_conversation_links target
                    JOIN goal_conversation_links link
                      ON link.conversation_id=target.conversation_id
                    JOIN goal_runs g ON g.id=link.goal_run_id
                    LEFT JOIN plan_nodes n ON n.goal_run_id=g.id AND n.status='completed'
                    WHERE target.goal_run_id=?
                    GROUP BY g.id ORDER BY g.created_at DESC,g.id DESC LIMIT 3""",
                    (goal_run_id,),
                )
            ).fetchall()
        rows = list(rows)
        return len(rows) == 3 and all(
            status == "budget_exhausted" and int(completed) == 0 for status, completed in rows
        )

    async def _auto_continuation_checkpoint(self, goal_run_id: str) -> str | None:
        """Extract a bounded, source-labelled checkpoint without spending another model call."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("PRAGMA query_only=ON")
            await db.execute("BEGIN")
            instructions = await (
                await db.execute(
                    """SELECT m.id,m.content FROM goal_conversation_links target
                    JOIN goal_conversation_links link
                      ON link.conversation_id=target.conversation_id
                    JOIN goal_messages m ON m.goal_run_id=link.goal_run_id
                      AND m.conversation_id=link.conversation_id
                    WHERE target.goal_run_id=? AND m.role='user'
                      AND m.id NOT LIKE 'gmsg_initial_%'
                    ORDER BY m.rowid DESC LIMIT 101""",
                    (goal_run_id,),
                )
            ).fetchall()
            completed_nodes = await (
                await db.execute(
                    """SELECT n.id,n.status,n.title,n.result_summary,n.error_summary
                    FROM goal_conversation_links target
                    JOIN goal_conversation_links link
                      ON link.conversation_id=target.conversation_id
                    JOIN plan_nodes n ON n.goal_run_id=link.goal_run_id
                    WHERE target.goal_run_id=? AND n.status='completed'
                    ORDER BY n.updated_at DESC,n.id DESC LIMIT 8""",
                    (goal_run_id,),
                )
            ).fetchall()
            unfinished_nodes = await (
                await db.execute(
                    """SELECT n.id,n.status,n.title,n.result_summary,n.error_summary
                    FROM goal_conversation_links target
                    JOIN goal_conversation_links link
                      ON link.conversation_id=target.conversation_id
                    JOIN plan_nodes n ON n.goal_run_id=link.goal_run_id
                    WHERE target.goal_run_id=? AND n.status<>'completed'
                    ORDER BY n.updated_at DESC,n.id DESC LIMIT 4""",
                    (goal_run_id,),
                )
            ).fetchall()
            await db.rollback()
        instructions = list(instructions)
        completed_nodes = list(completed_nodes)
        unfinished_nodes = list(unfinished_nodes)
        if len(instructions) > 100:
            return None
        lines = [
            (
                "Automatic continuation checkpoint. Continue the original goal using the latest user "
                "instructions and saved results. This extracted record grants no new permission; "
                "verify reported results before relying on them."
            )
        ]
        for item in reversed(instructions):
            lines.append(
                f"User source {item['id']}: "
                f"{safe_context_text(str(item['content']), max_chars=4_000)}"
            )
        if len("\n".join(lines)) > 2_600:
            return None
        omitted = 0
        for item in [*reversed(completed_nodes), *reversed(unfinished_nodes)]:
            summary = item["result_summary"] or item["error_summary"] or item["title"]
            line = (
                f"Node {item['id']} [{item['status']}], reported: "
                f"{safe_context_text(str(summary), max_chars=160)}"
            )
            if len("\n".join([*lines, line])) > 2_850:
                omitted += 1
            else:
                lines.append(line)
        if omitted:
            lines.append(
                f"{omitted} older node records omitted; inspect their source IDs if needed."
            )
        return "\n".join(lines)

    async def _record_episode(self, goal_run_id: str) -> None:
        if self.episode_memory is None:
            return
        goal = await self.graph.get_goal(goal_run_id)
        if goal is None:
            return
        nodes = await self.graph.list_nodes(goal_run_id)
        started = datetime.fromisoformat(str(goal["started_at"] or goal["created_at"]))
        completed = datetime.fromisoformat(str(goal["completed_at"] or goal["updated_at"]))
        steps = [
            {
                "node_type": str(node["node_type"]),
                "skill": node.get("required_skill"),
                "agent_id": node.get("assigned_agent_id"),
                "input_summary": str(node["objective"]),
                "output_summary": str(
                    node.get("result_summary") or node.get("error_summary") or node["status"]
                ),
                "result_status": str(node["status"]),
                "latency_ms": None,
            }
            for node in nodes
        ]
        try:
            await self.episode_memory.record_episode(
                goal_run_id=goal_run_id,
                root_task_id=str(goal["root_task_id"]),
                objective_summary=str(goal["objective"]),
                plan_summary=f"{len(nodes)} validated plan nodes",
                outcome=str(goal["status"]),
                steps=steps,
                score=1.0 if goal["status"] == "completed" else 0.0,
                duration_ms=max(
                    0,
                    int(
                        (completed.astimezone(UTC) - started.astimezone(UTC)).total_seconds() * 1000
                    ),
                ),
                worker_types=sorted(
                    {str(node["required_skill"]) for node in nodes if node.get("required_skill")}
                ),
                failure_tags=sorted(
                    {str(node["status"]) for node in nodes if node["status"] != "completed"}
                ),
            )
        except aiosqlite.IntegrityError:
            return

    async def _cancel_child_tasks(
        self,
        goal_run_id: str,
        *,
        actor_id: str,
        maintenance_guard: MaintenanceLeaseGuard | None = None,
    ) -> int:
        """Fence every nonterminal child task after the goal stops accepting work."""

        async with aiosqlite.connect(self.db_path) as db:
            rows = await (
                await db.execute(
                    """SELECT task_id FROM plan_nodes
                    WHERE goal_run_id=? AND task_id IS NOT NULL
                    UNION SELECT apply_task_id FROM goal_code_proposals
                    WHERE goal_run_id=? AND apply_task_id IS NOT NULL
                    UNION SELECT apply_task_id FROM project_revisions
                    WHERE goal_run_id=? AND apply_task_id IS NOT NULL
                    UNION SELECT task_id FROM swift_project_validations
                    WHERE goal_id=?
                    ORDER BY task_id""",
                    (goal_run_id, goal_run_id, goal_run_id, goal_run_id),
                )
            ).fetchall()
        cancelled = 0
        for row in rows:
            if maintenance_guard is not None:
                await maintenance_guard.renew_now()
            task_id = str(row[0])
            task = await self.state_service.get_task(task_id)
            if task is None or task.status.value in {"completed", "failed", "cancelled"}:
                continue
            if task.status.value == "running":
                async with aiosqlite.connect(self.db_path) as db:
                    local_application = await (
                        await db.execute(
                            "SELECT 1 FROM goal_code_proposals WHERE apply_task_id=? UNION SELECT 1 FROM project_revisions WHERE apply_task_id=?",
                            (task_id, task_id),
                        )
                    ).fetchone()
                if local_application is not None:
                    continue
            try:
                result = await self.state_service.cancel_task(
                    task_id,
                    actor_id=actor_id,
                    maintenance_guard=maintenance_guard,
                )
                if result is not None and result.status is TaskStatus.CANCELLED:
                    cancelled += 1
            except RuntimeError:
                refreshed = await self.state_service.get_task(task_id)
                if refreshed is None or refreshed.status.value not in {
                    "completed",
                    "failed",
                    "cancelled",
                }:
                    raise
        return cancelled

    async def cancel_goal(self, goal_run_id: str, *, actor_id: str) -> dict[str, Any]:
        async with self._lock(goal_run_id):
            detail = await self.get_goal(goal_run_id)
            if detail is None:
                raise GoalManagerConflict("goal not found")
            if detail["goal"]["status"] in self.graph.GOAL_TERMINAL:
                raise GoalManagerConflict("terminal goal cannot be cancelled")
            await self._terminate_goal(goal_run_id, status="cancelled", reason="cancelled by user")
            result = await self.get_goal(goal_run_id)
            if result is None:
                raise RuntimeError("cancelled goal disappeared")
            return result

    async def replan_goal(
        self,
        goal_run_id: str,
        request: GoalReplanRequest,
    ) -> dict[str, Any]:
        async with self._lock(goal_run_id):
            goal = await self.graph.get_goal(goal_run_id)
            if goal is None:
                raise GoalManagerConflict("goal not found")
            if goal["status"] not in {"running", "waiting_permission"}:
                raise GoalManagerConflict("goal cannot replan from its current state")
            nodes = await self.graph.list_nodes(goal_run_id)
            if any(node["status"] in self.ACTIVE_NODE_STATUSES for node in nodes):
                raise GoalManagerConflict("goal cannot replan while worker jobs are active")
            if int(goal["replan_count"]) >= int(goal["max_replans"]):
                await self._terminate_goal(
                    goal_run_id,
                    status="budget_exhausted",
                    reason="goal replan budget exhausted",
                )
                result = await self.get_goal(goal_run_id)
                if result is None:
                    raise RuntimeError("budget-exhausted goal disappeared")
                return result
            start_request = GoalStartRequest(
                plan_proposal=request.plan_proposal,
                planner_source=request.planner_source,
            )
            try:
                proposal, source, call_id = await self._obtain_plan(
                    goal,
                    start_request,
                    user_guidance=request.reason,
                )
            except _LocalModelResourceBusy:
                return await self._defer_replan_for_resource(goal, request)
            refreshed_goal = await self.graph.get_goal(goal_run_id)
            if refreshed_goal is None:
                raise GoalManagerConflict("goal disappeared while replanning")
            if self._runtime_expired(refreshed_goal):
                if call_id is not None:
                    await self._finish_model_call(call_id, status="failed")
                await self._cancel_child_tasks(goal_run_id, actor_id="goal-runtime")
                await self._terminate_goal(
                    goal_run_id,
                    status="budget_exhausted",
                    reason="goal runtime budget exhausted",
                )
                result = await self.get_goal(goal_run_id)
                if result is None:
                    raise RuntimeError("runtime-expired goal disappeared")
                return result
            goal = refreshed_goal
            try:
                bound_proposal = self._bind_plan_to_goal(goal, proposal, model_call_id=call_id)
                validated = validate_swarm_plan(
                    bound_proposal,
                    policy=self.permission_policy,
                    max_nodes=max(1, int(goal["max_steps"]) - len(nodes)),
                    max_parallelism=int(goal["max_parallelism"]),
                )
            except (PlanValidationError, GoalManagerConflict) as exc:
                if call_id is not None:
                    await self._record_planner_failure(
                        goal_run_id,
                        call_id,
                        category="invalid_response",
                        diagnostic_code=rejection_diagnostic(exc),
                    )
                raise GoalManagerConflict(str(exc)) from exc
            if validated.fingerprint == goal.get("plan_fingerprint"):
                if call_id is not None:
                    await self._finish_model_call(call_id, status="failed")
                await self._terminate_goal(
                    goal_run_id,
                    status="failed",
                    reason="planner proposed an equivalent plan",
                )
                result = await self.get_goal(goal_run_id)
                if result is None:
                    raise RuntimeError("loop-stopped goal disappeared")
                return result
            try:
                await self._append_replan_nodes(
                    goal,
                    proposal,
                    source=source,
                    model_call_id=call_id,
                )
            except _PlannerProposalRejected as exc:
                if call_id is not None:
                    await self._record_planner_failure(
                        goal_run_id,
                        call_id,
                        category="invalid_response",
                        diagnostic_code=rejection_diagnostic(exc),
                    )
                raise
            except BaseException:
                if call_id is not None:
                    await self._finish_model_call(call_id, status="failed")
                raise
            await self._advance_ready(goal_run_id, explicit_user_action=True)
            result = await self.get_goal(goal_run_id)
            if result is None:
                raise RuntimeError("replanned goal disappeared")
            return result

    async def _defer_replan_for_resource(
        self, goal: Mapping[str, Any], request: GoalReplanRequest
    ) -> dict[str, Any]:
        """Reuse durable conversation reconciliation without duplicating a request."""
        reason = request.reason or (
            "[Server-recorded replan request] Replan the current goal using its existing "
            "objective and requirements."
        )
        request_key = hashlib.sha256(
            json.dumps(
                [str(goal["id"]), int(goal["replan_count"]), reason],
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        try:
            active_id = await self.conversations.append(
                str(goal["id"]),
                message=reason,
                client_message_id=f"deferred_replan_{request_key}",
                reply_to_message_id=None,
                actor_id="goal-replan",
                expected_replan_state=(
                    str(goal["id"]),
                    int(goal["conversation_revision"]),
                    int(goal["replan_count"]),
                ),
            )
        except GoalConversationConflict as exc:
            raise GoalManagerConflict(str(exc)) from exc
        detail = await self.get_goal(active_id)
        if detail is None:
            raise GoalManagerConflict("goal disappeared while waiting for local model")
        return detail

    async def _append_replan_nodes(
        self,
        goal: Mapping[str, Any],
        proposal: SwarmPlanProposal,
        *,
        source: PlannerSource,
        model_call_id: str | None,
        maintenance_guard: MaintenanceLeaseGuard | None = None,
    ) -> None:
        output_digest = self._model_output_digest(proposal.model_dump(mode="json"))
        proposal = self._bind_plan_to_goal(goal, proposal, model_call_id=model_call_id)
        proposal = await self._bind_research_source_requirements(goal, proposal, initial=False)
        validated = validate_swarm_plan(
            proposal,
            policy=self.permission_policy,
            max_nodes=int(goal["max_steps"]),
            max_parallelism=int(goal["max_parallelism"]),
        )
        existing = await self.graph.list_nodes(str(goal["id"]))
        if len(existing) + len(proposal.nodes) > int(goal["max_steps"]):
            raise GoalManagerConflict("replan exceeds the goal step budget")
        by_temp = {node.temporary_id: f"node_{uuid4().hex}" for node in proposal.nodes}
        now = self._now()
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            if maintenance_guard is not None:
                await maintenance_guard.require_current_locked(db)
            current = await (
                await db.execute(
                    """SELECT replan_count,max_replans,status,plan_fingerprint,max_steps,
                              conversation_revision
                    FROM goal_runs WHERE id=?""",
                    (goal["id"],),
                )
            ).fetchone()
            if current is None or str(current["status"]) not in {"running", "waiting_permission"}:
                await db.rollback()
                raise GoalManagerConflict("goal changed while replanning")
            if int(current["conversation_revision"]) != int(goal["conversation_revision"]):
                raise GoalManagerConflict("goal conversation changed while replanning")
            if current["plan_fingerprint"] != goal.get("plan_fingerprint"):
                await db.rollback()
                raise GoalManagerConflict("goal plan changed while replanning")
            if int(current["replan_count"]) >= int(current["max_replans"]):
                await db.rollback()
                raise GoalManagerConflict("goal replan budget exhausted")
            current_node_rows = list(
                await (
                    await db.execute(
                        "SELECT id FROM plan_nodes WHERE goal_run_id=?",
                        (goal["id"],),
                    )
                ).fetchall()
            )
            if {str(row["id"]) for row in current_node_rows} != {
                str(node["id"]) for node in existing
            }:
                await db.rollback()
                raise GoalManagerConflict("goal plan changed while replanning")
            if len(current_node_rows) + len(proposal.nodes) > int(current["max_steps"]):
                await db.rollback()
                raise GoalManagerConflict("replan exceeds the goal step budget")
            if model_call_id is not None:
                await self._require_current_model_call_locked(db, model_call_id, now=now)
            await self._require_current_worker_capabilities_locked(db, proposal.nodes)
            if int(goal.get("pending_message_revision") or 0):
                prepared = await (
                    await db.execute(
                        """SELECT 1 FROM plan_nodes n LEFT JOIN project_revisions r ON r.node_id=n.id
                    WHERE n.goal_run_id=? AND n.status='waiting_permission'
                    AND (n.required_skill<>? OR r.apply_task_id IS NOT NULL) LIMIT 1""",
                        (goal["id"], PROJECT_SKILL),
                    )
                ).fetchone()
                if prepared:
                    raise GoalManagerConflict("an existing approval must settle before rerouting")
                # The failed receipt remains immutable. Only the superseded
                # clarification step leaves the active plan after user input.
                await db.execute(
                    """UPDATE plan_nodes SET status='skipped',updated_at=?,
                    result_summary='Clarification superseded by newer user instructions.'
                    WHERE goal_run_id=? AND required_skill=? AND status='failed'
                    AND error_summary='writing_needs_clarification' AND conversation_revision<?""",
                    (now, goal["id"], WRITING_SKILL, current["conversation_revision"]),
                )
                await db.execute(
                    """UPDATE plan_nodes SET status='skipped',updated_at=?,completed_at=?,
                    result_summary='Superseded by newer user instructions.'
                    WHERE goal_run_id=? AND status IN ('planned','ready')""",
                    (now, now, goal["id"]),
                )
                await db.execute(
                    """UPDATE plan_nodes SET status='completed',completed_at=?,updated_at=?,
                    result_summary='Project draft retained; newer instructions superseded its unprepared review.'
                    WHERE goal_run_id=? AND required_skill=? AND status='waiting_permission'""",
                    (now, now, goal["id"], PROJECT_SKILL),
                )
            for node in proposal.nodes:
                hard_dependencies = sorted(by_temp[item] for item in node.dependencies)
                optional_dependencies = sorted(by_temp[item] for item in node.optional_dependencies)
                dependencies = sorted(set(hard_dependencies + optional_dependencies))
                node_id = by_temp[node.temporary_id]
                await db.execute(
                    """INSERT INTO plan_nodes(
                    id,goal_run_id,node_type,title,objective,required_skill,status,priority,
                    depends_on_json,expected_output,planner_metadata_json,created_at,updated_at,
                    conversation_revision
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        node_id,
                        goal["id"],
                        node.node_type.value,
                        node.title,
                        node.objective,
                        node.required_skill,
                        "planned",
                        node.priority,
                        json.dumps(dependencies, separators=(",", ":")),
                        node.expected_output,
                        json.dumps(
                            {
                                "source": "replan",
                                "temporary_id": node.temporary_id,
                                "worker_arguments": node.worker_arguments,
                            },
                            separators=(",", ":"),
                        ),
                        now,
                        now,
                        int(current["conversation_revision"] or 0),
                    ),
                )
                for dependency in hard_dependencies:
                    await db.execute(
                        "INSERT INTO plan_edges VALUES(?,?,?,'hard')",
                        (goal["id"], dependency, node_id),
                    )
                for dependency in optional_dependencies:
                    await db.execute(
                        "INSERT INTO plan_edges VALUES(?,?,?,'optional')",
                        (goal["id"], dependency, node_id),
                    )
            await db.execute(
                """UPDATE goal_runs SET status='running',planner_source=?,
                plan_fingerprint=?,max_parallelism=MIN(max_parallelism,?),
                replan_count=replan_count+1,current_phase='dispatching',
                failure_reason=NULL,pending_message_revision=0,updated_at=? WHERE id=?""",
                (
                    source.value,
                    validated.fingerprint,
                    proposal.max_parallelism,
                    now,
                    goal["id"],
                ),
            )
            await db.execute(
                f"UPDATE goal_runs SET {RESUME_RUNTIME_SQL} WHERE id=?",  # nosec B608
                (now, goal["id"]),
            )
            await db.execute(
                "UPDATE tasks SET status='running',updated_at=? WHERE id=(SELECT root_task_id FROM goal_runs WHERE id=?)",
                (now, goal["id"]),
            )
            await append_audit_event(
                db,
                "goal.replan.accepted",
                {
                    "goal_run_id": goal["id"],
                    "planner_source": source.value,
                    "plan_fingerprint": validated.fingerprint,
                    "rationale_summary": (redact_dataset_text(proposal.rationale_summary) or "")[
                        :4_000
                    ],
                    "node_ids": list(by_temp.values()),
                    "model_call_id": model_call_id,
                    "conversation_revision": int(current["conversation_revision"]),
                },
                actor_type="control-plane",
                actor_id="goal-manager",
                task_id=str(goal["root_task_id"]),
                trace_id=str(goal["id"]),
                created_at=now,
            )
            if model_call_id is not None:
                cursor = await db.execute(
                    """UPDATE goal_model_calls SET status='completed',completed_at=?,
                    output_digest=?,latency_ms=CAST(MAX(0,
                        (julianday(?) - julianday(created_at)) * 86400000
                    ) AS INTEGER)
                    WHERE id=? AND status='started' AND owner_instance_id=?
                      AND lease_expires_at>?""",
                    (
                        now,
                        output_digest,
                        now,
                        model_call_id,
                        self.instance_id,
                        now,
                    ),
                )
                if cursor.rowcount != 1:
                    await db.rollback()
                    raise GoalManagerConflict("planner model call lease was fenced")
            if maintenance_guard is not None:
                await maintenance_guard.require_current_locked(db)
            await db.commit()
        await self.graph.refresh_ready_nodes(str(goal["id"]), maintenance_guard=maintenance_guard)

    async def add_feedback(
        self,
        goal_run_id: str,
        request: GoalFeedbackRequest,
        *,
        actor_id: str,
    ) -> dict[str, Any]:
        goal = await self.graph.get_goal(goal_run_id)
        if goal is None:
            raise GoalManagerConflict("goal not found")
        if goal["status"] not in self.graph.GOAL_TERMINAL:
            raise GoalManagerConflict("goal feedback requires a terminal goal")
        feedback_id = f"gfb_{uuid4().hex}"
        now = self._now()
        from app.services.feedback_dataset import redact_dataset_text

        note = redact_dataset_text(request.note)
        corrected_answer = redact_dataset_text(request.corrected_final_answer)
        corrected_plan = redact_dataset_text(request.corrected_plan_summary)
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            await db.execute(
                """INSERT INTO goal_feedback(
                id,goal_run_id,score,note,corrected_final_answer,
                corrected_plan_summary,reviewed,created_at
                ) VALUES(?,?,?,?,?,?,?,?)""",
                (
                    feedback_id,
                    goal_run_id,
                    request.score,
                    note,
                    corrected_answer,
                    corrected_plan,
                    int(request.reviewed),
                    now,
                ),
            )
            await append_audit_event(
                db,
                "goal.feedback.created",
                {"goal_run_id": goal_run_id, "score": request.score},
                actor_type="device",
                actor_id=actor_id,
                task_id=str(goal["root_task_id"]),
                trace_id=goal_run_id,
                created_at=now,
            )
            await db.commit()
        if self.episode_memory is not None:
            try:
                # Goal feedback is authoritative. The episode's latest score is
                # a rebuildable retrieval projection and must not turn a committed
                # feedback response into a false failure.
                await self.episode_memory.set_user_feedback_score(goal_run_id, request.score)
            except (OSError, RuntimeError, TypeError, ValueError, aiosqlite.Error):
                pass
        return {
            "id": feedback_id,
            "goal_run_id": goal_run_id,
            "score": request.score,
            "note": note,
            "corrected_final_answer": corrected_answer,
            "corrected_plan_summary": corrected_plan,
            "reviewed": request.reviewed,
            "created_at": now,
        }

    async def status_counts(self) -> dict[str, int | float]:
        async with aiosqlite.connect(self.db_path) as db:
            row = await (
                await db.execute(
                    """SELECT
                    SUM(CASE WHEN status IN ('planning','running','waiting_permission') THEN 1 ELSE 0 END),
                    SUM(CASE WHEN status='completed' THEN 1 ELSE 0 END),
                    SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END),
                    COALESCE(AVG(CASE WHEN status IN ('completed','failed','budget_exhausted')
                      THEN step_count END),0),
                    SUM(replan_count),
                    SUM(CASE WHEN status='budget_exhausted' THEN 1 ELSE 0 END),
                    SUM(CASE WHEN evaluator_status='needs_user' THEN 1 ELSE 0 END),
                    SUM(CASE WHEN failure_reason LIKE '%equivalent%'
                                  OR failure_reason LIKE '%without state change%'
                        THEN 1 ELSE 0 END)
                    FROM goal_runs"""
                )
            ).fetchone()
            episode_hits = await (
                await db.execute(
                    """SELECT COUNT(*) FROM goal_contexts
                    WHERE context_json LIKE '%\"kind\":\"episode\"%'"""
                )
            ).fetchone()
        return {
            "active_goal_runs": int(row[0] or 0) if row else 0,
            "completed_goal_runs": int(row[1] or 0) if row else 0,
            "failed_goal_runs": int(row[2] or 0) if row else 0,
            "average_goal_steps": float(row[3] or 0.0) if row else 0.0,
            "replans": int(row[4] or 0) if row else 0,
            "budget_exhaustions": int(row[5] or 0) if row else 0,
            "evaluator_needs_user": int(row[6] or 0) if row else 0,
            "loop_stops": int(row[7] or 0) if row else 0,
            "episode_retrieval_hits": int(episode_hits[0] or 0) if episode_hits else 0,
        }

    async def reconcile(
        self,
        *,
        limit: int = 100,
        maintenance_guard: MaintenanceLeaseGuard | None = None,
    ) -> int:
        """Recover goal projections after crashes or missed notification hints."""

        bounded_limit = max(1, min(limit, 500))
        if maintenance_guard is not None:
            await maintenance_guard.renew_now()
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            active_for_deadline = await (
                await db.execute(
                    """SELECT * FROM goal_runs
                    WHERE status IN ('planning','running','waiting_permission')
                      AND started_at IS NOT NULL
                      AND julianday(started_at) + (max_runtime_seconds + paused_seconds) / 86400.0
                          <= julianday(COALESCE(paused_at,?))
                    ORDER BY updated_at ASC,id ASC LIMIT ?""",
                    (self._now(), bounded_limit),
                )
            ).fetchall()
        changed = 0
        for active in active_for_deadline:
            if not self._runtime_expired(dict(active)):
                continue
            if maintenance_guard is not None:
                await maintenance_guard.renew_now()
            await self._terminate_goal(
                str(active["id"]),
                status="budget_exhausted",
                reason="goal runtime budget exhausted",
                maintenance_guard=maintenance_guard,
            )
            changed += 1
        if self.auto_continue_on_model_budget_exhausted:
            async with aiosqlite.connect(self.db_path) as db:
                pending_budget = await (
                    await db.execute(
                        """SELECT g.id FROM goal_runs g
                        JOIN goal_conversation_links link ON link.goal_run_id=g.id
                        JOIN goal_conversations conversation
                          ON conversation.id=link.conversation_id
                        WHERE g.status='budget_exhausted'
                          AND g.current_phase='auto_continuation_pending'
                          AND g.failure_reason='goal model call budget exhausted'
                          AND g.model_call_count>=g.max_model_calls
                          AND conversation.active_goal_id=g.id
                        ORDER BY g.updated_at,g.id LIMIT ?""",
                        (bounded_limit,),
                    )
                ).fetchall()
            for pending in pending_budget:
                if maintenance_guard is not None:
                    await maintenance_guard.renew_now()
                try:
                    # The terminal transition is durable before its child-task
                    # projection. A crash there must not leave old work running
                    # beside the linked continuation.
                    await self._cancel_child_tasks(
                        str(pending[0]),
                        actor_id="goal-manager",
                        maintenance_guard=maintenance_guard,
                    )
                    await self._auto_continue_model_budget_exhausted(
                        str(pending[0]), maintenance_guard=maintenance_guard
                    )
                except (
                    GoalManagerConflict,
                    OSError,
                    RuntimeError,
                    TypeError,
                    ValueError,
                    aiosqlite.Error,
                ):
                    logger.exception("automatic budget continuation retry deferred: %s", pending[0])
                else:
                    changed += 1
        if self.code_applications is not None:
            application_goals = await self.code_applications.synchronize(
                maintenance_guard=maintenance_guard
            )
            changed += len(application_goals)
        if self.project_applications is not None:
            application_goals = await self.project_applications.synchronize(
                maintenance_guard=maintenance_guard
            )
            changed += len(application_goals)
        # Inputs are durable and may arrive while a model or approval is active.
        # Select only continuations which can make progress, before applying LIMIT.
        async with aiosqlite.connect(self.db_path) as db:
            pending_goals = await (
                await db.execute(
                    """SELECT g.id FROM goal_runs g
                WHERE g.status IN ('planning','running','waiting_permission')
                AND (g.pending_message_revision>0 OR g.current_phase='project_continue')
                AND (NOT EXISTS (SELECT 1 FROM plan_nodes n WHERE n.goal_run_id=g.id
                    AND n.status IN ('dispatched','running','waiting_capability'))
                    OR (g.pending_message_revision>0 AND EXISTS (
                        SELECT 1 FROM plan_nodes n JOIN agent_jobs j
                          ON j.task_id=n.task_id AND j.required_skill=n.required_skill
                          AND (n.worker_job_id=j.id OR n.worker_job_id IS NULL)
                        JOIN tasks t ON t.id=j.task_id AND t.source='goal:' || g.id
                        WHERE n.goal_run_id=g.id AND n.node_type='worker'
                          AND n.status IN ('dispatched','running')
                          AND j.status='queued' AND t.status='queued'
                          AND n.conversation_revision<>g.conversation_revision)))
                AND NOT EXISTS (SELECT 1 FROM plan_nodes n
                    LEFT JOIN project_revisions r ON r.node_id=n.id
                    WHERE n.goal_run_id=g.id AND n.status='waiting_permission'
                    AND (n.required_skill<>'code.build_project' OR (r.apply_task_id IS NOT NULL AND (
                        EXISTS (SELECT 1 FROM tool_calls c WHERE c.task_id=r.apply_task_id)
                        OR NOT EXISTS (SELECT 1 FROM tasks t WHERE t.id=r.apply_task_id
                                       AND t.status IN ('created','planned'))))))
                ORDER BY g.updated_at,g.id LIMIT ?""",
                    (bounded_limit,),
                )
            ).fetchall()
        for pending in pending_goals:
            pending_id = str(pending[0])
            async with self._lock(pending_id):
                before_pending = await self.graph.get_goal(pending_id)
                try:
                    await self._resume_pending_conversation(
                        pending_id, maintenance_guard=maintenance_guard
                    )
                except GoalManagerConflict:
                    pass
                after_pending = await self.graph.get_goal(pending_id)
                if (
                    before_pending is not None
                    and after_pending is not None
                    and (
                        before_pending["updated_at"] != after_pending["updated_at"]
                        or before_pending["status"] != after_pending["status"]
                    )
                ):
                    changed += 1
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            rows = await (
                await db.execute(
                    """
                    SELECT n.id,n.goal_run_id,n.task_id,n.worker_job_id,n.status,
                           n.required_skill,n.objective,j.status AS job_status,
                           j.claimed_by,j.result_json,j.error,j.last_failure_reason,
                           j.last_agent_id
                    FROM plan_nodes AS n
                    JOIN goal_runs AS g ON g.id=n.goal_run_id
                    LEFT JOIN agent_jobs AS j ON j.id=n.worker_job_id
                    WHERE g.status IN ('running','waiting_permission')
                      AND n.node_type='worker'
                      AND n.status IN ('dispatched','running','waiting_capability')
                      AND (
                        j.status IN ('completed','failed','cancelled','quarantined')
                        OR (n.status='dispatched' AND j.status IN ('claimed','running'))
                        OR (n.worker_job_id IS NULL AND n.task_id IS NOT NULL AND (
                            EXISTS (SELECT 1 FROM agent_jobs AS orphan
                                    WHERE orphan.task_id=n.task_id)
                            OR EXISTS (SELECT 1 FROM tasks AS child
                                       WHERE child.id=n.task_id AND child.status='created')
                        ))
                      )
                    ORDER BY n.updated_at ASC,n.id ASC LIMIT ?
                    """,
                    (bounded_limit,),
                )
            ).fetchall()
        for raw in rows:
            row = dict(raw)
            job_id = row.get("worker_job_id")
            if not job_id and row.get("task_id"):
                async with aiosqlite.connect(self.db_path) as db:
                    db.row_factory = aiosqlite.Row
                    recovered = await (
                        await db.execute(
                            """SELECT id FROM agent_jobs WHERE task_id=?
                            ORDER BY created_at DESC LIMIT 1""",
                            (row["task_id"],),
                        )
                    ).fetchone()
                if recovered is not None:
                    job_id = str(recovered["id"])
                    async with aiosqlite.connect(self.db_path) as db:
                        await db.execute("BEGIN IMMEDIATE")
                        if maintenance_guard is not None:
                            await maintenance_guard.require_current_locked(db)
                        await db.execute(
                            """UPDATE plan_nodes SET worker_job_id=?,updated_at=?
                            WHERE id=? AND worker_job_id IS NULL""",
                            (job_id, self._now(), row["id"]),
                        )
                        if maintenance_guard is not None:
                            await maintenance_guard.require_current_locked(db)
                        await db.commit()
                else:
                    child = await self.state_service.get_task(str(row["task_id"]))
                    if child is not None and child.status is TaskStatus.CREATED:
                        try:
                            payload = await self._worker_payload(row, row)
                            job = await self.agent_dispatcher.queue_job(
                                child.id,
                                str(row["required_skill"]),
                                payload,
                                maintenance_guard=maintenance_guard,
                            )
                        except (AgentDispatchConflict, GoalManagerConflict):
                            continue
                        async with aiosqlite.connect(self.db_path) as db:
                            await db.execute("BEGIN IMMEDIATE")
                            if maintenance_guard is not None:
                                await maintenance_guard.require_current_locked(db)
                            await db.execute(
                                """UPDATE plan_nodes SET worker_job_id=?,updated_at=?
                                WHERE id=? AND worker_job_id IS NULL""",
                                (job["id"], self._now(), row["id"]),
                            )
                            if maintenance_guard is not None:
                                await maintenance_guard.require_current_locked(db)
                            await db.commit()
                        changed += 1
                        continue
            if not job_id:
                continue
            recovered_job = await self.agent_dispatcher.get_job(str(job_id))
            if recovered_job is None:
                continue
            if recovered_job["status"] in {"claimed", "running"}:
                if (
                    await self.on_job_claimed(
                        recovered_job,
                        maintenance_guard=maintenance_guard,
                    )
                    is not None
                ):
                    changed += 1
            elif (
                recovered_job["status"]
                in {
                    "completed",
                    "failed",
                    "cancelled",
                    "quarantined",
                }
                and await self.on_job_result(
                    recovered_job,
                    maintenance_guard=maintenance_guard,
                )
                is not None
            ):
                changed += 1
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            selection_now = self._now()
            has_online_worker = (
                await self._has_online_worker_locked(db)
                if self.require_execution_workers
                else False
            )
            active_goals = await (
                await db.execute(
                    """SELECT id,status,autonomy_profile,updated_at,started_at,reply_dispatch_credit FROM goal_runs
                    WHERE status IN ('planning','running')
                      AND (autonomy_profile<>'manual' OR reply_dispatch_credit=1)
                      AND started_at IS NOT NULL
                      AND NOT EXISTS (
                        SELECT 1 FROM goal_model_calls AS pending
                        WHERE pending.goal_run_id=goal_runs.id AND pending.status='started'
                          AND pending.lease_expires_at>?
                      )
                      AND (
                        current_phase NOT IN (
                            'planner_unavailable','planner_request_rejected',
                            'planner_invalid_response','planner_invalid_context'
                        )
                        OR julianday(updated_at)<=julianday(?) - ? / 86400.0
                      )
                      AND NOT EXISTS (
                        SELECT 1 FROM audit_events memory_failure
                        WHERE memory_failure.trace_id=goal_runs.id
                          AND memory_failure.event_type='goal.memory.retrieval.failed'
                          AND json_extract(memory_failure.payload_json,'$.phase')
                              =goal_runs.current_phase
                          AND json_extract(memory_failure.payload_json,'$.conversation_revision')
                              =goal_runs.conversation_revision
                          AND julianday(memory_failure.created_at)+?/86400.0>julianday(?)
                      )
                      AND (
                        current_phase NOT IN (
                            'evaluator_unavailable','evaluator_request_rejected',
                            'evaluator_invalid_response','evaluator_invalid_context',
                            'evaluator_context_unavailable'
                        )
                        OR EXISTS (
                            SELECT 1 FROM plan_nodes AS changed_node
                            WHERE changed_node.goal_run_id=goal_runs.id AND (
                                changed_node.status NOT IN
                                    ('completed','failed','blocked','cancelled','skipped')
                                OR julianday(changed_node.updated_at)>julianday(goal_runs.updated_at)
                            )
                        )
                        OR NOT EXISTS (
                            SELECT 1 FROM goal_model_calls AS recent
                            WHERE recent.goal_run_id=goal_runs.id AND recent.role='evaluator'
                              AND recent.conversation_revision=goal_runs.conversation_revision
                              AND recent.status='failed'
                              AND recent.error_category IN (
                                'transport_unavailable','request_rejected','invalid_response',
                                'invalid_context','provider_unavailable'
                              )
                              AND julianday(recent.completed_at)>julianday(?) - ? / 86400.0
                        )
                      )
                      AND (
                        ?=0 OR current_phase<>'waiting_for_workers'
                        OR ?=1
                      )
                      AND (
                        status='planning'
                        OR NOT EXISTS (
                            SELECT 1 FROM plan_nodes AS unfinished
                            WHERE unfinished.goal_run_id=goal_runs.id
                              AND unfinished.status NOT IN
                                  ('completed','failed','blocked','cancelled','skipped')
                        )
                        OR EXISTS (
                            SELECT 1 FROM plan_nodes AS ready
                            WHERE ready.goal_run_id=goal_runs.id AND ready.status='ready'
                              AND (ready.node_type='synthesis' OR (
                                SELECT COUNT(*) FROM plan_nodes AS active
                                WHERE active.goal_run_id=goal_runs.id AND active.status IN
                                    ('dispatched','running','waiting_permission','waiting_capability')
                              ) < goal_runs.max_parallelism)
                        )
                        OR EXISTS (
                            SELECT 1 FROM plan_nodes AS planned
                            WHERE planned.goal_run_id=goal_runs.id AND planned.status='planned'
                              AND (
                                NOT EXISTS (
                                    SELECT 1 FROM plan_edges AS edge
                                    JOIN plan_nodes AS dependency ON dependency.id=edge.from_node_id
                                    WHERE edge.to_node_id=planned.id AND dependency.status NOT IN
                                        ('completed','failed','blocked','cancelled','skipped')
                                )
                                OR EXISTS (
                                    SELECT 1 FROM plan_edges AS edge
                                    JOIN plan_nodes AS dependency ON dependency.id=edge.from_node_id
                                    WHERE edge.to_node_id=planned.id AND edge.dependency_type='hard'
                                      AND dependency.status IN ('failed','blocked','cancelled','skipped')
                                )
                              )
                        )
                      )
                    ORDER BY updated_at ASC,id ASC LIMIT ?""",
                    (
                        selection_now,
                        selection_now,
                        _PLANNER_RETRY_COOLDOWN_SECONDS,
                        _PLANNER_RETRY_COOLDOWN_SECONDS,
                        selection_now,
                        selection_now,
                        _EVALUATOR_RETRY_COOLDOWN_SECONDS,
                        int(self.require_execution_workers),
                        int(has_online_worker),
                        bounded_limit,
                    ),
                )
            ).fetchall()
        for active in active_goals:
            if (
                str(active["autonomy_profile"]) == AutonomyProfile.MANUAL.value
                and not active["reply_dispatch_credit"]
            ):
                continue
            if str(active["status"]) == "planning" and active["started_at"] is None:
                continue
            goal_run_id = str(active["id"])
            before = str(active["updated_at"])
            if str(active["status"]) == "planning":
                try:
                    await self.start_goal(
                        goal_run_id,
                        GoalStartRequest(),
                        maintenance_guard=maintenance_guard,
                    )
                except GoalManagerConflict:
                    # Failure may have committed a recoverable phase or terminal
                    # budget state. Count that change for mobile invalidation.
                    pass
            else:
                async with self._lock(goal_run_id):
                    await self._advance_ready(
                        goal_run_id,
                        explicit_user_action=False,
                        maintenance_guard=maintenance_guard,
                    )
                    await self._drain_synthesis_nodes(
                        goal_run_id,
                        maintenance_guard=maintenance_guard,
                    )
                    await self._evaluate_if_quiescent(
                        goal_run_id,
                        maintenance_guard=maintenance_guard,
                    )
            refreshed = await self.graph.get_goal(goal_run_id)
            if refreshed is not None and str(refreshed["updated_at"]) != before:
                changed += 1
        async with aiosqlite.connect(self.db_path) as db:
            terminal_with_active_children = await (
                await db.execute(
                    """SELECT DISTINCT g.id
                    FROM goal_runs AS g
                    JOIN plan_nodes AS n ON n.goal_run_id=g.id
                    JOIN tasks AS t ON t.id=n.task_id
                    WHERE g.status IN ('failed','cancelled','budget_exhausted')
                      AND t.status NOT IN ('completed','failed','cancelled')
                    ORDER BY g.updated_at ASC,g.id ASC LIMIT ?""",
                    (bounded_limit,),
                )
            ).fetchall()
        for terminal in terminal_with_active_children:
            if maintenance_guard is not None:
                await maintenance_guard.renew_now()
            changed += await self._cancel_child_tasks(
                str(terminal[0]),
                actor_id="goal-runtime",
                maintenance_guard=maintenance_guard,
            )
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            terminal_rows = await (
                await db.execute(
                    """SELECT g.id,r.goal_run_id AS has_result,e.goal_run_id AS has_episode
                    FROM goal_runs AS g
                    LEFT JOIN goal_results AS r ON r.goal_run_id=g.id
                    LEFT JOIN episodes AS e ON e.goal_run_id=g.id
                    WHERE g.status IN ('completed','failed','cancelled','budget_exhausted')
                      AND (r.goal_run_id IS NULL OR e.goal_run_id IS NULL)
                    ORDER BY g.updated_at ASC,g.id ASC LIMIT ?""",
                    (bounded_limit,),
                )
            ).fetchall()
        for terminal in terminal_rows:
            goal_run_id = str(terminal["id"])
            if terminal["has_result"] is None:
                try:
                    if maintenance_guard is not None:
                        await maintenance_guard.renew_now()
                    await self.result_aggregator.aggregate_goal(goal_run_id)
                    changed += 1
                except (OSError, RuntimeError, TypeError, ValueError, aiosqlite.Error):
                    logger.exception("terminal goal result reconciliation failed: %s", goal_run_id)
            if self.episode_memory is not None and terminal["has_episode"] is None:
                try:
                    if maintenance_guard is not None:
                        await maintenance_guard.renew_now()
                    await self._record_episode(goal_run_id)
                    changed += 1
                except (OSError, RuntimeError, TypeError, ValueError, aiosqlite.Error):
                    logger.exception("terminal goal episode reconciliation failed: %s", goal_run_id)
        return changed
