from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import aiosqlite
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.models import TaskCreate, TaskRecord
from app.services.goal_manager import GoalManager
from app.services.swarm_contracts import GoalCreateRequest
from app.services.task_execution import read_task_goal_execution
from tests.test_goal_runtime_recovery import _manager, _worker_plan


async def seed(manager: GoalManager, count: int = 2) -> tuple[dict[str, Any], list[str]]:
    goal = await manager.create_goal(
        GoalCreateRequest(objective="Inspecter les fichiers du CRM"), actor_id="phone"
    )
    children = []
    for index in range(count):
        task = await manager.state_service.create_task(
            TaskRecord.new(TaskCreate(input="Inspecter le CRM"), source=f"goal:{goal['id']}")
        )
        children.append(task.id)
        created_at = task.created_at.isoformat()
        async with aiosqlite.connect(manager.db_path) as db:
            await db.execute(
                """INSERT INTO agent_jobs(id,task_id,required_skill,payload_json,status,
                result_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)""",
                (
                    f"job_{index}",
                    task.id,
                    "workspace.list_dir",
                    '{"secret":"raw-payload-must-never-leak"}',
                    "completed",
                    '{"text":"full-draft-must-never-leak","files":["private-source"]}',
                    created_at,
                    created_at,
                ),
            )
            await db.execute(
                """INSERT INTO plan_nodes(id,goal_run_id,task_id,node_type,title,objective,
                required_skill,status,expected_output,worker_job_id,result_summary,error_summary,
                created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    f"node_{index:02}",
                    goal["id"],
                    task.id,
                    "worker",
                    "Lire password=private-title",
                    "private-objective-not-projected",
                    "workspace.list_dir",
                    "completed",
                    "Fichiers inspectés",
                    f"job_{index}",
                    "Liste reçue token=private-summary /home/user/private-file",
                    "secret=private-error",
                    created_at,
                    created_at,
                ),
            )
            await db.commit()
    return goal, children


@pytest.mark.asyncio
async def test_root_and_child_projection_redacts_and_does_not_advance_state(tmp_path: Path) -> None:
    manager = await _manager(tmp_path / "state.db", _worker_plan())
    goal, children = await seed(manager)
    async with aiosqlite.connect(manager.db_path) as db:
        before = [line async for line in db.iterdump()]
    root = await read_task_goal_execution(manager.db_path, goal["root_task_id"])
    child = await read_task_goal_execution(manager.db_path, children[1])
    assert root is not None and child is not None
    assert [node.node_id for node in root.nodes] == ["node_00", "node_01"]
    assert [node.node_id for node in child.nodes] == ["node_01"]
    assert root.goal_run_id == child.goal_run_id == goal["id"]
    assert child.task_id == children[1]
    encoded = root.model_dump_json()
    for private in (
        "private-title",
        "private-summary",
        "private-error",
        "/home/user",
        "raw-payload",
        "full-draft",
        "private-source",
        "private-objective",
    ):
        assert private not in encoded
    assert "Liste reçue" in encoded and "<redacted-secret>" in encoded
    assert all(node.provenance.result_digest is None for node in root.nodes)
    async with aiosqlite.connect(manager.db_path) as db:
        assert [line async for line in db.iterdump()] == before
    assert await read_task_goal_execution(manager.db_path, "unrelated-task") is None


@pytest.mark.asyncio
async def test_projection_is_bounded_and_rejects_unsafe_metadata(tmp_path: Path) -> None:
    manager = await _manager(tmp_path / "state.db", _worker_plan())
    goal, _ = await seed(manager, 21)
    evidence = await read_task_goal_execution(manager.db_path, goal["root_task_id"])
    assert evidence is not None and len(evidence.nodes) == 20 and evidence.truncated
    async with aiosqlite.connect(manager.db_path) as db:
        await db.execute("UPDATE plan_nodes SET assigned_agent_id='../private' WHERE id='node_00'")
        await db.commit()
    with pytest.raises(ValueError):
        await read_task_goal_execution(manager.db_path, goal["root_task_id"])


def test_task_api_adds_agent_evidence_without_fabricating_tool_calls(
    test_app: FastAPI, client: TestClient, paired_headers: dict[str, str]
) -> None:
    assert client.portal is not None
    manager = test_app.state.goal_manager
    goal, children = client.portal.call(seed, manager)
    assert client.get(f"/tasks/{goal['root_task_id']}").status_code == 401
    response = client.get(f"/tasks/{goal['root_task_id']}", headers=paired_headers)
    assert response.status_code == 200
    data = response.json()
    assert data["tool_calls"] == []
    assert len(data["goal_execution"]["nodes"]) == 2
    assert data["goal_execution"]["root_task_id"] == goal["root_task_id"]
    response = client.get(f"/tasks/{children[0]}", headers=paired_headers)
    assert response.status_code == 200
    assert len(response.json()["goal_execution"]["nodes"]) == 1
    ordinary = client.post("/tasks", headers=paired_headers, json={"input": "Conversation"}).json()
    response = client.get(f"/tasks/{ordinary['id']}", headers=paired_headers)
    assert response.status_code == 200 and response.json()["goal_execution"] is None
    assert "raw-payload" not in json.dumps(data)
