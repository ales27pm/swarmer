from __future__ import annotations

import asyncio
from pathlib import Path

import aiosqlite
import pytest

from app.models import AgentCreate
from app.services.agent_dispatcher import AgentDispatcher
from app.services.evaluator_provider import (
    DeterministicEvaluatorProvider,
    NoopEvaluatorProvider,
)
from app.services.goal_manager import GoalManager, GoalManagerConflict
from app.services.goal_state import GoalStateConflict, GoalStateService
from app.services.message_board import SQLiteMessageBoard
from app.services.permission_policy import PermissionPolicy
from app.services.planner_provider import DeterministicSwarmPlannerProvider
from app.services.state_service import SCHEMA_VERSION, StateService
from app.services.swarm_contracts import (
    AutonomyProfile,
    EvaluationDecision,
    EvaluationStatus,
    GoalCreateRequest,
    GoalStartRequest,
    PlannerSource,
    PlanNodeType,
    SwarmPlanNodeProposal,
    SwarmPlanProposal,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.asyncio
async def test_state_initialization_adds_restart_safe_goal_dag_schema(tmp_path: Path) -> None:
    db_path = tmp_path / "state.db"
    state = StateService(db_path)

    await state.initialize()
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            """
            INSERT INTO tasks(
                id,title,input,mode,source,conversation_id,status,priority,
                created_at,updated_at,completed_at,error_json
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                "tsk_goal_root",
                "Inspect the repository",
                "Inspect the repository",
                "normal",
                "test-device",
                None,
                "created",
                0,
                "2026-09-08T00:00:00+00:00",
                "2026-09-08T00:00:00+00:00",
                None,
                None,
            ),
        )
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
                "goal_restart_safe",
                "tsk_goal_root",
                "Inspect the repository",
                "planning",
                "assisted",
                "test",
                20,
                3,
                3,
                1_800,
                30,
                0,
                0,
                0,
                '["Repository evidence is summarized"]',
                "planning",
                "2026-09-08T00:00:00+00:00",
                "2026-09-08T00:00:00+00:00",
            ),
        )
        await db.commit()

    await state.initialize()

    async with aiosqlite.connect(db_path) as db:
        tables = {
            str(row[0])
            for row in await (
                await db.execute("SELECT name FROM sqlite_master WHERE type='table'")
            ).fetchall()
        }
        assert {
            "goal_runs",
            "plan_nodes",
            "plan_edges",
            "goal_evaluations",
            "goal_results",
            "goal_feedback",
            "goal_model_calls",
            "goal_contexts",
            "episodes",
            "episode_steps",
            "episode_embeddings",
        }.issubset(tables)
        version = int((await (await db.execute("PRAGMA user_version")).fetchone())[0])
        assert version == SCHEMA_VERSION
        assert (
            await (
                await db.execute(
                    "SELECT objective,status FROM goal_runs WHERE id='goal_restart_safe'"
                )
            ).fetchone()
        ) == ("Inspect the repository", "planning")


async def _seed_goal_graph(db_path: Path, *, dependency_type: str = "hard") -> None:
    state = StateService(db_path)
    await state.initialize()
    created_at = "2026-09-08T00:00:00+00:00"
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            """INSERT INTO tasks(
                id,title,input,mode,source,status,priority,created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?)""",
            (
                "tsk_graph_root",
                "Graph goal",
                "Graph goal",
                "normal",
                "test",
                "created",
                0,
                created_at,
                created_at,
            ),
        )
        await db.execute(
            """INSERT INTO goal_runs(
                id,root_task_id,objective,status,autonomy_profile,planner_source,
                max_steps,max_parallelism,max_replans,max_runtime_seconds,max_model_calls,
                completion_criteria_json,current_phase,created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "goal_graph",
                "tsk_graph_root",
                "Graph goal",
                "running",
                "autonomous",
                "test",
                20,
                3,
                3,
                1_800,
                30,
                '["All evidence collected"]',
                "execution",
                created_at,
                created_at,
            ),
        )
        for node_id, title in (("node_a", "Collect"), ("node_b", "Analyze")):
            dependencies = "[]" if node_id == "node_a" else '["node_a"]'
            await db.execute(
                """INSERT INTO plan_nodes(
                    id,goal_run_id,node_type,title,objective,required_skill,status,
                    priority,depends_on_json,expected_output,planner_metadata_json,
                    created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    node_id,
                    "goal_graph",
                    "worker",
                    title,
                    title,
                    "workspace.list_dir",
                    "planned",
                    0,
                    dependencies,
                    "Evidence",
                    "{}",
                    created_at,
                    created_at,
                ),
            )
        await db.execute(
            "INSERT INTO plan_edges VALUES(?,?,?,?)",
            ("goal_graph", "node_a", "node_b", dependency_type),
        )
        await db.commit()


@pytest.mark.asyncio
async def test_ready_nodes_follow_hard_dag_dependencies(tmp_path: Path) -> None:
    db_path = tmp_path / "state.db"
    await _seed_goal_graph(db_path)
    graph = GoalStateService(db_path)

    assert await graph.refresh_ready_nodes("goal_graph") == ["node_a"]
    await graph.transition_node("node_a", expected="ready", target="running")
    await graph.transition_node("node_a", expected="running", target="completed")
    assert await graph.refresh_ready_nodes("goal_graph") == ["node_b"]


@pytest.mark.asyncio
async def test_hard_dependency_failure_blocks_downstream_node(tmp_path: Path) -> None:
    db_path = tmp_path / "state.db"
    await _seed_goal_graph(db_path)
    graph = GoalStateService(db_path)

    await graph.refresh_ready_nodes("goal_graph")
    await graph.transition_node("node_a", expected="ready", target="running")
    await graph.transition_node(
        "node_a",
        expected="running",
        target="failed",
        error_summary="worker unavailable",
    )
    assert await graph.refresh_ready_nodes("goal_graph") == []
    node = await graph.get_node("node_b")
    assert node is not None
    assert node["status"] == "blocked"
    assert node["error_summary"] == "hard dependency failed"


@pytest.mark.asyncio
async def test_optional_dependency_failure_does_not_block_downstream(tmp_path: Path) -> None:
    db_path = tmp_path / "state.db"
    await _seed_goal_graph(db_path, dependency_type="optional")
    graph = GoalStateService(db_path)

    await graph.refresh_ready_nodes("goal_graph")
    await graph.transition_node("node_a", expected="ready", target="running")
    await graph.transition_node("node_a", expected="running", target="failed")
    assert await graph.refresh_ready_nodes("goal_graph") == ["node_b"]


@pytest.mark.asyncio
async def test_terminal_node_cannot_be_resurrected(tmp_path: Path) -> None:
    db_path = tmp_path / "state.db"
    await _seed_goal_graph(db_path)
    graph = GoalStateService(db_path)

    await graph.refresh_ready_nodes("goal_graph")
    await graph.transition_node("node_a", expected="ready", target="running")
    await graph.transition_node("node_a", expected="running", target="completed")
    with pytest.raises(GoalStateConflict, match="completed"):
        await graph.transition_node("node_a", expected="completed", target="ready")


def _parallel_plan(*, objective: str = "Inspect the repository") -> SwarmPlanProposal:
    return SwarmPlanProposal(
        schema_version="1.0",
        objective=objective,
        rationale_summary="Collect independent evidence, then synthesize it.",
        completion_criteria=["Repository evidence is summarized"],
        max_parallelism=2,
        nodes=[
            SwarmPlanNodeProposal(
                temporary_id="files",
                node_type=PlanNodeType.WORKER,
                title="List files",
                objective="List the repository root",
                required_skill="workspace.list_dir",
                dependencies=[],
                expected_output="A bounded root listing",
                priority=20,
            ),
            SwarmPlanNodeProposal(
                temporary_id="status",
                node_type=PlanNodeType.WORKER,
                title="Inspect status",
                objective="Inspect the repository status",
                required_skill="code_review.git_status",
                dependencies=[],
                expected_output="A bounded status summary",
                priority=10,
            ),
            SwarmPlanNodeProposal(
                temporary_id="synthesis",
                node_type=PlanNodeType.SYNTHESIS,
                title="Synthesize",
                objective="Combine the independent evidence",
                required_skill=None,
                dependencies=["files", "status"],
                expected_output="An evidence-backed answer",
                priority=0,
            ),
        ],
    )


async def _manager(
    tmp_path: Path,
    plan: SwarmPlanProposal,
    *,
    evaluator: object | None = None,
) -> GoalManager:
    db_path = tmp_path / "state.db"
    policy = PermissionPolicy.from_yaml(REPO_ROOT / "configs" / "permissions.yaml")
    state = StateService(db_path, permission_policy=policy)
    await state.initialize()
    dispatcher = AgentDispatcher(
        db_path,
        SQLiteMessageBoard(db_path),
        permission_policy=policy,
    )
    return GoalManager(
        db_path,
        state_service=state,
        agent_dispatcher=dispatcher,
        planner=DeterministicSwarmPlannerProvider(plan),
        evaluator=evaluator or NoopEvaluatorProvider(),  # type: ignore[arg-type]
        permission_policy=policy,
    )


@pytest.mark.asyncio
async def test_goal_manager_persists_validated_dag_and_parallel_child_jobs(
    tmp_path: Path,
) -> None:
    manager = await _manager(tmp_path, _parallel_plan())
    goal = await manager.create_goal(
        GoalCreateRequest(
            objective="Inspect the repository",
            autonomy_profile=AutonomyProfile.AUTONOMOUS,
            completion_criteria=["Repository evidence is summarized"],
            max_parallelism=2,
        ),
        actor_id="test-phone",
    )

    detail = await manager.start_goal(goal["id"], GoalStartRequest())

    assert detail["goal"]["status"] == "running"
    assert detail["goal"]["planner_source"] == "test"
    assert len(detail["nodes"]) == 3
    by_title = {node["title"]: node for node in detail["nodes"]}
    assert by_title["List files"]["status"] == "dispatched"
    assert by_title["Inspect status"]["status"] == "dispatched"
    assert by_title["Synthesize"]["status"] == "planned"
    assert by_title["List files"]["task_id"] != goal["root_task_id"]
    assert by_title["Inspect status"]["task_id"] != goal["root_task_id"]
    assert by_title["List files"]["worker_job_id"]
    assert by_title["Inspect status"]["worker_job_id"]
    assert by_title["Synthesize"]["depends_on"] == sorted(
        [by_title["List files"]["id"], by_title["Inspect status"]["id"]]
    )

    async with aiosqlite.connect(manager.db_path) as db:
        active_per_child = await (
            await db.execute(
                """SELECT task_id,COUNT(*) FROM agent_jobs
                WHERE status IN ('queued','claimed','running') GROUP BY task_id"""
            )
        ).fetchall()
        model_call = await (
            await db.execute(
                """SELECT provider_source,model_id,input_digest,output_digest,status,latency_ms
                FROM goal_model_calls WHERE goal_run_id=? AND role='planner'""",
                (goal["id"],),
            )
        ).fetchone()
    assert sorted(count for _, count in active_per_child) == [1, 1]
    assert model_call is not None
    assert model_call[0:2] == ("test", None)
    assert len(str(model_call[2])) == 64
    assert len(str(model_call[3])) == 64
    assert model_call[4] == "completed"
    assert int(model_call[5]) >= 0


@pytest.mark.asyncio
async def test_validated_plan_parallelism_is_an_effective_execution_cap(
    tmp_path: Path,
) -> None:
    proposal = _parallel_plan().model_copy(update={"max_parallelism": 1})
    manager = await _manager(tmp_path, proposal)
    goal = await manager.create_goal(
        GoalCreateRequest(
            objective="Inspect the repository",
            autonomy_profile=AutonomyProfile.AUTONOMOUS,
            max_parallelism=3,
        ),
        actor_id="test-phone",
    )

    detail = await manager.start_goal(goal["id"], GoalStartRequest())

    assert detail["goal"]["max_parallelism"] == 1
    assert sum(node["status"] == "dispatched" for node in detail["nodes"]) == 1


@pytest.mark.asyncio
async def test_parallelism_cap_is_atomic_across_goal_manager_instances(
    tmp_path: Path,
) -> None:
    race_dir = tmp_path / "parallelism-race"
    proposal = _parallel_plan().model_copy(update={"max_parallelism": 1})
    first = await _manager(race_dir, proposal)
    created = await first.create_goal(
        GoalCreateRequest(
            objective="Inspect the repository",
            autonomy_profile=AutonomyProfile.AUTONOMOUS,
            max_parallelism=3,
        ),
        actor_id="test-phone",
    )
    started = await first._mark_start_requested(str(created["id"]))
    await first._persist_initial_plan(
        started,
        proposal,
        source=PlannerSource.TEST,
        model_call_id=None,
    )
    second = await _manager(race_dir, proposal)
    goal = await first.graph.get_goal(str(created["id"]))
    nodes = await first.graph.list_nodes(str(created["id"]))
    assert goal is not None
    ready_workers = [node for node in nodes if node["node_type"] == "worker"]

    await asyncio.gather(
        first._dispatch_worker_node(goal, ready_workers[0]),
        second._dispatch_worker_node(goal, ready_workers[1]),
    )

    final = await first.get_goal(str(created["id"]))
    assert final is not None
    assert final["goal"]["step_count"] == 1
    assert sum(node["status"] == "dispatched" for node in final["nodes"]) == 1
    async with aiosqlite.connect(first.db_path) as db:
        active_jobs = await (
            await db.execute(
                """SELECT COUNT(*) FROM agent_jobs
                WHERE status IN ('queued','claimed','running')"""
            )
        ).fetchone()
    assert active_jobs == (1,)


@pytest.mark.asyncio
async def test_goal_manager_rejects_plan_that_exceeds_goal_step_budget(tmp_path: Path) -> None:
    manager = await _manager(tmp_path, _parallel_plan())
    goal = await manager.create_goal(
        GoalCreateRequest(
            objective="Inspect the repository",
            max_steps=2,
            max_parallelism=2,
        ),
        actor_id="test-phone",
    )

    with pytest.raises(GoalManagerConflict, match="step budget"):
        await manager.start_goal(goal["id"], GoalStartRequest())
    recovered = await manager.get_goal(goal["id"])
    assert recovered is not None
    assert recovered["goal"]["status"] == "planning"
    assert recovered["nodes"] == []


@pytest.mark.asyncio
async def test_manual_goal_dispatches_only_one_node_per_explicit_start(tmp_path: Path) -> None:
    manager = await _manager(tmp_path, _parallel_plan())
    goal = await manager.create_goal(
        GoalCreateRequest(
            objective="Inspect the repository",
            autonomy_profile=AutonomyProfile.MANUAL,
            max_parallelism=2,
        ),
        actor_id="test-phone",
    )

    first = await manager.start_goal(goal["id"], GoalStartRequest())
    assert sum(node["status"] == "dispatched" for node in first["nodes"]) == 1
    second = await manager.start_goal(goal["id"], GoalStartRequest())
    assert sum(node["status"] == "dispatched" for node in second["nodes"]) == 2


@pytest.mark.asyncio
async def test_goal_finishes_only_after_server_observed_worker_evidence(tmp_path: Path) -> None:
    evaluator = DeterministicEvaluatorProvider(
        EvaluationDecision(
            schema_version="1.0",
            status=EvaluationStatus.DONE,
            reason_summary="The bounded worker evidence satisfies the goal.",
            missing_requirements=[],
            invalid_results=[],
            suggested_new_nodes=[],
            completion_summary="Repository evidence is complete.",
        )
    )
    manager = await _manager(tmp_path, _parallel_plan(), evaluator=evaluator)
    goal = await manager.create_goal(
        GoalCreateRequest(
            objective="Inspect the repository",
            autonomy_profile=AutonomyProfile.AUTONOMOUS,
            max_parallelism=2,
        ),
        actor_id="test-phone",
    )
    detail = await manager.start_goal(goal["id"], GoalStartRequest())
    registrations = []
    for name, skill in (
        ("Goal file worker", "workspace.list_dir"),
        ("Goal review worker", "code_review.git_status"),
    ):
        registration = await manager.state_service.register_agent(
            AgentCreate(
                name=name,
                endpoint="https://worker.invalid",
                skills=[skill],
                max_concurrency=1,
            ),
            "test-phone",
        )
        agent_id = str(registration["id"])
        credential = str(registration["credential"])
        assert await manager.state_service.heartbeat_agent(agent_id, "online", credential)
        registrations.append(registration)

    for registration in registrations:
        agent_id = str(registration["id"])
        claimed = await manager.agent_dispatcher.claim(agent_id)
        assert claimed is not None
        await manager.on_job_claimed(claimed)
        result = (
            {"entries": ["README.md"]}
            if claimed["required_skill"] == "workspace.list_dir"
            else {
                "content_trust": "untrusted",
                "entries": [],
                "protected_entries_omitted": 0,
                "truncated": False,
            }
        )
        job, changed = await manager.agent_dispatcher.submit_result(
            agent_id,
            str(claimed["id"]),
            str(claimed["claim_token"]),
            status="completed",
            result=result,
            error=None,
            lease_id=str(claimed["lease_id"]),
            lease_generation=int(claimed["lease_generation"]),
        )
        assert changed is True
        detail = await manager.on_job_result(job)
        assert detail is not None

    assert detail["goal"]["status"] == "completed"
    assert detail["result"] is not None
    assert detail["result"]["status"] == "completed"
    assert len(detail["result"]["completed_nodes"]) == 3
    assert detail["result"]["failed_nodes"] == []
    assert sorted(detail["result"]["agents_used"]) == sorted(
        str(registration["id"]) for registration in registrations
    )
    assert detail["goal"].get("plan_fingerprint") is None
    assert all("planner_metadata" not in node for node in detail["nodes"])


@pytest.mark.asyncio
async def test_done_without_completed_worker_evidence_is_rejected(tmp_path: Path) -> None:
    plan = SwarmPlanProposal(
        schema_version="1.0",
        objective="Answer without evidence",
        rationale_summary="Attempt an inert synthesis.",
        completion_criteria=["Evidence exists"],
        max_parallelism=1,
        nodes=[
            SwarmPlanNodeProposal(
                temporary_id="synthesis",
                node_type=PlanNodeType.SYNTHESIS,
                title="Synthesize",
                objective="Synthesize",
                required_skill=None,
                dependencies=[],
                expected_output="An answer",
                priority=0,
            )
        ],
    )
    evaluator = DeterministicEvaluatorProvider(
        EvaluationDecision(
            schema_version="1.0",
            status=EvaluationStatus.DONE,
            reason_summary="The model says it is done.",
            missing_requirements=[],
            invalid_results=[],
            suggested_new_nodes=[],
            completion_summary="Done.",
        )
    )
    manager = await _manager(tmp_path, plan, evaluator=evaluator)
    goal = await manager.create_goal(
        GoalCreateRequest(objective="Answer without evidence"),
        actor_id="test-phone",
    )

    detail = await manager.start_goal(goal["id"], GoalStartRequest())

    assert detail["goal"]["status"] == "failed"
    assert detail["result"] is not None
    assert "1/1 completed" in detail["result"]["answer"]
    assert detail["goal"]["failure_reason"] == (
        "evaluator completion lacked acceptable worker evidence"
    )


@pytest.mark.asyncio
async def test_malformed_completed_worker_result_fails_goal_node(tmp_path: Path) -> None:
    evaluator = DeterministicEvaluatorProvider(
        EvaluationDecision(
            schema_version="1.0",
            status=EvaluationStatus.DONE,
            reason_summary="The worker claimed completion.",
            missing_requirements=[],
            invalid_results=[],
            suggested_new_nodes=[],
        )
    )
    manager = await _manager(tmp_path, _parallel_plan(), evaluator=evaluator)
    goal = await manager.create_goal(
        GoalCreateRequest(
            objective="Inspect the repository",
            autonomy_profile=AutonomyProfile.AUTONOMOUS,
            max_parallelism=2,
        ),
        actor_id="test-phone",
    )
    await manager.start_goal(goal["id"], GoalStartRequest())
    registrations = []
    for name, skill in (
        ("Malformed file worker", "workspace.list_dir"),
        ("Valid review worker", "code_review.git_status"),
    ):
        registration = await manager.state_service.register_agent(
            AgentCreate(name=name, endpoint="https://worker.invalid", skills=[skill]),
            "test-phone",
        )
        assert await manager.state_service.heartbeat_agent(
            str(registration["id"]), "online", str(registration["credential"])
        )
        registrations.append(registration)

    for registration in registrations:
        agent_id = str(registration["id"])
        claimed = await manager.agent_dispatcher.claim(agent_id)
        assert claimed is not None
        await manager.on_job_claimed(claimed)
        result = (
            {}
            if claimed["required_skill"] == "workspace.list_dir"
            else {
                "content_trust": "untrusted",
                "entries": [],
                "protected_entries_omitted": 0,
                "truncated": False,
            }
        )
        job, changed = await manager.agent_dispatcher.submit_result(
            agent_id,
            str(claimed["id"]),
            str(claimed["claim_token"]),
            status="completed",
            result=result,
            error=None,
            lease_id=str(claimed["lease_id"]),
            lease_generation=int(claimed["lease_generation"]),
        )
        assert changed is True
        detail = await manager.on_job_result(job)
        assert detail is not None

    assert detail["goal"]["status"] == "failed"
    invalid_node = next(
        node for node in detail["nodes"] if node["required_skill"] == "workspace.list_dir"
    )
    assert invalid_node["status"] == "failed"
    assert invalid_node["error_summary"] == "worker completed without valid skill evidence"
