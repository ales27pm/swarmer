from dataclasses import replace
from pathlib import Path

import pytest

from app.models import AgentCreate, TaskCreate, TaskRecord
from app.services.agent_dispatcher import AgentDispatcher
from app.services.message_board import MessageBoardService
from app.services.permission_policy import PermissionPolicy
from app.services.state_service import StateService

REPO_ROOT = Path(__file__).resolve().parents[2]


def denied_worker_policy(skill: str) -> PermissionPolicy:
    base = PermissionPolicy.from_yaml(REPO_ROOT / "configs" / "permissions.yaml")
    worker_rules = dict(base.worker_skill_rules)
    worker_rules[skill] = replace(worker_rules[skill], decision="deny", auto_redistribute=False)
    return PermissionPolicy(
        protected_paths=base.protected_paths,
        process=base.process,
        tool_rules=base.tool_rules,
        capability_rules=base.capability_rules,
        worker_skill_rules=worker_rules,
    )


@pytest.mark.asyncio
async def test_only_one_agent_can_claim_one_job(tmp_path: Path) -> None:
    state = StateService(tmp_path / "state.db")
    await state.initialize()
    task = await state.create_task(TaskRecord.new(TaskCreate(input="read"), source="device"))
    first = await state.register_agent(
        AgentCreate(
            name="first",
            endpoint="http://127.0.0.1:1",
            skills=["workspace.list_dir"],
        ),
        "device",
    )
    second = await state.register_agent(
        AgentCreate(
            name="second",
            endpoint="http://127.0.0.1:2",
            skills=["workspace.list_dir"],
        ),
        "device",
    )
    assert await state.heartbeat_agent(first["id"], "online", first["credential"])
    assert await state.heartbeat_agent(second["id"], "online", second["credential"])
    dispatcher = AgentDispatcher(state.db_path, MessageBoardService(state.db_path))
    await dispatcher.queue_job(task.id, "workspace.list_dir", {"path": "."})

    claimed = await dispatcher.claim(first["id"])
    unavailable = await dispatcher.claim(second["id"])
    assert claimed is not None
    assert unavailable is None


@pytest.mark.asyncio
async def test_policy_revocation_prevents_claiming_an_already_queued_job(
    tmp_path: Path,
) -> None:
    state = StateService(tmp_path / "state.db")
    await state.initialize()
    task = await state.create_task(TaskRecord.new(TaskCreate(input="read"), source="device"))
    agent = await state.register_agent(
        AgentCreate(
            name="reader",
            endpoint="http://127.0.0.1:1",
            skills=["workspace.list_dir"],
        ),
        "device",
    )
    assert await state.heartbeat_agent(agent["id"], "online", agent["credential"])
    board = MessageBoardService(state.db_path)
    allowed = PermissionPolicy.from_yaml(REPO_ROOT / "configs" / "permissions.yaml")
    dispatcher = AgentDispatcher(state.db_path, board, permission_policy=allowed)
    job = await dispatcher.queue_job(task.id, "workspace.list_dir", {"path": "."})

    revoked_dispatcher = AgentDispatcher(
        state.db_path,
        board,
        permission_policy=denied_worker_policy("workspace.list_dir"),
    )

    assert await revoked_dispatcher.claim(agent["id"]) is None
    persisted = await revoked_dispatcher.get_job(job["id"])
    assert persisted is not None and persisted["status"] == "queued"
