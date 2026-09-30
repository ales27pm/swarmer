from pathlib import Path

import aiosqlite
import pytest

from app.models import MemoryCreate, MemorySearch, MemoryUpdate
from app.services.context_builder import ContextBuilder
from app.services.embedding_service import EmbeddingServiceError
from app.services.episode_memory import EpisodeMemoryService
from app.services.state_service import StateService
from app.services.strategy_retrieval import StrategyRetrieval
from tests.test_memory_project_boundaries import _fixture


class Provider:
    provider_name = "correction-test"

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.fail = False

    async def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(texts)
        if self.fail:
            raise EmbeddingServiceError("test unavailable")
        return [[1.0, 0.0] for _ in texts]


@pytest.mark.asyncio
async def test_content_correction_reaches_context_and_strategy_without_stale_summary_or_vector(
    tmp_path: Path,
) -> None:
    state, goal, _ = await _fixture(tmp_path)
    provider = Provider()
    state.embedding_service = provider
    item = await state.create_memory(
        MemoryCreate(
            content="Investigate JSON storage.",
            summary="JSON is the chosen storage engine.",
            scope="project:project_a",
            pinned=True,
        ),
        "phone",
    )
    corrected = "SQLite is the chosen storage engine."
    provider.fail = True
    updated = await state.update_memory(item["id"], MemoryUpdate(content=corrected), "phone")
    assert updated is not None and updated["content"] == corrected and updated["summary"] is None
    assert updated["updated_at"] != item["updated_at"]
    context = await ContextBuilder(state.db_path, max_tokens=8192).build(goal_run_id=goal)
    card = next(card for card in context.cards if item["id"] in card.provenance_ids)
    assert corrected in card.summary and "JSON" not in card.summary
    hints = await StrategyRetrieval(state.db_path, EpisodeMemoryService(state.db_path)).retrieve(
        "SQLite storage engine", goal_run_id=goal
    )
    hint = next(hint for hint in hints.memory if hint.source_id == item["id"])
    assert hint.text == corrected
    results = await state.search_memory(MemorySearch(query="SQLite", scope="project:project_a"))
    result = next(result for result in results if result["id"] == item["id"])
    assert result["search_kind"] == "lexical"
    async with aiosqlite.connect(state.db_path) as db:
        assert await (
            await db.execute(
                "SELECT COUNT(*) FROM memory_embeddings WHERE memory_id=?", (item["id"],)
            )
        ).fetchone() == (0,)
    provider.fail = False
    await state.index_memory(item["id"])
    assert [text.strip() for text in provider.calls[-1]] == [corrected]
    async with aiosqlite.connect(state.db_path) as db:
        assert await (
            await db.execute(
                "SELECT updated_at FROM memory_embeddings WHERE memory_id=?", (item["id"],)
            )
        ).fetchone() == (updated["updated_at"],)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("update", "summary", "calls"),
    [
        (MemoryUpdate(pinned=True), "Existing summary", 1),
        (MemoryUpdate(content="Existing source"), "Existing summary", 1),
        (MemoryUpdate(content=None), "Existing summary", 1),
        (MemoryUpdate(content="New source", summary="New summary"), "New summary", 2),
        (MemoryUpdate(content="New source", summary=None), None, 2),
    ],
)
async def test_summary_policy_preserves_explicit_and_unchanged_edits(
    tmp_path: Path, update: MemoryUpdate, summary: str | None, calls: int
) -> None:
    provider = Provider()
    state = StateService(tmp_path / "state.db", provider)
    await state.initialize()
    item = await state.create_memory(
        MemoryCreate(content="Existing source", summary="Existing summary"), "phone"
    )
    changed = await state.update_memory(item["id"], update, "phone")
    assert changed is not None and changed["summary"] == summary
    assert len(provider.calls) == calls
    async with aiosqlite.connect(state.db_path) as db:
        assert await (
            await db.execute(
                "SELECT updated_at FROM memory_embeddings WHERE memory_id=?", (item["id"],)
            )
        ).fetchone() == (changed["updated_at"],)
