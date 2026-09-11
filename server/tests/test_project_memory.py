from __future__ import annotations

import asyncio
import json
import math
from pathlib import Path
from typing import Any
from uuid import uuid4

import aiosqlite
import pytest

from app.services.embedding_service import EmbeddingServiceError
from app.services.execution_engine import ExecutionEngine
from app.services.goal_project import GoalProjectService
from app.services.project_memory import ProjectMemoryService
from app.services.swarm_contracts import GoalCreateRequest, GoalStartRequest, PlannerSource
from tests.test_goal_project_runtime import SOURCE, _project, _result
from tests.test_goal_runtime_recovery import _manager, _worker_plan


class SemanticProvider:
    provider_name = "test-semantic"
    base_url = "http://local-one.invalid/v1"
    model = "semantic-model"

    def __init__(self, dimensions: int = 3) -> None:
        self.calls: list[list[str]] = []
        self.dimensions = dimensions

    async def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(texts)
        vectors = []
        for text in texts:
            # Deliberately different vocabularies: token overlap cannot recover
            # the historical decision from the new request.
            related = any(word in text.lower() for word in ("purchaser", "customer", "contact"))
            vectors.append(([1.0, 0.0] if related else [0.0, 1.0]) + [0.0] * (self.dimensions - 2))
        return vectors


async def _messages(manager: Any, goal_id: str, contents: list[str]) -> list[str]:
    ids = [f"gmsg_{uuid4().hex}" for _ in contents]
    async with aiosqlite.connect(manager.db_path) as db:
        conversation = await (
            await db.execute(
                "SELECT conversation_id FROM goal_conversation_links WHERE goal_run_id=?",
                (goal_id,),
            )
        ).fetchone()
        assert conversation
        await db.executemany(
            """INSERT INTO goal_messages(id,conversation_id,goal_run_id,role,content,created_at)
            VALUES(?,?,?,'user',?,'2026-09-11T00:00:00+00:00')""",
            [
                (identity, conversation[0], goal_id, content)
                for identity, content in zip(ids, contents, strict=True)
            ],
        )
        await db.execute(
            "UPDATE goal_runs SET conversation_revision=conversation_revision+? WHERE id=?",
            (len(contents), goal_id),
        )
        await db.commit()
    return ids


async def _count(manager: Any, goal_id: str) -> int:
    goal = await manager.graph.get_goal(goal_id)
    assert goal
    return int(goal["model_call_count"])


@pytest.mark.asyncio
async def test_semantic_history_outside_recent_window_and_restart_cache(tmp_path: Path) -> None:
    manager, detail, _ = await _project(tmp_path)
    goal_id, node_id = detail["goal"]["id"], detail["nodes"][0]["id"]
    ids = await _messages(
        manager,
        goal_id,
        ["Customer contact records must remain shared by the team."]
        + [f"Theme discussion number {i}" for i in range(50)],
    )
    assert ids[0] not in [
        m["id"] for m in (await manager.conversations.messages(goal_id, limit=40))["messages"]
    ]
    provider = SemanticProvider()
    memory = ProjectMemoryService(manager.db_path, provider, model_revision="pinned-one")
    await memory.initialize()
    before = await _count(manager, goal_id)
    result = await memory.retrieve(goal_id, node_id, "Purchaser profiles", base_revision_id=None)
    assert result["mode"] == "semantic" and result["items"][0]["source_id"] == ids[0]
    assert result["items"][0]["score"] == 1 and len(result["items"]) <= 4
    assert len(provider.calls) == 1 and len(provider.calls[0]) == 25
    assert await _count(manager, goal_id) == before + 1
    restarted_provider = SemanticProvider()
    restarted = ProjectMemoryService(
        manager.db_path, restarted_provider, model_revision="pinned-one"
    )
    assert (
        await restarted.retrieve(goal_id, node_id, "Purchaser profiles", base_revision_id=None)
        == result
    )
    assert restarted_provider.calls == [] and await _count(manager, goal_id) == before + 1


@pytest.mark.asyncio
async def test_memory_is_project_scoped_and_never_reads_source_or_checks(tmp_path: Path) -> None:
    manager, detail, agent = await _project(tmp_path)
    goal_id, node_id = detail["goal"]["id"], detail["nodes"][0]["id"]
    await _messages(
        manager,
        goal_id,
        [
            "Customer privacy decision\n```python\nPRIVATE_FENCED_SOURCE = 'hidden'\n```\nsecret_key='hide-this'",
            "Customer contacts stay private",
        ],
    )
    other = await manager.create_goal(
        GoalCreateRequest(objective="Customer CROSS_PROJECT_SECRET"), actor_id="phone"
    )
    await manager.project_applications.ensure_project(other["id"])
    await _messages(manager, other["id"], ["Customer CROSS_PROJECT_SECRET"])
    job, _ = await _result(manager, agent, action="continue", receive=False)
    revision = await manager.project_applications.capture_result(goal_id, node_id, job["id"])
    provider = SemanticProvider()
    memory = ProjectMemoryService(manager.db_path, provider)
    result = await memory.retrieve(
        goal_id, node_id, "Purchaser", base_revision_id=revision["revision_id"]
    )
    assert result["mode"] == "semantic"
    serialized = json.dumps(provider.calls) + json.dumps(result)
    for forbidden in (
        SOURCE,
        "def add_customer",
        "Ran 1 test",
        "PRIVATE_FENCED_SOURCE",
        "hide-this",
        "CROSS_PROJECT_SECRET",
    ):
        assert forbidden not in serialized
    async with aiosqlite.connect(manager.db_path) as db:
        summaries = json.dumps(
            await (await db.execute("SELECT summary FROM project_memory_items")).fetchall()
        )
    assert "CROSS_PROJECT_SECRET" not in summaries and "def add_customer" not in summaries


@pytest.mark.asyncio
async def test_content_origin_model_revision_and_dimensions_invalidate_vectors(
    tmp_path: Path,
) -> None:
    manager, detail, _ = await _project(tmp_path, max_calls=12)
    goal_id, node_id = detail["goal"]["id"], detail["nodes"][0]["id"]
    message = (await _messages(manager, goal_id, ["Customer contact decision"]))[0]
    provider = SemanticProvider()
    memory = ProjectMemoryService(manager.db_path, provider, model_revision="one")
    assert (await memory.retrieve(goal_id, node_id, "Purchaser", base_revision_id=None))[
        "mode"
    ] == "semantic"
    async with aiosqlite.connect(manager.db_path) as db:
        await db.execute("UPDATE goal_messages SET content='Theme decision' WHERE id=?", (message,))
        await db.commit()
    result = await memory.retrieve(goal_id, node_id, "Purchaser", base_revision_id=None)
    assert result["mode"] == "lexical" and result["items"] == []
    async with aiosqlite.connect(manager.db_path) as db:
        assert await (
            await db.execute(
                "SELECT vector_json FROM project_memory_items WHERE source_id=?", (message,)
            )
        ).fetchone() == (None,)
    provider.base_url = "http://local-two.invalid/v1"
    changed = ProjectMemoryService(manager.db_path, provider, model_revision="one")
    await changed.retrieve(goal_id, node_id, "Purchaser", base_revision_id=None)
    assert len(provider.calls) == 2 and len(provider.calls[-1]) > 1
    changed = ProjectMemoryService(manager.db_path, provider, model_revision="two")
    await changed.retrieve(goal_id, node_id, "Purchaser", base_revision_id=None)
    assert len(provider.calls) == 3 and len(provider.calls[-1]) > 1
    provider.dimensions = 4
    await changed.retrieve(goal_id, node_id, "Purchaser updated", base_revision_id=None)
    assert len(provider.calls[-1]) == 1
    async with aiosqlite.connect(manager.db_path) as db:
        assert await (
            await db.execute("SELECT COUNT(*) FROM project_memory_items WHERE dimensions=3")
        ).fetchone() == (0,)
    await changed.retrieve(goal_id, node_id, "Purchaser final", base_revision_id=None)
    assert len(provider.calls[-1]) > 1
    async with aiosqlite.connect(manager.db_path) as db:
        assert await (
            await db.execute("SELECT DISTINCT dimensions FROM project_memory_items")
        ).fetchall() == [(4,)]


@pytest.mark.asyncio
async def test_failed_embedding_is_explicit_lexical_and_never_retried(tmp_path: Path) -> None:
    manager, detail, _ = await _project(tmp_path)
    goal_id, node_id = detail["goal"]["id"], detail["nodes"][0]["id"]
    await _messages(manager, goal_id, ["Customer records remain shared"])

    class Failing(SemanticProvider):
        async def embed(self, texts: list[str]) -> list[list[float]]:
            self.calls.append(texts)
            raise EmbeddingServiceError("provider unavailable")

    provider = Failing()
    memory = ProjectMemoryService(manager.db_path, provider)
    before = await _count(manager, goal_id)
    first = await memory.retrieve(goal_id, node_id, "Customer", base_revision_id=None)
    second = await ProjectMemoryService(manager.db_path, provider).retrieve(
        goal_id, node_id, "Customer", base_revision_id=None
    )
    assert (
        first == second
        and first["mode"] == "lexical"
        and first["reason"] == "embedding_unavailable"
    )
    assert first["items"] and len(provider.calls) == 1
    assert await _count(manager, goal_id) == before + 1


@pytest.mark.asyncio
async def test_timeout_and_interrupted_request_do_not_retry_after_restart(tmp_path: Path) -> None:
    manager, detail, _ = await _project(tmp_path)
    goal_id, node_id = detail["goal"]["id"], detail["nodes"][0]["id"]
    entered = asyncio.Event()

    class Slow(SemanticProvider):
        async def embed(self, texts: list[str]) -> list[list[float]]:
            self.calls.append(texts)
            entered.set()
            await asyncio.Event().wait()
            return []

    provider = Slow()
    memory = ProjectMemoryService(manager.db_path, provider, timeout_seconds=0.02)
    before = await _count(manager, goal_id)
    timed_out = await memory.retrieve(goal_id, node_id, "Build", base_revision_id=None)
    assert timed_out["reason"] == "embedding_unavailable"
    restarted = ProjectMemoryService(manager.db_path, provider)
    assert await restarted.retrieve(goal_id, node_id, "Build", base_revision_id=None) == timed_out
    entered.clear()
    task = asyncio.create_task(restarted.retrieve(goal_id, node_id, "CRM", base_revision_id=None))
    await asyncio.wait_for(entered.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    async with aiosqlite.connect(manager.db_path) as db:
        await db.execute(
            "UPDATE project_memory_queries SET expires_at='2000-01-01' WHERE status='started'"
        )
        await db.commit()
    interrupted = await ProjectMemoryService(manager.db_path, provider).retrieve(
        goal_id, node_id, "CRM", base_revision_id=None
    )
    assert interrupted["reason"] == "embedding_interrupted" and len(provider.calls) == 2
    assert await _count(manager, goal_id) == before + 2


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["conversation", "cancel", "deadline"])
async def test_changed_goal_while_embedding_discards_historical_context(
    tmp_path: Path, change: str
) -> None:
    manager, detail, _ = await _project(tmp_path)
    goal_id, node_id = detail["goal"]["id"], detail["nodes"][0]["id"]
    await _messages(manager, goal_id, ["Customer records"])

    class Changed(SemanticProvider):
        async def embed(self, texts: list[str]) -> list[list[float]]:
            clauses = {
                "conversation": "conversation_revision=conversation_revision+1",
                "cancel": "status='cancelled'",
                "deadline": "started_at='2000-01-01T00:00:00+00:00'",
            }
            async with aiosqlite.connect(manager.db_path) as db:
                await db.execute(f"UPDATE goal_runs SET {clauses[change]} WHERE id=?", (goal_id,))
                await db.commit()
            return await super().embed(texts)

    result = await ProjectMemoryService(manager.db_path, Changed()).retrieve(
        goal_id, node_id, "Customer", base_revision_id=None
    )
    assert result["reason"] == "project_context_changed" and result["items"] == []
    async with aiosqlite.connect(manager.db_path) as db:
        assert await (
            await db.execute(
                "SELECT COUNT(*) FROM project_memory_items WHERE vector_json IS NOT NULL"
            )
        ).fetchone() == (0,)


@pytest.mark.asyncio
async def test_modified_cached_query_vector_is_not_trusted(tmp_path: Path) -> None:
    manager, detail, _ = await _project(tmp_path)
    goal_id, node_id = detail["goal"]["id"], detail["nodes"][0]["id"]
    provider = SemanticProvider()
    memory = ProjectMemoryService(manager.db_path, provider)
    await memory.retrieve(goal_id, node_id, "Build", base_revision_id=None)
    async with aiosqlite.connect(manager.db_path) as db:
        await db.execute("UPDATE project_memory_queries SET query_vector_json='[1,0,0]'")
        await db.commit()
    result = await memory.retrieve(goal_id, node_id, "Build", base_revision_id=None)
    assert result["mode"] == "lexical" and result["reason"] == "embedding_cache_invalid"
    assert len(provider.calls) == 1


@pytest.mark.asyncio
async def test_concurrent_retrieval_reserves_only_one_actual_call(tmp_path: Path) -> None:
    manager, detail, _ = await _project(tmp_path)
    goal_id, node_id = detail["goal"]["id"], detail["nodes"][0]["id"]
    await _messages(manager, goal_id, ["Customer records"])
    entered, release = asyncio.Event(), asyncio.Event()

    class Blocking(SemanticProvider):
        async def embed(self, texts: list[str]) -> list[list[float]]:
            entered.set()
            await release.wait()
            return await super().embed(texts)

    provider = Blocking()
    memory = ProjectMemoryService(manager.db_path, provider)
    before = await _count(manager, goal_id)
    first = asyncio.create_task(
        memory.retrieve(goal_id, node_id, "Purchaser", base_revision_id=None)
    )
    await asyncio.wait_for(entered.wait(), 2)
    second = await ProjectMemoryService(manager.db_path, provider).retrieve(
        goal_id, node_id, "Purchaser", base_revision_id=None
    )
    assert second["reason"] == "embedding_in_progress"
    release.set()
    assert (await first)["mode"] == "semantic"
    assert len(provider.calls) == 1 and await _count(manager, goal_id) == before + 1


@pytest.mark.asyncio
@pytest.mark.parametrize("during_call", [False, True])
async def test_revision_change_is_fenced_before_reservation_and_persistence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, during_call: bool
) -> None:
    manager, detail, agent = await _project(tmp_path)
    goal_id, node_id = detail["goal"]["id"], detail["nodes"][0]["id"]
    await _messages(manager, goal_id, ["Customer records"])
    job, _ = await _result(manager, agent, action="continue", receive=False)

    async def capture() -> None:
        await manager.project_applications.capture_result(goal_id, node_id, job["id"])

    class Racing(SemanticProvider):
        async def embed(self, texts: list[str]) -> list[list[float]]:
            if during_call:
                await capture()
            return await super().embed(texts)

    provider = Racing()
    memory = ProjectMemoryService(manager.db_path, provider)
    original = memory._refresh_items

    async def refresh(*args: Any) -> list[dict[str, Any]]:
        result = await original(*args)
        await capture()
        return result

    if not during_call:
        monkeypatch.setattr(memory, "_refresh_items", refresh)
    before = await _count(manager, goal_id)
    result = await memory.retrieve(goal_id, node_id, "Purchaser", base_revision_id=None)
    assert result["mode"] == "lexical" and result["items"] == []
    assert len(provider.calls) == int(during_call)
    assert await _count(manager, goal_id) == before + int(during_call)
    async with aiosqlite.connect(manager.db_path) as db:
        assert await (
            await db.execute(
                "SELECT COUNT(*) FROM project_memory_items WHERE vector_json IS NOT NULL"
            )
        ).fetchone() == (0,)


@pytest.mark.asyncio
async def test_old_rows_do_not_hide_latest_decisions_after_large_churn(tmp_path: Path) -> None:
    manager, detail, _ = await _project(tmp_path)
    goal_id, node_id = detail["goal"]["id"], detail["nodes"][0]["id"]
    old = await _messages(
        manager, goal_id, [f"Obsolete architecture decision {i}" for i in range(600)]
    )
    memory = ProjectMemoryService(manager.db_path)
    await memory.retrieve(goal_id, node_id, "Obsolete", base_revision_id=None)
    new = await _messages(
        manager,
        goal_id,
        [f"Current architecture decision {i}" for i in range(599)]
        + ["Freshest distinctive retention requirement"],
    )
    result = await memory.retrieve(goal_id, node_id, "Freshest distinctive", base_revision_id=None)
    assert result["mode"] == "lexical" and result["items"][0]["source_id"] == new[-1]
    async with aiosqlite.connect(manager.db_path) as db:
        retained = await (await db.execute("SELECT source_id FROM project_memory_items")).fetchall()
    assert len(retained) <= 512 and not set(old) & {row[0] for row in retained}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad", [[0.0, 0.0], [float("inf"), 1.0], [float("nan"), 1.0], [True, 1.0], [1.0]]
)
async def test_invalid_provider_vectors_fail_to_lexical(tmp_path: Path, bad: list[float]) -> None:
    manager, detail, _ = await _project(tmp_path)
    goal_id, node_id = detail["goal"]["id"], detail["nodes"][0]["id"]
    await _messages(manager, goal_id, ["Customer records"])

    class Invalid(SemanticProvider):
        async def embed(self, texts: list[str]) -> list[list[float]]:
            return [[1.0, 0.0]] + [bad for _ in texts[1:]]

    result = await ProjectMemoryService(manager.db_path, Invalid()).retrieve(
        goal_id, node_id, "Customer", base_revision_id=None
    )
    assert result["mode"] == "lexical" and result["reason"] == "embedding_unavailable"
    assert all(math.isfinite(item["score"]) for item in result["items"])


@pytest.mark.asyncio
async def test_extreme_finite_vectors_have_finite_cosine(tmp_path: Path) -> None:
    manager, detail, _ = await _project(tmp_path)
    goal_id, node_id = detail["goal"]["id"], detail["nodes"][0]["id"]

    class Huge(SemanticProvider):
        async def embed(self, texts: list[str]) -> list[list[float]]:
            return [[1e308, 1e308] for _ in texts]

    result = await ProjectMemoryService(manager.db_path, Huge()).retrieve(
        goal_id, node_id, "Purchaser", base_revision_id=None
    )
    assert result["mode"] == "semantic" and all(
        math.isfinite(item["score"]) for item in result["items"]
    )
    assert result["items"][0]["score"] == pytest.approx(1)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "max_calls, expected_mode, embedding_calls", [(1, "lexical", 0), (2, "semantic", 1)]
)
async def test_payload_preserves_generation_credit_and_counts_actual_embedding(
    tmp_path: Path, max_calls: int, expected_mode: str, embedding_calls: int
) -> None:
    plan = _worker_plan(objective="Build customer records")
    plan.nodes[0] = plan.nodes[0].model_copy(
        update={"required_skill": "code.build_project", "objective": "Build customer records"}
    )
    manager = await _manager(tmp_path / "budget.db", plan)
    provider = SemanticProvider()
    manager.project_applications = GoalProjectService(
        manager.db_path,
        ExecutionEngine(manager.db_path, tmp_path, manager.permission_policy),
        memory=ProjectMemoryService(manager.db_path, provider),
    )
    goal = await manager.create_goal(
        GoalCreateRequest(objective=plan.objective, max_model_calls=max_calls), actor_id="phone"
    )
    started = await manager.start_goal(
        goal["id"], GoalStartRequest(plan_proposal=plan, planner_source=PlannerSource.MANUAL)
    )
    assert started["nodes"][0]["status"] == "dispatched"
    assert started["goal"]["model_call_count"] == 1 + embedding_calls
    assert len(provider.calls) == embedding_calls
    async with aiosqlite.connect(manager.db_path) as db:
        row = await (
            await db.execute(
                "SELECT payload_json FROM agent_jobs WHERE task_id=?",
                (started["nodes"][0]["task_id"],),
            )
        ).fetchone()
    assert row and json.loads(row[0])["memory"]["mode"] == expected_mode
