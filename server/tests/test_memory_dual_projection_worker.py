"""Actual schema31 writes, view-specific projection and durable uncertainty."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import aiosqlite
import pytest

from app.models import MemoryCreate, MemoryUpdate
from app.services import state_service as state_module
from app.services.memory_projection_worker import MemoryProjectionWorker
from app.services.memory_text_views import (
    claim_projection_batch,
    complete_current_projection_locked,
    finish_projection_locked,
    mark_projection_batch_dispatched,
    mark_projection_batch_response_received,
    mark_projection_dispatched,
    mark_projection_response_received,
    read_projection_source,
    reserve_projection_batch,
)
from app.services.memory_vectors import embedding_identity
from app.services.state_service import StateService
from tests.test_memory_canonical_store import ReviewedNormalizer


class ViewProvider:
    provider_name = "qualified-dual-fixture"
    dimensions = 2

    def __init__(self):
        self.calls = []

    async def embed(self, texts, **kwargs):
        self.calls.append(texts)
        return [[1.0, 0.0] if "Garder" in text else [0.0, 1.0] for text in texts]


async def fixture(tmp_path):
    state = StateService(
        tmp_path / "state.db", canonical_language="en", memory_normalizer=ReviewedNormalizer()
    )
    await state.initialize()
    item = await state.create_memory(MemoryCreate(content="Garder les dates exactes."), "fixture")
    provider = ViewProvider()
    worker = MemoryProjectionWorker(state.db_path, lambda: provider, lambda: None)
    return state, item, provider, worker


async def rows(state, table):
    assert table in {
        "memory_view_embeddings",
        "memory_embeddings",
        "memory_index_outbox",
        "memory_text_views",
        "memory_text_heads",
    }
    async with aiosqlite.connect(state.db_path) as db:
        db.row_factory = aiosqlite.Row
        return [
            dict(row)
            for row in await (await db.execute(f"SELECT * FROM {table} ORDER BY rowid")).fetchall()
        ]


async def snapshots(state, memory_id):
    async with aiosqlite.connect(state.db_path) as db:
        db.row_factory = aiosqlite.Row
        return [
            dict(row)
            for row in await (
                await db.execute(
                    """SELECT m.id,m.updated_at,v.content,v.summary,
            h.revision AS projection_revision,v.id AS projection_view_id,
            h.source_sha256 AS projection_source_sha256 FROM memory_items m
            JOIN memory_text_heads h ON h.memory_id=m.id
            JOIN memory_text_views v ON v.memory_id=m.id AND v.revision=h.revision
            WHERE m.id=? ORDER BY v.role='canonical'""",
                    (memory_id,),
                )
            ).fetchall()
        ]


async def test_dual_projection_has_exact_view_vectors_and_only_pivot_compatibility_cache(tmp_path):
    state, item, provider, worker = await fixture(tmp_path)
    assert len(await rows(state, "memory_index_outbox")) == 2
    assert (await worker.drain(limit=2)).projected == 2
    views = {row["id"]: row for row in await rows(state, "memory_text_views")}
    vectors = await rows(state, "memory_view_embeddings")
    assert len(vectors) == 2
    for vector in vectors:
        view = views[vector["view_id"]]
        assert vector["vector_json"] == ("[1.0,0.0]" if view["role"] == "original" else "[0.0,1.0]")
        assert vector["source_sha256"] == view["source_sha256"]
        assert vector["view_sha256"] == view["text_sha256"]
        assert vector["pipeline_signature"] == view["pipeline_signature"]
        assert vector["source_id"] == view["source_id"]
        assert vector["item_revision"] == item["updated_at"]
    assert (await rows(state, "memory_embeddings"))[0]["vector_json"] == "[0.0,1.0]"
    assert provider.calls == [["Keep the exact dates. "], ["Garder les dates exactes. "]]
    restarted = StateService(state.db_path, provider)
    await restarted.initialize()
    assert (await restarted.drain_memory_projections(limit=2)).claimed == 0
    assert len(provider.calls) == 2


async def test_pin_refreshes_both_view_revisions_without_embedding(tmp_path):
    state, item, provider, worker = await fixture(tmp_path)
    await worker.drain(limit=2)
    previous = await rows(state, "memory_view_embeddings")
    pinned = await state.update_memory(item["id"], MemoryUpdate(pinned=True), "fixture")
    current = await rows(state, "memory_view_embeddings")
    assert len(current) == 2
    assert all(row["item_revision"] == pinned["updated_at"] for row in current)
    assert [{k: v for k, v in row.items() if k != "item_revision"} for row in current] == [
        {k: v for k, v in row.items() if k != "item_revision"} for row in previous
    ]
    assert len(provider.calls) == 2


@pytest.mark.parametrize("change", ["update", "delete"])
async def test_edit_and_forget_purge_every_view_and_provider_atomically(
    tmp_path, change, monkeypatch
):
    state, item, _, worker = await fixture(tmp_path)
    await worker.drain(limit=2)
    async with aiosqlite.connect(state.db_path) as db:
        await db.execute("""INSERT INTO memory_view_embeddings SELECT memory_id,revision,view_id,'other-provider',
            source_id,source_sha256,view_sha256,pipeline_signature,item_revision,dimensions,vector_json,updated_at
            FROM memory_view_embeddings""")
        await db.commit()
    before = await rows(state, "memory_view_embeddings")
    assert len(before) == 4
    if change == "delete":

        async def fault(*args, **kwargs):
            raise RuntimeError("injected rollback after view deletion")

        with monkeypatch.context() as patch:
            patch.setattr(state_module, "append_audit_event", fault)
            with pytest.raises(RuntimeError):
                await state.delete_memory(item["id"], "fixture")
        assert await rows(state, "memory_view_embeddings") == before
        await state.delete_memory(item["id"], "fixture")
    else:
        await state.update_memory(
            item["id"], MemoryUpdate(content="Ne pas envoyer automatiquement."), "fixture"
        )
    assert await rows(state, "memory_view_embeddings") == []
    assert await rows(state, "memory_embeddings") == []


@pytest.mark.parametrize("change", ["update", "delete"])
async def test_obsolete_native_inflight_fences_all_upserts_after_restart_and_expiry(
    tmp_path, change
):
    state, item, provider, worker = await fixture(tmp_path)
    assert (await worker.drain(limit=1)).projected == 1
    identity = embedding_identity(provider)
    (native,) = await claim_projection_batch(
        state.db_path, "native-owner", memory_id=item["id"], provider=identity, limit=1
    )
    assert (await read_projection_source(state.db_path, native)).role == "original"
    assert await mark_projection_dispatched(state.db_path, native)
    await state.create_memory(MemoryCreate(content="Unrelated source."), "fixture")
    if change == "delete":
        await state.delete_memory(item["id"], "fixture")
        assert (await worker.drain(memory_id=item["id"])).deleted == 1
    else:
        await state.update_memory(
            item["id"], MemoryUpdate(content="Ne pas envoyer automatiquement."), "fixture"
        )
    restarted = StateService(state.db_path, provider)
    await restarted.initialize()
    future = (datetime.now(UTC) + timedelta(minutes=5)).isoformat()
    assert (
        await claim_projection_batch(state.db_path, "other-owner", provider=identity, now=future)
        == []
    )
    assert (await restarted.drain_memory_projections(limit=16)).claimed == 0
    old = next(
        row for row in await rows(state, "memory_index_outbox") if row["id"] == native.event_id
    )
    assert old["status"] == "obsolete" and old["request_state"] == "in_flight"
    assert old["owner"] == native.owner and old["generation"] == native.generation
    assert not await mark_projection_response_received(
        state.db_path, replace(native, generation=native.generation + 1), now=future
    )
    assert await mark_projection_response_received(state.db_path, native, now=future)
    async with aiosqlite.connect(state.db_path) as db:
        await db.execute("BEGIN IMMEDIATE")
        assert not await finish_projection_locked(db, native, vector=[1.0, 0.0], now=future)
        await db.commit()
    assert len(provider.calls) == 1
    assert await rows(state, "memory_view_embeddings") == []


async def test_explicit_backfill_reserves_both_views_without_cross_acknowledgement(tmp_path):
    state, item, provider, _ = await fixture(tmp_path)
    pending = await snapshots(state, item["id"])
    claims = await reserve_projection_batch(
        state.db_path, "dual-backfill", snapshots=pending, provider=embedding_identity(provider)
    )
    assert len(claims) == 2 and len({claim.view_id for claim in claims}) == 2
    assert await mark_projection_batch_dispatched(state.db_path, claims)
    assert (
        await claim_projection_batch(
            state.db_path, "competing", provider=embedding_identity(provider)
        )
        == []
    )
    assert await mark_projection_batch_response_received(state.db_path, claims)
    for i, claim in enumerate(claims):
        async with aiosqlite.connect(state.db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            assert await finish_projection_locked(
                db, claim, vector=[1.0, 0.0] if i == 0 else [0.0, 1.0]
            )
            await db.commit()
        completed = [
            row for row in await rows(state, "memory_index_outbox") if row["status"] == "completed"
        ]
        assert len(completed) == i + 1
    assert len(await rows(state, "memory_view_embeddings")) == 2
    assert (await rows(state, "memory_embeddings"))[0]["vector_json"] == "[0.0,1.0]"


async def test_canonical_cache_cannot_acknowledge_native_backfill(tmp_path):
    state, item, provider, worker = await fixture(tmp_path)
    await worker.drain(limit=1)
    native = (await snapshots(state, item["id"]))[0]
    async with aiosqlite.connect(state.db_path) as db:
        await db.execute("BEGIN IMMEDIATE")
        assert not await complete_current_projection_locked(
            db,
            memory_id=item["id"],
            item_revision=item["updated_at"],
            provider=embedding_identity(provider),
            revision=native["projection_revision"],
            view_id=native["projection_view_id"],
            source_sha256=native["projection_source_sha256"],
        )
        await db.commit()


@pytest.mark.parametrize(
    "change",
    ["scope", "kind", "sensitivity", "pipeline_signature", "language", "source_id", "text_sha256"],
)
async def test_tampered_original_is_never_projected_through_either_view(tmp_path, change):
    state, _, provider, worker = await fixture(tmp_path)
    async with aiosqlite.connect(state.db_path) as db:
        await db.execute(f"UPDATE memory_text_views SET {change}='tampered' WHERE role='original'")
        await db.commit()
    report = await worker.drain(limit=2)
    assert report.projected == 0 and report.stale == 2
    assert provider.calls == []
    assert await rows(state, "memory_view_embeddings") == []
