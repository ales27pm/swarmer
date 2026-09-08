import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import aiosqlite
import pytest
from app.services.message_board import DurableEvent

from app.services.outbox import OutboxService
from app.services.state_service import StateService


class BlockingExternalMessageBoard:
    """An external board double with no accidental deduplication safety net."""

    def __init__(self) -> None:
        self.record_calls: list[str] = []
        self.first_publication_started = asyncio.Event()
        self.release_first_publication = asyncio.Event()

    async def publish(self, event: DurableEvent) -> dict[str, Any]:
        self.record_calls.append(event.dedupe_key)
        if len(self.record_calls) == 1:
            self.first_publication_started.set()
            await self.release_first_publication.wait()
        return {"dedupe_key": event.dedupe_key}


async def enqueue_event(database: Path, *, dedupe_key: str = "task-race:queued") -> int:
    await StateService(database).initialize()
    async with aiosqlite.connect(database) as db:
        await db.execute("BEGIN IMMEDIATE")
        outbox_id = await OutboxService.enqueue_locked(
            db,
            aggregate_type="task",
            aggregate_id="task-race",
            topic="tasks.status",
            event_type="published",
            payload={"status": "queued", "task_id": "task-race"},
            dedupe_key=dedupe_key,
            task_id="task-race",
        )
        await db.commit()
    return outbox_id


async def outbox_row(database: Path, outbox_id: int) -> dict[str, Any]:
    async with aiosqlite.connect(database) as db:
        db.row_factory = aiosqlite.Row
        row = await (
            await db.execute("SELECT * FROM outbox_events WHERE id=?", (outbox_id,))
        ).fetchone()
    assert row is not None
    return dict(row)


@pytest.mark.asyncio
async def test_two_independent_drainers_do_not_publish_same_live_claim(tmp_path: Path) -> None:
    """A second process must not select a row while the first publisher owns it."""

    database = tmp_path / "state.db"
    await enqueue_event(database)
    board = BlockingExternalMessageBoard()
    drainer_a = OutboxService(database, board)
    drainer_b = OutboxService(database, board)

    first_drain = asyncio.create_task(drainer_a.drain(limit=1))
    await asyncio.wait_for(board.first_publication_started.wait(), timeout=2)
    try:
        second_result = await asyncio.wait_for(drainer_b.drain(limit=1), timeout=2)
    finally:
        board.release_first_publication.set()
        first_result = await asyncio.wait_for(first_drain, timeout=2)

    assert first_result["published"] == 1
    assert second_result["selected"] == 0
    assert board.record_calls == ["task-race:queued"]


@pytest.mark.asyncio
async def test_wrong_publishing_owner_cannot_mark_claim_published(tmp_path: Path) -> None:
    database = tmp_path / "state.db"
    outbox_id = await enqueue_event(database)
    now = datetime(2026, 9, 8, 16, 0, tzinfo=UTC)
    board = BlockingExternalMessageBoard()
    drainer_a = OutboxService(
        database,
        board,
        instance_id="publisher-a",
        publication_lease_seconds=30,
    )

    claimed = await drainer_a.claim_batch(limit=1, now=now)

    assert len(claimed) == 1
    generation = int(claimed[0]["publish_generation"])
    assert generation == 1
    assert claimed[0]["publishing_owner"] == "publisher-a"
    changed = await drainer_a.mark_published(
        outbox_id,
        owner_instance_id="publisher-b",
        publish_generation=generation,
        now=now + timedelta(seconds=1),
    )

    assert changed is False
    row = await outbox_row(database, outbox_id)
    assert row["published_at"] is None
    assert row["publishing_owner"] == "publisher-a"
    assert row["publish_generation"] == generation


@pytest.mark.asyncio
async def test_expired_claim_is_recovered_and_stale_generation_is_fenced(tmp_path: Path) -> None:
    database = tmp_path / "state.db"
    outbox_id = await enqueue_event(database)
    claimed_at = datetime(2026, 9, 8, 16, 0, tzinfo=UTC)
    board = BlockingExternalMessageBoard()
    drainer_a = OutboxService(
        database,
        board,
        instance_id="publisher-a",
        publication_lease_seconds=30,
    )
    drainer_b = OutboxService(
        database,
        board,
        instance_id="publisher-b",
        publication_lease_seconds=30,
    )

    first_claim = await drainer_a.claim_batch(limit=1, now=claimed_at)
    while_live = await drainer_b.claim_batch(
        limit=1,
        now=claimed_at + timedelta(seconds=29),
    )
    recovered = await drainer_b.claim_batch(
        limit=1,
        now=claimed_at + timedelta(seconds=31),
    )

    assert len(first_claim) == 1
    assert while_live == []
    assert len(recovered) == 1
    old_generation = int(first_claim[0]["publish_generation"])
    new_generation = int(recovered[0]["publish_generation"])
    assert new_generation == old_generation + 1
    assert recovered[0]["publishing_owner"] == "publisher-b"

    stale_mark = await drainer_a.mark_published(
        outbox_id,
        owner_instance_id="publisher-a",
        publish_generation=old_generation,
        now=claimed_at + timedelta(seconds=32),
    )
    current_mark = await drainer_b.mark_published(
        outbox_id,
        owner_instance_id="publisher-b",
        publish_generation=new_generation,
        now=claimed_at + timedelta(seconds=32),
    )

    assert stale_mark is False
    assert current_mark is True
    row = await outbox_row(database, outbox_id)
    assert row["published_at"] is not None
    assert row["publishing_owner"] is None
    assert row["publishing_lease_expires_at"] is None
    assert row["publish_generation"] == new_generation
