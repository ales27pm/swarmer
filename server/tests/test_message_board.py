from pathlib import Path

import pytest

from app.services.message_board import MessageBoardService
from app.services.state_service import StateService


@pytest.mark.asyncio
async def test_sqlite_message_board_persists_lifecycle_events(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    await StateService(path).initialize()
    board = MessageBoardService(path)

    published = await board.publish("tasks.inbox", {"kind": "read"}, task_id="tsk_1")
    await board.claim(published["message_id"], topic="tasks.inbox", agent_id="agt_1")
    await board.heartbeat(published["message_id"], topic="agents.heartbeat", agent_id="agt_1")
    await board.ack(published["message_id"], topic="tasks.status", agent_id="agt_1")

    events = await board.list_events(after_id=0)
    assert [event["event_type"] for event in events] == [
        "published",
        "claimed",
        "heartbeat",
        "acked",
    ]
    assert events[0]["payload"] == {"kind": "read"}
    assert "payload_json" not in events[0]
