from pathlib import Path

import pytest

from app.models import MemoryCreate, MemorySearch
from app.services.embedding_service import DeterministicEmbeddingService
from app.services.state_service import StateService


@pytest.mark.asyncio
async def test_memory_search_is_hybrid_when_embeddings_exist(tmp_path: Path) -> None:
    state = StateService(tmp_path / "state.db", DeterministicEmbeddingService())
    await state.initialize()
    memory = await state.create_memory(
        MemoryCreate(content="canoe route lake", scope="outdoors"), "device"
    )
    results = await state.search_memory(MemorySearch(query="canoe lake", scope="outdoors"))
    assert results[0]["id"] == memory["id"]
    assert results[0]["search_kind"] == "hybrid"


@pytest.mark.asyncio
async def test_memory_search_falls_back_to_lexical_without_provider(tmp_path: Path) -> None:
    state = StateService(tmp_path / "state.db")
    await state.initialize()
    await state.create_memory(MemoryCreate(content="local Ubuntu truth"), "device")
    results = await state.search_memory(MemorySearch(query="Ubuntu"))
    assert results[0]["search_kind"] == "lexical"
