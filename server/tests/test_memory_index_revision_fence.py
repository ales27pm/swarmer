"""An explicit dual-view index request cannot adopt a later memory revision."""

from __future__ import annotations

import asyncio

import aiosqlite

from app.models import MemoryCreate, MemoryUpdate
from app.services import state_service as state_module
from app.services.memory_vectors import embedding_identity
from app.services.state_service import StateService
from tests.test_memory_canonical_store import ReviewedNormalizer


class ViewProvider:
    provider_name = "revision-fence-fixture"
    dimensions = 2

    def __init__(self, *, block_first=False):
        self.calls = []
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.block_first = block_first

    async def embed(self, texts):
        self.calls.append(list(texts))
        if self.block_first and len(self.calls) == 1:
            self.entered.set()
            await self.release.wait()
        return [[1.0, 0.0] for _ in texts]


async def fixture(tmp_path, *, block_first=False):
    state = StateService(
        tmp_path / "state.db",
        canonical_language="en",
        memory_normalizer=ReviewedNormalizer(),
    )
    await state.initialize()
    item = await state.create_memory(MemoryCreate(content="Garder les dates exactes."), "fixture")
    writer = StateService(
        state.db_path, canonical_language="en", memory_normalizer=ReviewedNormalizer()
    )
    provider = ViewProvider(block_first=block_first)
    state.embedding_service = provider
    return state, writer, item, provider


async def revision_state(state, memory_id):
    async with aiosqlite.connect(state.db_path) as db:
        db.row_factory = aiosqlite.Row
        head = await (
            await db.execute("SELECT * FROM memory_text_heads WHERE memory_id=?", (memory_id,))
        ).fetchone()
        intents = await (
            await db.execute(
                """SELECT * FROM memory_index_outbox WHERE memory_id=? AND revision=?
                ORDER BY id""",
                (memory_id, head["revision"]),
            )
        ).fetchall()
        vectors = await (
            await db.execute("SELECT * FROM memory_view_embeddings WHERE memory_id=?", (memory_id,))
        ).fetchall()
        legacy = await (
            await db.execute("SELECT * FROM memory_embeddings WHERE memory_id=?", (memory_id,))
        ).fetchall()
    return dict(head), [dict(row) for row in intents], [dict(row) for row in vectors], legacy


def assert_fresh_revision_unclaimed(snapshot):
    head, intents, vectors, legacy = snapshot
    assert head["revision"] == 2 and len(intents) == 2
    assert all(row["status"] == "pending" and row["claim_count"] == 0 for row in intents)
    assert all(row["owner"] is None and row["request_state"] == "not_sent" for row in intents)
    assert vectors == legacy == []


async def assert_explicit_request_indexes_new_revision(state, item, provider):
    assert await state.index_memory(item["id"])
    head, intents, vectors, legacy = await revision_state(state, item["id"])
    assert head["revision"] == 2 and len(vectors) == 2 and len(legacy) == 1
    assert all(row["revision"] == 2 for row in vectors)
    assert all(row["status"] == "completed" and row["claim_count"] == 1 for row in intents)
    assert provider.calls[-2:] == [
        ["Do not send automatically. "],
        ["Ne pas envoyer automatiquement. "],
    ]


async def test_update_during_first_dual_index_request_does_not_claim_new_revision(tmp_path):
    state, writer, item, provider = await fixture(tmp_path, block_first=True)
    pending = asyncio.create_task(state.index_memory(item["id"]))
    try:
        await asyncio.wait_for(provider.entered.wait(), timeout=2)
        await writer.update_memory(
            item["id"], MemoryUpdate(content="Ne pas envoyer automatiquement."), "fixture"
        )
    finally:
        provider.release.set()
        result = await asyncio.wait_for(asyncio.shield(pending), timeout=5)
    assert result is False
    assert provider.calls == [["Keep the exact dates. "]]
    assert_fresh_revision_unclaimed(await revision_state(state, item["id"]))
    await assert_explicit_request_indexes_new_revision(state, item, provider)
    assert len(provider.calls) == 3


async def test_update_after_canonical_store_does_not_advance_native_claim(tmp_path, monkeypatch):
    state, writer, item, provider = await fixture(tmp_path)
    finish = state.memory_projection_worker._finish
    changed = False

    async def update_after_store(*args, **kwargs):
        nonlocal changed
        stored = await finish(*args, **kwargs)
        if stored and not changed:
            changed = True
            _, _, vectors, legacy = await revision_state(state, item["id"])
            assert len(vectors) == len(legacy) == 1
            await writer.update_memory(
                item["id"], MemoryUpdate(content="Ne pas envoyer automatiquement."), "fixture"
            )
        return stored

    monkeypatch.setattr(state.memory_projection_worker, "_finish", update_after_store)
    await state.index_memory(item["id"])
    assert changed and provider.calls == [["Keep the exact dates. "]]
    assert_fresh_revision_unclaimed(await revision_state(state, item["id"]))
    await assert_explicit_request_indexes_new_revision(state, item, provider)
    assert len(provider.calls) == 3


async def test_old_index_retry_cannot_wake_new_revision_failure_backoff(tmp_path, monkeypatch):
    state, writer, item, provider = await fixture(tmp_path)
    retry = state_module.retry_known_projection_failure
    before_retry = None

    async def change_before_retry(*args, **kwargs):
        nonlocal before_retry
        if before_retry is None:
            await writer.update_memory(
                item["id"], MemoryUpdate(content="Ne pas envoyer automatiquement."), "fixture"
            )
            async with aiosqlite.connect(state.db_path) as db:
                await db.execute(
                    """UPDATE memory_index_outbox SET provider=?,
                    error_category='provider_unavailable',available_at='2099-01-01T00:00:00+00:00'
                    WHERE memory_id=? AND revision=2""",
                    (embedding_identity(provider), item["id"]),
                )
                await db.commit()
            before_retry = await revision_state(state, item["id"])
        return await retry(*args, **kwargs)

    monkeypatch.setattr(state_module, "retry_known_projection_failure", change_before_retry)
    assert await state.index_memory(item["id"]) is False
    assert before_retry is not None
    after_retry = await revision_state(state, item["id"])
    assert_fresh_revision_unclaimed(after_retry)
    assert after_retry == before_retry
    assert provider.calls == []
    await assert_explicit_request_indexes_new_revision(state, item, provider)
    assert len(provider.calls) == 2
