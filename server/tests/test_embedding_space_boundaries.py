from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import aiosqlite
import pytest
from aiosqlite.context import contextmanager as sqlite_contextmanager

from app.models import MemoryCreate, MemorySearch
from app.services.episode_memory import EpisodeMemoryService
from app.services.memory_vectors import embedding_identity
from app.services.project_memory import ProjectMemoryService
from app.services.state_service import StateService
from tests.test_episode_memory import _seed_goal_run
from tests.test_goal_project_runtime import _project
from tests.test_memory_indexing_regressions import LocalProvider
from tests.test_project_memory import SemanticProvider, _messages


@pytest.mark.parametrize("field", ["normalization", "chunker_version", "preprocessing_version"])
def test_embedding_signature_includes_declared_preparation(field: str) -> None:
    provider = LocalProvider()
    before = embedding_identity(provider)
    setattr(provider, field, "changed-contract")
    assert embedding_identity(provider) != before


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["normalization", "chunker_version", "preprocessing_version"])
async def test_general_old_preparation_falls_back_without_losing_memory(
    tmp_path: Path, field: str
) -> None:
    provider = LocalProvider()
    state = StateService(tmp_path / "state.db", provider)
    await state.initialize()
    item = await state.create_memory(MemoryCreate(content="canoe safety"), "test")
    setattr(provider, field, "changed-contract")
    found = await state.search_memory(MemorySearch(query="canoe"))
    assert [(row["id"], row["search_kind"]) for row in found] == [(item["id"], "lexical")]
    assert await state.get_memory(item["id"]) is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["query", "index", "backfill"])
async def test_general_provider_replaced_during_embedding_is_not_used(
    tmp_path: Path, operation: str
) -> None:
    class SwitchingProvider(LocalProvider):
        change: Callable[[], None] | None = None

        async def embed(self, texts: list[str]) -> list[list[float]]:
            result = await super().embed(texts)
            if self.change:
                self.change()
            return result

    provider = SwitchingProvider()
    state = StateService(tmp_path / "state.db", provider if operation == "query" else None)
    await state.initialize()
    item = await state.create_memory(MemoryCreate(content="canoe safety"), "test")
    state.embedding_service = provider
    replacement = LocalProvider()
    replacement.model_revision = "new-revision-same-dimension"
    provider.change = lambda: setattr(state, "embedding_service", replacement)
    if operation == "query":
        result = await state.search_memory(MemorySearch(query="canoe"))
        assert result[0]["search_kind"] == "lexical"
    elif operation == "index":
        assert await state.index_memory(item["id"]) is False
    else:
        report = await state.backfill_memory_embeddings(scope="general")
        assert report["indexed"] == 0 and report["conflicted"] == 1
    if operation != "query":
        async with aiosqlite.connect(state.db_path) as db:
            assert await (
                await db.execute("SELECT COUNT(*) FROM memory_embeddings")
            ).fetchone() == (0,)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field,value", [("dimensions", 4), ("model_revision", "new"), ("normalization", "new")]
)
async def test_project_cached_query_does_not_survive_space_change(
    tmp_path: Path, field: str, value: object
) -> None:
    manager, detail, _ = await _project(tmp_path)
    goal, node = detail["goal"]["id"], detail["nodes"][0]["id"]
    await _messages(manager, goal, ["Customer contact decision"])
    provider = SemanticProvider()
    memory = ProjectMemoryService(manager.db_path, provider)
    assert (await memory.retrieve(goal, node, "Customer", base_revision_id=None))[
        "mode"
    ] == "semantic"
    setattr(provider, field, value)
    found = await memory.retrieve(goal, node, "Customer", base_revision_id=None)
    assert found["mode"] == "lexical" and found["reason"] == "embedding_provider_changed"
    assert len(provider.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("replacement", [False, True])
async def test_project_provider_changes_during_embedding_persist_no_vectors(
    tmp_path: Path, replacement: bool
) -> None:
    class SwitchingProvider(SemanticProvider):
        async def embed(self, texts: list[str]) -> list[list[float]]:
            result = await super().embed(texts)
            if replacement:
                memory.embedding_service = SemanticProvider()
                memory.embedding_service.model_revision = "new"  # type: ignore[attr-defined]
            else:
                self.model_revision = "new"
            return result

    manager, detail, _ = await _project(tmp_path)
    goal, node = detail["goal"]["id"], detail["nodes"][0]["id"]
    await _messages(manager, goal, ["Customer contact decision"])
    memory = ProjectMemoryService(manager.db_path, SwitchingProvider())
    result = await memory.retrieve(goal, node, "Customer", base_revision_id=None)
    assert result["mode"] == "lexical" and result["reason"] == "embedding_provider_changed"
    async with aiosqlite.connect(manager.db_path) as db:
        assert await (
            await db.execute(
                "SELECT COUNT(*) FROM project_memory_items WHERE vector_json IS NOT NULL"
            )
        ).fetchone() == (0,)


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["model_revision", "normalization", "chunker_version"])
async def test_episode_same_name_new_space_never_reuses_old_vectors(
    tmp_path: Path, field: str
) -> None:
    path = tmp_path / "state.db"
    goal, root = await _seed_goal_run(path, "embedding_space")
    provider = LocalProvider()
    service = EpisodeMemoryService(path, provider)
    episode = await service.record_episode(
        goal_run_id=goal,
        root_task_id=root,
        objective_summary="canoe safety",
        plan_summary="Check canoe documentation",
        outcome="completed",
    )
    assert (await service.search("canoe", goal_run_id=goal))[0].search_kind == "hybrid"
    setattr(provider, field, "new")
    results = await service.search("canoe", goal_run_id=goal)
    assert results[0].search_kind == "lexical" and results[0].episode.id == episode.id


@pytest.mark.asyncio
async def test_episode_provider_changes_during_index_keeps_only_original_episode(
    tmp_path: Path,
) -> None:
    class SwitchingProvider(LocalProvider):
        async def embed(self, texts: list[str]) -> list[list[float]]:
            result = await super().embed(texts)
            self.model_revision = "new"
            return result

    path = tmp_path / "state.db"
    goal, root = await _seed_goal_run(path, "index_race")
    service = EpisodeMemoryService(path, SwitchingProvider())
    episode = await service.record_episode(
        goal_run_id=goal,
        root_task_id=root,
        objective_summary="canoe safety",
        plan_summary="Check canoe documentation",
        outcome="completed",
    )
    assert await service.get_episode(episode.id) == episode
    async with aiosqlite.connect(path) as db:
        assert await (await db.execute("SELECT COUNT(*) FROM episode_embeddings")).fetchone() == (
            0,
        )


@pytest.mark.asyncio
async def test_project_new_revision_rebuilds_same_width_and_revalidates_goal_receipt(
    tmp_path: Path,
) -> None:
    from app.services.project_memory import ProjectMemoryConflict
    from tests.test_goal_memory_context import _goal

    manager, goal = await _goal(tmp_path)
    provider = SemanticProvider()
    provider.model_revision = "one"
    memory = ProjectMemoryService(manager.db_path, provider)
    initial = await memory.retrieve_for_goal(goal, "planner")
    assert initial["mode"] == "semantic"
    assert len(initial["provider_fingerprint"]) == 64
    int(initial["provider_fingerprint"], 16)
    provider.model_revision = "two"
    with pytest.raises(ProjectMemoryConflict, match="context changed"):
        await memory.assert_context_current(goal, "planner", initial["context_fingerprint"])
    fallback = await memory.retrieve_for_goal(goal, "planner")
    assert fallback["mode"] == "lexical"
    assert fallback["reason"] == "embedding_provider_changed"
    assert len(provider.calls) == 1
    restarted = ProjectMemoryService(manager.db_path, provider)
    current = await restarted.retrieve_for_goal(goal, "planner")
    assert current["mode"] == "semantic"
    assert current["provider_fingerprint"] != initial["provider_fingerprint"]
    assert len(provider.calls) == 2 and len(provider.calls[-1]) > 1
    assert await restarted.retrieve_for_goal(goal, "planner") == current
    assert len(provider.calls) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("replace", [False, True])
async def test_episode_query_does_not_use_vectors_after_provider_changes(
    tmp_path: Path, replace: bool
) -> None:
    class SwitchingProvider(LocalProvider):
        switch = False

        async def embed(self, texts: list[str]) -> list[list[float]]:
            result = await super().embed(texts)
            if self.switch:
                if replace:
                    service.embedding_service = LocalProvider()
                else:
                    self.model_revision = "two"
            return result

    path = tmp_path / "state.db"
    goal, root = await _seed_goal_run(path, "query_race")
    provider = SwitchingProvider()
    service = EpisodeMemoryService(path, provider)
    episode = await service.record_episode(
        goal_run_id=goal,
        root_task_id=root,
        objective_summary="canoe safety",
        plan_summary="Check documentation",
        outcome="completed",
    )
    provider.switch = True
    result = await service.search("canoe", goal_run_id=goal)
    assert result[0].episode.id == episode.id and result[0].search_kind == "lexical"


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["update", "delete"])
async def test_episode_index_does_not_persist_stale_or_deleted_source(
    tmp_path: Path, change: str
) -> None:
    path = tmp_path / "state.db"
    goal, root = await _seed_goal_run(path, "source_race")
    service = EpisodeMemoryService(path)
    episode = await service.record_episode(
        goal_run_id=goal,
        root_task_id=root,
        objective_summary="canoe safety",
        plan_summary="Check documentation",
        outcome="completed",
    )

    class ChangingSource(LocalProvider):
        async def embed(self, texts: list[str]) -> list[list[float]]:
            result = await super().embed(texts)
            async with aiosqlite.connect(path) as db:
                if change == "update":
                    await db.execute(
                        "UPDATE episodes SET objective_summary='corrected source' WHERE id=?",
                        (episode.id,),
                    )
                else:
                    await db.execute("DELETE FROM episodes WHERE id=?", (episode.id,))
                await db.commit()
            return result

    service.embedding_service = ChangingSource()
    await service._index_episode(episode.id)
    async with aiosqlite.connect(path) as db:
        assert await (await db.execute("SELECT COUNT(*) FROM episode_embeddings")).fetchone() == (
            0,
        )
    current = await service.get_episode(episode.id)
    if change == "update":
        assert current and current.objective_summary == "corrected source"
    else:
        assert current is None


@pytest.mark.asyncio
@pytest.mark.parametrize("dimensions,declared", [(8193, 8193), (3, 2)])
async def test_episode_unsupported_width_is_lexical(
    tmp_path: Path, dimensions: int, declared: int
) -> None:
    class BadWidth(LocalProvider):
        async def embed(self, texts: list[str]) -> list[list[float]]:
            return [[1.0] * dimensions for _ in texts]

    path = tmp_path / "state.db"
    goal, root = await _seed_goal_run(path, "bad_width")
    provider = BadWidth()
    provider.dimensions = declared
    service = EpisodeMemoryService(path, provider)
    episode = await service.record_episode(
        goal_run_id=goal,
        root_task_id=root,
        objective_summary="canoe safety",
        plan_summary="Check documentation",
        outcome="completed",
    )
    result = await service.search("canoe", goal_run_id=goal)
    assert result[0].episode.id == episode.id and result[0].search_kind == "lexical"
    async with aiosqlite.connect(path) as db:
        assert await (await db.execute("SELECT COUNT(*) FROM episode_embeddings")).fetchone() == (
            0,
        )


@pytest.mark.asyncio
async def test_reader_prefix_contract_matches_actual_inputs(tmp_path: Path) -> None:
    provider = LocalProvider()
    provider.query_prefix = "provider-declared-but-not-reader-applied:"
    provider.document_prefix = "provider-declared-but-not-reader-applied:"
    state = StateService(tmp_path / "state.db", provider)
    await state.initialize()
    await state.create_memory(MemoryCreate(content="canoe safety"), "test")
    await state.search_memory(MemorySearch(query="canoe"))
    assert provider.calls == [["canoe safety "], ["canoe"]]

    manager, detail, _ = await _project(tmp_path / "project")
    goal, node = detail["goal"]["id"], detail["nodes"][0]["id"]
    project_provider = SemanticProvider()
    memory = ProjectMemoryService(
        manager.db_path, project_provider, query_prefix="query: ", document_prefix="passage: "
    )
    await memory.retrieve(goal, node, "Customer", base_revision_id=None)
    assert project_provider.calls[0][0] == "query: Customer"
    assert all(text.startswith("passage: ") for text in project_provider.calls[0][1:])
    assert (
        memory.provider_identity
        != ProjectMemoryService(manager.db_path, project_provider).provider_identity
    )


@pytest.mark.asyncio
async def test_goal_cached_receipt_rechecks_space_after_reading_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.services.project_memory import ProjectMemoryConflict
    from tests.test_goal_memory_context import _goal

    manager, goal = await _goal(tmp_path)
    provider = SemanticProvider()
    memory = ProjectMemoryService(manager.db_path, provider)
    receipt = await memory.retrieve_for_goal(goal, "planner")
    assert receipt["items"] and receipt["mode"] == "semantic"
    original = aiosqlite.Connection.execute
    switched = False

    async def execute(connection, sql, parameters=None):
        nonlocal switched
        result = await original(connection, sql, parameters)
        if "SELECT source_kind,source_id FROM project_memory_items" in sql:
            switched = True
            provider.model_revision = "new-revision"
        return result

    monkeypatch.setattr(aiosqlite.Connection, "execute", execute)
    with pytest.raises(ProjectMemoryConflict, match="context changed"):
        await memory.retrieve_for_goal(goal, "planner")
    assert switched and len(provider.calls) == 1


@pytest.mark.asyncio
async def test_backfill_cannot_override_declared_space_width(tmp_path: Path) -> None:
    provider = LocalProvider()
    state = StateService(tmp_path / "state.db")
    await state.initialize()
    item = await state.create_memory(MemoryCreate(content="canoe safety"), "test")
    state.embedding_service = provider
    with pytest.raises(ValueError, match="dimensions conflict"):
        await state.backfill_memory_embeddings(scope="general", dimensions=3)
    assert provider.calls == []
    assert await state.get_memory(item["id"]) is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["index", "backfill"])
@pytest.mark.parametrize("replacement", [False, True])
async def test_general_storage_rechecks_provider_instance_after_insert(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str, replacement: bool
) -> None:
    state = StateService(tmp_path / "state.db")
    await state.initialize()
    item = await state.create_memory(MemoryCreate(content="canoe safety"), "test")
    provider = LocalProvider()
    state.embedding_service = provider
    original = aiosqlite.Connection.execute
    changed = False

    @sqlite_contextmanager
    async def execute(connection, sql, parameters=None):
        nonlocal changed
        cursor = await original(connection, sql, parameters)
        if "INSERT OR REPLACE INTO memory_embeddings" in sql:
            changed = True
            if replacement:
                # Identical declared metadata still identifies a different in-flight provider.
                state.embedding_service = LocalProvider()
            else:
                provider.model_revision = "new-revision"
        return cursor

    monkeypatch.setattr(aiosqlite.Connection, "execute", execute)
    if operation == "index":
        assert await state.index_memory(item["id"]) is False
    else:
        report = await state.backfill_memory_embeddings(scope="general")
        assert report["indexed"] == 0 and report["conflicted"] == 1
        assert report["complete"] is False and report["next_after_id"] is None
    assert changed
    async with aiosqlite.connect(state.db_path) as db:
        assert await (await db.execute("SELECT COUNT(*) FROM memory_embeddings")).fetchone() == (0,)
        view_vectors = await (
            await db.execute("SELECT COUNT(*) FROM memory_view_embeddings")
        ).fetchone()
        assert view_vectors == (0,)
    assert await state.get_memory(item["id"]) is not None
