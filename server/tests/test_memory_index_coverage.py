"""Independent real-SQLite coverage diagnostics, without inference or cache repair."""

from __future__ import annotations

import sqlite3
from contextlib import asynccontextmanager

import aiosqlite
import pytest
from aiosqlite.context import contextmanager as sqlite_contextmanager
from pydantic import ValidationError

from app.models import MemoryCreate
from app.services.memory_index_coverage import (
    MemoryIndexCoverageError,
    MemoryIndexCoverageRequest,
)
from app.services.memory_vectors import embedding_identity
from app.services.state_service import StateService
from tests.test_memory_canonical_store import ReviewedNormalizer

STATUSES = {
    "current",
    "missing_vector",
    "embedding_configuration_mismatch",
    "stale_binding",
    "invalid_vector",
    "configuration_missing",
}


class NeverCalledProvider:
    provider_name = "coverage-only-fixture"
    base_url = "https://private-provider.invalid/secret-location"
    model = "fixture-model"
    dimensions = 2

    async def embed(self, texts, **kwargs):
        raise AssertionError("coverage must never call the embedding provider")


async def prepared(tmp_path, *, canonical=True):
    normalizer = ReviewedNormalizer() if canonical else None
    state = StateService(
        tmp_path / "state.db",
        canonical_language="en" if canonical else "legacy",
        memory_normalizer=normalizer,
    )
    await state.initialize()
    item = await state.create_memory(
        MemoryCreate(content="Garder les dates exactes.", scope="project:wanted"), "fixture"
    )
    state.embedding_service = NeverCalledProvider()
    return state, item


def cache_views(state, memory_id, *, role=None, provider=None):
    """Seed rebuildable artifacts from the actual public-service-created views."""
    identity = provider or embedding_identity(
        state.embedding_service, state.embedding_model_revision
    )
    with sqlite3.connect(state.db_path) as db:
        db.execute(
            """INSERT INTO memory_view_embeddings(
                memory_id,revision,view_id,provider,source_id,source_sha256,
                view_sha256,pipeline_signature,item_revision,dimensions,vector_json,updated_at)
            SELECT v.memory_id,v.revision,v.id,?,v.source_id,v.source_sha256,
                v.text_sha256,v.pipeline_signature,h.item_revision,2,'[1,0]',h.updated_at
            FROM memory_text_views v JOIN memory_text_heads h
                ON h.memory_id=v.memory_id AND h.revision=v.revision
            WHERE v.memory_id=? AND (? IS NULL OR v.role=?)""",
            (identity, memory_id, role, role),
        )


async def coverage(state, **kwargs):
    return await state.memory_index_coverage(
        MemoryIndexCoverageRequest(scope="project:wanted", **kwargs)
    )


def assert_view_counts(report, **expected):
    assert report["views_by_status"] == {key: expected.get(key, 0) for key in STATUSES}
    assert sum(report["views_by_status"].values()) == report["expected_views"]
    assert report["covered_views"] == expected.get("current", 0)


def database_dump(state):
    with sqlite3.connect(state.db_path) as db:
        return list(db.iterdump())


async def test_cold_schema31_does_not_infer_coverage_from_completed_outbox_or_old_cache(tmp_path):
    state, item = await prepared(tmp_path)
    with sqlite3.connect(state.db_path) as db:
        db.execute("UPDATE memory_index_outbox SET status='completed'")
        db.execute(
            "INSERT INTO memory_embeddings VALUES(?,?,?,?,?)",
            (
                item["id"],
                embedding_identity(state.embedding_service),
                2,
                "[1,0]",
                item["updated_at"],
            ),
        )
    before = database_dump(state)
    report = await coverage(state)
    assert report["page_status"] == "incomplete"
    assert report["qualified_memories"] == report["scanned_memories"] == 1
    assert_view_counts(report, missing_vector=2)
    assert database_dump(state) == before


@pytest.mark.parametrize("canonical, expected_views", [(False, 1), (True, 2)])
async def test_covered_current_views_do_not_invoke_any_model_or_write(
    tmp_path, canonical, expected_views
):
    state, item = await prepared(tmp_path, canonical=canonical)
    cache_views(state, item["id"])
    before = database_dump(state)
    prior_calls = list(state.memory_normalizer.calls) if canonical else None

    @asynccontextmanager
    async def forbidden_admission():
        raise AssertionError("coverage must not reserve model admission")
        yield

    state.embedding_admission = forbidden_admission
    report = await coverage(state)
    assert report["page_status"] == "covered"
    assert report["consistency"] == "page_snapshot" and report["counts_scope"] == "page"
    assert report["provider_readiness"] == "configured_not_probed"
    assert report["provider_fingerprint"] == embedding_identity(state.embedding_service)
    assert report["dimensions"] == 2
    assert report["dimension_check"] == "provider_declared"
    assert report["unqualified_memories"] == 0
    assert report["has_more"] is False and report["next_after_id"] is None
    assert report["covers_entire_selection"] is True
    assert_view_counts(report, current=expected_views)
    assert database_dump(state) == before
    if canonical:
        assert state.memory_normalizer.calls == prior_calls
        for key in ("source_id", "canonical_receipt_id"):
            assert item["metadata"][key] not in str(report)


@pytest.mark.parametrize("artifact", ["foreign_current", "old_revision", "valid_with_noise"])
async def test_historical_and_other_provider_rows_never_supply_current_coverage(tmp_path, artifact):
    state, item = await prepared(tmp_path)
    cache_views(state, item["id"], provider="memory-v3:other-provider")
    if artifact == "old_revision":
        with sqlite3.connect(state.db_path) as db:
            db.execute("UPDATE memory_view_embeddings SET revision=revision+100")
    elif artifact == "valid_with_noise":
        cache_views(state, item["id"])
        with sqlite3.connect(state.db_path) as db:
            db.execute(
                "UPDATE memory_view_embeddings SET revision=revision+100, vector_json='bad' "
                "WHERE provider='memory-v3:other-provider'"
            )
    report = await coverage(state)
    status = {
        "foreign_current": "embedding_configuration_mismatch",
        "old_revision": "missing_vector",
        "valid_with_noise": "current",
    }[artifact]
    assert_view_counts(report, **{status: 2})


@pytest.mark.parametrize(
    "field,value,status",
    [
        ("source_id", "forged", "stale_binding"),
        ("source_sha256", "0" * 64, "stale_binding"),
        ("view_sha256", "0" * 64, "stale_binding"),
        ("pipeline_signature", "forged", "stale_binding"),
        ("item_revision", "stale", "stale_binding"),
        ("dimensions", 3, "embedding_configuration_mismatch"),
        ("vector_json", "[1]", "invalid_vector"),
        ("vector_json", "bad", "invalid_vector"),
        ("vector_json", "[true,0]", "invalid_vector"),
        ("vector_json", "[NaN,0]", "invalid_vector"),
        ("vector_json", "[Infinity,0]", "invalid_vector"),
        ("vector_json", "[0,0]", "invalid_vector"),
        pytest.param(
            "vector_json",
            "[" * 10_000 + "0" + "]" * 10_000,
            "invalid_vector",
            id="deeply_nested_vector_json",
        ),
    ],
)
async def test_exact_row_damage_is_reported_and_not_rescued_by_foreign_vectors(
    tmp_path, field, value, status
):
    state, item = await prepared(tmp_path)
    cache_views(state, item["id"])
    cache_views(state, item["id"], provider="memory-v3:other-provider")
    with sqlite3.connect(state.db_path) as db:
        db.execute(
            f"UPDATE memory_view_embeddings SET {field}=? WHERE provider=?",
            (value, embedding_identity(state.embedding_service)),
        )
    report = await coverage(state)
    assert report["page_status"] == "incomplete"
    assert_view_counts(report, **{status: 2})


@pytest.mark.parametrize("damage", ["receipt", "source_hash", "head_revision", "missing_head"])
async def test_unqualified_sources_remain_visible_in_counts_without_usable_views(tmp_path, damage):
    state, item = await prepared(tmp_path)
    cache_views(state, item["id"])
    with sqlite3.connect(state.db_path) as db:
        if damage == "receipt":
            db.execute("UPDATE memory_canonical_receipts SET status='failed'")
        elif damage == "source_hash":
            db.execute("UPDATE memory_source_journal SET source_sha256=?", ("0" * 64,))
        elif damage == "head_revision":
            db.execute("UPDATE memory_text_heads SET item_revision='stale'")
        else:
            db.execute("DELETE FROM memory_text_heads")
    report = await coverage(state)
    assert report["page_status"] == "unqualified"
    assert report["scanned_memories"] == report["unqualified_memories"] == 1
    assert report["qualified_memories"] == 0
    assert report["items"][0]["status"] == "unqualified_source"
    assert report["items"][0]["views"] == []
    assert_view_counts(report)


async def test_missing_configuration_and_empty_selection_are_explicit(tmp_path):
    state, _ = await prepared(tmp_path)
    state.embedding_service = None
    report = await coverage(state)
    assert report["page_status"] == "configuration_missing"
    assert report["provider_fingerprint"] is None and report["dimensions"] is None
    assert report["provider_readiness"] == "not_configured"
    assert report["dimension_check"] == "not_configured"
    assert_view_counts(report, configuration_missing=2)
    empty = await coverage(state, kind="absent")
    assert empty["page_status"] == "empty" and empty["scanned_memories"] == 0
    assert empty["items"] == [] and empty["covers_entire_selection"] is True


async def test_unqualified_memory_cannot_disappear_from_mixed_page_denominator(tmp_path):
    state, _ = await prepared(tmp_path, canonical=False)
    state.embedding_service = None
    with sqlite3.connect(state.db_path) as db:
        db.execute(
            """INSERT INTO memory_items(id,scope,kind,content,created_at,updated_at)
            VALUES('missing-head','project:wanted','fact','Legacy fixture','2026','2026')"""
        )
    report = await coverage(state)
    assert report["page_status"] == "unqualified"
    assert report["scanned_memories"] == 2
    assert report["qualified_memories"] == report["unqualified_memories"] == 1
    assert_view_counts(report, configuration_missing=1)


async def test_scope_kind_sensitivity_filter_before_bounded_keyset_pagination(tmp_path):
    state, first = await prepared(tmp_path, canonical=False)
    state.embedding_service = None
    expected = {first["id"]}
    for index in range(100):
        item = await state.create_memory(
            MemoryCreate(content=f"Wanted {index}", scope="project:wanted"), "fixture"
        )
        expected.add(item["id"])
    for kwargs in (
        {"scope": "other"},
        {"scope": "project:wanted", "kind": "preference"},
        {"scope": "project:wanted", "sensitivity": "private"},
    ):
        await state.create_memory(MemoryCreate(content="Excluded", **kwargs), "fixture")
    first_page = await coverage(state, kind="fact", limit=100)
    assert first_page["scanned_memories"] == 100 and first_page["has_more"] is True
    assert first_page["covers_entire_selection"] is False
    ids = [item["memory_id"] for item in first_page["items"]]
    assert ids == sorted(expected)[:100]
    assert first_page["next_after_id"] == ids[-1]
    last = await coverage(state, kind="fact", limit=100, after_id=first_page["next_after_id"])
    assert last["scanned_memories"] == 1 and last["has_more"] is False
    assert last["next_after_id"] is None and last["covers_entire_selection"] is False
    assert set(ids + [item["memory_id"] for item in last["items"]]) == expected
    private = await coverage(state, sensitivity="private")
    assert private["scanned_memories"] == 1


@pytest.mark.parametrize("change", ["instance", "revision", "dimensions"])
async def test_configuration_change_during_awaited_read_cannot_return_mixed_coverage(
    tmp_path, monkeypatch, change
):
    state, item = await prepared(tmp_path)
    cache_views(state, item["id"])
    original_execute = aiosqlite.Connection.execute
    changed = False

    @sqlite_contextmanager
    async def change_during_read(db, sql, parameters=None):
        nonlocal changed
        cursor = await original_execute(db, sql, parameters)
        if not changed and "memory_items" in sql and sql.lstrip().upper().startswith("SELECT"):
            changed = True
            if change == "instance":
                state.embedding_service = NeverCalledProvider()
            elif change == "revision":
                state.embedding_model_revision = "changed"
            else:
                state.embedding_service.dimensions = 3
        return cursor

    monkeypatch.setattr(aiosqlite.Connection, "execute", change_during_read)
    with pytest.raises(MemoryIndexCoverageError) as caught:
        await coverage(state)
    assert changed
    assert caught.value.code == "memory_index_configuration_changed"
    assert caught.value.status_code == 409


@pytest.mark.parametrize("dimensions", [True, 0, -1, 8193, "2", 2.5])
async def test_invalid_declared_provider_dimensions_are_unavailable(tmp_path, dimensions):
    state, _ = await prepared(tmp_path)
    state.embedding_service.dimensions = dimensions
    with pytest.raises(MemoryIndexCoverageError) as caught:
        await coverage(state)
    assert caught.value.status_code == 503


async def test_database_failure_is_explicit_not_successful_empty_coverage(tmp_path):
    state, _ = await prepared(tmp_path)
    with sqlite3.connect(state.db_path) as db:
        db.execute("ALTER TABLE memory_text_views RENAME TO unavailable_views")
    with pytest.raises(MemoryIndexCoverageError) as caught:
        await coverage(state)
    assert caught.value.code == "memory_index_unavailable"
    assert caught.value.status_code == 503


async def test_deeply_nested_metadata_is_sanitized_unavailable_not_empty_coverage(tmp_path):
    state, item = await prepared(tmp_path)
    cache_views(state, item["id"])
    with sqlite3.connect(state.db_path) as db:
        db.execute(
            "UPDATE memory_items SET metadata_json=? WHERE id=?",
            ("[" * 10_000 + "0" + "]" * 10_000, item["id"]),
        )
    before = database_dump(state)
    with pytest.raises(MemoryIndexCoverageError) as caught:
        await coverage(state)
    assert caught.value.code == "memory_index_unavailable"
    assert caught.value.status_code == 503
    assert str(caught.value) == "memory_index_unavailable"
    assert database_dump(state) == before


async def test_missing_database_is_not_created_by_diagnostics(tmp_path):
    state, _ = await prepared(tmp_path)
    state.db_path = tmp_path / "does-not-exist.db"
    with pytest.raises(MemoryIndexCoverageError) as caught:
        await coverage(state)
    assert caught.value.code == "memory_index_unavailable"
    assert not state.db_path.exists()


async def test_read_connection_enforces_readonly_and_explicit_transaction(tmp_path, monkeypatch):
    state, item = await prepared(tmp_path)
    cache_views(state, item["id"])
    before = database_dump(state)
    original_execute = aiosqlite.Connection.execute
    checked = False

    @sqlite_contextmanager
    async def probe_readonly(db, sql, parameters=None):
        nonlocal checked
        cursor = await original_execute(db, sql, parameters)
        if not checked and "memory_items" in sql and sql.lstrip().upper().startswith("SELECT"):
            checked = True
            assert db.in_transaction
            with pytest.raises(aiosqlite.OperationalError, match="readonly"):
                await original_execute(db, "CREATE TABLE forbidden_coverage_write(id INTEGER)")
        return cursor

    monkeypatch.setattr(aiosqlite.Connection, "execute", probe_readonly)
    assert (await coverage(state))["page_status"] == "covered"
    assert checked and database_dump(state) == before


async def test_page_uses_one_snapshot_even_if_writer_changes_head_between_reads(
    tmp_path, monkeypatch
):
    state, item = await prepared(tmp_path)
    cache_views(state, item["id"])
    original_execute = aiosqlite.Connection.execute
    changed = False

    @sqlite_contextmanager
    async def write_between_reads(db, sql, parameters=None):
        nonlocal changed
        cursor = await original_execute(db, sql, parameters)
        if not changed and "memory_items" in sql and sql.lstrip().upper().startswith("SELECT"):
            changed = True
            with sqlite3.connect(state.db_path) as writer:
                writer.execute("UPDATE memory_text_heads SET item_revision='changed-by-writer'")
        return cursor

    monkeypatch.setattr(aiosqlite.Connection, "execute", write_between_reads)
    first = await coverage(state)
    assert changed and first["page_status"] == "covered"
    assert first["consistency"] == "page_snapshot"
    # A later call starts another snapshot; the first response was not global readiness.
    second = await coverage(state)
    assert second["page_status"] == "unqualified"


async def test_unknown_provider_width_still_rejects_stored_vector_count_mismatch(tmp_path):
    state, item = await prepared(tmp_path)
    state.embedding_service.dimensions = None
    cache_views(state, item["id"])
    initial = await coverage(state)
    assert initial["page_status"] == "covered"
    assert initial["dimension_check"] == "stored_vector_only"
    assert initial["provider_readiness"] == "configured_not_probed"
    with sqlite3.connect(state.db_path) as db:
        db.execute("UPDATE memory_view_embeddings SET dimensions=3")
    report = await coverage(state)
    assert report["dimensions"] is None
    assert report["provider_readiness"] == "configured_not_probed"
    assert_view_counts(report, invalid_vector=2)


@pytest.mark.parametrize(
    "fields",
    [
        {},
        {"scope": ""},
        {"scope": "  "},
        {"scope": "x" * 101},
        {"scope": "ok", "kind": " "},
        {"scope": "ok", "sensitivity": ""},
        {"scope": "ok", "sensitivity": "x" * 51},
        {"scope": "ok", "after_id": "x" * 201},
        {"scope": "ok", "limit": 0},
        {"scope": "ok", "limit": 101},
    ],
)
def test_request_rejects_unbounded_or_ambiguous_selections(fields):
    with pytest.raises(ValidationError):
        MemoryIndexCoverageRequest(**fields)
