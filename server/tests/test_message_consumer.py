from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.services.message_board import DurableEvent
from app.services.message_consumer import (
    ConsumerCheckpointStore,
    ConsumerDeliveryConflict,
    ConsumerHandlerContext,
    ConsumerLeaseLost,
    MessageConsumer,
)


class MutableClock:
    def __init__(self, value: datetime) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value


def durable_event(
    index: int = 1,
    *,
    event_id: str | None = None,
    dedupe_key: str | None = None,
) -> DurableEvent:
    return DurableEvent(
        schema_version="1.0",
        event_id=event_id or f"evt_task_{index}",
        dedupe_key=dedupe_key or f"task-{index}:projection",
        topic="tasks.status",
        event_type="updated",
        aggregate_type="task",
        aggregate_id=f"task-{index}",
        task_id=f"task-{index}",
        agent_id=None,
        payload={"status": "running", "task_id": f"task-{index}"},
        created_at=datetime(2026, 9, 8, 16, index, tzinfo=UTC).isoformat(),
    )


@pytest.mark.asyncio
async def test_checkpoint_is_written_only_after_handler_success(tmp_path: Path) -> None:
    store = ConsumerCheckpointStore(tmp_path / "consumer.db")
    checkpoints_seen_during_handler: list[object] = []

    async def handler(event: DurableEvent, context: ConsumerHandlerContext) -> None:
        checkpoints_seen_during_handler.append(await store.get_checkpoint(context.consumer_group))
        assert context.event_id == event.event_id
        assert context.dedupe_key == event.dedupe_key
        assert context.idempotency_key == f"projection:{event.event_id}"

    consumer = MessageConsumer(
        store,
        consumer_group="projection",
        consumer_id="cp_instance_a:projection",
        handler=handler,
    )
    await consumer.initialize()
    await consumer.initialize()  # additive initialization is restart-safe
    submitted = await consumer.submit(durable_event(), source_cursor="41-0")
    result = await consumer.process_once()

    assert submitted.inserted is True
    assert checkpoints_seen_during_handler == [None]
    assert result.claimed == 1
    assert result.succeeded == 1
    checkpoint = await store.get_checkpoint("projection")
    assert checkpoint is not None
    assert checkpoint.last_event_id == "evt_task_1"
    assert checkpoint.last_source_cursor == "41-0"
    assert checkpoint.completed_count == 1
    assert await store.status_counts("projection") == {
        "pending": 0,
        "processing": 0,
        "completed": 1,
        "dead_letter": 0,
    }


@pytest.mark.asyncio
async def test_retry_reuses_stable_handler_idempotency_identity(tmp_path: Path) -> None:
    store = ConsumerCheckpointStore(tmp_path / "consumer.db")
    attempts: list[ConsumerHandlerContext] = []

    async def handler(_event: DurableEvent, context: ConsumerHandlerContext) -> None:
        attempts.append(context)
        if len(attempts) < 3:
            raise RuntimeError("transient secret=must-not-persist")

    consumer = MessageConsumer(
        store,
        consumer_group="telemetry",
        consumer_id="cp_a:telemetry",
        handler=handler,
        max_attempts=3,
    )
    await consumer.initialize()
    await consumer.submit(durable_event())

    first = await consumer.process_once()
    second = await consumer.process_once()
    third = await consumer.process_once()

    assert first.retrying == 1
    assert second.retrying == 1
    assert third.succeeded == 1
    assert [context.attempt for context in attempts] == [1, 2, 3]
    assert {context.event_id for context in attempts} == {"evt_task_1"}
    assert {context.idempotency_key for context in attempts} == {"telemetry:evt_task_1"}
    checkpoint = await store.get_checkpoint("telemetry")
    assert checkpoint is not None
    assert checkpoint.completed_count == 1


@pytest.mark.asyncio
async def test_bounded_failure_dead_letters_with_redacted_error(tmp_path: Path) -> None:
    store = ConsumerCheckpointStore(tmp_path / "consumer.db")

    async def handler(_event: DurableEvent, _context: ConsumerHandlerContext) -> None:
        raise OSError("redis://user:password@example.invalid private payload")

    consumer = MessageConsumer(
        store,
        consumer_group="telemetry",
        consumer_id="cp_a:telemetry",
        handler=handler,
        max_attempts=2,
    )
    await consumer.initialize()
    await consumer.submit(durable_event())

    first = await consumer.process_once()
    second = await consumer.process_once()

    assert first.retrying == 1
    assert second.dead_lettered == 1
    assert await store.get_checkpoint("telemetry") is None
    dead_letters = await store.list_dead_letters("telemetry")
    assert len(dead_letters) == 1
    assert dead_letters[0]["attempt_count"] == 2
    assert dead_letters[0]["last_error"] == "OSError: handler failed"
    assert "password" not in repr(dead_letters)
    assert "payload" not in dead_letters[0]


@pytest.mark.asyncio
async def test_duplicate_delivery_is_idempotent_but_conflicting_identity_is_rejected(
    tmp_path: Path,
) -> None:
    store = ConsumerCheckpointStore(tmp_path / "consumer.db")
    await store.initialize()
    original = durable_event()

    first = await store.enqueue("projection", original, source_cursor="1-0")
    duplicate = await store.enqueue("projection", original, source_cursor="1-0")

    assert first.inserted is True
    assert duplicate.inserted is False
    with pytest.raises(ConsumerDeliveryConflict, match="dedupe key"):
        await store.enqueue(
            "projection",
            durable_event(event_id="evt_tampered", dedupe_key=original.dedupe_key),
            source_cursor="1-0",
        )
    with pytest.raises(ConsumerDeliveryConflict, match="event ID"):
        await store.enqueue(
            "projection",
            durable_event(event_id=original.event_id, dedupe_key="different-dedupe-key"),
            source_cursor="1-0",
        )


@pytest.mark.asyncio
async def test_concurrent_consumers_cannot_claim_the_same_delivery(tmp_path: Path) -> None:
    database = tmp_path / "consumer.db"
    store_a = ConsumerCheckpointStore(database)
    store_b = ConsumerCheckpointStore(database)
    await store_a.initialize()
    await store_a.enqueue("projection", durable_event())

    claimed_a, claimed_b = await asyncio.gather(
        store_a.claim_batch("projection", "cp_a:projection"),
        store_b.claim_batch("projection", "cp_b:projection"),
    )

    assert len(claimed_a) + len(claimed_b) == 1
    claimed = (claimed_a or claimed_b)[0]
    assert claimed.claim_generation == 1
    assert claimed.attempt == 1


@pytest.mark.asyncio
async def test_expired_claim_is_reclaimed_with_generation_fencing(tmp_path: Path) -> None:
    started = datetime(2026, 9, 8, 16, 0, tzinfo=UTC)
    database = tmp_path / "consumer.db"
    store = ConsumerCheckpointStore(database, claim_lease_seconds=30)
    await store.initialize()
    await store.enqueue("projection", durable_event())

    old = (await store.claim_batch("projection", "cp_old:projection", now=started))[0]
    current = (
        await store.claim_batch(
            "projection",
            "cp_current:projection",
            now=started + timedelta(seconds=31),
        )
    )[0]

    assert current.claim_generation == old.claim_generation + 1
    assert current.attempt == 2
    assert await store.acknowledge(old, now=started + timedelta(seconds=32)) is False
    assert await store.acknowledge(current, now=started + timedelta(seconds=32)) is True
    checkpoint = await store.get_checkpoint("projection")
    assert checkpoint is not None
    assert checkpoint.last_event_id == current.event.event_id


@pytest.mark.asyncio
async def test_crashed_final_attempt_is_dead_lettered_on_recovery(tmp_path: Path) -> None:
    started = datetime(2026, 9, 8, 16, 0, tzinfo=UTC)
    store = ConsumerCheckpointStore(tmp_path / "consumer.db", claim_lease_seconds=10)
    await store.initialize()
    await store.enqueue("projection", durable_event(), max_attempts=1)
    claimed = await store.claim_batch("projection", "cp_crashed:projection", now=started)
    assert len(claimed) == 1

    recovered = await store.recover_expired_claims(
        "projection", now=started + timedelta(seconds=11)
    )

    assert recovered == {"retrying": 0, "dead_lettered": 1}
    assert (await store.list_dead_letters("projection"))[0]["last_error"] == (
        "LeaseExpired: handler did not acknowledge"
    )


@pytest.mark.asyncio
async def test_expired_nonfinal_attempt_returns_to_pending_on_recovery(tmp_path: Path) -> None:
    started = datetime(2026, 9, 8, 16, 0, tzinfo=UTC)
    store = ConsumerCheckpointStore(tmp_path / "consumer.db", claim_lease_seconds=10)
    await store.initialize()
    await store.enqueue("projection", durable_event(), max_attempts=2)
    await store.claim_batch("projection", "cp_crashed:projection", now=started)

    recovered = await store.recover_expired_claims(
        "projection", now=started + timedelta(seconds=11)
    )
    reclaimed = await store.claim_batch(
        "projection", "cp_restarted:projection", now=started + timedelta(seconds=11)
    )

    assert recovered == {"retrying": 1, "dead_lettered": 0}
    assert len(reclaimed) == 1
    assert reclaimed[0].attempt == 2
    assert reclaimed[0].claim_generation == 2


@pytest.mark.asyncio
async def test_require_process_once_surfaces_handler_lease_loss(tmp_path: Path) -> None:
    started = datetime(2026, 9, 8, 16, 0, tzinfo=UTC)
    clock = MutableClock(started)
    store = ConsumerCheckpointStore(
        tmp_path / "consumer.db",
        claim_lease_seconds=5,
        clock=clock,
    )

    async def handler(_event: DurableEvent, _context: ConsumerHandlerContext) -> None:
        clock.value = started + timedelta(seconds=6)

    consumer = MessageConsumer(
        store,
        consumer_group="projection",
        consumer_id="cp_slow:projection",
        handler=handler,
    )
    await consumer.initialize()
    await consumer.submit(durable_event())

    with pytest.raises(ConsumerLeaseLost, match="lost"):
        await consumer.require_process_once()
    assert await store.get_checkpoint("projection") is None


def test_consumer_identity_is_application_controlled_and_validated(tmp_path: Path) -> None:
    store = ConsumerCheckpointStore(tmp_path / "consumer.db")

    async def handler(_event: DurableEvent, _context: ConsumerHandlerContext) -> None:
        return None

    with pytest.raises(ValueError, match="consumer identity"):
        MessageConsumer(
            store,
            consumer_group="projection",
            consumer_id="worker supplied identity with spaces",
            handler=handler,
        )
