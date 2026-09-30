from __future__ import annotations

import asyncio
import json
from pathlib import Path

import aiosqlite
import pytest

from app.models import MemoryCreate, MemorySearch, MemoryUpdate
from app.services.embedding_service import EmbeddingServiceError
from app.services.project_memory import ProjectMemoryService
from app.services.state_service import StateService
from app.services.swarm_contracts import GoalCreateRequest
from tests.test_goal_project_runtime import _project
from tests.test_project_memory import SemanticProvider, _messages


class LocalProvider:
    provider_name = "local-fixture"
    base_url = "http://fixture.invalid/v1"
    model = "fixture-model"
    model_revision = "one"
    dimensions = 2

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.fail = False

    async def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(texts)
        if self.fail:
            raise EmbeddingServiceError("fixture unavailable")
        return [[1.0, 0.0] if "canoe" in text else [0.0, 1.0] for text in texts]


async def _state(tmp_path: Path, provider: LocalProvider | None = None) -> StateService:
    state = StateService(tmp_path / "state.db", provider)
    await state.initialize()
    return state


@pytest.mark.asyncio
async def test_summary_only_update_reindexes_and_failed_update_stays_lexical(
    tmp_path: Path,
) -> None:
    provider = LocalProvider()
    state = await _state(tmp_path, provider)
    item = await state.create_memory(MemoryCreate(content="boat", summary="old"), "phone")
    await state.update_memory(item["id"], MemoryUpdate(summary="canoe"), "phone")
    assert provider.calls[-1] == ["boat canoe"]
    assert (await state.search_memory(MemorySearch(query="canoe")))[0]["search_kind"] == "hybrid"

    provider.fail = True
    await state.update_memory(item["id"], MemoryUpdate(summary="kayak"), "phone")
    provider.fail = False
    results = await state.search_memory(MemorySearch(query="kayak"))
    assert [(row["id"], row["search_kind"]) for row in results] == [(item["id"], "lexical")]
    async with aiosqlite.connect(state.db_path) as db:
        assert await (await db.execute("SELECT COUNT(*) FROM memory_embeddings")).fetchone() == (0,)


@pytest.mark.asyncio
async def test_hybrid_search_retains_lexical_only_matches_and_filters(tmp_path: Path) -> None:
    state = await _state(tmp_path)
    lexical = await state.create_memory(
        MemoryCreate(content="canoe safety", scope="outdoors"), "phone"
    )
    await state.create_memory(MemoryCreate(content="canoe other scope", scope="private"), "phone")
    await state.create_memory(
        MemoryCreate(content="canoe wrong kind", scope="outdoors", kind="other"), "phone"
    )
    state.embedding_service = LocalProvider()
    semantic = await state.create_memory(
        MemoryCreate(content="canoe route", scope="outdoors"), "phone"
    )
    results = await state.search_memory(MemorySearch(query="canoe", scope="outdoors", kind="fact"))
    assert {row["id"] for row in results} == {lexical["id"], semantic["id"]}
    assert next(row for row in results if row["id"] == lexical["id"])["search_kind"] == "lexical"


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["base_url", "model", "model_revision", "dimensions"])
async def test_changed_provider_identity_never_reuses_old_vectors(
    tmp_path: Path, change: str
) -> None:
    provider = LocalProvider()
    state = await _state(tmp_path, provider)
    item = await state.create_memory(MemoryCreate(content="canoe safety"), "phone")
    setattr(provider, change, 3 if change == "dimensions" else "different")
    results = await state.search_memory(MemorySearch(query="canoe"))
    assert [(row["id"], row["search_kind"]) for row in results] == [(item["id"], "lexical")]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "vector", [[1.0], [0.0, 0.0], [True, 0.0], [float("nan"), 0.0], [float("inf"), 1.0]]
)
async def test_corrupt_or_incompatible_cached_vectors_fall_back_to_lexical(
    tmp_path: Path, vector: list[float]
) -> None:
    state = await _state(tmp_path, LocalProvider())
    item = await state.create_memory(MemoryCreate(content="canoe safety"), "phone")
    async with aiosqlite.connect(state.db_path) as db:
        await db.execute("UPDATE memory_embeddings SET vector_json=?", (json.dumps(vector),))
        await db.commit()
    results = await state.search_memory(MemorySearch(query="canoe"))
    assert [(row["id"], row["search_kind"]) for row in results] == [(item["id"], "lexical")]


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["update", "delete"])
async def test_embedding_in_flight_cannot_restore_changed_or_deleted_memory(
    tmp_path: Path, change: str
) -> None:
    state = await _state(tmp_path)
    item = await state.create_memory(MemoryCreate(content="canoe old"), "phone")
    entered, release = asyncio.Event(), asyncio.Event()

    class Blocking(LocalProvider):
        async def embed(self, texts: list[str]) -> list[list[float]]:
            entered.set()
            await release.wait()
            return await super().embed(texts)

    state.embedding_service = Blocking()
    pending = asyncio.create_task(state.index_memory(item["id"]))
    await asyncio.wait_for(entered.wait(), 2)
    writer = StateService(state.db_path)
    if change == "update":
        await writer.update_memory(item["id"], MemoryUpdate(content="kayak new"), "phone")
    else:
        await writer.delete_memory(item["id"], "phone")
    release.set()
    await pending
    async with aiosqlite.connect(state.db_path) as db:
        assert await (await db.execute("SELECT COUNT(*) FROM memory_embeddings")).fetchone() == (0,)
    if change == "update":
        results = await state.search_memory(MemorySearch(query="kayak"))
        assert results[0]["content"] == "kayak new" and results[0]["search_kind"] == "lexical"
    else:
        assert await state.search_memory(MemorySearch(query="canoe")) == []


@pytest.mark.asyncio
async def test_delete_removes_all_embedding_caches(tmp_path: Path) -> None:
    state = await _state(tmp_path, LocalProvider())
    item = await state.create_memory(MemoryCreate(content="canoe"), "phone")
    await state.delete_memory(item["id"], "phone")
    async with aiosqlite.connect(state.db_path) as db:
        assert await (await db.execute("SELECT COUNT(*) FROM memory_embeddings")).fetchone() == (0,)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["lexical", "semantic", "hybrid"])
async def test_repeated_project_content_does_not_crowd_out_distinct_decisions(
    tmp_path: Path, mode: str
) -> None:
    manager, detail, _ = await _project(tmp_path)
    goal_id, node_id = detail["goal"]["id"], detail["nodes"][0]["id"]
    repeated = "Customer contacts must stay local."
    decisions = [
        repeated,
        "Customer contacts may be shared.",
        "Customer phone numbers are optional.",
        "Customer profiles require an identifier.",
    ]
    ids = await _messages(manager, goal_id, [repeated] * 30 + decisions[1:])
    provider = SemanticProvider() if mode != "lexical" else None
    memory = ProjectMemoryService(manager.db_path, provider, hybrid=mode == "hybrid")
    result = await memory.retrieve(goal_id, node_id, "Customer contacts", base_revision_id=None)
    assert result["mode"] == mode
    assert {item["summary"] for item in result["items"]} == set(decisions)
    assert len({item["source_id"] for item in result["items"]}) == 4
    assert (
        await memory.retrieve(goal_id, node_id, "Customer contacts", base_revision_id=None)
        == result
    )
    if provider is not None:
        assert len(provider.calls) == 1
        assert provider.calls[0].count(repeated) == 1
    async with aiosqlite.connect(manager.db_path) as db:
        rows = await (
            await db.execute(
                "SELECT id,content FROM goal_messages WHERE id IN (SELECT value FROM json_each(?))",
                (json.dumps(ids),),
            )
        ).fetchall()
    assert len(rows) == 33 and sum(content == repeated for _, content in rows) == 30


@pytest.mark.asyncio
async def test_project_refresh_keeps_other_conversation_projection(tmp_path: Path) -> None:
    manager, detail, _ = await _project(tmp_path)
    first_id, node_id = detail["goal"]["id"], detail["nodes"][0]["id"]
    first_message = (await _messages(manager, first_id, ["Customer legacy contract"]))[0]
    memory = ProjectMemoryService(manager.db_path)
    await memory.retrieve(first_id, node_id, "Customer", base_revision_id=None)
    second = await manager.create_goal(
        GoalCreateRequest(objective="Customer second conversation"), actor_id="phone"
    )
    await manager.project_applications.ensure_project(second["id"])
    async with aiosqlite.connect(manager.db_path) as db:
        await db.execute(
            "UPDATE goal_project_links SET project_id=(SELECT project_id FROM goal_project_links WHERE goal_run_id=?) WHERE goal_run_id=?",
            (first_id, second["id"]),
        )
        await db.commit()
    second_message = (await _messages(manager, second["id"], ["Customer new contract"]))[0]
    result = await memory.retrieve_for_goal(second["id"], "planner")
    assert second_message in {item["source_id"] for item in result["items"]}
    async with aiosqlite.connect(manager.db_path) as db:
        retained = await (await db.execute("SELECT source_id FROM project_memory_items")).fetchall()
    assert first_message in {row[0] for row in retained}


@pytest.mark.asyncio
async def test_bounded_backfill_resumes_after_failure_without_rewriting_sources(
    tmp_path: Path,
) -> None:
    state = await _state(tmp_path)
    for index in range(5):
        await state.create_memory(
            MemoryCreate(content=f"canoe item {index}", scope="outdoors"), "phone"
        )
    outside = await state.create_memory(MemoryCreate(content="untouched", scope="private"), "phone")
    before = await state.list_memory()
    provider = LocalProvider()
    state.embedding_service = provider
    first = await state.backfill_memory_embeddings(scope="outdoors", limit=2)
    assert first["indexed"] == 2 and first["complete"] is False and first["next_after_id"]
    assert len(provider.calls) == 1 and len(provider.calls[0]) == 2
    provider.fail = True
    failed = await state.backfill_memory_embeddings(
        scope="outdoors", after_id=first["next_after_id"], limit=2
    )
    assert (
        failed["failed"] == 2
        and failed["next_after_id"] == first["next_after_id"]
        and failed["complete"] is False
    )
    provider.fail = False
    second = await StateService(state.db_path, provider).backfill_memory_embeddings(
        scope="outdoors", after_id=failed["next_after_id"], limit=2
    )
    assert second["indexed"] == 2
    final = await state.backfill_memory_embeddings(
        scope="outdoors", after_id=second["next_after_id"], limit=2
    )
    assert final["indexed"] == 1 and final["complete"] is True
    replay = await state.backfill_memory_embeddings(scope="outdoors", limit=100)
    assert replay["indexed"] == 0 and replay["unchanged"] == 5 and replay["complete"] is True
    assert await state.list_memory() == before
    async with aiosqlite.connect(state.db_path) as db:
        rows = await (await db.execute("SELECT memory_id FROM memory_embeddings")).fetchall()
    assert len(rows) == 5 and outside["id"] not in {row[0] for row in rows}


@pytest.mark.asyncio
async def test_backfill_cas_keeps_changed_item_pending_and_successful_items_durable(
    tmp_path: Path,
) -> None:
    state = await _state(tmp_path)
    changed = await state.create_memory(MemoryCreate(content="canoe old"), "phone")
    stable = await state.create_memory(MemoryCreate(content="canoe stable"), "phone")

    class Mutating(LocalProvider):
        async def embed(self, texts: list[str]) -> list[list[float]]:
            await StateService(state.db_path).update_memory(
                changed["id"], MemoryUpdate(content="canoe new"), "phone"
            )
            return await super().embed(texts)

    state.embedding_service = Mutating()
    first = await state.backfill_memory_embeddings(scope="general")
    assert first["conflicted"] == 1 and first["indexed"] == 1
    assert first["next_after_id"] is None and first["complete"] is False
    async with aiosqlite.connect(state.db_path) as db:
        assert await (await db.execute("SELECT memory_id FROM memory_embeddings")).fetchall() == [
            (stable["id"],)
        ]
    provider = LocalProvider()
    state.embedding_service = provider
    second = await state.backfill_memory_embeddings(scope="general")
    assert second["unchanged"] == 1 and second["indexed"] == 1 and second["complete"] is True
    assert provider.calls == [["canoe new "]]


@pytest.mark.asyncio
async def test_general_search_uses_current_sources_after_query_embedding(tmp_path: Path) -> None:
    state = await _state(tmp_path)
    item = await state.create_memory(MemoryCreate(content="canoe deleted"), "phone")

    class Deleting(LocalProvider):
        async def embed(self, texts: list[str]) -> list[list[float]]:
            await StateService(state.db_path).delete_memory(item["id"], "phone")
            return await super().embed(texts)

    state.embedding_service = Deleting()
    assert await state.search_memory(MemorySearch(query="canoe")) == []


@pytest.mark.asyncio
async def test_pinning_keeps_valid_cache_and_explicit_null_summary_reindexes(
    tmp_path: Path,
) -> None:
    provider = LocalProvider()
    state = await _state(tmp_path, provider)
    item = await state.create_memory(MemoryCreate(content="boat", summary="canoe"), "phone")
    await state.update_memory(item["id"], MemoryUpdate(pinned=True), "phone")
    assert len(provider.calls) == 1
    assert (await state.search_memory(MemorySearch(query="canoe")))[0]["search_kind"] == "hybrid"
    updated = await state.update_memory(item["id"], MemoryUpdate(summary=None), "phone")
    assert updated and updated["summary"] is None and updated["pinned"] is True
    assert provider.calls[-1] == ["boat "]


@pytest.mark.asyncio
async def test_provider_identity_survives_equivalent_wrapper_restart_without_exposing_origin(
    tmp_path: Path,
) -> None:
    provider = LocalProvider()
    provider.base_url = "http://user:secret@fixture.invalid/v1"
    state = await _state(tmp_path, provider)
    await state.create_memory(MemoryCreate(content="canoe"), "phone")

    class Wrapper:
        provider_name = provider.provider_name
        base_url = provider.base_url + "/"
        model = provider.model
        model_revision = provider.model_revision
        dimensions = provider.dimensions

        async def embed(self, texts: list[str]) -> list[list[float]]:
            return await provider.embed(texts)

    restarted = StateService(state.db_path, Wrapper())
    report = await restarted.backfill_memory_embeddings(scope="general")
    assert report["unchanged"] == 1 and report["indexed"] == 0
    assert "user" not in report["provider_identity"] and "secret" not in report["provider_identity"]
    assert (await restarted.search_memory(MemorySearch(query="canoe")))[0][
        "search_kind"
    ] == "hybrid"


@pytest.mark.asyncio
async def test_project_partial_refresh_and_truncated_opposing_sources_remain_distinct(
    tmp_path: Path,
) -> None:
    manager, detail, _ = await _project(tmp_path)
    goal_id, node_id = detail["goal"]["id"], detail["nodes"][0]["id"]
    common_prefix = "Customer data context. " * 80
    ids = await _messages(
        manager, goal_id, [common_prefix + "Keep records.", common_prefix + "Delete records."]
    )
    memory = ProjectMemoryService(manager.db_path)
    result = await memory.retrieve(goal_id, node_id, "Customer", base_revision_id=None)
    assert {item["source_id"] for item in result["items"]} == set(ids)
    source = await memory._read_sources(goal_id, None)
    assert source
    goal, documents = source
    await memory._refresh_items(goal, documents[:1])
    async with aiosqlite.connect(manager.db_path) as db:
        rows = await (await db.execute("SELECT source_id FROM project_memory_items")).fetchall()
    assert set(ids) <= {row[0] for row in rows}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "arguments",
    [
        {"scope": ""},
        {"scope": "general", "limit": 101},
        {"scope": "general", "limit": 0},
        {"scope": "general", "dimensions": 8193},
    ],
)
async def test_backfill_rejects_unbounded_requests_without_provider_calls(
    tmp_path: Path, arguments: dict[str, object]
) -> None:
    provider = LocalProvider()
    state = await _state(tmp_path, provider)
    with pytest.raises(ValueError):
        await state.backfill_memory_embeddings(**arguments)
    assert provider.calls == []
