from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import aiosqlite
import pytest

from app.models import AgentCreate
from app.services.agent_dispatcher import AgentDispatcher
from app.services.episode_memory import EpisodeMemoryService
from app.services.evaluator_provider import (
    DeterministicEvaluatorProvider,
    NoopEvaluatorProvider,
)
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
    GoalStartRequest,
    PlanNodeType,
    SwarmPlanNodeProposal,
    SwarmPlanProposal,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


def _worker_plan(
    *,
    objective: str = "Inspect the repository",
    include_review: bool = False,
) -> SwarmPlanProposal:
    nodes = [
        SwarmPlanNodeProposal(
            temporary_id="files",
            node_type=PlanNodeType.WORKER,
            title="List files",
            objective="List the repository root",
            required_skill="workspace.list_dir",
            dependencies=[],
            expected_output="A bounded repository listing",
            priority=20,
        )
    ]
    if include_review:
        nodes.append(
            SwarmPlanNodeProposal(
                temporary_id="review",
                node_type=PlanNodeType.WORKER,
                title="Review status",
                objective="Review the repository status",
                required_skill="code_review.git_status",
                dependencies=[],
                expected_output="A bounded repository status summary",
                priority=10,
            )
        )
    return SwarmPlanProposal(
        schema_version="1.0",
        objective=objective,
        rationale_summary="Collect independent, read-only evidence.",
        completion_criteria=["Required repository evidence is available"],
        max_parallelism=len(nodes),
        nodes=nodes,
    )


async def _manager(
    db_path: Path,
    plan: SwarmPlanProposal,
    *,
    evaluator: object | None = None,
    episode_memory: EpisodeMemoryService | None = None,
) -> GoalManager:
    policy = PermissionPolicy.from_yaml(REPO_ROOT / "configs" / "permissions.yaml")
    state = StateService(db_path, permission_policy=policy)
    await state.initialize()
    dispatcher = AgentDispatcher(
        db_path,
        SQLiteMessageBoard(db_path),
        permission_policy=policy,
    )
    manager = GoalManager(
        db_path,
        state_service=state,
        agent_dispatcher=dispatcher,
        planner=DeterministicSwarmPlannerProvider(plan),
        evaluator=evaluator or NoopEvaluatorProvider(),  # type: ignore[arg-type]
        permission_policy=policy,
        episode_memory=episode_memory,
    )
    await manager.initialize()
    return manager


async def _create_and_start(
    manager: GoalManager,
    *,
    objective: str = "Inspect the repository",
) -> dict[str, Any]:
    goal = await manager.create_goal(
        GoalCreateRequest(
            objective=objective,
            autonomy_profile=AutonomyProfile.AUTONOMOUS,
            max_runtime_seconds=30,
        ),
        actor_id="test-phone",
    )
    await manager.start_goal(str(goal["id"]), GoalStartRequest())
    return goal


@pytest.mark.asyncio
async def test_reconcile_does_not_start_or_expire_a_created_goal(tmp_path: Path) -> None:
    db_path = tmp_path / "idle-goal.db"
    manager = await _manager(db_path, _worker_plan())
    goal = await manager.create_goal(
        GoalCreateRequest(
            objective="Inspect the repository",
            autonomy_profile=AutonomyProfile.AUTONOMOUS,
            max_runtime_seconds=30,
        ),
        actor_id="test-phone",
    )
    old_created_at = (datetime.now(UTC) - timedelta(days=1)).isoformat()
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "UPDATE goal_runs SET created_at=?,updated_at=? WHERE id=?",
            (old_created_at, old_created_at, goal["id"]),
        )
        await db.commit()

    changed = await manager.reconcile()

    recovered = await manager.get_goal(str(goal["id"]))
    assert recovered is not None
    assert changed == 0
    assert recovered["goal"]["status"] == "planning"
    assert recovered["goal"]["started_at"] is None
    assert recovered["goal"]["model_call_count"] == 0
    assert recovered["nodes"] == []


@pytest.mark.asyncio
async def test_reconcile_resumes_only_after_durable_start_boundary(tmp_path: Path) -> None:
    db_path = tmp_path / "started-planning-goal.db"
    manager = await _manager(db_path, _worker_plan())
    goal = await manager.create_goal(
        GoalCreateRequest(
            objective="Inspect the repository",
            autonomy_profile=AutonomyProfile.AUTONOMOUS,
        ),
        actor_id="test-phone",
    )
    marked = await manager._mark_start_requested(str(goal["id"]))
    assert marked["started_at"] is not None
    assert marked["status"] == "planning"

    changed = await manager.reconcile()

    recovered = await manager.get_goal(str(goal["id"]))
    assert recovered is not None
    assert changed >= 1
    assert recovered["goal"]["status"] == "running"
    assert len(recovered["nodes"]) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("active_status", ["running", "waiting_permission"])
async def test_reconcile_terminalizes_every_expired_active_goal(
    tmp_path: Path,
    active_status: str,
) -> None:
    db_path = tmp_path / f"{active_status}.db"
    manager = await _manager(db_path, _worker_plan())
    goal = await _create_and_start(manager)
    expired_at = (datetime.now(UTC) - timedelta(minutes=5)).isoformat()
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            """UPDATE goal_runs SET status=?,started_at=?,updated_at=? WHERE id=?""",
            (active_status, expired_at, expired_at, goal["id"]),
        )
        await db.commit()

    changed = await manager.reconcile()

    recovered = await manager.get_goal(str(goal["id"]))
    assert recovered is not None
    assert changed >= 1
    assert recovered["goal"]["status"] == "budget_exhausted"
    assert recovered["goal"]["failure_reason"] == "goal runtime budget exhausted"


@pytest.mark.asyncio
async def test_cancel_goal_fences_claimed_and_queued_child_jobs(tmp_path: Path) -> None:
    db_path = tmp_path / "cancel.db"
    manager = await _manager(db_path, _worker_plan(include_review=True))
    goal = await _create_and_start(manager)
    registration = await manager.state_service.register_agent(
        AgentCreate(
            name="File worker",
            endpoint="https://worker.invalid",
            skills=["workspace.list_dir"],
        ),
        "test-phone",
    )
    agent_id = str(registration["id"])
    credential = str(registration["credential"])
    assert await manager.state_service.heartbeat_agent(agent_id, "online", credential)
    claimed = await manager.agent_dispatcher.claim(agent_id)
    assert claimed is not None
    claimed_generation = int(claimed["lease_generation"])
    await manager.on_job_claimed(claimed)

    cancelled = await manager.cancel_goal(str(goal["id"]), actor_id="test-phone")

    assert cancelled["goal"]["status"] == "cancelled"
    assert all(
        node["status"] in {"completed", "failed", "blocked", "cancelled", "skipped"}
        for node in cancelled["nodes"]
    )
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        jobs = await (
            await db.execute(
                """SELECT j.* FROM agent_jobs AS j
                JOIN plan_nodes AS n ON n.task_id=j.task_id
                WHERE n.goal_run_id=? ORDER BY j.id""",
                (goal["id"],),
            )
        ).fetchall()
        child_tasks = await (
            await db.execute(
                """SELECT t.status FROM tasks AS t
                JOIN plan_nodes AS n ON n.task_id=t.id WHERE n.goal_run_id=?""",
                (goal["id"],),
            )
        ).fetchall()
    assert len(jobs) == 2
    assert {str(job["status"]) for job in jobs} == {"cancelled"}
    assert {str(row["status"]) for row in child_tasks} == {"cancelled"}
    claimed_after = next(job for job in jobs if str(job["id"]) == str(claimed["id"]))
    assert int(claimed_after["lease_generation"]) > claimed_generation
    assert claimed_after["lease_id"] is None
    assert claimed_after["lease_token_hash"] is None


@pytest.mark.asyncio
async def test_dispatch_cancel_race_does_not_leave_an_active_child_job(tmp_path: Path) -> None:
    db_path = tmp_path / "dispatch-cancel-race.db"
    plan = _worker_plan()
    dispatching_manager = await _manager(db_path, plan)
    cancelling_manager = await _manager(db_path, plan)
    goal = await dispatching_manager.create_goal(
        GoalCreateRequest(
            objective="Inspect the repository",
            autonomy_profile=AutonomyProfile.AUTONOMOUS,
        ),
        actor_id="test-phone",
    )
    original_queue_job = dispatching_manager.agent_dispatcher.queue_job
    job_committed = asyncio.Event()
    let_dispatch_return = asyncio.Event()

    async def pause_after_job_commit(*args: object, **kwargs: object) -> dict[str, Any]:
        job = await original_queue_job(*args, **kwargs)  # type: ignore[arg-type]
        job_committed.set()
        await let_dispatch_return.wait()
        return job

    dispatching_manager.agent_dispatcher.queue_job = pause_after_job_commit  # type: ignore[method-assign]
    start = asyncio.create_task(dispatching_manager.start_goal(str(goal["id"]), GoalStartRequest()))
    try:
        await asyncio.wait_for(job_committed.wait(), timeout=5)
        cancelled = await cancelling_manager.cancel_goal(str(goal["id"]), actor_id="test-phone")
        assert cancelled["goal"]["status"] == "cancelled"
    finally:
        let_dispatch_return.set()
    await asyncio.wait_for(start, timeout=5)

    async with aiosqlite.connect(db_path) as db:
        active_jobs = int(
            (
                await (
                    await db.execute(
                        """SELECT COUNT(*) FROM agent_jobs AS j
                        JOIN plan_nodes AS n ON n.task_id=j.task_id
                        WHERE n.goal_run_id=?
                          AND j.status IN ('queued','claimed','running')""",
                        (goal["id"],),
                    )
                ).fetchone()
            )[0]
        )
        active_tasks = int(
            (
                await (
                    await db.execute(
                        """SELECT COUNT(*) FROM tasks AS t
                        JOIN plan_nodes AS n ON n.task_id=t.id
                        WHERE n.goal_run_id=?
                          AND t.status NOT IN ('completed','failed','cancelled')""",
                        (goal["id"],),
                    )
                ).fetchone()
            )[0]
        )
    assert active_jobs == 0
    assert active_tasks == 0


@pytest.mark.asyncio
async def test_evaluator_done_rejects_failed_required_worker_evidence(tmp_path: Path) -> None:
    db_path = tmp_path / "failed-required-evidence.db"
    evaluator = DeterministicEvaluatorProvider(
        EvaluationDecision(
            schema_version="1.0",
            status=EvaluationStatus.DONE,
            reason_summary="The available evidence is enough.",
            missing_requirements=[],
            invalid_results=[],
            suggested_new_nodes=[],
            completion_summary="Repository inspection is complete.",
        )
    )
    manager = await _manager(
        db_path,
        _worker_plan(include_review=True),
        evaluator=evaluator,
    )
    goal = await _create_and_start(manager)
    workers: list[tuple[dict[str, Any], str]] = []
    for name, skill in (
        ("File worker", "workspace.list_dir"),
        ("Review worker", "code_review.git_status"),
    ):
        registration = await manager.state_service.register_agent(
            AgentCreate(name=name, endpoint="https://worker.invalid", skills=[skill]),
            "test-phone",
        )
        assert await manager.state_service.heartbeat_agent(
            str(registration["id"]), "online", str(registration["credential"])
        )
        workers.append((registration, skill))

    for registration, skill in workers:
        agent_id = str(registration["id"])
        claimed = await manager.agent_dispatcher.claim(agent_id)
        assert claimed is not None
        await manager.on_job_claimed(claimed)
        succeeds = skill == "workspace.list_dir"
        job, changed = await manager.agent_dispatcher.submit_result(
            agent_id,
            str(claimed["id"]),
            str(claimed["claim_token"]),
            status="completed" if succeeds else "failed",
            result={"entries": ["README.md"]} if succeeds else None,
            error=None if succeeds else "review worker failed",
            lease_id=str(claimed["lease_id"]),
            lease_generation=int(claimed["lease_generation"]),
        )
        assert changed is True
        await manager.on_job_result(job)

    detail = await manager.get_goal(str(goal["id"]))
    assert detail is not None
    assert detail["goal"]["status"] == "failed"
    assert detail["result"] is not None
    assert len(detail["result"]["completed_nodes"]) == 1
    assert len(detail["result"]["failed_nodes"]) == 1
    assert detail["goal"]["failure_reason"] == (
        "evaluator completion lacked acceptable worker evidence"
    )


@pytest.mark.asyncio
async def test_reconcile_rebuilds_terminal_result_and_episode_after_projection_crash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "projection-recovery.db"
    episodes = EpisodeMemoryService(db_path)
    manager = await _manager(db_path, _worker_plan(), episode_memory=episodes)
    goal = await manager.create_goal(
        GoalCreateRequest(objective="Inspect the repository"),
        actor_id="test-phone",
    )
    aggregate_goal = manager.result_aggregator.aggregate_goal
    record_episode = episodes.record_episode

    async def projection_crashed(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise RuntimeError("simulated crash before projection commit")

    monkeypatch.setattr(manager.result_aggregator, "aggregate_goal", projection_crashed)
    monkeypatch.setattr(episodes, "record_episode", projection_crashed)
    await manager._terminate_goal(
        str(goal["id"]),
        status="failed",
        reason="simulated authoritative failure",
    )
    async with aiosqlite.connect(db_path) as db:
        assert (
            await (
                await db.execute(
                    "SELECT COUNT(*) FROM goal_results WHERE goal_run_id=?", (goal["id"],)
                )
            ).fetchone()
        )[0] == 0
        assert (
            await (
                await db.execute("SELECT COUNT(*) FROM episodes WHERE goal_run_id=?", (goal["id"],))
            ).fetchone()
        )[0] == 0

    monkeypatch.setattr(manager.result_aggregator, "aggregate_goal", aggregate_goal)
    monkeypatch.setattr(episodes, "record_episode", record_episode)
    changed = await manager.reconcile()

    detail = await manager.get_goal(str(goal["id"]))
    assert detail is not None
    assert detail["goal"]["status"] == "failed"
    assert detail["result"] is not None
    assert changed >= 2
    async with aiosqlite.connect(db_path) as db:
        episode = await (
            await db.execute("SELECT id,outcome FROM episodes WHERE goal_run_id=?", (goal["id"],))
        ).fetchone()
    assert episode is not None
    assert str(episode[1]) == "failed"
