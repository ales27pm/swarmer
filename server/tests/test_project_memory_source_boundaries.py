from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import aiosqlite
import pytest

from app.services.execution_engine import ExecutionEngine
from app.services.goal_project import GoalProjectService
from app.services.project_context import ProjectContextConflict, ProjectContextService
from app.services.project_memory import ProjectMemoryService
from app.services.swarm_contracts import GoalCreateRequest, GoalMessageRequest
from tests.test_goal_runtime_recovery import _manager, _worker_plan

FOREIGN = "ORIGIN_ALPHA retention must remain under the Alpha storage policy."
OWN = "ORIGIN_BETA retention must remain under the Beta storage policy."


async def _split_legacy_lineage(tmp_path: Path):
    manager = await _manager(tmp_path / "state.db", _worker_plan())
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    projects = GoalProjectService(
        manager.db_path,
        ExecutionEngine(manager.db_path, workspace, manager.permission_policy),
    )
    manager.project_applications = projects
    # Simulate a historical split identity. New continuations now preserve one
    # identity; old stores can still contain the topology this boundary protects.
    parent = await manager.create_goal(GoalCreateRequest(objective=FOREIGN), actor_id="test-phone")
    await manager.cancel_goal(parent["id"], actor_id="test-phone")
    child = await manager.reply_goal(
        parent["id"],
        GoalMessageRequest(message=OWN, client_message_id="explicit-continuation"),
        actor_id="test-phone",
    )
    child_id = child["goal"]["id"]
    parent_project = await projects.ensure_project(parent["id"])
    child_project = "project_historical_split"
    async with aiosqlite.connect(manager.db_path) as db:
        await db.execute("INSERT INTO coding_projects VALUES(?,'now','now')", (child_project,))
        await db.execute(
            "UPDATE goal_project_links SET project_id=? WHERE goal_run_id=?",
            (child_project, child_id),
        )
        await db.commit()
    await ProjectContextService(manager.db_path).refresh(parent["id"])
    assert parent_project != child_project
    return manager, parent["id"], child_id, parent_project, child_project


@pytest.mark.asyncio
async def test_schema_accepts_historical_split_identity_without_foreign_key_corruption(
    tmp_path: Path,
):
    manager, parent, child, project_a, project_b = await _split_legacy_lineage(tmp_path)
    async with aiosqlite.connect(manager.db_path) as db:
        await db.execute("PRAGMA foreign_keys=ON")
        assert await (await db.execute("PRAGMA foreign_keys")).fetchone() == (1,)
        assert await (await db.execute("PRAGMA foreign_key_check")).fetchall() == []
        rows = await (
            await db.execute(
                "SELECT c.goal_run_id,c.conversation_id,c.parent_goal_id,p.project_id "
                "FROM goal_conversation_links c JOIN goal_project_links p ON p.goal_run_id=c.goal_run_id "
                "ORDER BY c.parent_goal_id IS NOT NULL"
            )
        ).fetchall()
        assert rows == [
            (parent, rows[0][1], None, project_a),
            (child, rows[0][1], parent, project_b),
        ]
        counts = await (
            await db.execute(
                "SELECT (SELECT COUNT(*) FROM agent_jobs), (SELECT COUNT(*) FROM goal_model_calls), "
                "(SELECT SUM(model_call_count) FROM goal_runs)"
            )
        ).fetchone()
        assert counts == (0, 0, 0)


@pytest.mark.asyncio
async def test_durable_context_excludes_message_owned_by_other_project(tmp_path: Path):
    manager, parent, child, _, _ = await _split_legacy_lineage(tmp_path)
    result = await ProjectContextService(manager.db_path).refresh(child)
    source_ids = {item["source_id"] for item in result["requirements"]}
    assert f"gmsg_initial_{parent}" not in source_ids, json.dumps(result["requirements"])


@pytest.mark.asyncio
async def test_source_endpoint_service_rejects_message_owned_by_other_project(tmp_path: Path):
    manager, parent, child, _, _ = await _split_legacy_lineage(tmp_path)
    with pytest.raises(ProjectContextConflict, match="source not found"):
        await ProjectContextService(manager.db_path).source(child, f"gmsg_initial_{parent}")


@pytest.mark.asyncio
async def test_lexical_retrieval_excludes_other_project_message(tmp_path: Path):
    manager, parent, child, _, _ = await _split_legacy_lineage(tmp_path)
    result = await ProjectMemoryService(manager.db_path).retrieve_for_goal(child, "evaluator")
    assert f"gmsg_initial_{parent}" not in {item["source_id"] for item in result["items"]}, result


@pytest.mark.asyncio
async def test_recent_conversation_in_memory_receipt_respects_source_project(tmp_path: Path):
    manager, _, child, _, _ = await _split_legacy_lineage(tmp_path)
    result = await ProjectMemoryService(manager.db_path).retrieve_for_goal(child, "evaluator")
    assert FOREIGN not in [item["content"] for item in result["recent_conversation"]], result


@pytest.mark.asyncio
async def test_existing_receipt_is_not_accepted_after_source_moves_to_other_project(tmp_path: Path):
    from app.services.project_memory import ProjectMemoryConflict

    manager, parent, child, parent_project, child_project = await _split_legacy_lineage(tmp_path)
    # Fixture-only setup: join the source to the child project before retrieval,
    # then restore the valid-in-schema, incompatible source identity. This tests
    # validation of an already recorded receipt, not normal migration authority.
    async with aiosqlite.connect(manager.db_path) as db:
        await db.execute("PRAGMA foreign_keys=ON")
        await db.execute(
            "UPDATE goal_project_links SET project_id=? WHERE goal_run_id=?",
            (child_project, parent),
        )
        await db.commit()
    memory = ProjectMemoryService(manager.db_path)
    result = await memory.retrieve_for_goal(child, "evaluator")
    assert f"gmsg_initial_{parent}" in {item["source_id"] for item in result["items"]}
    async with aiosqlite.connect(manager.db_path) as db:
        await db.execute("PRAGMA foreign_keys=ON")
        await db.execute(
            "UPDATE goal_project_links SET project_id=? WHERE goal_run_id=?",
            (parent_project, parent),
        )
        await db.commit()
    with pytest.raises(ProjectMemoryConflict, match="(source|context) changed"):
        await memory.assert_context_current(child, "evaluator", result["context_fingerprint"])


@pytest.mark.asyncio
async def test_same_project_continuation_keeps_history_sources_and_cached_receipt(tmp_path: Path):
    manager = await _manager(tmp_path / "state.db", _worker_plan())
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    projects = GoalProjectService(
        manager.db_path, ExecutionEngine(manager.db_path, workspace, manager.permission_policy)
    )
    manager.project_applications = projects
    parent = await manager.create_goal(GoalCreateRequest(objective=FOREIGN), actor_id="phone")
    project_id = await projects.ensure_project(parent["id"])
    await manager.cancel_goal(parent["id"], actor_id="phone")
    child = await manager.reply_goal(
        parent["id"], GoalMessageRequest(message=OWN, client_message_id="next"), actor_id="phone"
    )
    goal_id = child["goal"]["id"]
    assert await projects.ensure_project(goal_id) == project_id
    source_id = f"gmsg_initial_{parent['id']}"
    context = ProjectContextService(manager.db_path)
    result = await context.refresh(goal_id)
    assert source_id in {item["source_id"] for item in result["requirements"]}
    assert (await context.source(goal_id, source_id))["content"] == FOREIGN
    memory = ProjectMemoryService(manager.db_path)
    receipt = await memory.retrieve_for_goal(goal_id, "evaluator")
    assert source_id in {item["source_id"] for item in receipt["items"]}
    assert FOREIGN in [item["content"] for item in receipt["recent_conversation"]]
    await memory.assert_context_current(goal_id, "evaluator", receipt["context_fingerprint"])
    assert (
        await ProjectMemoryService(manager.db_path).retrieve_for_goal(goal_id, "evaluator")
        == receipt
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("source_state", ["unlinked", "wrong_conversation"])
async def test_ambiguous_message_origin_never_enters_context_or_retrieval(
    tmp_path: Path, source_state: str
):
    manager, parent, child, _, child_project = await _split_legacy_lineage(tmp_path)
    async with aiosqlite.connect(manager.db_path) as db:
        await db.execute("PRAGMA foreign_keys=ON")
        if source_state == "unlinked":
            await db.execute("DELETE FROM goal_project_links WHERE goal_run_id=?", (parent,))
        else:
            await db.execute(
                "UPDATE goal_project_links SET project_id=? WHERE goal_run_id=?",
                (child_project, parent),
            )
            await db.execute("INSERT INTO goal_conversations VALUES('gconv_separate',?)", (parent,))
            await db.execute(
                "UPDATE goal_conversation_links SET conversation_id='gconv_separate' WHERE goal_run_id=?",
                (parent,),
            )
        await db.commit()
        assert await (await db.execute("PRAGMA foreign_key_check")).fetchall() == []
    source_id = f"gmsg_initial_{parent}"
    context = ProjectContextService(manager.db_path)
    assert source_id not in {
        item["source_id"] for item in (await context.refresh(child))["requirements"]
    }
    with pytest.raises(ProjectContextConflict, match="source not found"):
        await context.source(child, source_id)
    receipt = await ProjectMemoryService(manager.db_path).retrieve_for_goal(child, "evaluator")
    assert source_id not in {item["source_id"] for item in receipt["items"]}
    assert FOREIGN not in [item["content"] for item in receipt["recent_conversation"]]


@pytest.mark.asyncio
async def test_source_project_filter_precedes_recent_and_retrieval_limits(tmp_path: Path):
    from tests.test_project_memory import _messages

    manager, parent, child, _, _ = await _split_legacy_lineage(tmp_path)
    await _messages(manager, parent, [f"Foreign retention source {i}" for i in range(485)])
    receipt = await ProjectMemoryService(manager.db_path).retrieve_for_goal(child, "evaluator")
    assert [item["content"] for item in receipt["recent_conversation"]] == [OWN]
    assert [item["summary"] for item in receipt["items"]] == [OWN]


@pytest.mark.asyncio
async def test_unlinked_continuation_preserves_explicit_same_conversation_history(tmp_path: Path):
    manager = await _manager(tmp_path / "state.db", _worker_plan())
    parent = await manager.create_goal(GoalCreateRequest(objective=FOREIGN), actor_id="phone")
    await manager.cancel_goal(parent["id"], actor_id="phone")
    child = await manager.reply_goal(
        parent["id"], GoalMessageRequest(message=OWN, client_message_id="next"), actor_id="phone"
    )
    # Persisted pre-identity histories remain readable without GET migration.
    async with aiosqlite.connect(manager.db_path) as db:
        await db.execute("DELETE FROM goal_project_links")
        await db.commit()
    receipt = await ProjectMemoryService(manager.db_path).retrieve_for_goal(
        child["goal"]["id"], "evaluator"
    )
    assert receipt["project_id"] is None and receipt["reason"] == "no_linked_project"
    assert [item["content"] for item in receipt["recent_conversation"]] == [FOREIGN, OWN]
    assert receipt["items"] == []


@pytest.mark.asyncio
async def test_unlinked_target_excludes_linked_sources_before_recent_limit(tmp_path: Path):
    from tests.test_project_memory import _messages

    manager, parent, child, _, _ = await _split_legacy_lineage(tmp_path)
    async with aiosqlite.connect(manager.db_path) as db:
        await db.execute("DELETE FROM goal_project_links WHERE goal_run_id=?", (child,))
        await db.commit()
    await _messages(manager, parent, [f"Foreign linked retention source {i}" for i in range(45)])
    receipt = await ProjectMemoryService(manager.db_path).retrieve_for_goal(child, "evaluator")
    assert receipt["project_id"] is None
    assert [item["content"] for item in receipt["recent_conversation"]] == [OWN]


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["move_owner", "delete", "correct"])
@pytest.mark.parametrize("provider_failure", [False, True])
async def test_direct_worker_revalidates_sources_after_embedding(
    tmp_path: Path, mutation: str, provider_failure: bool
):
    from app.services.embedding_service import EmbeddingServiceError
    from tests.test_goal_project_runtime import _project
    from tests.test_project_memory import SemanticProvider, _messages

    manager, detail, _ = await _project(tmp_path)
    goal_id, node_id = detail["goal"]["id"], detail["nodes"][0]["id"]
    message_id = (
        await _messages(manager, goal_id, ["Customer ORIGIN_SECRET original contact decision"])
    )[0]
    other = await manager.create_goal(
        GoalCreateRequest(objective="Separate project"), actor_id="phone"
    )
    assert manager.project_applications is not None
    await manager.project_applications.ensure_project(other["id"])

    class MovingProvider(SemanticProvider):
        async def embed(self, texts: list[str]) -> list[list[float]]:
            async with aiosqlite.connect(manager.db_path) as db:
                await db.execute("PRAGMA foreign_keys=ON")
                if mutation == "move_owner":
                    await db.execute(
                        "UPDATE goal_messages SET goal_run_id=? WHERE id=?",
                        (other["id"], message_id),
                    )
                elif mutation == "delete":
                    await db.execute("DELETE FROM goal_messages WHERE id=?", (message_id,))
                else:
                    await db.execute(
                        "UPDATE goal_messages SET content=? WHERE id=?",
                        ("Corrected unrelated decision", message_id),
                    )
                await db.commit()
            if provider_failure:
                raise EmbeddingServiceError("synthetic unavailable")
            return await super().embed(texts)

    result = await ProjectMemoryService(manager.db_path, MovingProvider()).retrieve(
        goal_id, node_id, "Customer contact", base_revision_id=None
    )
    assert result["reason"] == "project_context_changed"
    assert result["items"] == []
    async with aiosqlite.connect(manager.db_path) as db:
        row = await (
            await db.execute(
                "SELECT vector_json FROM project_memory_items WHERE source_id=?", (message_id,)
            )
        ).fetchone()
        assert row is not None and row[0] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["before_projection", "after_projection", "after_cached_query"])
async def test_worker_rechecks_source_after_projection_or_cached_query(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stage: str
):
    from tests.test_goal_project_runtime import _project
    from tests.test_project_memory import SemanticProvider, _messages

    manager, detail, _ = await _project(tmp_path)
    goal_id, node_id = detail["goal"]["id"], detail["nodes"][0]["id"]
    message_id = (
        await _messages(manager, goal_id, ["Customer ORIGIN_SECRET original contact decision"])
    )[0]
    provider = SemanticProvider()
    memory = ProjectMemoryService(
        manager.db_path, provider if stage == "after_cached_query" else None
    )

    async def delete_source() -> None:
        async with aiosqlite.connect(manager.db_path) as db:
            await db.execute("DELETE FROM goal_messages WHERE id=?", (message_id,))
            await db.commit()

    if stage == "after_cached_query":
        initial = await memory.retrieve(goal_id, node_id, "Customer contact", base_revision_id=None)
        assert message_id in {item["source_id"] for item in initial["items"]}
        original_reserve = memory._reserve

        async def reserve(*args: Any, **kwargs: Any) -> Any:
            result = await original_reserve(*args, **kwargs)
            await delete_source()
            return result

        monkeypatch.setattr(memory, "_reserve", reserve)
    else:
        original_refresh = memory._refresh_items

        async def refresh(*args: Any, **kwargs: Any) -> Any:
            if stage == "before_projection":
                await delete_source()
            result = await original_refresh(*args, **kwargs)
            if stage == "after_projection":
                await delete_source()
            return result

        monkeypatch.setattr(memory, "_refresh_items", refresh)
    result = await memory.retrieve(goal_id, node_id, "Customer contact", base_revision_id=None)
    assert result["items"] == []
    assert result["reason"] == "project_context_changed"
    if stage == "before_projection":
        async with aiosqlite.connect(manager.db_path) as db:
            assert await (
                await db.execute(
                    "SELECT COUNT(*) FROM project_memory_items WHERE source_id=?", (message_id,)
                )
            ).fetchone() == (0,)
    if stage == "after_cached_query":
        assert len(provider.calls) == 1
