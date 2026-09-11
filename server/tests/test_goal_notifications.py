from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import aiosqlite
import pytest

from app.services.swarm_contracts import AutonomyProfile, GoalCreateRequest
from tests.test_websocket_notifications import RecordingWebSocket, make_app, pair_device


@pytest.mark.asyncio
@pytest.mark.parametrize("projection_failure", [False, True])
async def test_goal_maintenance_invalidates_other_process_mobile_after_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, projection_failure: bool
) -> None:
    database = tmp_path / "state.db"
    writer = make_app(tmp_path, database=database, name="writer")
    reader = make_app(tmp_path, database=database, name="reader")
    async with (
        writer.router.lifespan_context(writer),
        reader.router.lifespan_context(reader),
    ):
        device_id, session_id = await pair_device(writer, "goal-observer")
        socket = RecordingWebSocket()
        connection_id = "goal-reader-connection"
        assert await reader.state.auth_service.activate_websocket_connection(
            device_id, session_id, connection_id
        )
        reader.state.websockets[device_id] = (socket, session_id, connection_id)
        manager = writer.state.goal_manager
        goal = await manager.create_goal(
            GoalCreateRequest(
                objective="Inspect the workspace",
                autonomy_profile=AutonomyProfile.MANUAL,
                max_runtime_seconds=30,
            ),
            actor_id=device_id,
        )
        await manager._mark_start_requested(goal["id"])
        async with aiosqlite.connect(database) as db:
            await db.execute(
                "UPDATE goal_runs SET started_at=? WHERE id=?",
                ((datetime.now(UTC) - timedelta(seconds=31)).isoformat(), goal["id"]),
            )
            await db.commit()

        if projection_failure:

            async def fail_projection(*args: object, **kwargs: object) -> None:
                raise RuntimeError("projection unavailable after terminal commit")

            monkeypatch.setattr(manager, "_finalize_terminal_goal", fail_projection)

        await writer.state.run_maintenance_cycle(refresh_scores=False)
        terminal = await manager.graph.get_goal(goal["id"])
        assert terminal["status"] == "budget_exhausted"
        await asyncio.wait_for(socket.received.wait(), timeout=1)
        assert socket.sent == [{"type": "sync.invalidated", "payload": {"refetch_required": True}}]

        if not projection_failure:
            await writer.state.run_maintenance_cycle(refresh_scores=False)
            await reader.state.drain_websocket_notifications()
            assert len(socket.sent) == 1
