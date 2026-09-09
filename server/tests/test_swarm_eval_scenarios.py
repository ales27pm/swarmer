from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import aiosqlite
import pytest

from app.models import AgentCreate
from app.services.agent_dispatcher import AgentDispatchConflict, AgentDispatcher
from app.services.agent_lease_reaper import AgentLeaseReaper
from app.services.evaluator_provider import DeterministicEvaluatorProvider
from app.services.goal_manager import GoalManager
from app.services.message_board import SQLiteMessageBoard
from app.services.permission_policy import PermissionPolicy
from app.services.planner_provider import DeterministicSwarmPlannerProvider
from app.services.state_service import StateService
from app.services.swarm_contracts import (
    AutonomyProfile,
    EvaluationDecision,
    EvaluationStatus,
    GoalCreateRequest,
    GoalEvaluationContext,
    GoalReplanRequest,
    GoalStartRequest,
    PlannerSource,
    PlanNodeType,
    SwarmPlanNodeProposal,
    SwarmPlanProposal,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


@dataclass
class MutableClock:
    value: datetime

    def __call__(self) -> datetime:
        return self.value

    def advance(self, seconds: int) -> None:
        self.value += timedelta(seconds=seconds)


@dataclass
class ScenarioRuntime:
    db_path: Path
    state: StateService
    board: SQLiteMessageBoard
    dispatcher: AgentDispatcher
    manager: GoalManager
    policy: PermissionPolicy
    clock: MutableClock | None = None


@dataclass
class CapturingPlanner:
    proposal: SwarmPlanProposal
    source: PlannerSource = PlannerSource.TEST
    contexts: list[dict[str, object]] = field(default_factory=list)

    async def propose(self, context: Any) -> SwarmPlanProposal:
        self.contexts.append(dict(context))
        return self.proposal.model_copy(deep=True)


@dataclass
class CapturingEvaluator:
    decision: EvaluationDecision
    source: PlannerSource = PlannerSource.TEST
    contexts: list[GoalEvaluationContext] = field(default_factory=list)

    async def evaluate(self, context: GoalEvaluationContext) -> EvaluationDecision:
        self.contexts.append(context.model_copy(deep=True))
        return self.decision.model_copy(deep=True)


@dataclass(frozen=True)
class FakeStrategyHints:
    payload: dict[str, object]

    def as_dict(self) -> dict[str, object]:
        return json.loads(json.dumps(self.payload))


@dataclass
class FakeStrategyRetrieval:
    hints: FakeStrategyHints
    queries: list[str] = field(default_factory=list)

    async def retrieve(self, query: str) -> FakeStrategyHints:
        self.queries.append(query)
        return self.hints


def _worker(
    temporary_id: str,
    title: str,
    skill: str,
    *,
    dependencies: list[str] | None = None,
    priority: int = 10,
) -> SwarmPlanNodeProposal:
    return SwarmPlanNodeProposal(
        temporary_id=temporary_id,
        node_type=PlanNodeType.WORKER,
        title=title,
        objective=title,
        required_skill=skill,
        dependencies=dependencies or [],
        expected_output=f"Bounded evidence for {title}",
        priority=priority,
    )


def _synthesis(
    *,
    dependencies: list[str],
    temporary_id: str = "synthesis",
) -> SwarmPlanNodeProposal:
    return SwarmPlanNodeProposal(
        temporary_id=temporary_id,
        node_type=PlanNodeType.SYNTHESIS,
        title="Synthesize evidence",
        objective="Synthesize only the server-observed evidence",
        required_skill=None,
        dependencies=dependencies,
        expected_output="An evidence-backed public summary",
        priority=0,
    )


def _plan(
    objective: str,
    nodes: list[SwarmPlanNodeProposal],
    *,
    max_parallelism: int = 2,
) -> SwarmPlanProposal:
    return SwarmPlanProposal(
        schema_version="1.0",
        objective=objective,
        rationale_summary="Collect bounded independent evidence before evaluation.",
        completion_criteria=["The requested evidence is evaluated"],
        max_parallelism=max_parallelism,
        nodes=nodes,
    )


def _evaluation(
    status: EvaluationStatus,
    reason: str,
    *,
    missing: list[str] | None = None,
    invalid: list[str] | None = None,
    question: str | None = None,
) -> EvaluationDecision:
    return EvaluationDecision(
        schema_version="1.0",
        status=status,
        reason_summary=reason,
        missing_requirements=missing or [],
        invalid_results=invalid or [],
        suggested_new_nodes=[],
        user_question=question,
        completion_summary=("Evidence evaluated." if status is EvaluationStatus.DONE else None),
    )


async def _runtime(
    tmp_path: Path,
    plan: SwarmPlanProposal,
    *,
    evaluator: Any | None = None,
    planner: Any | None = None,
    strategy_retrieval: Any | None = None,
    clock: MutableClock | None = None,
) -> ScenarioRuntime:
    db_path = tmp_path / "state.db"
    policy = PermissionPolicy.from_yaml(REPO_ROOT / "configs" / "permissions.yaml")
    state = StateService(db_path, permission_policy=policy)
    await state.initialize()
    board = SQLiteMessageBoard(db_path)
    dispatcher = AgentDispatcher(
        db_path,
        board,
        permission_policy=policy,
        lease_seconds=60,
        clock=clock,
    )
    manager = GoalManager(
        db_path,
        state_service=state,
        agent_dispatcher=dispatcher,
        planner=planner or DeterministicSwarmPlannerProvider(plan),
        evaluator=evaluator
        or DeterministicEvaluatorProvider(
            _evaluation(EvaluationStatus.DONE, "The evidence is sufficient.")
        ),
        permission_policy=policy,
        strategy_retrieval=strategy_retrieval,
    )
    await manager.initialize()
    return ScenarioRuntime(db_path, state, board, dispatcher, manager, policy, clock)


async def _create_and_start(
    runtime: ScenarioRuntime,
    objective: str,
    *,
    max_parallelism: int = 2,
    max_model_calls: int = 30,
    max_replans: int = 3,
) -> tuple[dict[str, Any], dict[str, Any]]:
    goal = await runtime.manager.create_goal(
        GoalCreateRequest(
            objective=objective,
            autonomy_profile=AutonomyProfile.AUTONOMOUS,
            completion_criteria=["The requested evidence is evaluated"],
            max_parallelism=max_parallelism,
            max_model_calls=max_model_calls,
            max_replans=max_replans,
        ),
        actor_id="test-phone",
    )
    detail = await runtime.manager.start_goal(goal["id"], GoalStartRequest())
    return goal, detail


async def _set_agent_seen(db_path: Path, agent_id: str, at: datetime) -> None:
    timestamp = at.isoformat()
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "UPDATE agents SET last_seen_at=?,last_heartbeat_at=? WHERE id=?",
            (timestamp, timestamp, agent_id),
        )
        await db.commit()


async def _register_agent(
    runtime: ScenarioRuntime,
    name: str,
    skill: str,
) -> dict[str, Any]:
    registration = await runtime.state.register_agent(
        AgentCreate(
            name=name,
            endpoint="https://worker.invalid",
            skills=[skill],
            max_concurrency=1,
        ),
        "test-phone",
    )
    assert await runtime.state.heartbeat_agent(
        str(registration["id"]),
        "online",
        str(registration["credential"]),
    )
    if runtime.clock is not None:
        await _set_agent_seen(runtime.db_path, str(registration["id"]), runtime.clock())
    return registration


async def _claim_and_finish(
    runtime: ScenarioRuntime,
    registration: dict[str, Any],
    *,
    status: str = "completed",
    result: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any] | None]:
    agent_id = str(registration["id"])
    claimed = await runtime.dispatcher.claim(agent_id)
    assert claimed is not None
    await runtime.manager.on_job_claimed(claimed)
    job, changed = await runtime.dispatcher.submit_result(
        agent_id,
        str(claimed["id"]),
        str(claimed["claim_token"]),
        status=status,
        result=result if status == "completed" else None,
        error="deterministic worker failure" if status == "failed" else None,
        lease_id=str(claimed["lease_id"]),
        lease_generation=int(claimed["lease_generation"]),
    )
    assert changed is True
    detail = await runtime.manager.on_job_result(job)
    return claimed, job, detail


@pytest.mark.asyncio
async def test_scenario_parallel_repository_analysis_dispatches_independent_jobs(
    tmp_path: Path,
) -> None:
    objective = "Analyze repository status and diff in parallel"
    plan = _plan(
        objective,
        [
            _worker("status", "Inspect repository status", "code_review.git_status", priority=20),
            _worker("diff", "Inspect repository diff", "code_review.git_diff", priority=10),
            _synthesis(dependencies=["status", "diff"]),
        ],
    )
    runtime = await _runtime(tmp_path, plan)

    _, detail = await _create_and_start(runtime, objective)

    by_title = {node["title"]: node for node in detail["nodes"]}
    assert by_title["Inspect repository status"]["status"] == "dispatched"
    assert by_title["Inspect repository diff"]["status"] == "dispatched"
    assert by_title["Synthesize evidence"]["status"] == "planned"
    assert {
        by_title["Inspect repository status"]["task_id"],
        by_title["Inspect repository diff"]["task_id"],
    }.isdisjoint({detail["goal"]["root_task_id"]})
    assert detail["goal"]["step_count"] == 2


@pytest.mark.asyncio
async def test_scenario_research_and_file_jobs_keep_bounded_payloads(
    tmp_path: Path,
) -> None:
    objective = "Research a release and list local evidence"
    plan = _plan(
        objective,
        [
            _worker("research", "Research the bounded release question", "research.query"),
            _worker("files", "List local evidence files", "workspace.list_dir"),
        ],
    )
    runtime = await _runtime(tmp_path, plan)

    await _create_and_start(runtime, objective)

    async with aiosqlite.connect(runtime.db_path) as db:
        rows = await (
            await db.execute(
                "SELECT required_skill,payload_json FROM agent_jobs ORDER BY required_skill"
            )
        ).fetchall()
    payloads = {str(skill): json.loads(str(payload)) for skill, payload in rows}
    assert payloads == {
        "research.query": {
            "max_results": 5,
            "query": "Research the bounded release question",
        },
        "workspace.list_dir": {"path": "."},
    }


@pytest.mark.asyncio
async def test_scenario_worker_failure_keeps_evaluator_limitations_authoritative(
    tmp_path: Path,
) -> None:
    objective = "Combine local and researched evidence"
    evaluator = DeterministicEvaluatorProvider(
        _evaluation(
            EvaluationStatus.DONE,
            "Local evidence exists, but external evidence is unavailable.",
            missing=["External evidence is unavailable"],
            invalid=["The failed research node has no verified result"],
        )
    )
    plan = _plan(
        objective,
        [
            _worker("files", "List local files", "workspace.list_dir"),
            _worker("research", "Research external evidence", "research.query"),
        ],
    )
    runtime = await _runtime(tmp_path, plan, evaluator=evaluator)
    _goal, _ = await _create_and_start(runtime, objective)
    file_worker = await _register_agent(runtime, "File worker", "workspace.list_dir")
    research_worker = await _register_agent(runtime, "Research worker", "research.query")

    await _claim_and_finish(runtime, file_worker, result={"entries": ["README.md"]})
    _, _, detail = await _claim_and_finish(runtime, research_worker, status="failed")

    assert detail is not None
    assert detail["goal"]["status"] == "failed"
    assert detail["goal"]["evaluator_status"] == "done"
    assert "external evidence is unavailable" in detail["goal"]["evaluator_summary"].casefold()
    assert len(detail["result"]["completed_nodes"]) == 1
    assert len(detail["result"]["failed_nodes"]) == 1
    assert any(
        "remote worker reported failure" in limitation
        for limitation in detail["result"]["limitations"]
    )


@pytest.mark.asyncio
async def test_scenario_evaluator_needs_user_enters_permission_wait(
    tmp_path: Path,
) -> None:
    objective = "Inspect files, then ask before broadening scope"
    evaluator = DeterministicEvaluatorProvider(
        _evaluation(
            EvaluationStatus.NEEDS_USER,
            "The next scope requires an explicit user choice.",
            question="Should the swarm inspect generated artifacts too?",
        )
    )
    plan = _plan(
        objective,
        [_worker("files", "List repository files", "workspace.list_dir")],
        max_parallelism=1,
    )
    runtime = await _runtime(tmp_path, plan, evaluator=evaluator)
    goal, _ = await _create_and_start(runtime, objective, max_parallelism=1)
    worker = await _register_agent(runtime, "File worker", "workspace.list_dir")

    _, _, detail = await _claim_and_finish(runtime, worker, result={"entries": ["src"]})

    assert detail is not None
    assert detail["goal"]["status"] == "waiting_permission"
    assert detail["goal"]["current_phase"] == "needs_user"
    assert detail["goal"]["evaluator_status"] == "needs_user"
    assert detail["result"] is None
    async with aiosqlite.connect(runtime.db_path) as db:
        row = await (
            await db.execute(
                "SELECT decision_json FROM goal_evaluations WHERE goal_run_id=?",
                (goal["id"],),
            )
        ).fetchone()
    assert row is not None
    assert json.loads(str(row[0]))["user_question"] == (
        "Should the swarm inspect generated artifacts too?"
    )


@pytest.mark.asyncio
async def test_scenario_stale_worker_lease_cannot_override_reassigned_result(
    tmp_path: Path,
) -> None:
    objective = "List repository files after worker recovery"
    clock = MutableClock(datetime.now(UTC))
    plan = _plan(
        objective,
        [_worker("files", "List repository files", "workspace.list_dir")],
        max_parallelism=1,
    )
    runtime = await _runtime(tmp_path, plan, clock=clock)
    goal, _ = await _create_and_start(runtime, objective, max_parallelism=1)
    first = await _register_agent(runtime, "First file worker", "workspace.list_dir")
    first_claim = await runtime.dispatcher.claim(str(first["id"]))
    assert first_claim is not None
    await runtime.manager.on_job_claimed(first_claim)

    second = await _register_agent(runtime, "Second file worker", "workspace.list_dir")
    clock.advance(61)
    await _set_agent_seen(runtime.db_path, str(second["id"]), clock())
    reaper = AgentLeaseReaper(
        runtime.db_path,
        runtime.board,
        permission_policy=runtime.policy,
        clock=clock,
    )
    counts = await reaper.reap_expired()
    assert counts["requeued"] == 1
    second_claim = await runtime.dispatcher.claim(str(second["id"]))
    assert second_claim is not None
    assert second_claim["id"] == first_claim["id"]
    assert second_claim["lease_generation"] == first_claim["lease_generation"] + 1

    with pytest.raises(AgentDispatchConflict, match="stale"):
        await runtime.dispatcher.submit_result(
            str(first["id"]),
            str(first_claim["id"]),
            str(first_claim["claim_token"]),
            status="completed",
            result={"entries": ["STALE"]},
            error=None,
            lease_id=str(first_claim["lease_id"]),
            lease_generation=int(first_claim["lease_generation"]),
        )
    before = await runtime.manager.get_goal(goal["id"])
    assert before is not None
    assert before["goal"]["status"] == "running"

    await runtime.manager.on_job_claimed(second_claim)
    job, changed = await runtime.dispatcher.submit_result(
        str(second["id"]),
        str(second_claim["id"]),
        str(second_claim["claim_token"]),
        status="completed",
        result={"entries": ["README.md"]},
        error=None,
        lease_id=str(second_claim["lease_id"]),
        lease_generation=int(second_claim["lease_generation"]),
    )
    assert changed is True
    completed = await runtime.manager.on_job_result(job)
    assert completed is not None
    assert completed["goal"]["status"] == "completed"
    assert completed["result"]["agents_used"] == [str(second["id"])]


@pytest.mark.asyncio
async def test_scenario_equivalent_replan_is_stopped_as_a_loop(tmp_path: Path) -> None:
    objective = "List files with explicit follow-up"
    plan = _plan(
        objective,
        [_worker("files", "List repository files", "workspace.list_dir")],
        max_parallelism=1,
    )
    evaluator = DeterministicEvaluatorProvider(
        _evaluation(
            EvaluationStatus.NEEDS_USER,
            "User confirmation is required.",
            question="Should the same plan run again?",
        )
    )
    runtime = await _runtime(tmp_path, plan, evaluator=evaluator)
    goal, _ = await _create_and_start(runtime, objective, max_parallelism=1)
    worker = await _register_agent(runtime, "File worker", "workspace.list_dir")
    await _claim_and_finish(runtime, worker, result={"entries": ["README.md"]})

    detail = await runtime.manager.replan_goal(goal["id"], GoalReplanRequest())

    assert detail["goal"]["status"] == "failed"
    assert detail["goal"]["failure_reason"] == "planner proposed an equivalent plan"
    assert detail["result"]["status"] == "failed"


@pytest.mark.asyncio
async def test_scenario_user_cancellation_fences_active_worker_completion(
    tmp_path: Path,
) -> None:
    objective = "Cancel an active repository inspection"
    plan = _plan(
        objective,
        [_worker("files", "List repository files", "workspace.list_dir")],
        max_parallelism=1,
    )
    runtime = await _runtime(tmp_path, plan)
    goal, _ = await _create_and_start(runtime, objective, max_parallelism=1)
    worker = await _register_agent(runtime, "File worker", "workspace.list_dir")
    claimed = await runtime.dispatcher.claim(str(worker["id"]))
    assert claimed is not None
    await runtime.manager.on_job_claimed(claimed)

    detail = await runtime.manager.cancel_goal(goal["id"], actor_id="test-phone")

    assert detail["goal"]["status"] == "cancelled"
    assert {node["status"] for node in detail["nodes"]} == {"cancelled"}
    assert detail["result"]["status"] == "cancelled"
    with pytest.raises(AgentDispatchConflict):
        await runtime.dispatcher.submit_result(
            str(worker["id"]),
            str(claimed["id"]),
            str(claimed["claim_token"]),
            status="completed",
            result={"entries": ["late"]},
            error=None,
            lease_id=str(claimed["lease_id"]),
            lease_generation=int(claimed["lease_generation"]),
        )
    unchanged = await runtime.manager.get_goal(goal["id"])
    assert unchanged is not None
    assert unchanged["goal"]["status"] == "cancelled"


@pytest.mark.asyncio
async def test_scenario_model_call_budget_exhaustion_stops_before_evaluation(
    tmp_path: Path,
) -> None:
    objective = "List files within one model call"
    plan = _plan(
        objective,
        [_worker("files", "List repository files", "workspace.list_dir")],
        max_parallelism=1,
    )
    runtime = await _runtime(tmp_path, plan)
    _goal, _ = await _create_and_start(
        runtime,
        objective,
        max_parallelism=1,
        max_model_calls=1,
    )
    worker = await _register_agent(runtime, "File worker", "workspace.list_dir")

    _, _, detail = await _claim_and_finish(runtime, worker, result={"entries": ["README.md"]})

    assert detail is not None
    assert detail["goal"]["status"] == "budget_exhausted"
    assert detail["goal"]["model_call_count"] == 1
    assert detail["goal"]["failure_reason"] == "goal model call budget exhausted"
    assert detail["result"]["status"] == "budget_exhausted"


@pytest.mark.asyncio
async def test_scenario_episode_strategy_hints_reach_planner_as_bounded_context(
    tmp_path: Path,
) -> None:
    objective = "Review repository status using prior lessons"
    plan = _plan(
        objective,
        [_worker("status", "Inspect repository status", "code_review.git_status")],
        max_parallelism=1,
    )
    planner = CapturingPlanner(plan)
    hints = FakeStrategyHints(
        {
            "successful": [
                {
                    "kind": "success",
                    "source_id": "ep_success",
                    "text": "Prior successful objective: pin repository identity.",
                    "relevance": 0.9,
                }
            ],
            "failures": [
                {
                    "kind": "failure",
                    "source_id": "ep_failure",
                    "text": "Avoid snapshot-replaced.",
                    "relevance": 0.8,
                }
            ],
            "memory": [],
            "provenance_ids": ["ep_success", "ep_failure"],
        }
    )
    retrieval = FakeStrategyRetrieval(hints)
    runtime = await _runtime(
        tmp_path,
        plan,
        planner=planner,
        strategy_retrieval=retrieval,
    )

    await _create_and_start(runtime, objective, max_parallelism=1)

    assert retrieval.queries == [objective]
    assert len(planner.contexts) == 1
    cards = planner.contexts[0]["cards"]
    assert isinstance(cards, list)
    strategy_cards = [card for card in cards if card["kind"] == "strategy_hint"]
    assert [card["summary"] for card in strategy_cards] == [
        "successful strategy hint: Prior successful objective: pin repository identity.",
        "failures strategy hint: Avoid snapshot-replaced.",
    ]
    encoded = json.dumps(strategy_cards, sort_keys=True)
    assert "ep_success" not in encoded
    assert "ep_failure" not in encoded
    assert "temporary_id" not in encoded
    assert "dependencies" not in encoded


@pytest.mark.asyncio
async def test_scenario_conflicting_evidence_requires_user_resolution(
    tmp_path: Path,
) -> None:
    objective = "Resolve conflicting release evidence"
    plan = _plan(
        objective,
        [
            _worker("files", "Read local release evidence", "workspace.list_dir"),
            _worker("research", "Research published release evidence", "research.query"),
        ],
    )
    evaluator = CapturingEvaluator(
        _evaluation(
            EvaluationStatus.NEEDS_USER,
            "Two server-observed sources disagree about release readiness.",
            question="Which evidence source should define release readiness?",
        )
    )
    runtime = await _runtime(tmp_path, plan, evaluator=evaluator)
    goal, _ = await _create_and_start(runtime, objective)
    file_worker = await _register_agent(runtime, "File worker", "workspace.list_dir")
    research_worker = await _register_agent(runtime, "Research worker", "research.query")

    await _claim_and_finish(
        runtime,
        file_worker,
        result={"entries": ["release_ready:true"]},
    )
    _, _, detail = await _claim_and_finish(
        runtime,
        research_worker,
        result={
            "content_trust": "untrusted",
            "results": [
                {
                    "title": "Release status",
                    "url": "https://example.invalid/release",
                    "snippet": "release_ready:false",
                }
            ],
        },
    )

    assert detail is not None
    assert detail["goal"]["status"] == "waiting_permission"
    assert detail["goal"]["evaluator_status"] == "needs_user"
    assert detail["result"] is None
    assert len(evaluator.contexts) == 1
    summaries = [node.result_summary for node in evaluator.contexts[0].node_results]
    assert any("release_ready:true" in str(summary) for summary in summaries)
    assert any("release_ready:false" in str(summary) for summary in summaries)
    async with aiosqlite.connect(runtime.db_path) as db:
        row = await (
            await db.execute(
                "SELECT decision_json FROM goal_evaluations WHERE goal_run_id=?",
                (goal["id"],),
            )
        ).fetchone()
    assert row is not None
    decision = json.loads(str(row[0]))
    assert decision["user_question"] == ("Which evidence source should define release readiness?")
