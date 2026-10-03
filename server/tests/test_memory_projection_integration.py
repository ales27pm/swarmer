"""Actual memory writes, migration and operator backfill own their projections."""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager

import aiosqlite
import pytest

from app.models import MemoryCreate, MemoryUpdate
from app.services import memory_canonical_store, state_service
from app.services.embedding_service import EmbeddingServiceError
from app.services.memory_normalization import MemoryNormalizationError
from app.services.memory_text_views import claim_projection_batch
from app.services.memory_vectors import embedding_identity
from app.services.state_service import SCHEMA_VERSION, StateService
from tests.test_memory_canonical_store import ReviewedNormalizer
from tests.test_memory_indexing_regressions import LocalProvider

PROJECTIONS = ("memory_text_heads", "memory_text_views", "memory_index_outbox")
ATOMIC = ("memory_items", "memory_embeddings", *PROJECTIONS)


class InjectedFault(RuntimeError):
    pass


async def state_at(tmp_path, *, canonical=False, provider=None):
    state = StateService(
        tmp_path / "state.db",
        provider,
        canonical_language="en" if canonical else "legacy",
        memory_normalizer=ReviewedNormalizer() if canonical else None,
    )
    await state.initialize()
    return state


async def snapshot(state, tables=ATOMIC):
    async with aiosqlite.connect(state.db_path) as db:
        return {
            name: await (await db.execute(f'SELECT * FROM "{name}" ORDER BY rowid')).fetchall()
            for name in tables
        }


async def rows(state, table):
    assert table in (*ATOMIC, "memory_canonical_receipts", "memory_source_journal")
    async with aiosqlite.connect(state.db_path) as db:
        db.row_factory = aiosqlite.Row
        return [dict(row) for row in await (await db.execute(f"SELECT * FROM {table}")).fetchall()]


@pytest.mark.asyncio
@pytest.mark.parametrize("canonical", [False, True])
async def test_create_writes_exact_views_and_durable_intent_in_domain_transaction(
    tmp_path, canonical
):
    state = await state_at(tmp_path, canonical=canonical)
    text = "Ne pas envoyer automatiquement."
    item = await state.create_memory(MemoryCreate(content=text, scope="project:one"), "phone")
    (head,) = await rows(state, "memory_text_heads")
    views = await rows(state, "memory_text_views")
    (event,) = await rows(state, "memory_index_outbox")
    original = next(view for view in views if view["role"] == "original")
    assert original["content"] == text and original["scope"] == "project:one"
    assert original["language"] == ("fr" if canonical else "und")
    assert head["memory_id"] == item["id"] and head["item_revision"] == item["updated_at"]
    assert event["status"] == "pending" and event["provider"] is None
    assert event["revision"] == head["revision"] == 1
    assert text not in json.dumps(event)
    assert len(views) == (2 if canonical else 1)
    if canonical:
        pivot = next(view for view in views if view["role"] == "canonical")
        assert pivot["content"] == item["content"] == "Do not send automatically."
        assert event["view_id"] == pivot["id"] == head["index_view_id"]
        before = await snapshot(state)
        assert (
            await state.create_memory(MemoryCreate(content=text, scope="project:one"), "phone")
            == item
        )
        assert await snapshot(state) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("canonical", [False, True])
async def test_create_failure_after_projection_writes_rolls_back_domain_and_intent(
    tmp_path, monkeypatch, canonical
):
    state = await state_at(tmp_path, canonical=canonical)
    module = memory_canonical_store if canonical else state_service
    original = module.record_text_views_locked

    async def fail_after_insert(*args, **kwargs):
        await original(*args, **kwargs)
        raise InjectedFault("after projection")

    monkeypatch.setattr(module, "record_text_views_locked", fail_after_insert)
    with pytest.raises((InjectedFault, MemoryNormalizationError)):
        await state.create_memory(MemoryCreate(content="Ne pas envoyer automatiquement."), "phone")
    assert all(not values for values in (await snapshot(state)).values())
    assert not any(
        row["status"] == "accepted" for row in await rows(state, "memory_canonical_receipts")
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("canonical", [False, True])
async def test_update_failure_rolls_back_text_views_vectors_and_outbox(
    tmp_path, monkeypatch, canonical
):
    state = await state_at(tmp_path, canonical=canonical, provider=LocalProvider())
    item = await state.create_memory(
        MemoryCreate(content="Ne pas envoyer automatiquement."), "phone"
    )
    before = await snapshot(state)
    module = memory_canonical_store if canonical else state_service

    async def fail_audit(*args, **kwargs):
        raise InjectedFault("before commit")

    monkeypatch.setattr(module, "append_audit_event", fail_audit)
    with pytest.raises((InjectedFault, MemoryNormalizationError)):
        await state.update_memory(
            item["id"], MemoryUpdate(content="Garder les dates exactes."), "phone"
        )
    assert await snapshot(state) == before


@pytest.mark.asyncio
async def test_delete_is_atomic_and_erases_all_view_text_but_keeps_tombstone(tmp_path, monkeypatch):
    state = await state_at(tmp_path, canonical=True, provider=LocalProvider())
    item = await state.create_memory(
        MemoryCreate(content="Ne pas envoyer automatiquement."), "phone"
    )
    tables = (*ATOMIC, "memory_canonical_receipts", "memory_source_journal")
    before = await snapshot(state, tables)

    async def fail_audit(*args, **kwargs):
        raise InjectedFault("before commit")

    with monkeypatch.context() as patch:
        patch.setattr(state_service, "append_audit_event", fail_audit)
        with pytest.raises(InjectedFault):
            await state.delete_memory(item["id"], "phone")
        assert await snapshot(state, tables) == before
    assert await state.delete_memory(item["id"], "phone")
    assert await state.get_memory(item["id"]) is None
    assert await rows(state, "memory_text_views") == []
    assert await rows(state, "memory_source_journal") == []
    assert await rows(state, "memory_embeddings") == []
    (head,) = await rows(state, "memory_text_heads")
    assert head["deleted"] == 1 and head["revision"] == 2
    assert head["source_sha256"] is None and head["index_view_id"] is None
    report = await state.drain_memory_projections(memory_id=item["id"])
    assert report.deleted == 1
    assert await rows(state, "memory_embeddings") == []


@pytest.mark.asyncio
async def test_pin_only_preserves_view_revision_and_vector_without_provider_call(tmp_path):
    provider = LocalProvider()
    state = await state_at(tmp_path, canonical=True, provider=provider)
    item = await state.create_memory(
        MemoryCreate(content="Ne pas envoyer automatiquement."), "phone"
    )
    before_views = await rows(state, "memory_text_views")
    before_events = await rows(state, "memory_index_outbox")
    calls = len(provider.calls)
    pinned = await state.update_memory(item["id"], MemoryUpdate(pinned=True), "phone")
    assert await rows(state, "memory_text_views") == before_views
    assert await rows(state, "memory_index_outbox") == before_events
    assert len(provider.calls) == calls
    (head,) = await rows(state, "memory_text_heads")
    (vector,) = await rows(state, "memory_embeddings")
    assert head["revision"] == 1
    assert head["item_revision"] == vector["updated_at"] == pinned["updated_at"]


async def make_schema28_fixture(tmp_path):
    provider = LocalProvider()
    legacy = await state_at(tmp_path, provider=provider)
    await legacy.create_memory(MemoryCreate(content="Legacy canoe."), "phone")
    canonical = StateService(
        legacy.db_path, provider, canonical_language="en", memory_normalizer=ReviewedNormalizer()
    )
    await canonical.create_memory(MemoryCreate(content="Ne pas envoyer automatiquement."), "phone")
    async with aiosqlite.connect(legacy.db_path) as db:
        for name in reversed(PROJECTIONS):
            await db.execute(f"DROP TABLE {name}")
        await db.execute("PRAGMA user_version=28")
        await db.commit()
        names = [
            row[0]
            for row in await (
                await db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name<>'sqlite_sequence' ORDER BY name"
                )
            ).fetchall()
        ]
    return legacy, provider, await snapshot(legacy, names)


@pytest.mark.asyncio
async def test_schema28_migration_is_additive_idempotent_and_never_calls_a_model(tmp_path):
    state, provider, before = await make_schema28_fixture(tmp_path)
    calls = len(provider.calls)
    await state.initialize()
    assert await snapshot(state, before) == before
    assert len(provider.calls) == calls
    async with aiosqlite.connect(state.db_path) as db:
        assert await (await db.execute("PRAGMA user_version")).fetchone() == (SCHEMA_VERSION,)
    assert len(await rows(state, "memory_text_heads")) == 2
    assert len(await rows(state, "memory_text_views")) == 3
    assert len(await rows(state, "memory_index_outbox")) == 2
    after = await snapshot(state)
    await state.initialize()
    assert await snapshot(state) == after
    assert len(provider.calls) == calls


@pytest.mark.asyncio
async def test_migration_failure_rolls_back_schema_and_seed_then_can_retry(tmp_path, monkeypatch):
    state, _provider, before = await make_schema28_fixture(tmp_path)
    original = state_service.record_text_views_locked

    async def fail_after_seed(*args, **kwargs):
        await original(*args, **kwargs)
        raise InjectedFault("migration failure")

    with monkeypatch.context() as patch:
        patch.setattr(state_service, "record_text_views_locked", fail_after_seed)
        with pytest.raises(InjectedFault):
            await state.initialize()
    async with aiosqlite.connect(state.db_path) as db:
        assert await (await db.execute("PRAGMA user_version")).fetchone() == (28,)
        assert (
            await (
                await db.execute("SELECT name FROM sqlite_master WHERE name='memory_text_heads'")
            ).fetchone()
            is None
        )
    assert await snapshot(state, before) == before
    await state.initialize()
    assert len(await rows(state, "memory_text_heads")) == 2


@pytest.mark.asyncio
async def test_migration_refuses_unqualified_canonical_source_without_promoting_it(tmp_path):
    state, _provider, _ = await make_schema28_fixture(tmp_path)
    async with aiosqlite.connect(state.db_path) as db:
        await db.execute("UPDATE memory_source_journal SET content='changed'")
        await db.commit()
    with pytest.raises(RuntimeError, match="provenance"):
        await state.initialize()
    async with aiosqlite.connect(state.db_path) as db:
        assert await (await db.execute("PRAGMA user_version")).fetchone() == (28,)


@pytest.mark.asyncio
async def test_backfill_acks_actual_batch_without_second_provider_call_and_holds_admission(
    tmp_path,
):
    state = await state_at(tmp_path)
    await state.create_memory(MemoryCreate(content="canoe one"), "phone")
    await state.create_memory(MemoryCreate(content="canoe two"), "phone")
    held = False

    @asynccontextmanager
    async def admission():
        nonlocal held
        held = True
        try:
            yield
        finally:
            assert (
                sum(
                    event["status"] == "completed"
                    for event in await rows(state, "memory_index_outbox")
                )
                == 2
            )
            held = False

    class CheckedProvider(LocalProvider):
        async def embed(self, texts):
            assert held
            return await super().embed(texts)

    provider = CheckedProvider()
    state.embedding_service = provider
    state.embedding_admission = admission
    report = await state.backfill_memory_embeddings(scope="general")
    assert report["indexed"] == 2 and len(provider.calls) == 1 and not held
    assert len(provider.calls[0]) == 2
    events = await rows(state, "memory_index_outbox")
    assert {event["status"] for event in events} <= {"completed", "obsolete"}
    assert sum(event["status"] == "completed" for event in events) == 2
    assert (await state.drain_memory_projections(limit=16)).claimed == 0


@pytest.mark.asyncio
async def test_backfill_does_not_steal_a_current_claim(tmp_path):
    state = await state_at(tmp_path)
    item = await state.create_memory(MemoryCreate(content="canoe"), "phone")
    provider = LocalProvider()
    state.embedding_service = provider
    (claim,) = await claim_projection_batch(
        state.db_path, "other", provider=embedding_identity(provider)
    )
    report = await state.backfill_memory_embeddings(scope="general")
    assert report["indexed"] == 0 and report["conflicted"] == 1
    assert await rows(state, "memory_embeddings") == []
    (event,) = await rows(state, "memory_index_outbox")
    assert event["status"] == "claimed" and event["generation"] == claim.generation
    assert event["memory_id"] == item["id"]
    assert provider.calls == []


@pytest.mark.asyncio
async def test_backfill_delayed_completion_cannot_restore_tombstoned_source(tmp_path):
    state = await state_at(tmp_path)
    item = await state.create_memory(MemoryCreate(content="canoe"), "phone")
    started, release = asyncio.Event(), asyncio.Event()

    class Delayed(LocalProvider):
        async def embed(self, texts):
            started.set()
            await release.wait()
            return await super().embed(texts)

    state.embedding_service = Delayed()
    task = asyncio.create_task(state.backfill_memory_embeddings(scope="general"))
    await asyncio.wait_for(started.wait(), 2)
    await state.delete_memory(item["id"], "phone")
    release.set()
    report = await task
    assert report["indexed"] == 0 and report["conflicted"] == 1
    assert await rows(state, "memory_embeddings") == []
    assert (await rows(state, "memory_text_heads"))[0]["deleted"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("canonical", [False, True])
async def test_only_explicit_index_request_wakes_known_failure(tmp_path, canonical):
    provider = LocalProvider()
    provider.fail = True
    state = await state_at(tmp_path, canonical=canonical, provider=provider)
    request = MemoryCreate(content="Ne pas envoyer automatiquement.")
    item = await state.create_memory(request, "phone")
    assert len(provider.calls) == 1
    (failed,) = await rows(state, "memory_index_outbox")
    assert failed["status"] == "pending" and failed["error_category"]
    provider.fail = False
    assert not await state.index_memory(item["id"], retry_known_failure=False)
    assert (await state.drain_memory_projections(memory_id=item["id"])).claimed == 0
    if canonical:
        assert (await state.create_memory(request, "phone"))["id"] == item["id"]
    await state.update_memory(item["id"], MemoryUpdate(pinned=True), "phone")
    assert len(provider.calls) == 1
    assert (await rows(state, "memory_index_outbox"))[0] == failed
    assert await state.index_memory(item["id"])
    assert len(provider.calls) == 2
    assert (await rows(state, "memory_index_outbox"))[0]["status"] == "completed"


@pytest.mark.asyncio
async def test_explicit_index_does_not_retry_cancelled_ambiguous_request(tmp_path):
    entered = asyncio.Event()

    class CancelledProvider(LocalProvider):
        async def embed(self, texts):
            self.calls.append(texts)
            entered.set()
            await asyncio.Event().wait()

    state = await state_at(tmp_path)
    item = await state.create_memory(MemoryCreate(content="canoe"), "phone")
    provider = CancelledProvider()
    state.embedding_service = provider
    pending = asyncio.create_task(state.index_memory(item["id"]))
    await asyncio.wait_for(entered.wait(), 2)
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    (claim,) = await rows(state, "memory_index_outbox")
    assert claim["status"] == "claimed" and claim["owner"]
    assert not await state.index_memory(item["id"])
    assert len(provider.calls) == 1
    assert (await rows(state, "memory_index_outbox"))[0] == claim
    assert await rows(state, "memory_embeddings") == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change", ["delete", "content", "scope", "sensitivity", "receipt", "provider"]
)
async def test_backfill_revalidates_entire_page_after_admission_before_dispatch(tmp_path, change):
    state = await state_at(tmp_path, canonical=True)
    item = await state.create_memory(
        MemoryCreate(content="Ne pas envoyer automatiquement."), "phone"
    )
    writer = StateService(state.db_path)
    provider = LocalProvider()
    state.embedding_service = provider

    @asynccontextmanager
    async def admission():
        if change == "delete":
            await writer.delete_memory(item["id"], "phone")
        elif change == "content":
            await writer.update_memory(item["id"], MemoryUpdate(content="Replacement."), "phone")
        elif change in {"scope", "sensitivity"}:
            async with aiosqlite.connect(state.db_path) as db:
                await db.execute(
                    f"UPDATE memory_items SET {change}=? WHERE id=?", ("private", item["id"])
                )
                await db.commit()
        elif change == "receipt":
            async with aiosqlite.connect(state.db_path) as db:
                await db.execute("UPDATE memory_canonical_receipts SET status='failed'")
                await db.commit()
        else:
            state.embedding_service = LocalProvider()
        yield

    state.embedding_admission = admission
    report = await state.backfill_memory_embeddings(scope="general")
    assert report["indexed"] == 0 and report["conflicted"] == 1
    assert report["complete"] is False and report["next_after_id"] is None
    assert provider.calls == []
    assert state.embedding_service.calls == []
    assert await rows(state, "memory_embeddings") == []


@pytest.mark.asyncio
async def test_cancelled_backfill_blocks_restart_and_expired_lease_retries(tmp_path):
    state = await state_at(tmp_path)
    for text in ("canoe one", "canoe two"):
        await state.create_memory(MemoryCreate(content=text), "phone")
    entered = asyncio.Event()

    class Waiting(LocalProvider):
        async def embed(self, texts):
            self.calls.append(texts)
            entered.set()
            await asyncio.Event().wait()

    provider = Waiting()
    state.embedding_service = provider
    task = asyncio.create_task(state.backfill_memory_embeddings(scope="general"))
    await asyncio.wait_for(entered.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    async with aiosqlite.connect(state.db_path) as db:
        await db.execute(
            "UPDATE memory_index_outbox SET lease_expires_at='2000-01-01T00:00:00+00:00' WHERE status='claimed'"
        )
        await db.commit()
    restarted = StateService(state.db_path, LocalProvider())
    await restarted.initialize()
    report = await restarted.backfill_memory_embeddings(scope="general")
    assert report["conflicted"] == 2 and report["indexed"] == 0
    assert restarted.embedding_service.calls == []
    assert len(provider.calls) == 1 and len(provider.calls[0]) == 2
    assert (
        sum(
            event["request_state"] == "in_flight"
            for event in await rows(state, "memory_index_outbox")
        )
        == 2
    )
    assert (await restarted.drain_memory_projections(limit=16)).claimed == 0
    assert await rows(state, "memory_embeddings") == []


@pytest.mark.asyncio
@pytest.mark.parametrize("known", [True, False])
async def test_backfill_known_failure_can_retry_but_unknown_stays_fenced(tmp_path, known):
    state = await state_at(tmp_path)
    for text in ("canoe one", "canoe two"):
        await state.create_memory(MemoryCreate(content=text), "phone")

    class Failure(LocalProvider):
        async def embed(self, texts):
            self.calls.append(texts)
            events = await rows(state, "memory_index_outbox")
            assert sum(event["request_state"] == "in_flight" for event in events) == 2
            raise EmbeddingServiceError("fixture failure", request_outcome_known=known)

    provider = Failure()
    state.embedding_service = provider
    first = await state.backfill_memory_embeddings(scope="general")
    assert first["failed"] == 2 and first["indexed"] == 0
    assert first["complete"] is False and first["next_after_id"] is None
    assert len(provider.calls) == 1
    events = await rows(state, "memory_index_outbox")
    bound = [event for event in events if event["provider"]]
    assert len(bound) == 2
    assert {event["request_state"] for event in bound} == {"known" if known else "in_flight"}
    assert await rows(state, "memory_embeddings") == []
    async with aiosqlite.connect(state.db_path) as db:
        await db.execute(
            "UPDATE memory_index_outbox SET lease_expires_at='2000-01-01T00:00:00+00:00' WHERE status='claimed'"
        )
        await db.commit()
    restarted = StateService(state.db_path, LocalProvider())
    second = await restarted.backfill_memory_embeddings(scope="general")
    assert second["indexed"] == (2 if known else 0)
    assert len(restarted.embedding_service.calls) == (1 if known else 0)
    if not known:
        assert second["conflicted"] == 2


@pytest.mark.asyncio
async def test_backfill_late_known_response_clears_uncertainty_without_publishing(tmp_path):
    state = await state_at(tmp_path)
    for text in ("canoe one", "canoe two"):
        await state.create_memory(MemoryCreate(content=text), "phone")

    class Late(LocalProvider):
        async def embed(self, texts):
            async with aiosqlite.connect(state.db_path) as db:
                await db.execute(
                    "UPDATE memory_index_outbox SET lease_expires_at='2000-01-01T00:00:00+00:00' WHERE status='claimed'"
                )
                await db.commit()
            return await super().embed(texts)

    state.embedding_service = Late()
    result = await state.backfill_memory_embeddings(scope="general")
    assert result["indexed"] == 0 and result["conflicted"] == 2
    assert await rows(state, "memory_embeddings") == []
    bound = [event for event in await rows(state, "memory_index_outbox") if event["provider"]]
    assert {event["request_state"] for event in bound} == {"known"}
    assert all(event["response_received_at"] for event in bound)
    state.embedding_service = LocalProvider()
    assert (await state.backfill_memory_embeddings(scope="general"))["indexed"] == 2
    assert len(state.embedding_service.calls) == 1


@pytest.mark.asyncio
async def test_backfill_rechecks_sources_at_durable_dispatch_boundary(tmp_path, monkeypatch):
    state = await state_at(tmp_path)
    item = await state.create_memory(MemoryCreate(content="canoe one"), "phone")
    await state.create_memory(MemoryCreate(content="canoe two"), "phone")
    provider = LocalProvider()
    state.embedding_service = provider
    mark = state_service.mark_projection_batch_dispatched

    async def mutate_then_mark(*args, **kwargs):
        await state.delete_memory(item["id"], "phone")
        return await mark(*args, **kwargs)

    monkeypatch.setattr(state_service, "mark_projection_batch_dispatched", mutate_then_mark)
    result = await state.backfill_memory_embeddings(scope="general")
    assert result["indexed"] == 0 and result["conflicted"] == 2
    assert provider.calls == [] and await rows(state, "memory_embeddings") == []
    assert not any(
        event["request_state"] == "in_flight" for event in await rows(state, "memory_index_outbox")
    )


@pytest.mark.asyncio
async def test_backfill_sends_one_bounded_hundred_item_batch_with_durable_marker(tmp_path):
    state = await state_at(tmp_path)
    for index in range(101):
        await state.create_memory(MemoryCreate(content=f"canoe {index}"), "phone")

    class Bounded(LocalProvider):
        async def embed(self, texts):
            events = await rows(state, "memory_index_outbox")
            assert sum(event["request_state"] == "in_flight" for event in events) == len(texts)
            return await super().embed(texts)

    provider = Bounded()
    state.embedding_service = provider
    first = await state.backfill_memory_embeddings(scope="general", limit=100)
    assert first["indexed"] == first["scanned"] == 100 and first["complete"] is False
    assert len(provider.calls) == 1 and len(provider.calls[0]) == 100
    second = await state.backfill_memory_embeddings(
        scope="general", limit=100, after_id=first["next_after_id"]
    )
    assert second["indexed"] == 1 and second["complete"] is True
    assert len(provider.calls) == 2 and len(provider.calls[1]) == 1
