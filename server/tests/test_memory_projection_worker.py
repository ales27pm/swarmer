from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import aiosqlite
import pytest

from app.models import MemoryCreate, MemoryUpdate
from app.services.direct_model_admission import LocalGPUUnavailable
from app.services.embedding_service import EmbeddingServiceError
from app.services.memory_projection_worker import MemoryProjectionWorker
from app.services.memory_text_views import claim_projection_batch, projection_status
from app.services.memory_vectors import embedding_identity
from app.services.state_service import StateService


class Provider:
    provider_name = "private-projection-fixture"
    dimensions = 2

    def __init__(self, *, paused=False, vector=None):
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        if not paused:
            self.release.set()
        self.calls = []
        self.vector = [1.0, 0.0] if vector is None else vector

    async def embed(self, texts, **kwargs):
        self.calls.append(texts)
        self.entered.set()
        await self.release.wait()
        return [self.vector]


async def fixture(tmp_path):
    writer = StateService(tmp_path / "state.db")
    await writer.initialize()
    item = await writer.create_memory(MemoryCreate(content="Keep `Cache.py` unchanged."), "test")
    return writer, item


async def embeddings(writer):
    async with aiosqlite.connect(writer.db_path) as db:
        return await (await db.execute("SELECT * FROM memory_embeddings")).fetchall()


@pytest.mark.asyncio
async def test_committed_intent_survives_restart_and_projects_only_once(tmp_path):
    writer, item = await fixture(tmp_path)
    assert await embeddings(writer) == []
    provider = Provider()
    restarted = StateService(writer.db_path, provider)
    await restarted.initialize()
    report = await restarted.memory_projection_worker.drain(memory_id=item["id"])
    assert report.projected == 1
    assert len(await embeddings(writer)) == 1
    assert (await restarted.memory_projection_worker.drain(memory_id=item["id"])).claimed == 0
    assert len(provider.calls) == 1
    assert await writer.get_memory(item["id"]) == item


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["update", "delete"])
async def test_late_projection_never_restores_changed_or_deleted_content(tmp_path, change):
    writer, item = await fixture(tmp_path)
    provider = Provider(paused=True)
    worker = MemoryProjectionWorker(writer.db_path, lambda: provider, lambda: None)
    task = asyncio.create_task(worker.drain(memory_id=item["id"]))
    await asyncio.wait_for(provider.entered.wait(), 2)
    if change == "delete":
        assert await writer.delete_memory(item["id"], "test")
    else:
        await writer.update_memory(item["id"], MemoryUpdate(content="Keep `Updated.py`."), "test")
    provider.release.set()
    report = await asyncio.wait_for(task, 2)
    assert report.projected == 0
    assert report.stale == 1
    assert await embeddings(writer) == []
    next_report = await worker.drain(memory_id=item["id"])
    if change == "delete":
        assert next_report.deleted == 1
        assert len(provider.calls) == 1
        assert await embeddings(writer) == []
    else:
        assert next_report.projected == 1
        assert provider.calls[-1] == ["Keep `Updated.py`. "]


@pytest.mark.asyncio
async def test_concurrent_drains_do_not_issue_duplicate_requests(tmp_path):
    writer, item = await fixture(tmp_path)
    provider = Provider(paused=True)
    worker = MemoryProjectionWorker(writer.db_path, lambda: provider, lambda: None)
    first = asyncio.create_task(worker.drain(memory_id=item["id"]))
    await asyncio.wait_for(provider.entered.wait(), 2)
    second = await worker.drain(memory_id=item["id"])
    assert second.claimed == 0
    provider.release.set()
    assert (await asyncio.wait_for(first, 2)).projected == 1
    assert len(provider.calls) == 1


@pytest.mark.asyncio
async def test_provider_change_during_inference_cannot_publish_old_vector(tmp_path):
    writer, item = await fixture(tmp_path)
    provider = Provider(paused=True)
    holder = [provider]
    worker = MemoryProjectionWorker(writer.db_path, lambda: holder[0], lambda: None)
    task = asyncio.create_task(worker.drain(memory_id=item["id"]))
    await asyncio.wait_for(provider.entered.wait(), 2)
    replacement = Provider()
    replacement.provider_name = "other-embedding-space"
    holder[0] = replacement
    provider.release.set()
    assert (await asyncio.wait_for(task, 2)).projected == 0
    assert await embeddings(writer) == []
    assert replacement.calls == []
    status = await projection_status(writer.db_path, provider=embedding_identity(replacement))
    assert status["provider_mismatch"] > 0


@pytest.mark.asyncio
async def test_busy_gpu_defers_without_embedding_or_retry(tmp_path):
    writer, item = await fixture(tmp_path)
    provider = Provider()

    @asynccontextmanager
    async def busy():
        raise LocalGPUUnavailable("fixture contention")
        yield

    worker = MemoryProjectionWorker(
        writer.db_path, lambda: provider, lambda: None, model_admission=busy
    )
    report = await worker.drain(limit=16, memory_id=item["id"])
    assert report.deferred == 1
    assert report.claimed == 1
    assert provider.calls == []
    assert await embeddings(writer) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("vector", [[float("nan"), 1.0], [1.0], [0.0, 0.0]])
async def test_invalid_vector_leaves_intent_retryable_without_ack(tmp_path, vector):
    writer, item = await fixture(tmp_path)
    provider = Provider(vector=vector)
    worker = MemoryProjectionWorker(writer.db_path, lambda: provider, lambda: None)
    report = await worker.drain(limit=16, memory_id=item["id"])
    assert report.failed == 1
    assert report.projected == 0
    assert len(provider.calls) == 1
    assert await embeddings(writer) == []


@pytest.mark.asyncio
async def test_cancel_keeps_unacknowledged_lease_and_does_not_retry(tmp_path):
    writer, item = await fixture(tmp_path)
    provider = Provider(paused=True)
    worker = MemoryProjectionWorker(writer.db_path, lambda: provider, lambda: None)
    task = asyncio.create_task(worker.drain(memory_id=item["id"]))
    await asyncio.wait_for(provider.entered.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await embeddings(writer) == []
    assert (await projection_status(writer.db_path))["request_outcome_unknown"] == 1
    assert (await worker.drain(memory_id=item["id"])).claimed == 0
    assert len(provider.calls) == 1
    future = (datetime.now(UTC) + timedelta(minutes=2)).isoformat()
    assert (
        await claim_projection_batch(
            writer.db_path,
            "after-expired-unknown-request",
            memory_id=item["id"],
            provider=embedding_identity(provider),
            now=future,
        )
        == []
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("known", [False, True])
async def test_only_known_provider_failure_can_be_retried_after_expiry(tmp_path, known):
    writer, item = await fixture(tmp_path)

    class FailingProvider(Provider):
        async def embed(self, texts, **kwargs):
            self.calls.append(texts)
            raise EmbeddingServiceError("fixture failure", request_outcome_known=known)

    provider = FailingProvider()
    worker = MemoryProjectionWorker(writer.db_path, lambda: provider, lambda: None)
    report = await worker.drain(memory_id=item["id"])
    assert report.failed == 1
    assert report.unknown == int(not known)
    assert (await projection_status(writer.db_path))["request_outcome_unknown"] == int(not known)
    future = (datetime.now(UTC) + timedelta(minutes=2)).isoformat()
    claims = await claim_projection_batch(
        writer.db_path, "after-failure", provider=embedding_identity(provider), now=future
    )
    assert len(claims) == int(known)
    assert len(provider.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["delete", "update"])
async def test_source_changed_during_admission_is_not_sent(tmp_path, change):
    writer, item = await fixture(tmp_path)
    provider = Provider()

    @asynccontextmanager
    async def admission():
        if change == "delete":
            await writer.delete_memory(item["id"], "test")
        else:
            await writer.update_memory(item["id"], MemoryUpdate(content="New source"), "test")
        yield

    worker = MemoryProjectionWorker(
        writer.db_path, lambda: provider, lambda: None, model_admission=admission
    )
    report = await worker.drain(memory_id=item["id"])
    assert report.stale == 1
    assert provider.calls == []
    assert await embeddings(writer) == []


@pytest.mark.asyncio
async def test_received_response_after_lease_expiry_settles_without_publishing(tmp_path):
    writer, item = await fixture(tmp_path)

    class SlowProvider(Provider):
        async def embed(self, texts, **kwargs):
            async with aiosqlite.connect(writer.db_path) as db:
                await db.execute(
                    "UPDATE memory_index_outbox SET lease_expires_at='2000-01-01T00:00:00+00:00' "
                    "WHERE request_state='in_flight'"
                )
                await db.commit()
            return await super().embed(texts, **kwargs)

    provider = SlowProvider()
    worker = MemoryProjectionWorker(writer.db_path, lambda: provider, lambda: None)
    report = await worker.drain(memory_id=item["id"])
    assert report.stale == 1
    assert report.projected == 0
    assert await embeddings(writer) == []
    assert (await projection_status(writer.db_path))["request_outcome_unknown"] == 0
    assert (
        len(
            await claim_projection_batch(
                writer.db_path, "late-received", provider=embedding_identity(provider)
            )
        )
        == 1
    )


@pytest.mark.asyncio
async def test_provider_changed_while_marking_dispatch_is_not_called(tmp_path, monkeypatch):
    from app.services import memory_projection_worker as module

    writer, item = await fixture(tmp_path)
    old_provider = Provider()
    new_provider = Provider()
    holder = [old_provider]
    mark = module.mark_projection_dispatched

    async def mark_then_reconfigure(*args, **kwargs):
        marked = await mark(*args, **kwargs)
        holder[0] = new_provider
        return marked

    monkeypatch.setattr(module, "mark_projection_dispatched", mark_then_reconfigure)
    worker = MemoryProjectionWorker(writer.db_path, lambda: holder[0], lambda: None)
    assert (await worker.drain(memory_id=item["id"])).deferred == 1
    assert old_provider.calls == new_provider.calls == []
    assert (await projection_status(writer.db_path))["request_outcome_unknown"] == 0


@pytest.mark.asyncio
async def test_targeted_drain_does_not_take_another_memory(tmp_path):
    writer, item = await fixture(tmp_path)
    other = await writer.create_memory(MemoryCreate(content="Other source."), "test")
    provider = Provider()
    worker = MemoryProjectionWorker(writer.db_path, lambda: provider, lambda: None)
    report = await worker.drain(limit=16, memory_id=item["id"])
    assert report.projected == 1
    assert len(provider.calls) == 1
    assert (await worker.drain(memory_id=other["id"])).projected == 1
