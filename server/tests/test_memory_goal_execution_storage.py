"""Request-local execution survives memory storage/presentation boundaries."""

from pathlib import Path

import pytest

from app.models import MemorySearch
from app.services.model_request_execution import ModelExecutionControlError
from tests.test_memory_query_normalization import (
    FrenchPresenter,
    QueryNormalizer,
    row_counts,
    seeded,
)


class Executor:
    async def execute(self, **kwargs):
        raise AssertionError("Storage forwards execution; only a provider invokes it")


class ScopedNormalizer(QueryNormalizer):
    def __init__(self, seen, failure=None):
        super().__init__()
        self.seen = seen
        self.execution_failure = failure

    async def normalize(self, source, *, recheck_source=None, model_executor=None):
        self.seen.append(("normalize", model_executor))
        if self.execution_failure:
            raise self.execution_failure
        return await super().normalize(source, recheck_source=recheck_source)


class ScopedPresenter(FrenchPresenter):
    def __init__(self, seen, failure=None):
        super().__init__()
        self.seen = seen
        self.execution_failure = failure

    async def present(self, batch, *, recheck_sources=None, model_executor=None):
        self.seen.append(("present", model_executor))
        if self.execution_failure:
            raise self.execution_failure
        return await super().present(batch, recheck_sources=recheck_sources)


class ScopedEmbedding:
    provider_name = "storage-test"
    dimensions = 2

    def __init__(self, seen, failure=None):
        self.seen = seen
        self.failure = failure

    async def embed(self, texts, *, model_executor=None):
        self.seen.append(("embed", model_executor))
        assert texts == ["customer retention"]
        if self.failure:
            raise self.failure
        return [[1.0, 0.0]]


@pytest.mark.asyncio
async def test_request_executor_reaches_all_providers_without_memory_writes(tmp_path: Path):
    state, item = await seeded(tmp_path)
    seen = []
    state.memory_normalizer = ScopedNormalizer(seen)
    state.memory_presenter = ScopedPresenter(seen)
    state.embedding_service = ScopedEmbedding(seen)
    before = await row_counts(state)
    executor = Executor()
    found = await state.search_memory(
        MemorySearch(query="conservation des clients"), model_executor=executor
    )
    assert [row["id"] for row in found] == [item["id"]]
    assert found[0]["presentation"]["content"] == "Conservation des clients."
    assert seen == [(name, executor) for name in ("normalize", "embed", "present")]
    assert await row_counts(state) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["normalize", "embed", "present"])
async def test_execution_control_error_is_never_wrapped_or_empty_search(tmp_path: Path, stage):
    state, _ = await seeded(tmp_path)
    seen = []
    control = ModelExecutionControlError("goal revision changed")
    state.memory_normalizer = ScopedNormalizer(seen, control if stage == "normalize" else None)
    state.embedding_service = ScopedEmbedding(seen, control if stage == "embed" else None)
    state.memory_presenter = ScopedPresenter(seen, control if stage == "present" else None)
    before = await row_counts(state)
    with pytest.raises(ModelExecutionControlError) as caught:
        await state.search_memory(
            MemorySearch(query="conservation des clients"), model_executor=Executor()
        )
    assert caught.value is control
    assert not state.memory_normalization_gate.locked()
    assert [name for name, _ in seen] == ["normalize", "embed", "present"][: len(seen)]
    assert seen[-1][0] == stage
    assert await row_counts(state) == before


@pytest.mark.asyncio
async def test_direct_search_preserves_providers_without_executor_keyword(tmp_path: Path):
    state, item = await seeded(tmp_path)

    class OldEmbedding:
        provider_name = "old-signature"
        dimensions = 2

        async def embed(self, texts):
            return [[1.0, 0.0]]

    state.embedding_service = OldEmbedding()
    found = await state.search_memory(MemorySearch(query="conservation des clients"))
    assert [row["id"] for row in found] == [item["id"]]
    assert len(state.memory_normalizer.calls) == len(state.memory_presenter.calls) == 1


@pytest.mark.asyncio
async def test_invalid_scope_is_rejected_before_forwarding_executor(tmp_path: Path):
    state, _ = await seeded(tmp_path)
    seen = []
    state.memory_normalizer = ScopedNormalizer(seen)
    assert (
        await state.search_memory(
            MemorySearch(query="conservation des clients"),
            allowed_scopes=(),
            model_executor=Executor(),
        )
        == []
    )
    assert seen == []
