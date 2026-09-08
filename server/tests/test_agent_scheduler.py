from __future__ import annotations

from pathlib import Path

import aiosqlite
import pytest

from app.models import AgentCreate, TaskCreate, TaskRecord
from app.services.agent_dispatcher import AgentDispatcher
from app.services.agent_scheduler import SchedulerService
from app.services.message_board import MessageBoardService
from app.services.state_service import StateService


@pytest.mark.asyncio
async def test_scheduler_excludes_non_online_agents_and_is_stable(tmp_path: Path) -> None:
    state = StateService(tmp_path / "state.db")
    await state.initialize()
    created: list[dict[str, object]] = []
    for name in ("online-b", "online-a", "offline", "draining", "unverified"):
        created.append(
            await state.register_agent(
                AgentCreate(
                    name=name,
                    endpoint=f"http://127.0.0.1:{9000 + len(created)}",
                    skills=["workspace.list_dir"],
                    max_concurrency=2,
                ),
                "phone",
            )
        )
    for index, status in enumerate(("online", "online", "offline", "draining")):
        assert await state.heartbeat_agent(
            str(created[index]["id"]), status, str(created[index]["credential"])
        )

    ranked = await SchedulerService(state.db_path).rank_eligible_agents("workspace.list_dir")

    assert {agent["name"] for agent in ranked} == {"online-a", "online-b"}
    assert [agent["name"] for agent in ranked] == ["online-b", "online-a"]


@pytest.mark.asyncio
async def test_agent_registration_exposes_capacity_without_credential_hash(tmp_path: Path) -> None:
    state = StateService(tmp_path / "state.db")
    await state.initialize()
    registered = await state.register_agent(
        AgentCreate(
            name="reader",
            endpoint="http://127.0.0.1:9001",
            skills=["workspace.list_dir"],
            max_concurrency=4,
            capacity={"memory_mb": 2048},
        ),
        "phone",
    )
    fetched = await state.get_agent(str(registered["id"]))
    assert fetched is not None
    assert fetched["max_concurrency"] == 4
    assert fetched["active_jobs"] == 0
    assert fetched["capacity"] == {"memory_mb": 2048}
    assert "auth_token_hash" not in fetched


@pytest.mark.asyncio
async def test_scheduler_ranks_load_before_historical_score(tmp_path: Path) -> None:
    state = StateService(tmp_path / "state.db")
    await state.initialize()
    agents: dict[str, dict[str, object]] = {}
    for name in ("high-score-busy", "lower-score-idle"):
        agent = await state.register_agent(
            AgentCreate(
                name=name,
                endpoint=f"http://127.0.0.1:{9000 + len(agents)}",
                skills=["workspace.list_dir"],
                max_concurrency=2,
            ),
            "phone",
        )
        assert await state.heartbeat_agent(agent["id"], "online", agent["credential"])
        agents[name] = agent
    async with aiosqlite.connect(state.db_path) as db:
        await db.executemany(
            "INSERT INTO agent_scores(agent_id,sample_count,score,updated_at) VALUES(?,?,?,?)",
            [
                (agents["high-score-busy"]["id"], 10, 5.0, "2026-01-01T00:00:00+00:00"),
                (agents["lower-score-idle"]["id"], 10, 1.0, "2026-01-01T00:00:00+00:00"),
            ],
        )
        await db.commit()
    task = await state.create_task(TaskRecord.new(TaskCreate(input="read"), source="phone"))
    dispatcher = AgentDispatcher(state.db_path, MessageBoardService(state.db_path))
    await dispatcher.queue_job(task.id, "workspace.list_dir", {"path": "."})
    assert await dispatcher.claim(str(agents["high-score-busy"]["id"])) is not None

    ranked = await SchedulerService(state.db_path).rank_eligible_agents("workspace.list_dir")

    assert [candidate["name"] for candidate in ranked] == [
        "lower-score-idle",
        "high-score-busy",
    ]
