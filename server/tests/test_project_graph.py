from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from unittest.mock import AsyncMock

import aiosqlite
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.services.goal_manager import GoalManagerConflict
from app.services.project_contracts import project_digest
from app.services.project_graph import ProjectGraphEvidenceError, read_project_graph
from app.services.swarm_contracts import GoalCreateRequest, PlannerSource
from tests.test_goal_runtime_recovery import _manager, _worker_plan
from tests.test_task_execution import seed

NOW = "2026-09-27T12:00:00+00:00"


async def graph_evidence(path: Path):
    manager = await _manager(path, _worker_plan())
    goal, _ = await seed(manager, count=3)
    snapshot = {
        "files": [{"path": "crm.py", "content": "print('private-source')\n"}],
        "checks": [
            {
                "command": ["python", "-m", "pytest", "-q"],
                "status": "passed",
                "exit_code": 0,
                "duration_ms": 123,
                "output": "private-runner-output",
            }
        ],
        "message": "private-model-draft",
    }
    async with aiosqlite.connect(path) as db:
        await db.execute(
            "UPDATE goal_runs SET completion_criteria_json=?,conversation_revision=2 WHERE id=?",
            (json.dumps(["Conserver les clients", "Exporter les clients"]), goal["id"]),
        )
        await db.execute(
            "UPDATE plan_nodes SET depends_on_json='[\"node_00\",\"node_01\"]' WHERE id='node_02'"
        )
        await db.executemany(
            "INSERT INTO plan_edges VALUES(?,?,?,?)",
            [
                (goal["id"], "node_00", "node_02", "hard"),
                (goal["id"], "node_01", "node_02", "optional"),
            ],
        )
        await db.execute("INSERT INTO coding_projects VALUES('project_one',?,?)", (NOW, NOW))
        await db.execute(
            "UPDATE goal_project_links SET project_id='project_one' WHERE goal_run_id=?",
            (goal["id"],),
        )
        await db.execute(
            """INSERT INTO project_revisions(id,project_id,goal_run_id,node_id,worker_job_id,
            revision,snapshot_json,sha256,created_at) VALUES('revision_one','project_one',?,
            'node_02','job_2',1,?,?,?)""",
            (goal["id"], json.dumps(snapshot), project_digest(snapshot["files"]), NOW),
        )
        await db.execute(
            """INSERT INTO goal_evaluations VALUES('evaluation_one',?,1,'continue',?,?,
            'private-state-fingerprint','private-decision-fingerprint',?)""",
            (
                goal["id"],
                "Vérifier les exports. token=private-evaluation-secret",
                json.dumps(
                    {
                        "missing_requirements": ["Export CSV à vérifier"],
                        "invalid_results": [],
                        "raw_private_reasoning": "private-chain",
                        "suggested_new_nodes": [
                            {
                                "worker_arguments": {"secret": "private-worker-argument"},
                            }
                        ],
                    }
                ),
                NOW,
            ),
        )
        await db.commit()
    return manager, goal, snapshot


@pytest.mark.asyncio
async def test_graph_preserves_parallel_and_optional_dependencies_without_claiming_coverage(
    tmp_path,
):
    path = tmp_path / "state.db"
    _, goal, _ = await graph_evidence(path)
    graph = await read_project_graph(path, goal["id"])
    assert graph is not None
    assert graph.goal.id == goal["id"] and graph.conversation_revision == 2
    assert [node.id for node in graph.nodes] == ["node_00", "node_01", "node_02"]
    assert [edge.model_dump() for edge in graph.dependencies] == [
        {"from_node_id": "node_00", "to_node_id": "node_02", "dependency_type": "hard"},
        {"from_node_id": "node_01", "to_node_id": "node_02", "dependency_type": "optional"},
    ]
    assert not graph.nodes[0].depends_on and not graph.nodes[1].depends_on
    assert all(node.status == "completed" for node in graph.nodes)
    assert [criterion.text for criterion in graph.criteria] == [
        "Conserver les clients",
        "Exporter les clients",
    ]
    assert all(criterion.coverage == "not_mapped" for criterion in graph.criteria)
    assert graph.coverage.criterion_mapping == "not_recorded"
    assert graph.coverage.scope == "current_goal_and_latest_project_revision"


@pytest.mark.asyncio
async def test_graph_links_exact_revision_manifest_and_receipts_without_source_or_logs(tmp_path):
    path = tmp_path / "state.db"
    _, goal, snapshot = await graph_evidence(path)
    graph = await read_project_graph(path, goal["id"])
    assert graph is not None and graph.latest_revision is not None
    revision = graph.latest_revision
    assert revision.goal_run_id == goal["id"]
    assert revision.node_id == "node_02" and revision.worker_job_id == "job_2"
    assert revision.sha256 == project_digest(snapshot["files"])
    assert revision.files[0].sha256 == hashlib.sha256(b"print('private-source')\n").hexdigest()
    assert revision.files[0].bytes == len(b"print('private-source')\n")
    assert revision.files[0].path == "crm.py"
    assert revision.checks[0].status == "passed"
    assert revision.checks[0].provenance == "attached_to_revision"
    assert graph.coverage.check_freshness == "not_established"
    for private in (
        "private-source",
        "private-runner-output",
        "private-model-draft",
        "raw-payload",
        "private-chain",
        "private-worker-argument",
        "private-evaluation-secret",
        "private-title",
        "private-summary",
        "private-error",
        "/home/user",
        "private-state-fingerprint",
    ):
        assert private not in graph.model_dump_json()
    decision = graph.evaluations[0]
    assert decision.id == "evaluation_one" and decision.authority == "model_report"
    assert decision.missing_requirements == ["Export CSV à vérifier"]
    assert "Vérifier les exports" in decision.reason_summary
    assert graph.coverage.planner_rationale == "not_recorded"


@pytest.mark.asyncio
async def test_graph_read_is_stable_and_has_no_database_or_goal_side_effects(tmp_path):
    path = tmp_path / "state.db"
    _, goal, _ = await graph_evidence(path)
    async with aiosqlite.connect(path) as db:
        before = [line async for line in db.iterdump()]
    first = await read_project_graph(path, goal["id"])
    second = await read_project_graph(path, goal["id"])
    assert first is not None and second is not None
    assert first.model_dump(exclude={"observed_at"}) == second.model_dump(exclude={"observed_at"})
    async with aiosqlite.connect(path) as db:
        assert [line async for line in db.iterdump()] == before
    assert await read_project_graph(path, "missing") is None


@pytest.mark.asyncio
async def test_empty_graph_is_distinct_from_unknown_goal_and_isolates_other_projects(tmp_path):
    path = tmp_path / "state.db"
    manager, goal, _ = await graph_evidence(path)
    other = await manager.create_goal(GoalCreateRequest(objective="Autre projet"), actor_id="phone")
    graph = await read_project_graph(path, other["id"])
    assert graph is not None and graph.goal.objective == "Autre projet"
    assert graph.project_id is not None and graph.project_id != "project_one"
    assert graph.latest_revision is None
    assert not graph.nodes and not graph.dependencies and not graph.evaluations
    assert goal["id"] not in graph.model_dump_json()


@pytest.mark.asyncio
async def test_linked_revision_keeps_original_producer_when_read_from_successor_goal(tmp_path):
    path = tmp_path / "state.db"
    manager, goal, _ = await graph_evidence(path)
    successor = await manager.create_goal(
        GoalCreateRequest(objective="Poursuivre le CRM"),
        actor_id="phone",
    )
    async with aiosqlite.connect(path) as db:
        await db.execute(
            "UPDATE goal_project_links SET project_id='project_one' WHERE goal_run_id=?",
            (successor["id"],),
        )
        await db.commit()
    graph = await read_project_graph(path, successor["id"])
    assert graph is not None and graph.latest_revision is not None
    assert graph.latest_revision.goal_run_id == goal["id"]
    assert graph.latest_revision.node_id == "node_02"
    assert graph.nodes == [] and graph.evaluations == []


@pytest.mark.asyncio
async def test_decision_history_is_bounded_and_explicitly_has_more(tmp_path):
    path = tmp_path / "state.db"
    _, goal, _ = await graph_evidence(path)
    async with aiosqlite.connect(path) as db:
        for sequence in range(2, 8):
            await db.execute(
                """INSERT INTO goal_evaluations SELECT ?,goal_run_id,?,status,reason_summary,
                decision_json,state_fingerprint,decision_fingerprint,created_at
                FROM goal_evaluations WHERE id='evaluation_one'""",
                (f"evaluation_{sequence}", sequence),
            )
        await db.commit()
    graph = await read_project_graph(path, goal["id"])
    assert graph is not None and graph.evaluations_has_more
    assert [evaluation.sequence for evaluation in graph.evaluations] == [7, 6, 5, 4, 3]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "damage",
    [
        "UPDATE project_revisions SET sha256='0000000000000000000000000000000000000000000000000000000000000000'",
        "UPDATE project_revisions SET snapshot_json=json_set(snapshot_json,'$.checks[0].exit_code',1)",
        "UPDATE project_revisions SET snapshot_json=json_set(snapshot_json,'$.checks[0].duration_ms',-1)",
        "UPDATE project_revisions SET snapshot_json=json_set(snapshot_json,'$.files[0].path','../secret')",
        "UPDATE project_revisions SET worker_job_id='job_0'",
        "UPDATE plan_nodes SET depends_on_json='[]' WHERE id='node_02'",
        "UPDATE plan_edges SET from_node_id='absent' WHERE from_node_id='node_00'",
        "UPDATE goal_runs SET completion_criteria_json='[42]'",
    ],
)
async def test_corrupt_evidence_is_unavailable_not_silently_repaired(tmp_path, damage):
    path = tmp_path / "state.db"
    _, goal, _ = await graph_evidence(path)
    async with aiosqlite.connect(path) as db:
        await db.execute(damage)
        await db.commit()
    with pytest.raises(ProjectGraphEvidenceError):
        await read_project_graph(path, goal["id"])


@pytest.mark.asyncio
async def test_unknown_command_is_not_disclosed_but_receipt_status_is_preserved(tmp_path):
    path = tmp_path / "state.db"
    _, goal, _ = await graph_evidence(path)
    async with aiosqlite.connect(path) as db:
        await db.execute(
            """UPDATE project_revisions SET snapshot_json=json_set(snapshot_json,
            '$.checks[0].command',json('["curl","https://private-host?token=private-credential"]'))"""
        )
        await db.commit()
    graph = await read_project_graph(path, goal["id"])
    assert graph is not None and graph.latest_revision is not None
    assert graph.latest_revision.checks[0].command is None
    assert graph.latest_revision.checks[0].status == "passed"
    assert "private-host" not in graph.model_dump_json()


def test_graph_endpoint_is_authenticated_private_and_never_reconciles(
    test_app: FastAPI,
    client: TestClient,
    paired_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
):
    assert client.portal is not None
    goal, _ = client.portal.call(seed, test_app.state.goal_manager)
    reconcile = AsyncMock(side_effect=AssertionError("GET graph must never reconcile"))
    monkeypatch.setattr(test_app.state.goal_manager, "get_goal", reconcile)
    path = f"/goals/{goal['id']}/graph"
    assert client.get(path).status_code == 401
    response = client.get(path, headers=paired_headers)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "private, no-store"
    assert response.json()["coverage"]["mode"] == "persisted_records"
    assert client.get("/goals/missing/graph", headers=paired_headers).status_code == 404
    reconcile.assert_not_called()


@pytest.mark.asyncio
async def test_new_plan_summary_is_persisted_redacted_and_links_accepted_node_ids(tmp_path):
    path = tmp_path / "state.db"
    proposal = _worker_plan().model_copy(
        update={
            "rationale_summary": "Lister les fichiers avant examen. token=private-plan-secret",
        }
    )
    manager = await _manager(path, proposal)
    goal = await manager.create_goal(
        GoalCreateRequest(objective=proposal.objective), actor_id="phone"
    )
    started = await manager._mark_start_requested(goal["id"])
    await manager._persist_initial_plan(
        started,
        proposal,
        source=PlannerSource.MANUAL,
        model_call_id=None,
    )
    graph = await read_project_graph(path, goal["id"])
    assert graph is not None and len(graph.planning_decisions) == 1
    decision = graph.planning_decisions[0]
    assert decision.event_type == "goal.plan.accepted"
    assert decision.node_ids == [node.id for node in graph.nodes]
    assert decision.authority == "planner_proposal"
    assert decision.planner_source == "manual" and decision.model_id is None
    assert decision.conversation_revision == graph.conversation_revision == 0
    assert graph.coverage.planner_rationale == "recorded"
    assert "Lister les fichiers" in decision.rationale_summary
    assert "private-plan-secret" not in graph.model_dump_json()
    async with aiosqlite.connect(path) as db:
        event = await (
            await db.execute(
                "SELECT payload_json FROM audit_events WHERE id=?",
                (decision.audit_event_id,),
            )
        ).fetchone()
        assert "private-plan-secret" not in event[0]
        assert json.loads(event[0])["rationale_summary"] == decision.rationale_summary


@pytest.mark.asyncio
async def test_legacy_plan_events_have_no_invented_summary(tmp_path):
    path = tmp_path / "state.db"
    _, goal, _ = await graph_evidence(path)
    from app.services.audit_log import append_audit_event

    async with aiosqlite.connect(path) as db:
        await append_audit_event(
            db,
            "goal.plan.accepted",
            {"goal_run_id": goal["id"]},
            task_id=goal["root_task_id"],
            trace_id=goal["id"],
            actor_type="control-plane",
            actor_id="goal-manager",
            created_at=NOW,
        )
        await db.commit()
    graph = await read_project_graph(path, goal["id"])
    assert graph is not None and graph.planning_decisions == []
    assert graph.coverage.planner_rationale == "not_recorded"


@pytest.mark.asyncio
async def test_replanning_summary_is_scoped_to_new_nodes_and_stale_reply_cannot_append(tmp_path):
    path = tmp_path / "state.db"
    proposal = _worker_plan()
    manager = await _manager(path, proposal)
    goal = await manager.create_goal(
        GoalCreateRequest(objective=proposal.objective), actor_id="phone"
    )
    started = await manager._mark_start_requested(goal["id"])
    await manager._persist_initial_plan(
        started,
        proposal,
        source=PlannerSource.MANUAL,
        model_call_id=None,
    )
    original = await read_project_graph(path, goal["id"])
    refreshed = await manager.graph.get_goal(goal["id"])
    assert refreshed is not None and original is not None
    revision = proposal.model_copy(
        update={
            "rationale_summary": "Comparer le statut après inventaire.",
            "nodes": [proposal.nodes[0].model_copy(update={"title": "Vérifier l'inventaire"})],
        }
    )
    await manager._append_replan_nodes(
        refreshed,
        revision,
        source=PlannerSource.MANUAL,
        model_call_id=None,
    )
    graph = await read_project_graph(path, goal["id"])
    assert graph is not None and len(graph.planning_decisions) == 2
    latest = graph.planning_decisions[0]
    assert latest.event_type == "goal.replan.accepted"
    assert set(latest.node_ids) == {node.id for node in graph.nodes} - {
        node.id for node in original.nodes
    }
    with pytest.raises(GoalManagerConflict):
        await manager._append_replan_nodes(
            refreshed,
            revision,
            source=PlannerSource.MANUAL,
            model_call_id=None,
        )
    after = await read_project_graph(path, goal["id"])
    assert after is not None and after.planning_decisions == graph.planning_decisions


@pytest.mark.asyncio
async def test_cancelled_goal_cannot_record_acceptance_reasoning(tmp_path):
    path = tmp_path / "state.db"
    proposal = _worker_plan()
    manager = await _manager(path, proposal)
    goal = await manager.create_goal(
        GoalCreateRequest(objective=proposal.objective), actor_id="phone"
    )
    started = await manager._mark_start_requested(goal["id"])
    async with aiosqlite.connect(path) as db:
        await db.execute("UPDATE goal_runs SET status='cancelled' WHERE id=?", (goal["id"],))
        await db.commit()
    with pytest.raises(GoalManagerConflict):
        await manager._persist_initial_plan(
            started,
            proposal,
            source=PlannerSource.MANUAL,
            model_call_id=None,
        )
    graph = await read_project_graph(path, goal["id"])
    assert graph is not None and graph.planning_decisions == [] and graph.nodes == []


@pytest.mark.asyncio
async def test_graph_reads_one_consistent_snapshot_during_concurrent_update(tmp_path, monkeypatch):
    from app.services import project_graph

    path = tmp_path / "state.db"
    _, goal, _ = await graph_evidence(path)
    async with aiosqlite.connect(path) as db:
        await db.execute("PRAGMA journal_mode=WAL")
    entered, release = asyncio.Event(), asyncio.Event()
    original = project_graph._read_revision

    async def pause(db, project_id):
        entered.set()
        await release.wait()
        return await original(db, project_id)

    monkeypatch.setattr(project_graph, "_read_revision", pause)
    pending = asyncio.create_task(read_project_graph(path, goal["id"]))
    await asyncio.wait_for(entered.wait(), 2)
    try:
        async with aiosqlite.connect(path) as db:
            await db.execute(
                "UPDATE goal_runs SET conversation_revision=3 WHERE id=?", (goal["id"],)
            )
            await db.execute("DELETE FROM goal_evaluations WHERE id='evaluation_one'")
            await db.commit()
    finally:
        release.set()
    graph = await pending
    assert graph is not None and graph.conversation_revision == 2 and len(graph.evaluations) == 1
    fresh = await read_project_graph(path, goal["id"])
    assert fresh is not None and fresh.conversation_revision == 3 and fresh.evaluations == []


@pytest.mark.asyncio
async def test_plan_model_identity_uses_exact_call_and_rejects_wrong_role(tmp_path):
    path = tmp_path / "state.db"
    proposal = _worker_plan()
    manager = await _manager(path, proposal)
    goal = await manager.create_goal(
        GoalCreateRequest(objective=proposal.objective), actor_id="phone"
    )
    started = await manager._mark_start_requested(goal["id"])
    call_id = await manager._reserve_model_call(
        goal["id"],
        role="planner",
        context_id=None,
        input_digest="unused",
        provider_source="test",
        model_id="local-planner:9b",
        conversation_revision=0,
    )
    await manager._persist_initial_plan(
        started,
        proposal,
        source=PlannerSource.TEST,
        model_call_id=call_id,
    )
    graph = await read_project_graph(path, goal["id"])
    assert graph is not None
    assert graph.planning_decisions[0].model_call_id == call_id
    assert graph.planning_decisions[0].model_id == "local-planner:9b"
    async with aiosqlite.connect(path) as db:
        await db.execute("UPDATE goal_model_calls SET role='evaluator' WHERE id=?", (call_id,))
        await db.commit()
    with pytest.raises(ProjectGraphEvidenceError):
        await read_project_graph(path, goal["id"])


@pytest.mark.asyncio
async def test_acceptance_audit_rolls_back_with_fenced_model_call(tmp_path, monkeypatch):
    from app.services import goal_manager

    path = tmp_path / "state.db"
    proposal = _worker_plan()
    manager = await _manager(path, proposal)
    goal = await manager.create_goal(
        GoalCreateRequest(objective=proposal.objective), actor_id="phone"
    )
    started = await manager._mark_start_requested(goal["id"])
    call_id = await manager._reserve_model_call(
        goal["id"],
        role="planner",
        context_id=None,
        input_digest="unused",
        provider_source="test",
    )
    original = goal_manager.append_audit_event

    async def fence_after_audit(db, event_type, payload, **kwargs):
        result = await original(db, event_type, payload, **kwargs)
        if event_type == "goal.plan.accepted":
            await db.execute("UPDATE goal_model_calls SET status='failed' WHERE id=?", (call_id,))
        return result

    monkeypatch.setattr(goal_manager, "append_audit_event", fence_after_audit)
    with pytest.raises(GoalManagerConflict, match="fenced"):
        await manager._persist_initial_plan(
            started,
            proposal,
            source=PlannerSource.TEST,
            model_call_id=call_id,
        )
    graph = await read_project_graph(path, goal["id"])
    assert graph is not None and not graph.planning_decisions and not graph.nodes


@pytest.mark.asyncio
async def test_cancellation_closes_snapshot_without_writing(tmp_path, monkeypatch):
    from app.services import project_graph

    path = tmp_path / "state.db"
    _, goal, _ = await graph_evidence(path)
    entered = asyncio.Event()

    async def pause(db, project_id):
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(project_graph, "_read_revision", pause)
    pending = asyncio.create_task(read_project_graph(path, goal["id"]))
    await asyncio.wait_for(entered.wait(), 2)
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    async with aiosqlite.connect(path, timeout=0) as db:
        await db.execute("BEGIN EXCLUSIVE")
        await db.rollback()


@pytest.mark.asyncio
async def test_graph_rejects_cycles_instead_of_implying_a_working_plan(tmp_path):
    path = tmp_path / "state.db"
    _, goal, _ = await graph_evidence(path)
    async with aiosqlite.connect(path) as db:
        await db.execute(
            "INSERT INTO plan_edges VALUES(?,'node_02','node_00','hard')", (goal["id"],)
        )
        await db.execute("UPDATE plan_nodes SET depends_on_json='[\"node_02\"]' WHERE id='node_00'")
        await db.commit()
    with pytest.raises(ProjectGraphEvidenceError):
        await read_project_graph(path, goal["id"])


@pytest.mark.asyncio
async def test_graph_handles_twenty_nodes_and_rejects_twenty_one_without_truncating(tmp_path):
    path = tmp_path / "state.db"
    manager = await _manager(path, _worker_plan())
    goal, _ = await seed(manager, count=20)
    graph = await read_project_graph(path, goal["id"])
    assert graph is not None and len(graph.nodes) == 20
    async with aiosqlite.connect(path) as db:
        await db.execute(
            """INSERT INTO plan_nodes(id,goal_run_id,node_type,title,objective,status,expected_output,
            created_at,updated_at) VALUES('node_20',?,'worker','Extra','Extra','planned','Extra',?,?)""",
            (goal["id"], NOW, NOW),
        )
        await db.commit()
    with pytest.raises(ProjectGraphEvidenceError):
        await read_project_graph(path, goal["id"])


def test_graph_api_returns_safe_unavailability_for_corrupt_evidence(
    client: TestClient,
    paired_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
):
    from app import main

    monkeypatch.setattr(
        main,
        "read_project_graph",
        AsyncMock(
            side_effect=ProjectGraphEvidenceError("private-corrupt-payload"),
        ),
    )
    response = client.get("/goals/goal_test/graph", headers=paired_headers)
    assert response.status_code == 503
    assert response.json() == {"detail": "project graph evidence unavailable"}


@pytest.mark.asyncio
async def test_planning_audit_selection_is_scoped_and_bounded(tmp_path):
    from app.services.audit_log import append_audit_event

    path = tmp_path / "state.db"
    _, goal, _ = await graph_evidence(path)
    payload = {
        "goal_run_id": goal["id"],
        "planner_source": "manual",
        "rationale_summary": "Examiner les fichiers.",
        "node_ids": ["node_00"],
        "conversation_revision": 0,
        "model_call_id": None,
    }
    async with aiosqlite.connect(path) as db:
        for _ in range(7):
            await append_audit_event(
                db,
                "goal.replan.accepted",
                payload,
                task_id=goal["root_task_id"],
                trace_id=goal["id"],
                actor_type="control-plane",
                actor_id="goal-manager",
                created_at=NOW,
            )
        for changed in ({"goal_run_id": "other_goal"}, {"rationale_summary": "private-cross-task"}):
            await append_audit_event(
                db,
                "goal.plan.accepted",
                payload | changed,
                task_id="another_task",
                trace_id=goal["id"],
                actor_type="control-plane",
                actor_id="goal-manager",
                created_at=NOW,
            )
        await db.commit()
    graph = await read_project_graph(path, goal["id"])
    assert graph is not None and graph.planning_decisions_has_more
    assert len(graph.planning_decisions) == 5
    ids = [decision.audit_event_id for decision in graph.planning_decisions]
    assert ids == sorted(ids, reverse=True)
    assert "private-cross-task" not in graph.model_dump_json()
