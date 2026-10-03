"""Public StateService dual-view retrieval with deterministic, orthogonal vectors."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

import httpx
import pytest

from app.models import MemoryCreate, MemorySearch
from app.services import embedding_service as embedding_module
from app.services import state_service as state_module
from app.services.direct_model_admission import LocalGPUUnavailable
from app.services.embedding_service import HttpEmbeddingService
from app.services.memory_normalization import MemoryNormalizationError
from app.services.state_service import StateService
from tests.test_memory_canonical_store import ReviewedNormalizer
from tests.test_memory_query_normalization import FrenchPresenter, QueryNormalizer

E1 = [1.0, 0.0, 0.0, 0.0]
E2 = [0.0, 1.0, 0.0, 0.0]
E3 = [0.0, 0.0, 1.0, 0.0]
E4 = [0.0, 0.0, 0.0, 1.0]
NATIVE_QUERY = "natif_introuvable"
PIVOT_QUERY = "pivot_unmatched"


class OrthogonalProvider:
    provider_name = "dual-search-orthogonal-fixture"
    dimensions = 4

    def __init__(self):
        self.mapping = {
            "Garder les dates exactes.": E1,
            "Keep the exact dates.": E2,
            "Ne pas envoyer automatiquement.": E3,
            "Do not send automatically.": E4,
            "Customer retention.": E1,
            NATIVE_QUERY: E1,
            PIVOT_QUERY: E2,
            "Garder": E1,
        }
        self.calls = []
        self.response = None
        self.check_admission = None

    async def embed(self, texts, **kwargs):
        if self.check_admission:
            self.check_admission()
        self.calls.append(list(texts))
        return (
            self.response
            if self.response is not None
            else [self.mapping[text.strip()] for text in texts]
        )


async def prepared(tmp_path, contents=("Garder les dates exactes.",)):
    provider = OrthogonalProvider()
    state = StateService(
        tmp_path / "state.db",
        provider,
        canonical_language="en",
        memory_normalizer=ReviewedNormalizer(),
    )
    await state.initialize()
    items = [
        await state.create_memory(MemoryCreate(content=content), "fixture") for content in contents
    ]
    state.memory_normalizer = QueryNormalizer(english=PIVOT_QUERY)
    provider.calls.clear()
    return state, provider, items


async def test_distinct_native_and_pivot_query_vectors_retrieve_corresponding_views(tmp_path):
    state, provider, items = await prepared(
        tmp_path, ("Garder les dates exactes.", "Ne pas envoyer automatiquement.")
    )
    provider.mapping[PIVOT_QUERY] = E4
    found = await state.search_memory(MemorySearch(query=NATIVE_QUERY))
    by_id = {row["id"]: row for row in found}
    assert set(by_id) == {item["id"] for item in items}
    assert provider.calls == [[NATIVE_QUERY, PIVOT_QUERY]]
    assert by_id[items[0]["id"]]["ranking"]["ranks"] == {"original": 1}
    assert by_id[items[1]["id"]]["ranking"]["ranks"] == {"canonical": 1}
    assert all(row["search_kind"] == "vector" and row["score_kind"] == "rrf" for row in found)
    assert [by_id[item["id"]]["content"] for item in items] == [
        "Keep the exact dates.",
        "Do not send automatically.",
    ]
    provider.mapping[NATIVE_QUERY], provider.mapping[PIVOT_QUERY] = E2, E1
    assert await state.search_memory(MemorySearch(query=NATIVE_QUERY)) == []


async def test_two_view_hits_dedupe_to_one_english_memory(tmp_path):
    state, provider, (item,) = await prepared(tmp_path)
    found = await state.search_memory(MemorySearch(query=NATIVE_QUERY))
    assert [row["id"] for row in found] == [item["id"]]
    assert found[0]["content"] == "Keep the exact dates."
    assert found[0]["ranking"]["ranks"] == {"original": 1, "canonical": 1}
    assert found[0]["presentation"]["mode"] == "original"
    assert found[0]["score"] == 1.0
    assert provider.calls == [[NATIVE_QUERY, PIVOT_QUERY]]


async def test_identical_english_query_text_uses_one_embedding_input(tmp_path):
    state, provider, (item,) = await prepared(tmp_path)
    state.memory_normalizer = QueryNormalizer(english=PIVOT_QUERY)
    state.memory_normalizer.source_language = "en"
    found = await state.search_memory(MemorySearch(query=PIVOT_QUERY))
    assert provider.calls == [[PIVOT_QUERY]]
    assert [row["id"] for row in found] == [item["id"]]
    assert found[0]["ranking"]["ranks"] == {"canonical": 1}
    assert "presentation" not in found[0]


async def test_direct_search_holds_one_admission_slot_for_both_query_vectors(tmp_path):
    state, provider, _ = await prepared(tmp_path)
    active = False
    entries = 0

    @asynccontextmanager
    async def admission():
        nonlocal active, entries
        assert not active
        entries += 1
        active = True
        try:
            yield
        finally:
            active = False

    def check():
        assert active

    provider.check_admission = check
    state.embedding_admission = admission
    assert await state.search_memory(MemorySearch(query=NATIVE_QUERY))
    assert entries == 1 and not active
    assert provider.calls == [[NATIVE_QUERY, PIVOT_QUERY]]


async def test_busy_direct_admission_preserves_native_lexical_result(tmp_path):
    state, provider, (item,) = await prepared(tmp_path)

    @asynccontextmanager
    async def admission():
        raise LocalGPUUnavailable("fixture busy")
        yield

    state.embedding_admission = admission
    found = await state.search_memory(MemorySearch(query="Garder"))
    assert [row["id"] for row in found] == [item["id"]]
    assert found[0]["score_kind"] == "lexical_overlap"
    assert provider.calls == []


@pytest.mark.parametrize(
    "change", ["provider", "provider_revision", "normalizer", "normalizer_signature"]
)
async def test_configuration_changed_while_waiting_for_admission_never_calls_stale_provider(
    tmp_path, change
):
    state, provider, (item,) = await prepared(tmp_path)
    entered, release = asyncio.Event(), asyncio.Event()

    @asynccontextmanager
    async def admission():
        entered.set()
        await release.wait()
        yield

    state.embedding_admission = admission
    pending = asyncio.create_task(state.search_memory(MemorySearch(query="Garder")))
    await asyncio.wait_for(entered.wait(), 2)
    if change == "provider":
        state.embedding_service = OrthogonalProvider()
    elif change == "provider_revision":
        state.embedding_model_revision = "changed-revision"
    elif change == "normalizer":
        state.memory_normalizer = QueryNormalizer(english=PIVOT_QUERY)
    else:
        state.memory_normalizer.normalization_signature = "e" * 64
    release.set()
    if change.startswith("normalizer"):
        with pytest.raises(MemoryNormalizationError) as caught:
            await asyncio.wait_for(pending, 2)
        assert caught.value.category == "source_conflict"
    else:
        found = await asyncio.wait_for(pending, 2)
        assert [row["id"] for row in found] == [item["id"]]
        assert found[0]["score_kind"] == "lexical_overlap"
        assert state.embedding_service.calls == []
    assert provider.calls == []


@pytest.mark.parametrize(
    "response",
    [
        [E1],
        [E1, [float("nan"), 0.0, 0.0, 0.0]],
        [E1, [0.0, 0.0, 0.0, 0.0]],
        [E1, [0.0, 1.0]],
        [E1, [True, 0.0, 0.0, 0.0]],
        [E1, E2, E3],
    ],
)
async def test_invalid_query_batch_discards_every_semantic_channel(tmp_path, response):
    state, provider, (item,) = await prepared(tmp_path)
    provider.response = response
    found = await state.search_memory(MemorySearch(query="Garder"))
    assert [row["id"] for row in found] == [item["id"]]
    assert found[0]["score"] == 1.0 and found[0]["score_kind"] == "lexical_overlap"
    assert found[0]["ranking"]["active_streams"] == ["lexical"]
    assert provider.calls == [["Garder", PIVOT_QUERY]]


@pytest.mark.parametrize("target", ["presenter", "embedding_provider", "normalizer"])
async def test_pure_vector_presentation_rechecks_every_provider(tmp_path, target):
    state, provider, (item,) = await prepared(tmp_path, ("Customer retention.",))
    presenter = state.memory_presenter = FrenchPresenter()

    async def change():
        if target == "presenter":
            state.memory_presenter = FrenchPresenter()
        elif target == "embedding_provider":
            state.embedding_service = OrthogonalProvider()
        else:
            state.memory_normalizer = QueryNormalizer(english=PIVOT_QUERY)

    presenter.change = change
    with pytest.raises(MemoryNormalizationError) as caught:
        await state.search_memory(MemorySearch(query=NATIVE_QUERY))
    assert caught.value.category == "source_conflict"
    assert len(presenter.calls) == 1
    assert presenter.calls[0].items[0].memory_id == item["id"]
    assert provider.calls == [[NATIVE_QUERY, PIVOT_QUERY]]


async def test_pure_vector_presenter_is_pinned_through_final_view_read(tmp_path, monkeypatch):
    state, _, _ = await prepared(tmp_path, ("Customer retention.",))
    presenter = state.memory_presenter = FrenchPresenter()
    qualify = state_module.qualify_search_rows

    async def change_after_read(db, rows):
        result = await qualify(db, rows)
        if presenter.calls:
            state.memory_presenter = FrenchPresenter()
        return result

    monkeypatch.setattr(state_module, "qualify_search_rows", change_after_read)
    with pytest.raises(MemoryNormalizationError) as caught:
        await state.search_memory(MemorySearch(query=NATIVE_QUERY))
    assert caught.value.category == "source_conflict"
    assert len(presenter.calls) == 1


async def test_http_batch_response_indices_bind_native_and_pivot_inputs(monkeypatch):
    client_type = httpx.AsyncClient
    requests = []

    async def respond(request):
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "data": [
                    {"index": 1, "embedding": E2},
                    {"index": 0, "embedding": E1},
                ]
            },
        )

    monkeypatch.setattr(
        embedding_module.httpx,
        "AsyncClient",
        lambda **kwargs: client_type(transport=httpx.MockTransport(respond), **kwargs),
    )
    provider = HttpEmbeddingService("http://fixture.invalid/v1", "fixture-model")
    assert await provider.embed([NATIVE_QUERY, PIVOT_QUERY]) == [E1, E2]
    assert len(requests) == 1
