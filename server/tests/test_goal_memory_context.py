from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path
from typing import Any

import aiosqlite
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.services.embedding_service import EmbeddingServiceError
from app.services.execution_engine import ExecutionEngine
from app.services.goal_project import GoalProjectService
from app.services.project_memory import ProjectMemoryConflict, ProjectMemoryService
from app.services.state_service import SCHEMA_VERSION, StateService
from app.services.swarm_contracts import GoalCreateRequest
from tests.test_goal_api import _create_goal
from tests.test_goal_runtime_recovery import _manager, _worker_plan
from tests.test_project_memory import SemanticProvider, _count, _messages


async def _goal(tmp_path: Path, *, linked: bool = True, max_calls: int = 8) -> tuple[Any, str]:
    manager = await _manager(tmp_path / "memory.db", _worker_plan())
    goal = await manager.create_goal(
        GoalCreateRequest(objective="Create purchaser profiles", max_model_calls=max_calls),
        actor_id="phone",
    )
    if linked:
        projects = GoalProjectService(
            manager.db_path, ExecutionEngine(manager.db_path, tmp_path, manager.permission_policy)
        )
        await projects.ensure_project(goal["id"])
    return manager, str(goal["id"])


def test_memory_endpoint_auth_scope_stale_version_and_no_project_side_effects(
    client: TestClient, paired_headers: dict[str, str], test_app: FastAPI
) -> None:
    goal = _create_goal(client, paired_headers, objective="Build the CRM")["goal"]
    endpoint = f"/goals/{goal['id']}/memory-context"
    body = {"purpose": "planner", "expected_goal_updated_at": goal["updated_at"]}
    assert client.post(endpoint, json=body).status_code == 401
    assert (
        client.post(
            endpoint, headers=paired_headers, json={**body, "project_id": "other"}
        ).status_code
        == 422
    )
    assert (
        client.post(
            endpoint, headers=paired_headers, json={**body, "purpose": "worker"}
        ).status_code
        == 422
    )
    assert (
        client.post(
            endpoint, headers=paired_headers, json={**body, "purpose": "evaluator"}
        ).status_code
        == 409
    )
    assert (
        client.post(
            endpoint,
            headers=paired_headers,
            json={**body, "expected_goal_updated_at": "2000-01-01T00:00:00Z"},
        ).status_code
        == 409
    )
    assert (
        client.post(
            "/goals/goal_missing/memory-context", headers=paired_headers, json=body
        ).status_code
        == 404
    )
    response = client.post(endpoint, headers=paired_headers, json=body)
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    receipt = response.json()
    assert receipt["project_id"] is None and receipt["items"] == []
    assert receipt["mode"] == "lexical" and receipt["reason"] == "no_linked_project"
    assert receipt["local_planning_eligible"] is True
    assert receipt["planning_embedding_call_count"] == 0
    assert receipt["recent_conversation"] == [{"role": "user", "content": goal["objective"]}]
    assert receipt["embedding"] == {
        "configured": False,
        "model": None,
        "model_revision": None,
        "storage": "ubuntu_sqlite",
    }
    with sqlite3.connect(test_app.state.project_memory.db_path) as db:
        for table in (
            "coding_projects",
            "goal_memory_queries",
            "project_memory_items",
            "project_memory_queries",
            "plan_nodes",
            "goal_model_calls",
        ):
            assert db.execute(f"SELECT COUNT(*) FROM {table}").fetchone() == (0,)
        assert db.execute(
            "SELECT model_call_count FROM goal_runs WHERE id=?", (goal["id"],)
        ).fetchone() == (0,)


@pytest.mark.asyncio
async def test_empty_receipt_binds_complete_latest_answer_without_query_rows(
    tmp_path: Path,
) -> None:
    manager, goal_id = await _goal(tmp_path, linked=False)
    memory = ProjectMemoryService(manager.db_path, None)
    initial = await memory.retrieve_for_goal(goal_id, "planner")
    await memory.assert_context_current(goal_id, "planner", initial["context_fingerprint"])
    answer = "Clients et calendrier. " + "a" * 3_970
    await _messages(manager, goal_id, [f"Earlier decision {i}" for i in range(42)] + [answer])
    current = await memory.retrieve_for_goal(goal_id, "planner")
    assert len(current["recent_conversation"]) == 40
    assert current["recent_conversation"][-1] == {"role": "user", "content": answer}
    assert current["context_fingerprint"] != initial["context_fingerprint"]
    with pytest.raises(ProjectMemoryConflict):
        await memory.assert_context_current(goal_id, "planner", initial["context_fingerprint"])
    await memory.assert_context_current(goal_id, "planner", current["context_fingerprint"])
    assert await _count(manager, goal_id) == 0


@pytest.mark.asyncio
async def test_pre_node_semantic_receipt_is_project_scoped_and_restart_cached(
    tmp_path: Path,
) -> None:
    manager, goal_id = await _goal(tmp_path)
    message_id = (await _messages(manager, goal_id, ["Customer identifiers remain shared."]))[0]
    other = await manager.create_goal(
        GoalCreateRequest(objective="CROSS_PROJECT_SECRET"), actor_id="phone"
    )
    await _messages(manager, other["id"], ["Customer CROSS_PROJECT_SECRET"])
    provider = SemanticProvider()
    memory = ProjectMemoryService(manager.db_path, provider, model_revision="pin")
    first = await memory.retrieve_for_goal(goal_id, "planner")
    assert first["mode"] == "semantic" and any(
        item["source_id"] == message_id for item in first["items"]
    )
    assert first["local_planning_eligible"] is True and first["planning_embedding_call_count"] == 1
    assert (
        first["embedding"]["model"] == provider.model
        and first["embedding"]["model_revision"] == "pin"
    )
    assert "CROSS_PROJECT_SECRET" not in json.dumps(first) + json.dumps(provider.calls)
    restarted = ProjectMemoryService(manager.db_path, provider, model_revision="pin")
    assert await restarted.retrieve_for_goal(goal_id, "planner") == first
    await restarted.assert_context_current(goal_id, "planner", first["context_fingerprint"])
    assert len(provider.calls) == 1 and await _count(manager, goal_id) == 1
    async with aiosqlite.connect(manager.db_path) as db:
        assert await (await db.execute("SELECT COUNT(*) FROM plan_nodes")).fetchone() == (0,)
        assert await (
            await db.execute("SELECT COUNT(*) FROM project_memory_queries")
        ).fetchone() == (0,)
        assert await (
            await db.execute("SELECT status,embedding_requested FROM goal_memory_queries")
        ).fetchall() == [("completed", 1)]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "max_calls,failed,reason,charged",
    [(1, False, "embedding_budget_unavailable", 0), (8, True, "embedding_unavailable", 1)],
)
async def test_fallback_cache_preserves_generation_credit_and_never_retries(
    tmp_path: Path, max_calls: int, failed: bool, reason: str, charged: int
) -> None:
    manager, goal_id = await _goal(tmp_path, max_calls=max_calls)
    await _messages(manager, goal_id, ["Customer contact profiles use stable ids"])

    class Provider(SemanticProvider):
        async def embed(self, texts: list[str]) -> list[list[float]]:
            if failed:
                self.calls.append(texts)
                raise EmbeddingServiceError("unavailable")
            return await super().embed(texts)

    provider = Provider()
    memory = ProjectMemoryService(manager.db_path, provider)
    first = await memory.retrieve_for_goal(goal_id, "planner")
    assert first["mode"] == "lexical" and first["reason"] == reason and first["items"]
    assert await memory.retrieve_for_goal(goal_id, "planner") == first
    assert len(provider.calls) == charged and await _count(manager, goal_id) == charged
    assert first["planning_embedding_call_count"] == charged and first["local_planning_eligible"]


@pytest.mark.asyncio
async def test_concurrent_queries_charge_once_and_do_not_publish_inflight_fallback(
    tmp_path: Path,
) -> None:
    manager, goal_id = await _goal(tmp_path)
    await _messages(manager, goal_id, ["Customer contact profiles"])
    entered, release = asyncio.Event(), asyncio.Event()

    class Provider(SemanticProvider):
        async def embed(self, texts: list[str]) -> list[list[float]]:
            entered.set()
            await release.wait()
            return await super().embed(texts)

    provider = Provider()
    memory = ProjectMemoryService(manager.db_path, provider)
    task = asyncio.create_task(memory.retrieve_for_goal(goal_id, "planner"))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        with pytest.raises(ProjectMemoryConflict, match="still being prepared"):
            await memory.retrieve_for_goal(goal_id, "planner")
    finally:
        release.set()
    receipt = await asyncio.wait_for(task, 2)
    assert receipt["mode"] == "semantic"
    assert await memory.retrieve_for_goal(goal_id, "planner") == receipt
    assert len(provider.calls) == 1 and await _count(manager, goal_id) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mutation", ["source", "objective", "provider", "unrelated_credit", "corrupt_receipt"]
)
async def test_receipt_revalidation_rejects_changed_authority_without_embedding(
    tmp_path: Path, mutation: str
) -> None:
    manager, goal_id = await _goal(tmp_path)
    source_id = (await _messages(manager, goal_id, ["Customer contact profiles"]))[0]
    provider = SemanticProvider()
    memory = ProjectMemoryService(manager.db_path, provider)
    receipt = await memory.retrieve_for_goal(goal_id, "planner")
    async with aiosqlite.connect(manager.db_path) as db:
        if mutation == "source":
            await db.execute(
                "UPDATE goal_messages SET content='Revised customer decision' WHERE id=?",
                (source_id,),
            )
        elif mutation == "objective":
            await db.execute(
                "UPDATE goal_runs SET objective='New objective' WHERE id=?", (goal_id,)
            )
        elif mutation == "unrelated_credit":
            await db.execute(
                "UPDATE goal_runs SET model_call_count=model_call_count+1 WHERE id=?", (goal_id,)
            )
        elif mutation == "corrupt_receipt":
            await db.execute("UPDATE goal_memory_queries SET context_json='{}'")
        await db.commit()
    if mutation == "provider":
        memory = ProjectMemoryService(manager.db_path, provider, model_revision="new")
    with pytest.raises(ProjectMemoryConflict):
        await memory.assert_context_current(goal_id, "planner", receipt["context_fingerprint"])
    assert len(provider.calls) == 1
    if mutation == "unrelated_credit":
        assert not (await memory.retrieve_for_goal(goal_id, "planner"))["local_planning_eligible"]


@pytest.mark.asyncio
async def test_schema24_upgrade_preserves_pairing_and_is_additive_restart_safe(
    tmp_path: Path,
) -> None:
    path = tmp_path / "migration.db"
    state = StateService(path)
    await state.initialize()
    async with aiosqlite.connect(path) as db:
        await db.execute("INSERT INTO pairing_codes VALUES('code','2099-01-01',3,'2026-09-13')")
        await db.execute(
            "INSERT INTO pairing_candidates VALUES('pair','hash','phone','Device','pending','2026-09-13','2099-01-01',NULL)"
        )
        await db.execute(
            "INSERT INTO devices(id,name,token,created_at) VALUES('phone','Device',?,'2026-09-13')",
            ("sha256:" + "a" * 64,),
        )
        before = {
            table: await (await db.execute(f"SELECT * FROM {table}")).fetchall()
            for table in ("pairing_codes", "pairing_candidates", "devices")
        }
        await db.execute("DROP TABLE goal_memory_queries")
        await db.execute("PRAGMA user_version=23")
        await db.commit()
    await state.initialize()
    await state.initialize()
    async with aiosqlite.connect(path) as db:
        assert await (await db.execute("PRAGMA user_version")).fetchone() == (SCHEMA_VERSION,)
        for table, rows in before.items():
            assert await (await db.execute(f"SELECT * FROM {table}")).fetchall() == rows
        assert await (await db.execute("SELECT COUNT(*) FROM goal_memory_queries")).fetchone() == (
            0,
        )
        assert await (await db.execute("PRAGMA foreign_key_check")).fetchall() == []
