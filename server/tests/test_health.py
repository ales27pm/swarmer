import asyncio
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from pathlib import Path

import aiosqlite
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError

from app.main import create_app
from app.services.auth_service import AuthService
from app.services.outbox import OutboxService
from app.services.permission_policy import PermissionPolicyError
from app.settings import Settings


def test_health(client: TestClient) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_startup_maintenance_failure_stops_registered_instance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "state.db"
    app = create_app(
        Settings(
            db_path=database,
            workspace_root=tmp_path / "workspace",
            permissions_path=Path(__file__).resolve().parents[2] / "configs" / "permissions.yaml",
        )
    )

    async def fail_startup_maintenance() -> None:
        raise OSError("simulated startup maintenance failure")

    monkeypatch.setattr(
        app.state.agent_dispatcher,
        "quarantine_revoked_jobs",
        fail_startup_maintenance,
    )

    with (
        pytest.raises(OSError, match="startup maintenance"),
        TestClient(app, client=("127.0.0.1", 50_001)),
    ):
        pass

    with sqlite3.connect(database) as db:
        row = db.execute(
            "SELECT stopped_at FROM control_plane_instances WHERE instance_id=?",
            (app.state.control_plane_instance.instance_id,),
        ).fetchone()
    assert row is not None and row[0] is not None


@pytest.mark.asyncio
async def test_invalid_recurring_policy_does_not_strand_outbox_maintenance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "state.db"
    app = create_app(
        Settings(
            db_path=database,
            workspace_root=tmp_path / "workspace",
            permissions_path=Path(__file__).resolve().parents[2] / "configs" / "permissions.yaml",
        )
    )

    async with app.router.lifespan_context(app):
        async with aiosqlite.connect(database) as db:
            await db.execute("BEGIN IMMEDIATE")
            outbox_id = await OutboxService.enqueue_locked(
                db,
                aggregate_type="system",
                aggregate_id="maintenance-cycle-test",
                topic="system.status",
                event_type="maintenance.test",
                payload={"status": "pending"},
                dedupe_key="maintenance-cycle-test:pending",
            )
            await db.commit()

        policy = app.state.agent_dispatcher.permission_policy
        assert policy is not None

        async def fail_policy_reload(_path: Path) -> None:
            raise PermissionPolicyError("invalid replacement policy")

        async def quarantine_must_not_run() -> None:
            raise AssertionError("quarantine must not run against an invalid reload")

        completed_units: list[str] = []

        async def fail_reaper(*, maintenance_guard: object | None = None) -> dict[str, int]:
            del maintenance_guard
            raise OSError("simulated reaper failure")

        async def complete_capability_expiry(*, maintenance_guard: object | None = None) -> int:
            del maintenance_guard
            completed_units.append("capability-expiry")
            return 0

        async def fail_outbox_recovery(*, maintenance_guard: object | None = None) -> int:
            del maintenance_guard
            raise RuntimeError("simulated outbox recovery failure")

        async def complete_scoring(*, maintenance_guard: object | None = None) -> list[object]:
            del maintenance_guard
            completed_units.append("agent-scoring")
            return []

        monkeypatch.setattr(
            app.state.agent_dispatcher.worker_skill_policy,
            "reload_from_path",
            fail_policy_reload,
        )
        monkeypatch.setattr(
            app.state.agent_dispatcher,
            "quarantine_revoked_jobs",
            quarantine_must_not_run,
        )
        monkeypatch.setattr(app.state.agent_lease_reaper, "reap_expired", fail_reaper)
        monkeypatch.setattr(
            app.state.iphone_capability_service,
            "expire_requests",
            complete_capability_expiry,
        )
        monkeypatch.setattr(
            app.state.agent_dispatcher.outbox,
            "recover_expired_claims",
            fail_outbox_recovery,
        )
        monkeypatch.setattr(app.state.agent_scoring, "rebuild", complete_scoring)

        assert await app.state.run_maintenance_cycle(refresh_scores=True) is True

        async with aiosqlite.connect(database) as db:
            published = await (
                await db.execute("SELECT published_at FROM outbox_events WHERE id=?", (outbox_id,))
            ).fetchone()

    assert published is not None and published[0] is not None
    assert completed_units == ["capability-expiry", "agent-scoring"]


@pytest.mark.asyncio
async def test_live_websocket_session_is_closed_after_device_repair(tmp_path: Path) -> None:
    database = tmp_path / "state.db"
    app = create_app(
        Settings(
            db_path=database,
            workspace_root=tmp_path / "workspace",
            permissions_path=Path(__file__).resolve().parents[2] / "configs" / "permissions.yaml",
        )
    )

    class RecordingWebSocket:
        def __init__(self) -> None:
            self.sent: list[dict[str, object]] = []
            self.closed: list[int] = []

        async def send_json(self, event: dict[str, object]) -> None:
            self.sent.append(event)

        async def close(self, *, code: int) -> None:
            self.closed.append(code)

    async with app.router.lifespan_context(app):
        auth = app.state.auth_service
        old_code = await auth.create_pairing_code()
        old = await auth.complete_pairing(old_code, "repair-phone", "Old Phone")
        assert old is not None
        assert await auth.finalize_pairing(old.token, old.pairing_id, old.device_id) is not None
        old_principal = await auth.authenticate_token(old.token)
        assert old_principal is not None
        old_session_id = str(old_principal["session_id"])

        socket = RecordingWebSocket()
        connection_id = "conn_health_repair"
        assert await auth.activate_websocket_connection(
            old.device_id,
            old_session_id,
            connection_id,
        )
        app.state.websockets[old.device_id] = (socket, old_session_id, connection_id)

        replacement_code = await auth.create_pairing_code()
        replacement = await auth.complete_pairing(
            replacement_code,
            old.device_id,
            "Replacement Phone",
        )
        assert replacement is not None
        assert (
            await auth.finalize_pairing(
                replacement.token,
                replacement.pairing_id,
                replacement.device_id,
            )
            is not None
        )
        replacement_principal = await auth.authenticate_token(replacement.token)
        assert replacement_principal is not None
        assert replacement_principal["session_id"] != old_session_id

        await app.state.broadcast({"type": "task.updated", "payload": {"id": "tsk_after_repair"}})

        assert socket.sent == []
        assert socket.closed == [4401]
        assert old.device_id not in app.state.websockets


@pytest.mark.asyncio
async def test_device_repair_waits_for_paused_checked_websocket_send(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "state.db"
    app = create_app(
        Settings(
            db_path=database,
            workspace_root=tmp_path / "workspace",
            permissions_path=Path(__file__).resolve().parents[2] / "configs" / "permissions.yaml",
        )
    )
    send_entered = asyncio.Event()
    release_send = asyncio.Event()
    promotion_resolved = asyncio.Event()
    timeline: list[str] = []

    class PausedWebSocket:
        def __init__(self) -> None:
            self.sent: list[dict[str, object]] = []
            self.closed: list[int] = []

        async def send_json(self, event: dict[str, object]) -> None:
            # broadcast() has already checked the pairing generation when this
            # hook runs. Hold that exact former check/send race open.
            send_entered.set()
            await release_send.wait()
            self.sent.append(event)
            timeline.append("old-session-send")

        async def close(self, *, code: int) -> None:
            self.closed.append(code)

    async with app.router.lifespan_context(app):
        auth = app.state.auth_service
        old_code = await auth.create_pairing_code()
        old = await auth.complete_pairing(old_code, "repair-race-phone", "Old Phone")
        assert old is not None
        assert await auth.finalize_pairing(old.token, old.pairing_id, old.device_id) is not None
        old_principal = await auth.authenticate_token(old.token)
        assert old_principal is not None
        old_session_id = str(old_principal["session_id"])

        replacement_code = await auth.create_pairing_code()
        replacement = await auth.complete_pairing(
            replacement_code,
            old.device_id,
            "Replacement Phone",
        )
        assert replacement is not None
        other_code = await auth.create_pairing_code()
        other_candidate = await auth.complete_pairing(
            other_code,
            "unrelated-phone",
            "Unrelated Phone",
        )
        assert other_candidate is not None
        assert (
            await auth.finalize_pairing(
                other_candidate.token,
                other_candidate.pairing_id,
                other_candidate.device_id,
            )
            is not None
        )

        socket = PausedWebSocket()
        connection_id = "conn_paused_repair"
        assert await auth.activate_websocket_connection(
            old.device_id,
            old_session_id,
            connection_id,
        )
        app.state.websockets[old.device_id] = (socket, old_session_id, connection_id)
        first_broadcast = asyncio.create_task(
            app.state.broadcast({"type": "task.updated", "payload": {"id": "tsk_before_cutover"}})
        )
        await asyncio.wait_for(send_entered.wait(), timeout=2)
        assert (
            await auth.finalize_pairing(
                replacement.token,
                replacement.pairing_id,
                replacement.device_id,
            )
            is not None
        )

        # Use another AuthService instance to exercise the cross-process-style
        # lock path rather than relying on one Python object's state.
        replacement_auth = AuthService(
            database,
            pairing_pepper="independent-test-service-secret",
        )
        resolve_candidate = replacement_auth._candidate_device_id

        async def observe_candidate_resolution(token_hash: str) -> str | None:
            result = await resolve_candidate(token_hash)
            promotion_resolved.set()
            return result

        monkeypatch.setattr(
            replacement_auth,
            "_candidate_device_id",
            observe_candidate_resolution,
        )
        promotion = asyncio.create_task(replacement_auth.authenticate_token(replacement.token))
        await asyncio.wait_for(promotion_resolved.wait(), timeout=2)
        await asyncio.sleep(0)
        assert not promotion.done(), "session cutover passed a checked in-flight send"

        # The stalled send is scoped to repair-race-phone. A separate device's
        # cutover must not wait behind it.
        unrelated_principal = await asyncio.wait_for(
            replacement_auth.authenticate_token(other_candidate.token),
            timeout=2,
        )
        assert unrelated_principal is not None

        release_send.set()
        await first_broadcast
        replacement_principal = await asyncio.wait_for(promotion, timeout=2)
        timeline.append("replacement-cutover")
        assert replacement_principal is not None
        assert replacement_principal["session_id"] != old_session_id
        assert timeline == ["old-session-send", "replacement-cutover"]

        await app.state.broadcast({"type": "task.updated", "payload": {"id": "tsk_after_cutover"}})
        assert len(socket.sent) == 1
        assert socket.closed == [4401]
        assert old.device_id not in app.state.websockets


@pytest.mark.asyncio
async def test_multiple_slow_websockets_are_capped_and_cannot_block_device_repair(
    tmp_path: Path,
) -> None:
    database = tmp_path / "state.db"
    app = create_app(
        Settings(
            db_path=database,
            workspace_root=tmp_path / "workspace",
            permissions_path=Path(__file__).resolve().parents[2] / "configs" / "permissions.yaml",
            websocket_io_timeout_seconds=0.1,
        )
    )

    class NeverSettlingWebSocket:
        def __init__(self) -> None:
            self.domain_send_entered = asyncio.Event()
            self.domain_send_cancelled = asyncio.Event()
            self.close_entered = asyncio.Event()

        async def send_json(self, event: dict[str, object]) -> None:
            if event["type"] == "connected":
                return
            self.domain_send_entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                self.domain_send_cancelled.set()

        async def close(self, *, code: int) -> None:
            del code
            self.close_entered.set()
            await asyncio.Event().wait()

    async with app.router.lifespan_context(app):
        auth = app.state.auth_service
        old_code = await auth.create_pairing_code()
        old = await auth.complete_pairing(old_code, "wedged-phone", "Wedged Phone")
        assert old is not None
        assert await auth.finalize_pairing(old.token, old.pairing_id, old.device_id) is not None
        old_principal = await auth.authenticate_token(old.token)
        assert old_principal is not None

        replacement_code = await auth.create_pairing_code()
        replacement = await auth.complete_pairing(
            replacement_code,
            old.device_id,
            "Replacement Phone",
        )
        assert replacement is not None

        sockets = [NeverSettlingWebSocket() for _ in range(3)]
        for socket in sockets:
            attempt_id = app.state.begin_websocket_attempt(old.device_id)
            assert await app.state.install_websocket(
                socket,
                device_id=old.device_id,
                session_id=str(old_principal["session_id"]),
                attempt_id=attempt_id,
            )
        assert app.state.websockets == {
            old.device_id: (
                sockets[-1],
                str(old_principal["session_id"]),
                attempt_id,
            )
        }
        assert sockets[0].close_entered.is_set()
        assert sockets[1].close_entered.is_set()

        broadcast = asyncio.create_task(
            app.state.broadcast({"type": "task.updated", "payload": {"id": "tsk_wedged"}})
        )
        await asyncio.wait_for(sockets[-1].domain_send_entered.wait(), timeout=1)
        assert not sockets[0].domain_send_entered.is_set()
        assert not sockets[1].domain_send_entered.is_set()
        assert (
            await auth.finalize_pairing(
                replacement.token,
                replacement.pairing_id,
                replacement.device_id,
            )
            is not None
        )

        # Promotion waits for the bounded send cancellation, not the peer's
        # unbounded send or close implementation.
        promotion = asyncio.create_task(auth.authenticate_token(replacement.token))
        replacement_principal = await asyncio.wait_for(promotion, timeout=0.75)
        assert replacement_principal is not None
        assert replacement_principal["session_id"] != old_principal["session_id"]

        await asyncio.wait_for(broadcast, timeout=0.75)
        assert sockets[-1].domain_send_cancelled.is_set()
        assert sockets[-1].close_entered.is_set()
        assert old.device_id not in app.state.websockets


@pytest.mark.asyncio
async def test_durable_websocket_owner_caps_delivery_across_auth_instances(
    tmp_path: Path,
) -> None:
    database = tmp_path / "state.db"
    app = create_app(
        Settings(
            db_path=database,
            workspace_root=tmp_path / "workspace",
            permissions_path=Path(__file__).resolve().parents[2] / "configs" / "permissions.yaml",
            websocket_io_timeout_seconds=0.1,
        )
    )
    stale_send_entered = asyncio.Event()
    stale_close_entered = asyncio.Event()

    class StaleProcessWebSocket:
        async def send_json(self, event: dict[str, object]) -> None:
            del event
            stale_send_entered.set()
            await asyncio.Event().wait()

        async def close(self, *, code: int) -> None:
            del code
            stale_close_entered.set()
            await asyncio.Event().wait()

    async with app.router.lifespan_context(app):
        first_auth = app.state.auth_service
        old_code = await first_auth.create_pairing_code()
        old = await first_auth.complete_pairing(old_code, "cross-process-phone", "Phone")
        assert old is not None
        assert (
            await first_auth.finalize_pairing(old.token, old.pairing_id, old.device_id) is not None
        )
        old_principal = await first_auth.authenticate_token(old.token)
        assert old_principal is not None
        session_id = str(old_principal["session_id"])

        stale_socket = StaleProcessWebSocket()
        assert await first_auth.activate_websocket_connection(
            old.device_id,
            session_id,
            "connection-process-a",
        )
        app.state.websockets[old.device_id] = (
            stale_socket,
            session_id,
            "connection-process-a",
        )

        second_auth = AuthService(
            database,
            pairing_pepper="second-process-pairing-secret",
        )
        async with second_auth.serialize_device_session(old.device_id):
            assert await second_auth.activate_websocket_connection(
                old.device_id,
                session_id,
                "connection-process-b",
            )
        assert not await first_auth.is_websocket_connection_current(
            old.device_id,
            session_id,
            "connection-process-a",
        )
        assert await second_auth.is_websocket_connection_current(
            old.device_id,
            session_id,
            "connection-process-b",
        )

        replacement_code = await first_auth.create_pairing_code()
        replacement = await first_auth.complete_pairing(
            replacement_code,
            old.device_id,
            "Replacement Phone",
        )
        assert replacement is not None
        assert (
            await first_auth.finalize_pairing(
                replacement.token,
                replacement.pairing_id,
                replacement.device_id,
            )
            is not None
        )

        broadcast = asyncio.create_task(
            app.state.broadcast({"type": "task.updated", "payload": {"id": "tsk_cross_process"}})
        )
        await asyncio.wait_for(stale_close_entered.wait(), timeout=0.5)
        assert not stale_send_entered.is_set()

        # The stale socket's unbounded close is outside the session lock, so it
        # cannot add another timeout to bearer cutover on this or another process.
        replacement_principal = await asyncio.wait_for(
            second_auth.authenticate_token(replacement.token),
            timeout=0.5,
        )
        assert replacement_principal is not None
        assert replacement_principal["session_id"] != session_id
        await asyncio.wait_for(broadcast, timeout=0.5)


def test_control_plane_state_must_be_outside_tool_workspace(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    with pytest.raises(RuntimeError, match="db_path must be outside workspace_root"):
        create_app(Settings(db_path=workspace / "state.db", workspace_root=workspace))

    with pytest.raises(RuntimeError, match="vector_index_path must be outside workspace_root"):
        create_app(
            Settings(
                db_path=tmp_path / "state.db",
                workspace_root=workspace,
                vector_index_path=workspace / "vectors",
            )
        )


def test_weak_pairing_bootstrap_secret_is_rejected() -> None:
    with pytest.raises(ValidationError, match="high-entropy secret"):
        Settings(pairing_bootstrap_token=SecretStr("change-me"))


def test_remote_redis_requires_tls() -> None:
    with pytest.raises(ValidationError, match="TLS is required"):
        Settings(
            message_board_backend="redis",
            redis_url=SecretStr("redis://cache.example.invalid:6379/0"),
        )

    settings = Settings(
        message_board_backend="redis",
        redis_url=SecretStr("rediss://cache.example.invalid:6380/0"),
    )
    assert settings.redis_url.get_secret_value().startswith("rediss://")


def test_agent_offline_timeout_must_exceed_heartbeat_interval() -> None:
    with pytest.raises(ValidationError, match="shorter than the offline timeout"):
        Settings(agent_heartbeat_seconds=20, agent_offline_timeout_seconds=20)

    with pytest.raises(ValidationError, match="Redis URL is invalid"):
        Settings(
            message_board_backend="redis",
            redis_url=SecretStr("rediss://cache.example.invalid:6380/0?ssl_cert_reqs=none"),
        )


def test_redis_timeout_must_fit_inside_outbox_publication_lease() -> None:
    with pytest.raises(ValidationError, match="Redis operation timeout"):
        Settings(
            message_board_backend="redis",
            redis_url=SecretStr("rediss://cache.example.invalid:6380/0"),
            redis_operation_timeout_seconds=30,
            outbox_publication_lease_seconds=5,
        )


def test_status_vector_diagnostic_does_not_block_health(
    client: TestClient,
    test_app: FastAPI,
    paired_headers: dict[str, str],
) -> None:
    entered = threading.Event()
    release = threading.Event()

    class BlockingVectorProjection:
        @staticmethod
        def generation_age_seconds() -> float:
            entered.set()
            if not release.wait(timeout=5):
                raise TimeoutError("test did not release vector diagnostic")
            return 12.5

    # The route reads the active projection through application state so the
    # blocking diagnostic boundary can be exercised without requiring FAISS.
    test_app.state.vector_projection = BlockingVectorProjection()

    executor = ThreadPoolExecutor(max_workers=2)
    try:
        status_future = executor.submit(client.get, "/status", headers=paired_headers)
        assert entered.wait(timeout=2), "/status did not enter the vector diagnostic"
        health_future = executor.submit(client.get, "/health")
        try:
            health_response = health_future.result(timeout=1)
        except FutureTimeout:
            health_response = None
    finally:
        release.set()
        executor.shutdown(wait=True)

    status_response = status_future.result(timeout=2)
    assert health_response is not None, "/health was blocked by the vector diagnostic"
    assert status_response.status_code == 200
    assert status_response.json()["vector_generation_age_seconds"] == 12.5
    assert health_response.status_code == 200
