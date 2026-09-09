from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from time import monotonic
from typing import Any

import aiosqlite
import pytest

from app.main import create_app
from app.models import TaskCreate, TaskRecord
from app.services.control_plane_instance import ControlPlaneInstanceService
from app.services.state_service import StateService
from app.services.websocket_notifications import WebSocketNotificationService
from app.settings import Settings

REPO_ROOT = Path(__file__).resolve().parents[2]


class MutableClock:
    def __init__(self, value: datetime) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value


class RecordingWebSocket:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []
        self.closed: list[int] = []
        self.received = asyncio.Event()

    async def send_json(self, event: dict[str, Any]) -> None:
        self.sent.append(event)
        self.received.set()

    async def close(self, *, code: int) -> None:
        self.closed.append(code)


class StalledWebSocket(RecordingWebSocket):
    def __init__(self) -> None:
        super().__init__()
        self.send_entered = asyncio.Event()

    async def send_json(self, event: dict[str, Any]) -> None:
        del event
        self.send_entered.set()
        await asyncio.Event().wait()


def make_app(tmp_path: Path, *, database: Path, name: str):
    return create_app(
        Settings(
            db_path=database,
            workspace_root=tmp_path / f"workspace-{name}",
            permissions_path=REPO_ROOT / "configs" / "permissions.yaml",
            websocket_io_timeout_seconds=0.1,
            websocket_notification_poll_seconds=0.01,
        )
    )


async def pair_device(app: Any, device_id: str) -> tuple[str, str]:
    auth = app.state.auth_service
    code = await auth.create_pairing_code()
    candidate = await auth.complete_pairing(code, device_id, device_id)
    assert candidate is not None
    assert (
        await auth.finalize_pairing(candidate.token, candidate.pairing_id, candidate.device_id)
        is not None
    )
    principal = await auth.authenticate_token(candidate.token)
    assert principal is not None
    return candidate.device_id, str(principal["session_id"])


@pytest.mark.asyncio
async def test_process_a_notification_reaches_process_b_owned_socket(tmp_path: Path) -> None:
    database = tmp_path / "state.db"
    process_a = make_app(tmp_path, database=database, name="a")
    process_b = make_app(tmp_path, database=database, name="b")

    async with (
        process_a.router.lifespan_context(process_a),
        process_b.router.lifespan_context(process_b),
    ):
        device_id, session_id = await pair_device(process_a, "phone-on-process-b")
        socket = RecordingWebSocket()
        connection_id = "connection-owned-by-process-b"
        assert await process_b.state.auth_service.activate_websocket_connection(
            device_id,
            session_id,
            connection_id,
        )
        process_b.state.websockets[device_id] = (socket, session_id, connection_id)

        task = await process_a.state.state_service.create_task(
            TaskRecord.new(
                TaskCreate(input="authoritative mutation on process A"),
                source=device_id,
            )
        )
        task_payload = task.model_dump(mode="json")
        # This authoritative mutation notification is emitted by A, whose
        # in-memory registry has no socket. B observes it only through the
        # durable SQLite relay.
        await process_a.state.broadcast(
            {
                "type": "task.updated",
                "payload": task_payload,
            }
        )
        await asyncio.wait_for(socket.received.wait(), timeout=1)

        assert socket.sent == [
            {
                "type": "task.updated",
                "payload": {
                    "id": task.id,
                    "conversation_id": None,
                    "status": "created",
                    "created_at": task_payload["created_at"],
                    "updated_at": task_payload["updated_at"],
                    "completed_at": None,
                    "refetch_required": True,
                },
            }
        ]
        assert process_a.state.websockets == {}


@pytest.mark.asyncio
async def test_distinct_device_fanout_is_concurrent_and_stalls_are_bounded(
    tmp_path: Path,
) -> None:
    app = make_app(tmp_path, database=tmp_path / "state.db", name="fanout")

    async with app.router.lifespan_context(app):
        first_id, first_session = await pair_device(app, "slow-phone-one")
        second_id, second_session = await pair_device(app, "slow-phone-two")
        fast_id, fast_session = await pair_device(app, "fast-phone")
        first = StalledWebSocket()
        second = StalledWebSocket()
        fast = RecordingWebSocket()

        connections = (
            (first_id, first_session, "connection-slow-one", first),
            (second_id, second_session, "connection-slow-two", second),
            (fast_id, fast_session, "connection-fast", fast),
        )
        for device_id, session_id, connection_id, socket in connections:
            assert await app.state.auth_service.activate_websocket_connection(
                device_id,
                session_id,
                connection_id,
            )
            app.state.websockets[device_id] = (socket, session_id, connection_id)

        started = monotonic()
        broadcast = asyncio.create_task(
            app.state.broadcast({"type": "agent.updated", "payload": {"agent_id": "agt_safe"}})
        )
        await asyncio.wait_for(fast.received.wait(), timeout=0.05)
        await asyncio.wait_for(broadcast, timeout=0.3)
        elapsed = monotonic() - started

        assert elapsed < 0.18, "stalled devices were timed out serially"
        assert len(fast.sent) == 1
        assert first.closed == [1011]
        assert second.closed == [1011]
        assert fast.closed == []
        assert first_id not in app.state.websockets
        assert second_id not in app.state.websockets
        assert app.state.websockets[fast_id][0] is fast


@pytest.mark.asyncio
async def test_targeted_notification_never_reaches_another_device(tmp_path: Path) -> None:
    app = make_app(tmp_path, database=tmp_path / "state.db", name="targeting")

    async with app.router.lifespan_context(app):
        target_id, target_session = await pair_device(app, "target-phone")
        other_id, other_session = await pair_device(app, "other-phone")
        target = RecordingWebSocket()
        other = RecordingWebSocket()
        for device_id, session_id, connection_id, socket in (
            (target_id, target_session, "connection-target", target),
            (other_id, other_session, "connection-other", other),
        ):
            assert await app.state.auth_service.activate_websocket_connection(
                device_id,
                session_id,
                connection_id,
            )
            app.state.websockets[device_id] = (socket, session_id, connection_id)

        await app.state.broadcast(
            {
                "type": "iphone.capability.requested",
                "payload": {
                    "request_id": "iphreq_target",
                    "capability_name": "iphone.location.current",
                    "expires_at": "2030-01-01T00:01:00+00:00",
                    "preview": {"arguments_redacted": True},
                },
            },
            device_id=target_id,
        )

        assert len(target.sent) == 1
        assert other.sent == []


@pytest.mark.asyncio
async def test_failed_old_socket_cannot_evict_replacement_registry_entry(
    tmp_path: Path,
) -> None:
    app = make_app(tmp_path, database=tmp_path / "state.db", name="replacement")

    async with app.router.lifespan_context(app):
        device_id, session_id = await pair_device(app, "replacement-phone")
        stalled = StalledWebSocket()
        replacement = RecordingWebSocket()
        old_connection_id = "connection-old"
        assert await app.state.auth_service.activate_websocket_connection(
            device_id,
            session_id,
            old_connection_id,
        )
        app.state.websockets[device_id] = (stalled, session_id, old_connection_id)

        broadcast = asyncio.create_task(
            app.state.broadcast(
                {"type": "agent.updated", "payload": {"agent_id": "agt_safe"}},
                device_id=device_id,
            )
        )
        await asyncio.wait_for(stalled.send_entered.wait(), timeout=0.1)

        # Mainline installation is serialized by the same device lock. This
        # direct registry swap isolates the conditional-eviction invariant:
        # completion of old I/O must compare identity before removing anything.
        replacement_entry = (replacement, session_id, "connection-replacement")
        app.state.websockets[device_id] = replacement_entry
        await asyncio.wait_for(broadcast, timeout=0.3)

        assert app.state.websockets[device_id] == replacement_entry
        assert stalled.closed == [1011]


@pytest.mark.asyncio
async def test_websocket_install_rediscovers_only_unconsumed_capability_requests(
    tmp_path: Path,
) -> None:
    app = make_app(tmp_path, database=tmp_path / "state.db", name="pending-capability")

    async with app.router.lifespan_context(app):
        device_id, session_id = await pair_device(app, "capability-phone")
        now = datetime.now(UTC)
        common = (
            "tsk_pending",
            "agt_pending",
            "job_pending",
            1,
            device_id,
            "iphone.location.current",
            "{}",
            "fingerprint",
            "digest",
            "approval",
            1,
            now.isoformat(),
            (now + timedelta(minutes=1)).isoformat(),
        )
        async with aiosqlite.connect(app.state.settings.db_path) as db:
            await db.execute(
                """
                INSERT INTO iphone_capability_requests(
                    id,task_id,requesting_agent_id,requesting_job_id,lease_generation,
                    device_id,capability_name,arguments_json,request_fingerprint,
                    action_digest,status,approval_id,request_audit_id,created_at,expires_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                ("iphreq_waiting", *common[:9], "waiting_approval", *common[9:]),
            )
            await db.execute(
                """
                INSERT INTO iphone_capability_requests(
                    id,task_id,requesting_agent_id,requesting_job_id,lease_generation,
                    device_id,capability_name,arguments_json,request_fingerprint,
                    action_digest,status,approval_id,request_audit_id,created_at,expires_at,
                    delivered_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    "iphreq_consumed",
                    *common[:7],
                    "fingerprint-consumed",
                    "digest-consumed",
                    "consumed",
                    "approval-consumed",
                    2,
                    now.isoformat(),
                    (now + timedelta(minutes=1)).isoformat(),
                    now.isoformat(),
                ),
            )
            await db.commit()

        socket = RecordingWebSocket()
        attempt_id = app.state.begin_websocket_attempt(device_id)
        assert await app.state.install_websocket(
            socket,
            device_id=device_id,
            session_id=session_id,
            attempt_id=attempt_id,
        )

        assert [event["type"] for event in socket.sent] == [
            "connected",
            "iphone.capability.requested",
        ]
        assert socket.sent[1]["payload"] == {
            "request_id": "iphreq_waiting",
            "capability_name": "iphone.location.current",
            "expires_at": (now + timedelta(minutes=1)).isoformat(),
            "preview": {"arguments_redacted": True},
        }
        assert set(socket.sent[1]["payload"]) == {
            "request_id",
            "capability_name",
            "expires_at",
            "preview",
        }


@pytest.mark.asyncio
async def test_checkpoint_failure_replays_only_the_safe_invalidation(tmp_path: Path) -> None:
    database = tmp_path / "state.db"
    app = make_app(tmp_path, database=database, name="checkpoint-replay")

    async with app.router.lifespan_context(app):
        service = WebSocketNotificationService(database, instance_id="test-replay-instance")
        await service.initialize()
        await service.publish(
            {
                "type": "approval.decided",
                "payload": {
                    "id": "apr_replay",
                    "status": "approved",
                    "user_note": "must never be persisted in the relay",
                },
            }
        )
        delivered: list[dict[str, Any]] = []
        original_advance = service._advance_checkpoint
        failed_once = False

        async def fail_after_first_send(notification_id: int) -> None:
            nonlocal failed_once
            if not failed_once:
                failed_once = True
                raise RuntimeError("simulated crash before checkpoint")
            await original_advance(notification_id)

        service._advance_checkpoint = fail_after_first_send  # type: ignore[method-assign]

        async def record(event: dict[str, Any], device_id: str | None) -> None:
            assert device_id is None
            delivered.append(event)

        with pytest.raises(RuntimeError, match="simulated crash"):
            await service.drain(record)
        assert await service.drain(record) == 1

        assert delivered == [
            {
                "type": "approval.decided",
                "payload": {
                    "id": "apr_replay",
                    "status": "approved",
                    "refetch_required": True,
                },
            },
            {
                "type": "approval.decided",
                "payload": {
                    "id": "apr_replay",
                    "status": "approved",
                    "refetch_required": True,
                },
            },
        ]


@pytest.mark.asyncio
async def test_cleanup_preserves_rows_needed_by_a_live_instance_and_removes_stopped_one(
    tmp_path: Path,
) -> None:
    database = tmp_path / "state.db"
    await StateService(database).initialize()
    now = datetime(2030, 1, 1, tzinfo=UTC)
    clock = MutableClock(now)
    first_instance = ControlPlaneInstanceService(
        database,
        version="0.11.0",
        instance_id="cp_cleanup_first",
        clock=clock,
    )
    second_instance = ControlPlaneInstanceService(
        database,
        version="0.11.0",
        instance_id="cp_cleanup_second",
        clock=clock,
    )
    await first_instance.start()
    await second_instance.start()
    first = WebSocketNotificationService(
        database,
        instance_id=first_instance.instance_id,
        clock=clock,
    )
    second = WebSocketNotificationService(
        database,
        instance_id=second_instance.instance_id,
        clock=clock,
    )
    await first.initialize()
    await second.initialize()
    await first.publish({"type": "task.updated", "payload": {"id": "tsk_retained"}})

    async def accept(event: dict[str, Any], device_id: str | None) -> None:
        del event, device_id

    assert await first.drain(accept) == 1
    assert await first.cleanup(stale_instance_seconds=60) == {
        "removed_checkpoints": 0,
        "removed_notifications": 0,
    }
    async with aiosqlite.connect(database) as db:
        assert (
            int(
                (
                    await (
                        await db.execute("SELECT COUNT(*) FROM websocket_notifications")
                    ).fetchone()
                )[0]
            )
            == 1
        )

    await second_instance.stop()
    assert await first.cleanup(stale_instance_seconds=60) == {
        "removed_checkpoints": 1,
        "removed_notifications": 1,
    }
    await first.close()
    async with aiosqlite.connect(database) as db:
        checkpoints = int(
            (
                await (
                    await db.execute("SELECT COUNT(*) FROM websocket_notification_checkpoints")
                ).fetchone()
            )[0]
        )
    assert checkpoints == 0


@pytest.mark.asyncio
async def test_crashed_checkpoint_is_reaped_and_resumer_gets_global_refetch(
    tmp_path: Path,
) -> None:
    database = tmp_path / "state.db"
    await StateService(database).initialize()
    clock = MutableClock(datetime(2030, 1, 1, tzinfo=UTC))
    active_instance = ControlPlaneInstanceService(
        database,
        version="0.11.0",
        instance_id="cp_active_after_crash",
        clock=clock,
    )
    crashed_instance = ControlPlaneInstanceService(
        database,
        version="0.11.0",
        instance_id="cp_eventually_resumes",
        clock=clock,
    )
    await active_instance.start()
    await crashed_instance.start()
    active = WebSocketNotificationService(
        database,
        instance_id=active_instance.instance_id,
        clock=clock,
    )
    crashed = WebSocketNotificationService(
        database,
        instance_id=crashed_instance.instance_id,
        clock=clock,
    )
    await active.initialize()
    await crashed.initialize()
    await active.publish({"type": "task.updated", "payload": {"id": "tsk_while_peer_crashed"}})

    async def accept(event: dict[str, Any], device_id: str | None) -> None:
        del event, device_id

    assert await active.drain(accept) == 1
    clock.value += timedelta(seconds=61)
    await active_instance.heartbeat()
    assert await active.cleanup(stale_instance_seconds=60) == {
        "removed_checkpoints": 1,
        "removed_notifications": 1,
    }

    # The formerly crashed process returns after its durable checkpoint and old
    # rows were safely reclaimed. It must reconcile state, not pretend it saw
    # or replay the old event.
    await crashed_instance.heartbeat()
    recovered: list[dict[str, Any]] = []

    async def record_recovery(event: dict[str, Any], device_id: str | None) -> None:
        assert device_id is None
        recovered.append(event)

    assert await crashed.drain(record_recovery) == 0
    assert recovered == [{"type": "sync.invalidated", "payload": {"refetch_required": True}}]


@pytest.mark.asyncio
async def test_stale_cleaner_cannot_delete_a_row_ahead_of_its_own_checkpoint(
    tmp_path: Path,
) -> None:
    database = tmp_path / "state.db"
    await StateService(database).initialize()
    clock = MutableClock(datetime(2030, 1, 1, tzinfo=UTC))
    first_instance = ControlPlaneInstanceService(
        database,
        version="0.11.0",
        instance_id="cp_stale_cleaner",
        clock=clock,
    )
    second_instance = ControlPlaneInstanceService(
        database,
        version="0.11.0",
        instance_id="cp_fresh_peer",
        clock=clock,
    )
    await first_instance.start()
    await second_instance.start()
    first = WebSocketNotificationService(
        database,
        instance_id=first_instance.instance_id,
        clock=clock,
    )
    second = WebSocketNotificationService(
        database,
        instance_id=second_instance.instance_id,
        clock=clock,
    )
    await first.initialize()
    await second.initialize()
    await second.publish({"type": "task.updated", "payload": {"id": "tsk_needed_by_stale_cleaner"}})

    second_observed: list[str] = []

    async def record_second(event: dict[str, Any], device_id: str | None) -> None:
        del device_id
        second_observed.append(str(event["payload"]["id"]))

    assert await second.drain(record_second) == 1
    clock.value += timedelta(seconds=61)
    await second_instance.heartbeat()

    # A's heartbeat is stale and B is at checkpoint 1. A is nevertheless the
    # active caller with checkpoint 0; its own floor must never be ignored.
    assert await first.cleanup(stale_instance_seconds=60) == {
        "removed_checkpoints": 0,
        "removed_notifications": 0,
    }
    first_observed: list[str] = []

    async def record_first(event: dict[str, Any], device_id: str | None) -> None:
        del device_id
        first_observed.append(str(event["payload"]["id"]))

    assert await first.drain(record_first) == 1
    assert first_observed == ["tsk_needed_by_stale_cleaner"]


@pytest.mark.asyncio
async def test_new_boot_starts_at_high_water_and_only_receives_new_events(
    tmp_path: Path,
) -> None:
    database = tmp_path / "state.db"
    await StateService(database).initialize()
    now = datetime(2030, 1, 1, tzinfo=UTC)
    old_instance = ControlPlaneInstanceService(
        database,
        version="0.11.0",
        instance_id="cp_old_boot",
        clock=lambda: now,
    )
    await old_instance.start()
    old = WebSocketNotificationService(
        database,
        instance_id=old_instance.instance_id,
        clock=lambda: now,
    )
    await old.initialize()
    await old.publish({"type": "task.updated", "payload": {"id": "tsk_before_restart"}})
    await old.close()
    await old_instance.stop()

    new_instance = ControlPlaneInstanceService(
        database,
        version="0.11.0",
        instance_id="cp_new_boot",
        clock=lambda: now,
    )
    await new_instance.start()
    new = WebSocketNotificationService(
        database,
        instance_id=new_instance.instance_id,
        clock=lambda: now,
    )
    await new.initialize()
    observed: list[str] = []

    async def record(event: dict[str, Any], device_id: str | None) -> None:
        del device_id
        observed.append(str(event["payload"]["id"]))

    assert await new.drain(record) == 0
    await new.publish({"type": "task.updated", "payload": {"id": "tsk_after_restart"}})
    assert await new.drain(record) == 1
    assert observed == ["tsk_after_restart"]
