from __future__ import annotations

import json
from pathlib import Path

import aiosqlite
import pytest
from app.services.goal_project import GoalProjectService
from app.services.project_context import ProjectContextService
from app.services.remote_job_policy import RemoteJobPolicyError, validate_remote_job
from app.services.swarm_contracts import GoalCreateRequest, GoalStartRequest
from tests.test_evaluator_durable_context import _capsule
from tests.test_goal_code_application import _coding_manager
from tests.test_project_memory import _messages


def test_python_policy_accepts_complete_capsule_without_giving_it_execution_authority() -> None:
    capsule = _capsule()
    value = validate_remote_job(
        "code.generate_python",
        {"objective": "Create a local notes CLI.", "durable_context": capsule},
    )

    assert value["durable_context"] == capsule
    assert value["durable_context"] is not capsule
    assert set(value) == {"objective", "durable_context"}
    assert value["durable_context"]["experiences"]["items"][0]["applicability"] == "historical"


@pytest.mark.parametrize("value", [None, {}, {"command": "execute"}])
def test_python_policy_rejects_unqualified_capsule(value: object) -> None:
    with pytest.raises(RemoteJobPolicyError):
        validate_remote_job(
            "code.generate_python", {"objective": "Create a CLI.", "durable_context": value}
        )


def test_python_policy_rejects_combined_objective_and_capsule_byte_overflow() -> None:
    capsule = _capsule()
    capsule["requirements"][0]["text"] = "Retain this rule. " * 1_600
    assert len(json.dumps(capsule, ensure_ascii=False, separators=(",", ":")).encode()) < 32_000

    with pytest.raises(RemoteJobPolicyError, match="byte limit"):
        validate_remote_job(
            "code.generate_python", {"objective": "é" * 4_000, "durable_context": capsule}
        )


@pytest.mark.asyncio
async def test_python_dispatch_carries_requirement_outside_node_objective_and_recent_history(
    tmp_path: Path,
) -> None:
    manager, engine = await _coding_manager(tmp_path)
    manager.project_applications = GoalProjectService(manager.db_path, engine)
    manager.project_applications.context = ProjectContextService(manager.db_path)
    goal = await manager.create_goal(
        GoalCreateRequest(objective="Create a Python CRM prototype"), actor_id="test"
    )
    source_ids = await _messages(
        manager,
        str(goal["id"]),
        ["Never send email automatically."] + [f"Discuss section {index}." for index in range(45)],
    )

    detail = await manager.start_goal(str(goal["id"]), GoalStartRequest())

    async with aiosqlite.connect(manager.db_path) as db:
        row = await (
            await db.execute(
                "SELECT payload_json FROM agent_jobs WHERE id=?",
                (detail["nodes"][0]["worker_job_id"],),
            )
        ).fetchone()
    assert row is not None
    payload = json.loads(row[0])
    assert "Never send email automatically." not in payload["objective"]
    assert {"text": "Never send email automatically.", "source_id": source_ids[0]} in payload[
        "durable_context"
    ]["requirements"]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["invalid", "overflow", "unavailable"])
async def test_python_dispatch_blocks_unavailable_required_context_without_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    from app.services.project_context import ProjectContextConflict

    manager, engine = await _coding_manager(tmp_path)
    manager.project_applications = GoalProjectService(manager.db_path, engine)
    context = ProjectContextService(manager.db_path)
    manager.project_applications.context = context
    goal = await manager.create_goal(
        GoalCreateRequest(objective="Create a Python CRM prototype"), actor_id="test"
    )
    worker_payload = manager._worker_payload

    async def dispatch_with_changed_context(goal, node):
        if failure == "unavailable":

            async def unavailable(goal_id: str) -> dict[str, object]:
                raise ProjectContextConflict("The project context changed.")

            monkeypatch.setattr(context, "refresh", unavailable)
        else:
            capsule = _capsule()
            if failure == "invalid":
                capsule["requirements"][0]["source_id"] = "invalid source id"
            else:
                capsule["requirements"][0]["text"] = "Required text. " * 3_000
            monkeypatch.setattr(context, "prompt_state", lambda state: capsule)
        return await worker_payload(goal, node)

    monkeypatch.setattr(manager, "_worker_payload", dispatch_with_changed_context)

    detail = await manager.start_goal(str(goal["id"]), GoalStartRequest())

    assert detail["nodes"][0]["status"] == "blocked"
    assert detail["nodes"][0]["worker_job_id"] is None
    assert detail["goal"]["model_call_count"] == 1  # Planning only; no code generation.
    async with aiosqlite.connect(manager.db_path) as db:
        row = await (await db.execute("SELECT COUNT(*) FROM agent_jobs")).fetchone()
    assert row == (0,)
