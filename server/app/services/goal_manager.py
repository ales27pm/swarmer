from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import aiosqlite

from app.models import TaskCreate, TaskMode, TaskRecord, TaskStatus
from app.services.agent_dispatcher import AgentDispatchConflict, AgentDispatcher
from app.services.audit_log import append_audit_event
from app.services.context_builder import (
    ContextCard,
    bound_evaluation_context,
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
from app.services.goal_project import GoalProjectConflict, GoalProjectService
from app.services.goal_state import (
    GoalStateConflict,
    GoalStateService,
    public_goal,
    public_plan_node,
)
from app.services.maintenance_lease import MaintenanceLeaseGuard
from app.services.permission_policy import PermissionPolicy
from app.services.plan_validation import (
    PlanValidationError,
    validate_evaluation_decision,
    validate_swarm_plan,
)
from app.services.planner_provider import SwarmPlannerProvider, SwarmPlannerProviderError
from app.services.remote_job_policy import RemoteJobPolicyError, validate_remote_job
from app.services.result_aggregator import (
    ResultAggregator,
    summarize_untrusted_worker_output,
    validate_worker_evidence,
)
from app.services.state_service import StateService
from app.services.swarm_contracts import (
    AutonomyProfile,
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

logger = logging.getLogger(__name__)
_PLANNER_RETRY_COOLDOWN_SECONDS = 60
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


class GoalManagerConflict(RuntimeError):
    """An authoritative goal invariant rejected a requested transition."""


class _PlannerProposalRejected(GoalManagerConflict):
    """A returned proposal failed validation before any graph mutation."""


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
        context_builder: Any | None = None,
        strategy_retrieval: Any | None = None,
        episode_memory: Any | None = None,
        result_aggregator: Any | None = None,
        default_max_steps: int = 20,
        default_max_parallelism: int = 3,
        default_max_replans: int = 3,
        default_max_runtime_seconds: int = 1_800,
        default_max_model_calls: int = 30,
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
        self.permission_policy = permission_policy
        self.context_builder = context_builder
        self.strategy_retrieval = strategy_retrieval
        self.episode_memory = episode_memory
        self.result_aggregator = result_aggregator or ResultAggregator(db_path)
        self.instance_id = instance_id or f"goal-manager-{uuid4().hex}"
        if not 30 <= model_call_lease_seconds <= 900:
            raise ValueError("model_call_lease_seconds must be between 30 and 900")
        self.model_call_lease_seconds = model_call_lease_seconds
        self.require_execution_workers = require_execution_workers
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
            raise _PlannerProposalRejected("a project plan requires one sequential project worker")
        if proposal.objective == goal["objective"]:
            return proposal
        if model_call_id is not None and proposal.objective == f"goal:{goal['id']}":
            return proposal.model_copy(update={"objective": str(goal["objective"])})
        raise _PlannerProposalRejected("planner changed the authoritative goal objective")

    @staticmethod
    def _runtime_expired(goal: Mapping[str, Any]) -> bool:
        return runtime_expired(goal)

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

    async def recent_conversation(self, goal_id: str, limit: int = 40) -> list[dict[str, str]]:
        history = await self.conversation_messages(goal_id, limit=limit)
        return [
            {"role": item["role"], "content": safe_context_text(item["content"], max_chars=4_000)}
            for item in history["messages"]
        ]

    async def reply_goal(
        self, goal_id: str, request: GoalMessageRequest, *, actor_id: str
    ) -> dict[str, Any]:
        # Seed a prior single-file artifact before creating its linked project continuation.
        if self.project_applications is not None:
            async with aiosqlite.connect(self.db_path) as db:
                legacy = await (
                    await db.execute(
                        """SELECT 1 FROM goal_code_proposals p JOIN goal_runs g ON g.id=p.goal_run_id
                    WHERE g.id=? AND g.status IN ('completed','failed','cancelled','budget_exhausted')
                    AND NOT EXISTS (SELECT 1 FROM goal_project_links l WHERE l.goal_run_id=g.id)
                    LIMIT 1""",
                        (goal_id,),
                    )
                ).fetchone()
            if legacy is not None:
                await self.project_applications.ensure_project(goal_id)
        # Never hold a model-call lock while accepting an independently durable reply.
        try:
            active_id = await self.conversations.append(
                goal_id,
                message=request.message,
                client_message_id=request.client_message_id,
                reply_to_message_id=request.reply_to_message_id,
                actor_id=actor_id,
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
        if node["required_skill"] != PROJECT_SKILL:
            return self._payload_for_node(node)
        if self.project_applications is None:
            raise GoalManagerConflict("The project application gateway is unavailable.")
        goal_id = str(node["goal_run_id"])
        return await self.project_applications.payload(
            goal_id, dict(node), await self.recent_conversation(goal_id)
        )

    async def _resume_pending_conversation(
        self, goal_id: str, *, maintenance_guard: MaintenanceLeaseGuard | None = None
    ) -> None:
        """Prepare the next iteration only after previous work and grants settle."""
        goal = await self.graph.get_goal(goal_id)
        if goal is None or goal["status"] in self.graph.GOAL_TERMINAL:
            return
        pending = int(goal.get("pending_message_revision") or 0) > 0
        if not pending and goal["current_phase"] != "project_continue":
            return
        nodes = await self.graph.list_nodes(goal_id)
        if any(node["status"] in {"dispatched", "running", "waiting_capability"} for node in nodes):
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
                await db.execute("SELECT 1 FROM goal_project_links WHERE goal_run_id=?", (goal_id,))
            ).fetchone()
            project = linked is not None or any(n["required_skill"] == PROJECT_SKILL for n in nodes)
            if project and self.project_applications is not None:
                count = len(nodes)
                now = self._now()
                if count >= int(current["max_steps"]) or int(current["model_call_count"]) >= int(
                    current["max_model_calls"]
                ):
                    await self._terminate_goal_locked(
                        db,
                        dict(current),
                        status="budget_exhausted",
                        reason="goal project iteration budget exhausted",
                        now=now,
                        maintenance_guard=maintenance_guard,
                    )
                    await db.commit()
                    return
                # Unprepared old proposals are superseded; their snapshots remain inspectable.
                await db.execute(
                    """UPDATE plan_nodes SET status='completed',completed_at=?,updated_at=?,
                    result_summary='Project draft retained; newer instructions require another iteration.'
                    WHERE goal_run_id=? AND required_skill=? AND status='waiting_permission'""",
                    (now, now, goal_id, PROJECT_SKILL),
                )
                if any(n["status"] in {"planned", "ready"} for n in nodes):
                    await db.execute(
                        "UPDATE goal_runs SET pending_message_revision=0 WHERE id=?", (goal_id,)
                    )
                    await db.commit()
                    return
                await db.execute(
                    """INSERT INTO plan_nodes(id,goal_run_id,node_type,title,objective,required_skill,
                    status,priority,expected_output,created_at,updated_at,conversation_revision)
                    VALUES(?,?,'worker','Continue project implementation',?,?,'ready',50,?,?,?,?)""",
                    (
                        f"node_{uuid4().hex}",
                        goal_id,
                        current["objective"],
                        PROJECT_SKILL,
                        "A cumulative project snapshot with actual build and test receipts.",
                        now,
                        now,
                        int(current["conversation_revision"]),
                    ),
                )
                # Only a fixed application SQL fragment is interpolated; values are bound.
                await db.execute(
                    f"""UPDATE goal_runs SET status='running',current_phase='project_building',
                    pending_message_revision=0,updated_at=?,{RESUME_RUNTIME_SQL} WHERE id=?""",  # nosec B608
                    (now, now, goal_id),
                )
                await db.execute(
                    "UPDATE tasks SET status='running',updated_at=? WHERE id=?",
                    (now, current["root_task_id"]),
                )
                await db.commit()
                return
        if not nodes or goal["status"] == "planning":
            return
        if any(n["status"] in self.ACTIVE_NODE_STATUSES for n in nodes):
            return
        if int(goal["replan_count"]) >= int(goal["max_replans"]):
            await self._terminate_goal(
                goal_id, status="budget_exhausted", reason="goal replan budget exhausted"
            )
            return
        proposal, source, call_id = await self._obtain_plan(
            goal, GoalStartRequest(), maintenance_guard=maintenance_guard
        )
        await self._append_replan_nodes(goal, proposal, source=source, model_call_id=call_id)
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """UPDATE goal_runs SET pending_message_revision=0 WHERE id=?
                AND conversation_revision=?""",
                (goal_id, goal["conversation_revision"]),
            )
            await db.commit()

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
            action = "continue" if stale else str(result["action"])
            message = (
                "The previous iteration was retained. Continuing with your latest instructions."
                if stale
                else str(result["message"])
            )
            await GoalConversationService.assistant_locked(
                db, goal_id, message, question=action == "clarify", now=now
            )
            waiting = action in {"clarify", "complete"}
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
                    if action == "clarify"
                    else "project_ready"
                    if action == "complete"
                    else "project_continue",
                    "Project needs your clarification."
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
            await db.commit()
        if action == "continue":
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
                mode=(
                    TaskMode.AUTONOME
                    if request.autonomy_profile is AutonomyProfile.AUTONOMOUS
                    else TaskMode.NORMAL
                ),
            ),
            source=actor_id,
        ).model_copy(update={"status": TaskStatus.PLANNED})
        criteria = request.completion_criteria or [
            "Provide an evidence-backed response to the objective"
        ]
        limits = {
            name: getattr(request, name) if getattr(request, name) is not None else default
            for name, default in self.defaults.items()
        }
        if int(limits["max_parallelism"]) > int(limits["max_steps"]):
            limits["max_parallelism"] = int(limits["max_steps"])
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
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
            await GoalConversationService.create_locked(
                db, goal_run_id, request.objective, now.isoformat()
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
            await self._resume_pending_conversation(
                goal_run_id, maintenance_guard=maintenance_guard
            )
            goal = await self.graph.get_goal(goal_run_id)
            if goal is None:
                raise GoalManagerConflict("goal not found")
            nodes = await self.graph.list_nodes(goal_run_id)
            if not nodes and goal["status"] == "planning":
                goal = await self._mark_start_requested(
                    goal_run_id,
                    maintenance_guard=maintenance_guard,
                )
            if self._runtime_expired(goal):
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
                proposal, source, call_id = await self._obtain_plan(
                    goal,
                    request,
                    maintenance_guard=maintenance_guard,
                )
                refreshed_goal = await self.graph.get_goal(goal_run_id)
                if refreshed_goal is None:
                    raise GoalManagerConflict("goal disappeared while its plan was generated")
                if self._runtime_expired(refreshed_goal):
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
                        maintenance_guard=maintenance_guard,
                    )
                except _PlannerProposalRejected:
                    if call_id is not None:
                        await self._record_planner_failure(
                            goal_run_id,
                            call_id,
                            category="invalid_response",
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
                maintenance_guard=maintenance_guard,
            )
            detail = await self.get_goal(goal_run_id)
            if detail is None:
                raise RuntimeError("started goal disappeared")
            return detail

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
            online = await (
                await db.execute(
                    "SELECT 1 FROM agents WHERE status='online' "
                    "AND json_array_length(skills_json)>0 LIMIT 1"
                )
            ).fetchone()
            if online is not None:
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
                for message in recent[-6:]
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
        if self.strategy_retrieval is not None:
            hints = await self.strategy_retrieval.retrieve(str(goal["objective"]))
            raw_hints = hints.as_dict()
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
        context_id: str | None = None
        if self.context_builder is not None:
            built = await self.context_builder.build_for_goal(
                goal_id,
                purpose="planner",
                additional_cards=tuple(additional_cards),
            )
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
            ]
            context_payload = {
                "schema_version": "1.0",
                "purpose": "planner",
                "cards": [card.as_model_dict() for card in fallback_cards],
            }
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
            remaining = self._remaining_runtime_seconds(goal)
            if remaining <= 0:
                raise TimeoutError("goal runtime budget exhausted")
            async with asyncio.timeout(remaining):
                proposal = await self.planner.propose(context_payload)
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
            await self._record_planner_failure(
                str(goal["id"]),
                call_id,
                category=exc.category,
                maintenance_guard=maintenance_guard,
            )
            reason = _PLANNER_FAILURE_DETAILS[exc.category][1]
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
        maintenance_guard: MaintenanceLeaseGuard | None = None,
    ) -> str:
        call_id = f"gmc_{uuid4().hex}"
        now_dt = datetime.now(UTC)
        now = now_dt.isoformat()
        lease_expires_at = (now_dt + timedelta(seconds=self.model_call_lease_seconds)).isoformat()
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            if maintenance_guard is not None:
                await maintenance_guard.require_current_locked(db)
            row = await (
                await db.execute(
                    "SELECT model_call_count,max_model_calls,status,conversation_revision FROM goal_runs WHERE id=?",
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
            if int(row["model_call_count"]) >= int(row["max_model_calls"]):
                await db.rollback()
                raise GoalManagerConflict("goal model call budget exhausted")
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

    async def _record_planner_failure(
        self,
        goal_run_id: str,
        call_id: str,
        *,
        category: str,
        maintenance_guard: MaintenanceLeaseGuard | None = None,
    ) -> bool:
        phase, reason = _PLANNER_FAILURE_DETAILS[category]
        now = self._now()
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            if maintenance_guard is not None:
                await maintenance_guard.require_current_locked(db)
            call = await (
                await db.execute(
                    "SELECT 1 FROM goal_model_calls WHERE id=? AND goal_run_id=? AND role='planner'",
                    (call_id, goal_run_id),
                )
            ).fetchone()
            if call is None:
                await db.rollback()
                raise GoalManagerConflict("planner call does not belong to this goal")
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
            await db.execute(
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
        maintenance_guard: MaintenanceLeaseGuard | None = None,
    ) -> None:
        output_digest = self._model_output_digest(proposal.model_dump(mode="json"))
        proposal = self._bind_plan_to_goal(goal, proposal, model_call_id=model_call_id)
        if len(proposal.nodes) > int(goal["max_steps"]):
            raise _PlannerProposalRejected("plan exceeds the goal step budget")
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
            current = await (
                await db.execute("SELECT * FROM goal_runs WHERE id=?", (goal["id"],))
            ).fetchone()
            if current is None or str(current["status"]) != "planning":
                await db.rollback()
                raise GoalManagerConflict("goal changed while its plan was generated")
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
                        created_at,updated_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
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
            if record is not None and record.get("result_summary"):
                summaries.append(str(record["result_summary"])[:2_000])
        summary = "\n".join(summaries)[:4_000]
        if not summary:
            summary = "No evidence summary was available for synthesis."
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
        objective = str(node["objective"])
        if skill == "workspace.list_dir":
            payload: dict[str, Any] = {"path": "."}
        elif skill == "research.query":
            payload = {"query": objective[:2_000], "max_results": 5}
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
            if node["required_skill"] in {CODE_PROPOSAL_SKILL, PROJECT_SKILL}:
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
                    "goal.codegen.reserved",
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
            cursor = await db.execute(
                """UPDATE plan_nodes SET worker_job_id=?,updated_at=?
                WHERE id=? AND task_id=? AND status='dispatched' AND worker_job_id IS NULL""",
                (job["id"], self._now(), node["id"], child.id),
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
            valid_evidence = validate_worker_evidence(
                node.get("required_skill"),
                job.get("result"),
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

    async def _evaluate_if_quiescent(
        self,
        goal_run_id: str,
        *,
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
        state_fingerprint = self._state_fingerprint(nodes)
        elapsed = int(active_runtime_seconds(goal))
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
                    result_summary=(
                        str(node["result_summary"]) if node.get("result_summary") else None
                    ),
                    failure_reason=(
                        str(node["error_summary"]) if node.get("error_summary") else None
                    ),
                )
                for node in nodes
            ],
            known_node_ids=[str(node["id"]) for node in nodes],
            remaining_step_budget=max(0, int(goal["max_steps"]) - int(goal["step_count"])),
            remaining_model_call_budget=max(
                0, int(goal["max_model_calls"]) - int(goal["model_call_count"])
            ),
            elapsed_seconds=min(elapsed, 86_400),
            state_fingerprint=state_fingerprint,
        )
        context_id: str | None = None
        try:
            if self.context_builder is not None:
                context, recorded = await self.context_builder.build_evaluation_context(
                    context,
                    provenance_ids=(goal_run_id, *(str(node["id"]) for node in nodes)),
                )
                context_id = str(recorded.id)
            else:
                context = bound_evaluation_context(context, max_tokens=2_048)
        except (TypeError, ValueError):
            await self._record_recoverable_error(
                goal_run_id,
                "evaluator_context_unavailable",
                conversation_revision=int(goal.get("conversation_revision") or 0),
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
        try:
            call_id = await self._reserve_model_call(
                goal_run_id,
                role="evaluator",
                conversation_revision=int(goal.get("conversation_revision") or 0),
                context_id=context_id,
                input_digest=input_digest,
                provider_source=self.evaluator.source.value,
                model_id=getattr(self.evaluator, "model", None),
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
            async with asyncio.timeout(remaining):
                decision = await self.evaluator.evaluate(context)
            validated = validate_evaluation_decision(
                decision,
                policy=self.permission_policy,
                known_node_ids=context.known_node_ids,
            )
        except TimeoutError:
            await self._record_model_timeout(
                goal_run_id, call_id, maintenance_guard=maintenance_guard
            )
            return
        except (EvaluatorProviderError, OSError, RuntimeError, TypeError, ValueError):
            await self._finish_model_call(
                call_id,
                status="failed",
                maintenance_guard=maintenance_guard,
            )
            await self._record_recoverable_error(
                goal_run_id,
                "evaluator_unavailable",
                conversation_revision=int(goal.get("conversation_revision") or 0),
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
                maintenance_guard=maintenance_guard,
            )
        except (GoalManagerConflict, GoalStateConflict) as exc:
            await self._finish_model_call(
                call_id,
                status="failed",
                maintenance_guard=maintenance_guard,
            )
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
                completed_evidence = any(
                    node["node_type"] == "worker" and node["status"] == "completed"
                    for node in current_nodes
                )
                failed_required_evidence = any(
                    node["node_type"] == "worker"
                    and node["status"] in {"failed", "blocked", "cancelled"}
                    for node in current_nodes
                )
                if (
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
                    terminal_reason = "goal replan budget exhausted"
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
        by_temp = {proposal.temporary_id: f"node_{uuid4().hex}" for proposal in proposals}
        known = {str(node["id"]) for node in existing}
        for proposal in proposals:
            node_id = by_temp[proposal.temporary_id]
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
                depends_on_json,expected_output,planner_metadata_json,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
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
                        {"source": "evaluator", "temporary_id": proposal.temporary_id},
                        separators=(",", ":"),
                    ),
                    now,
                    now,
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
                status,
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
                    ORDER BY task_id""",
                    (goal_run_id, goal_run_id, goal_run_id),
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
            proposal, source, call_id = await self._obtain_plan(
                goal,
                start_request,
                user_guidance=request.reason,
            )
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
                        goal_run_id, call_id, category="invalid_response"
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
            except BaseException:
                if call_id is not None:
                    await self._finish_model_call(call_id, status="failed")
                raise
            await self._advance_ready(goal_run_id, explicit_user_action=True)
            result = await self.get_goal(goal_run_id)
            if result is None:
                raise RuntimeError("replanned goal disappeared")
            return result

    async def _append_replan_nodes(
        self,
        goal: Mapping[str, Any],
        proposal: SwarmPlanProposal,
        *,
        source: PlannerSource,
        model_call_id: str | None,
    ) -> None:
        output_digest = self._model_output_digest(proposal.model_dump(mode="json"))
        proposal = self._bind_plan_to_goal(goal, proposal, model_call_id=model_call_id)
        existing = await self.graph.list_nodes(str(goal["id"]))
        if len(existing) + len(proposal.nodes) > int(goal["max_steps"]):
            raise GoalManagerConflict("replan exceeds the goal step budget")
        by_temp = {node.temporary_id: f"node_{uuid4().hex}" for node in proposal.nodes}
        now = self._now()
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            current = await (
                await db.execute(
                    """SELECT replan_count,max_replans,status,plan_fingerprint,max_steps
                    FROM goal_runs WHERE id=?""",
                    (goal["id"],),
                )
            ).fetchone()
            if current is None or str(current["status"]) not in {"running", "waiting_permission"}:
                await db.rollback()
                raise GoalManagerConflict("goal changed while replanning")
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
            if int(goal.get("pending_message_revision") or 0):
                await db.execute(
                    """UPDATE plan_nodes SET status='skipped',updated_at=?,completed_at=?,
                    result_summary='Superseded by newer user instructions.'
                    WHERE goal_run_id=? AND status IN ('planned','ready')""",
                    (now, now, goal["id"]),
                )
            for node in proposal.nodes:
                hard_dependencies = sorted(by_temp[item] for item in node.dependencies)
                optional_dependencies = sorted(by_temp[item] for item in node.optional_dependencies)
                dependencies = sorted(set(hard_dependencies + optional_dependencies))
                node_id = by_temp[node.temporary_id]
                await db.execute(
                    """INSERT INTO plan_nodes(
                    id,goal_run_id,node_type,title,objective,required_skill,status,priority,
                    depends_on_json,expected_output,planner_metadata_json,created_at,updated_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
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
                            {"source": "replan", "temporary_id": node.temporary_id},
                            separators=(",", ":"),
                        ),
                        now,
                        now,
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
                    validate_swarm_plan(
                        proposal,
                        policy=self.permission_policy,
                        max_nodes=int(goal["max_steps"]),
                        max_parallelism=int(goal["max_parallelism"]),
                    ).fingerprint,
                    proposal.max_parallelism,
                    now,
                    goal["id"],
                ),
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
            await db.commit()
        await self.graph.refresh_ready_nodes(str(goal["id"]))

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
                AND NOT EXISTS (SELECT 1 FROM plan_nodes n WHERE n.goal_run_id=g.id
                    AND n.status IN ('dispatched','running','waiting_capability'))
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
                      AND (
                        ?=0 OR current_phase<>'waiting_for_workers'
                        OR EXISTS (
                            SELECT 1 FROM agents WHERE status='online'
                              AND json_array_length(skills_json)>0
                        )
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
                        int(self.require_execution_workers),
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
