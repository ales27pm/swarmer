import asyncio
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import aiosqlite
import pytest

from app.services.message_board import DurableEvent, MessageBoardUnavailableError
from app.services.outbox import OutboxService
from app.services.state_service import StateService


class RemoteAcknowledgingBoard:
    """Records every delivery attempt and acknowledges application-level duplicates."""

    def __init__(self) -> None:
        self.seen: set[str] = set()
        self.delivery_attempts: list[str] = []

    async def publish(self, event: DurableEvent) -> dict[str, Any]:
        await asyncio.sleep(0)
        key = event.dedupe_key
        duplicate = key in self.seen
        self.seen.add(key)
        self.delivery_attempts.append(key)
        return {"dedupe_key": key, "duplicate": duplicate}


class UnavailableExternalBoard:
    def __init__(self) -> None:
        self.calls = 0

    async def publish(self, event: DurableEvent) -> dict[str, Any]:
        del event
        self.calls += 1
        raise MessageBoardUnavailableError("external board unavailable")


class BlockingDedupeBoard(RemoteAcknowledgingBoard):
    """Blocks one publish so its local publication lease can expire."""

    def __init__(self) -> None:
        super().__init__()
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def publish(self, event: DurableEvent) -> dict[str, Any]:
        self.entered.set()
        await self.release.wait()
        return await super().publish(event)


async def initialize_outbox(database: Path, *, event_count: int = 1) -> list[int]:
    await StateService(database).initialize()
    identifiers: list[int] = []
    async with aiosqlite.connect(database) as db:
        await db.execute("BEGIN IMMEDIATE")
        for index in range(event_count):
            identifiers.append(
                await OutboxService.enqueue_locked(
                    db,
                    aggregate_type="task",
                    aggregate_id=f"task-{index}",
                    topic="tasks.status",
                    event_type="published",
                    payload={"status": "queued", "task_id": f"task-{index}"},
                    dedupe_key=f"task-{index}:queued",
                    task_id=f"task-{index}",
                )
            )
        await db.commit()
    return identifiers


@pytest.mark.asyncio
async def test_outbox_dedupe_key_is_bound_to_immutable_domain_event(tmp_path: Path) -> None:
    database = tmp_path / "state.db"
    await StateService(database).initialize()
    async with aiosqlite.connect(database) as db:
        await db.execute("BEGIN IMMEDIATE")
        original_id = await OutboxService.enqueue_locked(
            db,
            aggregate_type="task",
            aggregate_id="task-bound",
            topic="tasks.status",
            event_type="published",
            payload={"status": "queued"},
            dedupe_key="task-bound:queued",
            task_id="task-bound",
        )
        duplicate_id = await OutboxService.enqueue_locked(
            db,
            aggregate_type="task",
            aggregate_id="task-bound",
            topic="tasks.status",
            event_type="published",
            payload={"status": "queued"},
            dedupe_key="task-bound:queued",
            task_id="task-bound",
        )
        assert duplicate_id == original_id
        with pytest.raises(ValueError, match="dedupe key"):
            await OutboxService.enqueue_locked(
                db,
                aggregate_type="task",
                aggregate_id="task-bound",
                topic="tasks.status",
                event_type="failed",
                payload={"status": "failed"},
                dedupe_key="task-bound:queued",
                task_id="task-bound",
            )
        await db.rollback()


async def fetch_outbox_row(database: Path, outbox_id: int) -> dict[str, Any]:
    async with aiosqlite.connect(database) as db:
        db.row_factory = aiosqlite.Row
        row = await (
            await db.execute("SELECT * FROM outbox_events WHERE id=?", (outbox_id,))
        ).fetchone()
    assert row is not None
    return dict(row)


@pytest.mark.asyncio
async def test_publish_success_then_crash_republishes_with_same_application_dedupe(
    tmp_path: Path,
) -> None:
    database = tmp_path / "state.db"
    outbox_id = (await initialize_outbox(database))[0]
    started = datetime(2026, 9, 8, 16, 0, tzinfo=UTC)
    board = RemoteAcknowledgingBoard()
    first_process = OutboxService(
        database,
        board,
        instance_id="publisher-before-crash",
        publication_lease_seconds=30,
    )

    first_claim = (await first_process.claim_batch(limit=1, now=started))[0]
    first_ack = await first_process.publish_claimed(first_claim)
    # Simulate process death after the broker accepted the event but before
    # published_at was fenced into authoritative SQLite state.
    restarted_process = OutboxService(
        database,
        board,
        instance_id="publisher-after-restart",
        publication_lease_seconds=30,
    )
    reclaimed = await restarted_process.claim_batch(
        limit=1,
        now=started + timedelta(seconds=31),
    )

    assert first_ack["duplicate"] is False
    assert len(reclaimed) == 1
    assert reclaimed[0]["publish_generation"] == first_claim["publish_generation"] + 1
    second_ack = await restarted_process.publish_claimed(reclaimed[0])
    marked = await restarted_process.mark_published(
        outbox_id,
        owner_instance_id="publisher-after-restart",
        publish_generation=int(reclaimed[0]["publish_generation"]),
        now=started + timedelta(seconds=32),
    )

    assert second_ack["duplicate"] is True
    assert marked is True
    assert board.delivery_attempts == ["task-0:queued", "task-0:queued"]
    row = await fetch_outbox_row(database, outbox_id)
    assert row["published_at"] is not None


@pytest.mark.asyncio
async def test_restart_recovers_expired_publication_claim(tmp_path: Path) -> None:
    database = tmp_path / "state.db"
    outbox_id = (await initialize_outbox(database))[0]
    started = datetime(2026, 9, 8, 16, 0, tzinfo=UTC)
    board = RemoteAcknowledgingBoard()
    stopped_process = OutboxService(
        database,
        board,
        instance_id="stopped-publisher",
        publication_lease_seconds=30,
    )
    claimed = await stopped_process.claim_batch(limit=1, now=started)
    assert len(claimed) == 1

    restarted_process = OutboxService(
        database,
        board,
        instance_id="restart-publisher",
        publication_lease_seconds=30,
    )
    recovered_count = await restarted_process.recover_expired_claims(
        now=started + timedelta(seconds=31)
    )
    recovered_claim = await restarted_process.claim_batch(
        limit=1,
        now=started + timedelta(seconds=31),
    )

    assert recovered_count == 1
    assert len(recovered_claim) == 1
    assert recovered_claim[0]["id"] == outbox_id
    assert recovered_claim[0]["publishing_owner"] == "restart-publisher"
    assert recovered_claim[0]["publish_generation"] == claimed[0]["publish_generation"] + 1


@pytest.mark.asyncio
async def test_release_or_fail_keeps_row_pending_and_clears_live_owner(tmp_path: Path) -> None:
    database = tmp_path / "state.db"
    outbox_id = (await initialize_outbox(database))[0]
    now = datetime(2026, 9, 8, 16, 0, tzinfo=UTC)
    board = RemoteAcknowledgingBoard()
    publisher = OutboxService(
        database,
        board,
        instance_id="failing-publisher",
        publication_lease_seconds=30,
    )
    claimed = (await publisher.claim_batch(limit=1, now=now))[0]

    released = await publisher.release_or_fail(
        outbox_id,
        owner_instance_id="failing-publisher",
        publish_generation=int(claimed["publish_generation"]),
        error=OSError("redis://user:secret@example.invalid was unavailable"),
        now=now + timedelta(seconds=1),
    )

    assert released is True
    row = await fetch_outbox_row(database, outbox_id)
    assert row["published_at"] is None
    assert row["publishing_owner"] is None
    assert row["publishing_started_at"] is None
    assert row["publishing_lease_expires_at"] is None
    assert row["attempts"] == 1
    assert "secret" not in str(row["last_error"])


@pytest.mark.asyncio
async def test_stale_generation_cannot_release_newer_publication_claim(tmp_path: Path) -> None:
    database = tmp_path / "state.db"
    outbox_id = (await initialize_outbox(database))[0]
    started = datetime(2026, 9, 8, 16, 0, tzinfo=UTC)
    board = RemoteAcknowledgingBoard()
    old_publisher = OutboxService(
        database,
        board,
        instance_id="old-publisher",
        publication_lease_seconds=30,
    )
    new_publisher = OutboxService(
        database,
        board,
        instance_id="new-publisher",
        publication_lease_seconds=30,
    )
    old_claim = (await old_publisher.claim_batch(limit=1, now=started))[0]
    new_claim = (await new_publisher.claim_batch(limit=1, now=started + timedelta(seconds=31)))[0]

    released = await old_publisher.release_or_fail(
        outbox_id,
        owner_instance_id="old-publisher",
        publish_generation=int(old_claim["publish_generation"]),
        error=OSError("stale publisher failed"),
        now=started + timedelta(seconds=32),
    )

    assert released is False
    row = await fetch_outbox_row(database, outbox_id)
    assert row["publishing_owner"] == "new-publisher"
    assert row["publish_generation"] == new_claim["publish_generation"]
    assert row["attempts"] == 0
    assert row["last_error"] is None


@pytest.mark.asyncio
async def test_five_concurrent_drainers_publish_each_event_once(tmp_path: Path) -> None:
    database = tmp_path / "state.db"
    event_count = 25
    await initialize_outbox(database, event_count=event_count)
    board = RemoteAcknowledgingBoard()
    drainers = [
        OutboxService(
            database,
            board,
            instance_id=f"stress-publisher-{index}",
            publication_lease_seconds=30,
        )
        for index in range(5)
    ]

    results = await asyncio.gather(*(drainer.drain(limit=event_count) for drainer in drainers))

    assert sum(result["published"] for result in results) == event_count
    assert sum(result["failed"] for result in results) == 0
    assert Counter(board.delivery_attempts) == Counter(
        {f"task-{index}:queued": 1 for index in range(event_count)}
    )
    assert await drainers[0].pending_count() == 0


@pytest.mark.asyncio
async def test_external_outage_stops_batch_and_releases_unattempted_claims(tmp_path: Path) -> None:
    database = tmp_path / "state.db"
    event_count = 5
    event_ids = await initialize_outbox(database, event_count=event_count)
    board = UnavailableExternalBoard()
    outbox = OutboxService(database, board, instance_id="bounded-outage")

    result = await outbox.drain(limit=event_count)

    assert board.calls == 1
    assert result == {
        "selected": event_count,
        "published": 0,
        "failed": 1,
        "pending": event_count,
    }
    rows = [await fetch_outbox_row(database, event_id) for event_id in event_ids]
    assert [row["attempts"] for row in rows] == [1, 0, 0, 0, 0]
    assert all(row["publishing_owner"] is None for row in rows)


@pytest.mark.asyncio
async def test_publish_longer_than_lease_republishes_once_and_records_duplicate(
    tmp_path: Path,
) -> None:
    database = tmp_path / "state.db"
    outbox_id = (await initialize_outbox(database))[0]
    started = datetime(2026, 9, 8, 16, 0, tzinfo=UTC)
    board = BlockingDedupeBoard()
    old = OutboxService(
        database,
        board,
        instance_id="slow-publisher",
        publication_lease_seconds=5,
    )
    new = OutboxService(
        database,
        board,
        instance_id="takeover-publisher",
        publication_lease_seconds=5,
    )
    old_claim = (await old.claim_batch(limit=1, now=started))[0]
    slow_publish = asyncio.create_task(old.publish_claimed(old_claim))
    await asyncio.wait_for(board.entered.wait(), timeout=1)

    board.release.set()
    first_ack = await asyncio.wait_for(slow_publish, timeout=1)
    stale_mark = await old.mark_published(
        outbox_id,
        owner_instance_id=old.instance_id,
        publish_generation=int(old_claim["publish_generation"]),
        now=started + timedelta(seconds=6),
    )
    new_claim = (await new.claim_batch(limit=1, now=started + timedelta(seconds=6)))[0]
    second_ack = await new.publish_claimed(new_claim)
    marked = await new.mark_published(
        outbox_id,
        owner_instance_id=new.instance_id,
        publish_generation=int(new_claim["publish_generation"]),
        now=started + timedelta(seconds=7),
    )

    assert first_ack["duplicate"] is False
    assert stale_mark is False
    assert second_ack["duplicate"] is True
    assert marked is True
    assert board.delivery_attempts == ["task-0:queued", "task-0:queued"]
    restarted = OutboxService(
        database,
        board,
        instance_id="metrics-after-restart",
        publication_lease_seconds=5,
    )
    metrics = await restarted.metrics()
    assert metrics["outbox_duplicate_publications"] == 1
    assert metrics["outbox_claim_expirations"] == 1
    assert metrics["outbox_publish_latency_ms_count"] == 2
    assert metrics["outbox_publish_latency_ms_total"] >= 0
    assert metrics["outbox_publish_latency_ms_max"] >= 0


@pytest.mark.asyncio
async def test_recover_expired_claim_records_one_durable_expiration_metric(
    tmp_path: Path,
) -> None:
    database = tmp_path / "state.db"
    await initialize_outbox(database)
    started = datetime(2026, 9, 8, 16, 0, tzinfo=UTC)
    board = RemoteAcknowledgingBoard()
    stopped = OutboxService(
        database,
        board,
        instance_id="stopped-publisher",
        publication_lease_seconds=5,
    )
    assert len(await stopped.claim_batch(limit=1, now=started)) == 1

    restarted = OutboxService(
        database,
        board,
        instance_id="restarted-publisher",
        publication_lease_seconds=5,
    )
    assert await restarted.recover_expired_claims(now=started + timedelta(seconds=6)) == 1
    assert await restarted.recover_expired_claims(now=started + timedelta(seconds=7)) == 0

    metrics = await OutboxService(
        database,
        board,
        instance_id="metrics-reader",
    ).metrics()
    assert metrics["outbox_claim_expirations"] == 1
    assert metrics["outbox_duplicate_publications"] == 0
    assert metrics["outbox_publish_latency_ms_count"] == 0


@pytest.mark.asyncio
async def test_metrics_ignore_unbounded_append_only_audit_history(tmp_path: Path) -> None:
    database = tmp_path / "state.db"
    await initialize_outbox(database)
    misleading_rows = [
        (
            "outbox.publication.claim_expired",
            '{"duplicate":true,"latency_ms":999999}',
            "2026-09-08T16:00:00+00:00",
        )
        for _ in range(5_000)
    ]
    misleading_rows.extend(
        (
            "outbox.publication.acknowledged",
            '{"duplicate":true,"latency_ms":999999}',
            "2026-09-08T16:00:00+00:00",
        )
        for _ in range(5_000)
    )
    async with aiosqlite.connect(database) as db:
        await db.executemany(
            "INSERT INTO audit_events(event_type,payload_json,created_at) VALUES(?,?,?)",
            misleading_rows,
        )
        await db.commit()

    metrics = await OutboxService(
        database,
        RemoteAcknowledgingBoard(),
        instance_id="bounded-metrics-reader",
    ).metrics()

    assert metrics["outbox_claim_expirations"] == 0
    assert metrics["outbox_duplicate_publications"] == 0
    assert metrics["outbox_publish_latency_ms_count"] == 0
    assert metrics["outbox_publish_latency_ms_total"] == 0
    assert metrics["outbox_publish_latency_ms_max"] == 0
