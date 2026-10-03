"""Current native/pivot lexical reads, without external provider calls."""

from __future__ import annotations

import json
import sqlite3

import aiosqlite
import pytest

from app.models import MemoryCreate, MemorySearch, MemoryUpdate
from app.services import state_service as state_module
from app.services.memory_normalization import MemoryNormalizationError
from app.services.state_service import StateService
from tests.test_memory_canonical_store import ReviewedNormalizer
from tests.test_memory_indexing_regressions import LocalProvider
from tests.test_memory_query_normalization import FrenchPresenter, QueryNormalizer


async def canonical_state(tmp_path):
    state = StateService(
        tmp_path / "state.db", canonical_language="en", memory_normalizer=ReviewedNormalizer()
    )
    await state.initialize()
    item = await state.create_memory(MemoryCreate(content="Garder les dates exactes."), "fixture")
    state.memory_normalizer = QueryNormalizer(english="unmatched pivot")
    return state, item


async def test_original_only_french_hit_retains_public_fields_and_score(tmp_path):
    state, item = await canonical_state(tmp_path)
    with sqlite3.connect(state.db_path) as db:
        before = list(db.iterdump())
    found = await state.search_memory(MemorySearch(query="Garder"))
    assert [r["id"] for r in found] == [item["id"]]
    assert found[0]["content"] == "Keep the exact dates."
    assert found[0]["score"] == 1.0 and found[0]["search_kind"] == "lexical"
    assert found[0]["presentation"]["content"] == "Garder les dates exactes."
    with sqlite3.connect(state.db_path) as db:
        assert list(db.iterdump()) == before


async def test_english_pivot_and_native_matches_return_one_memory(tmp_path):
    state, item = await canonical_state(tmp_path)
    state.memory_normalizer = QueryNormalizer(english="Keep dates")
    found = await state.search_memory(MemorySearch(query="Garder dates"))
    assert [r["id"] for r in found] == [item["id"]]
    assert found[0]["score"] == 1.0
    state.memory_normalizer.source_language = "en"
    found = await state.search_memory(MemorySearch(query="Keep dates"))
    assert [r["id"] for r in found] == [item["id"]]
    assert found[0]["score"] == 1.0 and "presentation" not in found[0]


@pytest.mark.parametrize(
    "content,summary",
    [
        ("Garder les dates exactes.", "Exact identifiers remain useful."),
        ("Exact identifiers remain useful.", "Garder les dates exactes."),
    ],
)
async def test_mixed_language_content_and_summary_keep_native_matching(tmp_path, content, summary):
    state = StateService(
        tmp_path / "state.db", canonical_language="en", memory_normalizer=ReviewedNormalizer()
    )
    await state.initialize()
    item = await state.create_memory(MemoryCreate(content=content, summary=summary), "fixture")
    state.memory_normalizer = QueryNormalizer(english="unmatched pivot")
    state.memory_normalizer.source_language = "en"
    found = await state.search_memory(MemorySearch(query="Garder"))
    assert [row["id"] for row in found] == [item["id"]]
    assert found[0]["score"] == 1.0
    assert found[0]["content"] == item["content"] and found[0]["summary"] == item["summary"]


async def test_old_exact_code_hit_is_not_evicted_by_500_recent_unrelated_items(tmp_path):
    state = StateService(tmp_path / "state.db")
    await state.initialize()
    item = await state.create_memory(
        MemoryCreate(content="Keep CRM_KEEP_30D in src/A.swift"), "fixture"
    )
    async with aiosqlite.connect(state.db_path) as db:
        await db.executemany(
            """INSERT INTO memory_items(id,scope,kind,content,created_at,updated_at)
            VALUES(?,'general','fact','Unrelated newer memory','2099','2099')""",
            [(f"recent_{i}",) for i in range(600)],
        )
        await db.commit()
    for query in ("CRM_KEEP_30D", "src/A.swift"):
        found = await state.search_memory(MemorySearch(query=query))
        assert [r["id"] for r in found] == [item["id"]]
        assert found[0]["score"] == 1.0


async def test_old_original_only_hit_is_not_evicted_by_500_recent_unrelated_items(tmp_path):
    state, item = await canonical_state(tmp_path)
    async with aiosqlite.connect(state.db_path) as db:
        await db.executemany(
            """INSERT INTO memory_items(id,scope,kind,content,created_at,updated_at)
            VALUES(?,'general','fact','Unrelated newer memory','2099','2099')""",
            [(f"recent_{i}",) for i in range(600)],
        )
        await db.commit()
    found = await state.search_memory(MemorySearch(query="Garder"))
    assert [row["id"] for row in found] == [item["id"]]
    assert found[0]["score"] == 1.0


@pytest.mark.parametrize(
    "change",
    [
        "content='forged Garder'",
        "scope='project:other'",
        "kind='other'",
        "sensitivity='secret'",
        "source_id='forged'",
        "source_sha256='forged'",
        "pipeline_signature='forged'",
        "language='forged'",
        "text_sha256='forged'",
    ],
)
async def test_tampered_original_never_contributes(tmp_path, change):
    state, _ = await canonical_state(tmp_path)
    async with aiosqlite.connect(state.db_path) as db:
        await db.execute("UPDATE memory_text_views SET " + change + " WHERE role='original'")
        await db.commit()
    assert await state.search_memory(MemorySearch(query="Garder")) == []


@pytest.mark.parametrize("key", ["canonical_receipt_id", "source_id"])
@pytest.mark.parametrize("value", [[], {}])
async def test_malformed_source_lookup_ids_withhold_original_and_fail_selected_pivot(
    tmp_path, key, value
):
    state, item = await canonical_state(tmp_path)
    async with aiosqlite.connect(state.db_path) as db:
        metadata = {**item["metadata"], key: value}
        await db.execute(
            "UPDATE memory_items SET metadata_json=? WHERE id=?", (json.dumps(metadata), item["id"])
        )
        await db.commit()
    assert await state.search_memory(MemorySearch(query="Garder")) == []
    state.memory_normalizer = QueryNormalizer(english="exact dates")
    with pytest.raises(MemoryNormalizationError) as caught:
        await state.search_memory(MemorySearch(query="exact dates"))
    assert caught.value.category == "unavailable"


@pytest.mark.parametrize(
    "change", ["edit", "forget", "missing_view", "tombstone", "revision", "receipt"]
)
async def test_stale_or_unqualified_original_never_contributes(tmp_path, change):
    state, item = await canonical_state(tmp_path)
    if change == "edit":
        state.memory_normalizer = ReviewedNormalizer()
        await state.update_memory(
            item["id"], MemoryUpdate(content="Ne pas envoyer automatiquement."), "fixture"
        )
        state.memory_normalizer = QueryNormalizer(english="unmatched pivot")
    elif change == "forget":
        await state.delete_memory(item["id"], "fixture")
    else:
        async with aiosqlite.connect(state.db_path) as db:
            sql = {
                "missing_view": "DELETE FROM memory_text_views WHERE role='original'",
                "tombstone": "UPDATE memory_text_heads SET deleted=1",
                "revision": "UPDATE memory_text_heads SET revision=revision+1",
                "receipt": "UPDATE memory_canonical_receipts SET status='failed'",
            }[change]
            await db.execute(sql)
            await db.commit()
    assert await state.search_memory(MemorySearch(query="Garder")) == []


async def test_more_than_one_page_invalid_matches_cannot_evict_valid_hit(tmp_path):
    state = StateService(tmp_path / "state.db")
    await state.initialize()
    valid = await state.create_memory(MemoryCreate(content="target_exact_identifier"), "fixture")
    for i in range(130):
        await state.create_memory(
            MemoryCreate(content=f"target_exact_identifier invalid {i}", pinned=True), "fixture"
        )
    async with aiosqlite.connect(state.db_path) as db:
        await db.execute(
            "UPDATE memory_text_views SET pipeline_signature='forged' WHERE memory_id<>?",
            (valid["id"],),
        )
        await db.commit()
    found = await state.search_memory(MemorySearch(query="target_exact_identifier"))
    assert [r["id"] for r in found] == [valid["id"]]


async def test_original_scope_kind_and_sensitivity_filters_precede_selection(tmp_path):
    state = StateService(
        tmp_path / "state.db", canonical_language="en", memory_normalizer=ReviewedNormalizer()
    )
    await state.initialize()
    wanted = await state.create_memory(
        MemoryCreate(content="Garder les dates exactes.", scope="project:a"), "fixture"
    )
    for scope, kind, sensitivity in [
        ("project:b", "fact", "normal"),
        ("project:a", "other", "normal"),
        ("project:a", "fact", "secret"),
    ]:
        await state.create_memory(
            MemoryCreate(
                content="Garder les dates exactes.",
                scope=scope,
                kind=kind,
                sensitivity=sensitivity,
                pinned=True,
            ),
            "fixture",
        )
    state.memory_normalizer = QueryNormalizer(english="unmatched pivot")
    found = await state.search_memory(
        MemorySearch(query="Garder", scope="project:a", kind="fact"),
        allowed_scopes=("project:a",),
        required_sensitivity="normal",
    )
    assert [r["id"] for r in found] == [wanted["id"]]


async def test_embedding_failure_keeps_original_lexical_hit(tmp_path):
    state, item = await canonical_state(tmp_path)
    provider = LocalProvider()
    provider.fail = True
    state.embedding_service = provider
    found = await state.search_memory(MemorySearch(query="Garder"))
    assert [r["id"] for r in found] == [item["id"]]
    assert found[0]["search_kind"] == "lexical" and found[0]["score"] == 1.0
    assert provider.calls == [["Garder", "unmatched pivot"]]


@pytest.mark.parametrize("change", ["identity", "signature"])
async def test_presenter_remains_pinned_during_final_view_recheck(tmp_path, monkeypatch, change):
    state = StateService(
        tmp_path / "state.db", canonical_language="en", memory_normalizer=ReviewedNormalizer()
    )
    await state.initialize()
    await state.create_memory(MemoryCreate(content="Customer retention."), "fixture")
    state.memory_normalizer = QueryNormalizer(english="customer")
    presenter = state.memory_presenter = FrenchPresenter()
    qualify = state_module.qualify_search_rows

    async def change_after_read(db, rows):
        result = await qualify(db, rows)
        if presenter.calls:
            if change == "identity":
                state.memory_presenter = FrenchPresenter()
            else:
                presenter.presentation_signature = "f" * 64
        return result

    monkeypatch.setattr(state_module, "qualify_search_rows", change_after_read)
    with pytest.raises(MemoryNormalizationError) as caught:
        await state.search_memory(MemorySearch(query="clients"))
    assert caught.value.category == "source_conflict"
    assert len(presenter.calls) == 1


def test_public_search_route_finds_original_and_preserves_english_contract(client, paired_headers):
    state = client.app.state.state_service
    state.canonical_language = "en"
    state.memory_normalizer = ReviewedNormalizer()
    created = client.post(
        "/memory", headers=paired_headers, json={"content": "Garder les dates exactes."}
    )
    assert created.status_code == 201
    state.memory_normalizer = QueryNormalizer(english="unmatched pivot")
    response = client.post("/memory/search", headers=paired_headers, json={"query": "Garder"})
    assert response.status_code == 200
    assert [r["id"] for r in response.json()] == [created.json()["id"]]
    assert response.json()[0]["content"] == "Keep the exact dates."
    assert response.json()[0]["presentation"]["mode"] == "original"
