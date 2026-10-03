"""Explicit backfill counts memories while durably projecting both text views."""

from __future__ import annotations

import asyncio
import json

import aiosqlite
import pytest

from app.models import MemoryCreate, MemoryUpdate
from app.services.embedding_service import EmbeddingServiceError
from app.services.memory_normalization import canonical_text_sha256
from app.services.memory_vectors import embedding_identity
from app.services.state_service import StateService
from tests.test_memory_canonical_store import ReviewedNormalizer
from tests.test_memory_indexing_regressions import LocalProvider


class DualNormalizer(ReviewedNormalizer):
    async def normalize(self, source, *, recheck_source=None):
        result = await super().normalize(source, recheck_source=recheck_source)
        text = "Canonical: " + source.text
        return result.model_copy(
            update={
                "canonical_text": text,
                "canonical_sha256": canonical_text_sha256(text),
                "source_language": "fr",
            }
        )


class RecordingProvider(LocalProvider):
    def __init__(self, state, *, fail_on=None, known=True):
        super().__init__()
        self.state = state
        self.fail_on = fail_on
        self.known = known

    async def embed(self, texts):
        assert 1 <= len(texts) <= 100
        async with aiosqlite.connect(self.state.db_path) as db:
            count = await (
                await db.execute(
                    "SELECT COUNT(*) FROM memory_index_outbox WHERE request_state='in_flight'"
                )
            ).fetchone()
        assert count == (len(texts),)
        self.calls.append(list(texts))
        if len(self.calls) == self.fail_on:
            raise EmbeddingServiceError("fixture batch failure", request_outcome_known=self.known)
        return [[1.0, 0.0] if text.startswith("Canonical: ") else [0.0, 1.0] for text in texts]


async def state_with_memories(tmp_path, count=1):
    state = StateService(
        tmp_path / "state.db", canonical_language="en", memory_normalizer=DualNormalizer()
    )
    await state.initialize()
    memories = [
        await state.create_memory(MemoryCreate(content=f"Source française {index:03}"), "phone")
        for index in range(count)
    ]
    return state, memories


async def vector_rows(state):
    async with aiosqlite.connect(state.db_path) as db:
        db.row_factory = aiosqlite.Row
        return [
            dict(row)
            for row in await (
                await db.execute(
                    "SELECT e.*,v.role FROM memory_view_embeddings e JOIN memory_text_views v ON v.id=e.view_id"
                )
            ).fetchall()
        ]


async def test_hundred_dual_memories_use_two_bounded_batches_and_keep_legacy_cache_canonical(
    tmp_path,
):
    state, _ = await state_with_memories(tmp_path, 100)
    outside = await state.create_memory(
        MemoryCreate(content="Private source", scope="project:private"), "phone"
    )
    provider = RecordingProvider(state)
    state.embedding_service = provider
    report = await state.backfill_memory_embeddings(scope="general", limit=100)
    assert (report["scanned"], report["indexed"], report["failed"], report["conflicted"]) == (
        100,
        100,
        0,
        0,
    )
    assert report["complete"] is True and report["views_indexed"] == 200
    assert report["batches"] == 2 and [len(call) for call in provider.calls] == [100, 100]
    vectors = await vector_rows(state)
    assert len(vectors) == 200 and outside["id"] not in {row["memory_id"] for row in vectors}
    assert all(
        json.loads(row["vector_json"]) == ([1.0, 0.0] if row["role"] == "canonical" else [0.0, 1.0])
        for row in vectors
    )
    async with aiosqlite.connect(state.db_path) as db:
        legacy = await (await db.execute("SELECT vector_json FROM memory_embeddings")).fetchall()
    assert legacy == [("[1.0,0.0]",)] * 100
    replay = await state.backfill_memory_embeddings(scope="general", limit=100)
    assert replay["unchanged"] == 100 and replay["views_unchanged"] == 200
    assert replay["batches"] == 0 and len(provider.calls) == 2


@pytest.mark.parametrize("known", [True, False])
async def test_second_batch_failure_keeps_completed_views_and_cursor_then_retries_safely(
    tmp_path, known
):
    state, _ = await state_with_memories(tmp_path, 100)
    provider = RecordingProvider(state, fail_on=2, known=known)
    state.embedding_service = provider
    first = await state.backfill_memory_embeddings(scope="general", limit=100)
    assert (first["indexed"], first["failed"], first["conflicted"]) == (50, 50, 0)
    assert first["complete"] is False and first["next_after_id"] is None
    before = await vector_rows(state)
    assert len(before) == 100
    provider.fail_on = None
    retry = await state.backfill_memory_embeddings(scope="general", limit=100)
    assert retry["unchanged"] == 50 and retry["views_unchanged"] == 100
    if known:
        assert retry["indexed"] == 50 and retry["complete"] is True
        assert len(provider.calls) == 3 and len(provider.calls[-1]) == 100
        assert set(provider.calls[0]).isdisjoint(provider.calls[-1])
        assert len(await vector_rows(state)) == 200
    else:
        assert retry["conflicted"] == 50 and retry["complete"] is False
        assert retry["next_after_id"] is None and len(provider.calls) == 2
        assert await vector_rows(state) == before


async def test_old_canonical_cache_does_not_prove_either_authoritative_view(tmp_path):
    state, (item,) = await state_with_memories(tmp_path)
    provider = RecordingProvider(state)
    state.embedding_service = provider
    async with aiosqlite.connect(state.db_path) as db:
        await db.execute(
            "INSERT INTO memory_embeddings VALUES(?,?,?,?,?)",
            (item["id"], embedding_identity(provider), 2, "[1.0,0.0]", item["updated_at"]),
        )
        await db.commit()
    report = await state.backfill_memory_embeddings(scope="general")
    assert report["indexed"] == 1 and report["unchanged"] == 0
    assert len(provider.calls) == 1 and len(provider.calls[0]) == 2
    assert len(await vector_rows(state)) == 2


async def test_memory_split_between_batches_counts_once_and_retries_only_its_missing_view(tmp_path):
    state, items = await state_with_memories(tmp_path, 51)
    state.embedding_service = LocalProvider()
    first_id = min(item["id"] for item in items)
    assert (await state.drain_memory_projections(limit=1, memory_id=first_id)).projected == 1
    provider = RecordingProvider(state, fail_on=2)
    state.embedding_service = provider
    first = await state.backfill_memory_embeddings(scope="general", limit=100, after_id="0")
    assert [len(call) for call in provider.calls] == [100, 1]
    assert first["indexed"] == 50 and first["failed"] == 1 and first["scanned"] == 51
    assert first["views_indexed"] == 100 and first["views_unchanged"] == 1
    assert first["complete"] is False and first["next_after_id"] == "0"
    assert len(await vector_rows(state)) == 101
    provider.fail_on = None
    retry = await state.backfill_memory_embeddings(scope="general", limit=100, after_id="0")
    assert retry["indexed"] == 1 and retry["unchanged"] == 50
    assert retry["views_indexed"] == 1 and retry["views_unchanged"] == 101
    assert retry["complete"] is True and retry["next_after_id"] == max(item["id"] for item in items)
    assert provider.calls[-1] == provider.calls[-2]
    assert len(await vector_rows(state)) == 102


async def test_one_current_view_is_skipped_and_the_other_finishes_the_logical_memory(tmp_path):
    state, _ = await state_with_memories(tmp_path)
    state.embedding_service = LocalProvider()
    assert (await state.drain_memory_projections(limit=1)).projected == 1
    before = await vector_rows(state)
    assert len(before) == 1
    provider = RecordingProvider(state)
    state.embedding_service = provider
    report = await state.backfill_memory_embeddings(scope="general")
    assert report["indexed"] == 1 and report["unchanged"] == 0
    assert report["views_indexed"] == report["views_unchanged"] == 1
    assert len(provider.calls[0]) == 1
    after = {row["view_id"]: row for row in await vector_rows(state)}
    assert after[before[0]["view_id"]] == before[0]


@pytest.mark.parametrize(
    "field,value",
    [
        ("source_id", "forged-source"),
        ("source_sha256", "forged-source-hash"),
        ("view_sha256", "forged-view-hash"),
        ("pipeline_signature", "forged-pipeline"),
        ("item_revision", "old-item-revision"),
        ("dimensions", 7),
    ],
)
async def test_a_vector_with_wrong_view_provenance_is_rebuilt_individually(tmp_path, field, value):
    state, _ = await state_with_memories(tmp_path)
    provider = RecordingProvider(state)
    state.embedding_service = provider
    await state.backfill_memory_embeddings(scope="general")
    async with aiosqlite.connect(state.db_path) as db:
        await db.execute(
            f"""UPDATE memory_view_embeddings SET {field}=?
                WHERE view_id IN (SELECT id FROM memory_text_views WHERE role='original')""",
            (value,),
        )
        await db.commit()
    report = await state.backfill_memory_embeddings(scope="general")
    assert report["indexed"] == 1 and report["views_indexed"] == report["views_unchanged"] == 1
    assert len(provider.calls[-1]) == 1 and not provider.calls[-1][0].startswith("Canonical: ")


async def test_source_edit_during_batch_keeps_other_memory_but_cannot_publish_stale_views(tmp_path):
    state, items = await state_with_memories(tmp_path, 2)
    changed, stable = items
    writer = StateService(
        state.db_path, canonical_language="en", memory_normalizer=DualNormalizer()
    )

    class Mutating(RecordingProvider):
        async def embed(self, texts):
            await writer.update_memory(
                changed["id"], MemoryUpdate(content="Corrected source"), "phone"
            )
            return await super().embed(texts)

    state.embedding_service = Mutating(state)
    report = await state.backfill_memory_embeddings(scope="general")
    assert report["indexed"] == 1 and report["conflicted"] == 1
    assert report["next_after_id"] is None and report["complete"] is False
    assert {row["memory_id"] for row in await vector_rows(state)} == {stable["id"]}
    provider = RecordingProvider(state)
    state.embedding_service = provider
    retry = await state.backfill_memory_embeddings(scope="general")
    assert retry["indexed"] == retry["unchanged"] == 1 and retry["complete"] is True
    assert set(provider.calls[0]) == {"Corrected source ", "Canonical: Corrected source "}


@pytest.mark.parametrize("damage", ["head", "receipt"])
async def test_invalid_head_or_receipt_is_counted_as_conflict_instead_of_paged_past(
    tmp_path, damage
):
    state, items = await state_with_memories(tmp_path, 2)
    invalid, valid = items
    async with aiosqlite.connect(state.db_path) as db:
        if damage == "head":
            await db.execute("DELETE FROM memory_text_heads WHERE memory_id=?", (invalid["id"],))
        else:
            await db.execute(
                "UPDATE memory_canonical_receipts SET status='failed' WHERE memory_id=?",
                (invalid["id"],),
            )
        await db.commit()
    provider = RecordingProvider(state)
    state.embedding_service = provider
    report = await state.backfill_memory_embeddings(scope="general")
    assert report["scanned"] == 2 and report["indexed"] == report["conflicted"] == 1
    assert report["complete"] is False and report["next_after_id"] is None
    assert len(provider.calls[0]) == 2
    assert {row["memory_id"] for row in await vector_rows(state)} == {valid["id"]}


async def test_cancellation_retains_uncertainty_for_both_views_and_blocks_restart(tmp_path):
    state, _ = await state_with_memories(tmp_path)
    entered = asyncio.Event()

    class Waiting(LocalProvider):
        async def embed(self, texts):
            self.calls.append(texts)
            entered.set()
            await asyncio.Event().wait()

    provider = Waiting()
    state.embedding_service = provider
    task = asyncio.create_task(state.backfill_memory_embeddings(scope="general"))
    await asyncio.wait_for(entered.wait(), 3)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    async with aiosqlite.connect(state.db_path) as db:
        await db.execute(
            "UPDATE memory_index_outbox SET lease_expires_at='2000-01-01T00:00:00+00:00'"
        )
        await db.commit()
    restarted = StateService(state.db_path, LocalProvider())
    await restarted.initialize()
    retry = await restarted.backfill_memory_embeddings(scope="general")
    assert retry["conflicted"] == 1 and retry["indexed"] == 0
    assert restarted.embedding_service.calls == [] and await vector_rows(state) == []
