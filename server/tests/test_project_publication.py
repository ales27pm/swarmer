from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import aiosqlite
import pytest

from app.models import AgentCreate
from app.services.approval_gateway import ApprovalGateway
from app.services.evaluator_provider import DeterministicEvaluatorProvider
from app.services.execution_engine import AuthenticatedRequester, ExecutionEngine, ExecutionError
from app.services.goal_manager import GoalManager
from app.services.goal_project import GoalProjectConflict, GoalProjectService
from app.services.project_contracts import (
    ProjectFile,
    ProjectPayload,
    ProjectResult,
    ProjectWriteArguments,
    project_digest,
)
from app.services.project_publication import publish_project
from app.services.result_aggregator import summarize_untrusted_worker_output
from app.services.swarm_contracts import EvaluationDecision, GoalCreateRequest, GoalStartRequest
from tests.test_goal_runtime_recovery import _manager, _worker_plan

REQUESTER = AuthenticatedRequester(id="project-phone", name="Project phone")
FILES = [
    {"path": "app/main.py", "content": "def total(values):\n    return sum(values)\n"},
    {
        "path": "tests/test_app.py",
        "content": "from app.main import total\ndef test_total():\n    assert total([2,3]) == 5\n",
    },
    {"path": "README.md", "content": "Private project source marker. Run python -m pytest.\n"},
]


def project_result(**updates: Any) -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "action": "complete",
        "message": "Project checks passed.",
        "plan": ["Implement application", "Run tests"],
        "files": FILES,
        "checks": [
            {
                "command": ["python", "-m", "pytest", "tests"],
                "status": "passed",
                "exit_code": 0,
                "output": "1 passed",
                "duration_ms": 30,
            }
        ],
        "run_instructions": "Run python -m pytest.",
        "runtime": "python",
        "base_revision_id": None,
        "base_sha256": None,
        **updates,
    }


@pytest.mark.parametrize(
    "path",
    [
        "/etc/passwd",
        "../escape",
        "app//x.py",
        "app/./x.py",
        "app\\x.py",
        ".env",
        ".env.local",
        ".npmrc",
        "app/.git/config",
        "private.pem",
        "auth.json",
    ],
)
def test_project_rejects_unsafe_paths(path: str) -> None:
    with pytest.raises(ValueError):
        ProjectFile(path=path, content="inert")


@pytest.mark.parametrize(
    "files",
    [
        [{"path": "app.py", "content": "a"}, {"path": "APP.py", "content": "b"}],
        [{"path": "app", "content": "a"}, {"path": "app/main.py", "content": "b"}],
        [{"path": "app.py", "content": "é" * 32_001}],
    ],
)
def test_project_manifest_rejects_conflicts_and_byte_overflow(files: list[dict[str, str]]) -> None:
    with pytest.raises(ValueError):
        ProjectResult.model_validate(project_result(files=files))


@pytest.mark.parametrize(
    "updates",
    [
        {"checks": []},
        {"run_instructions": ""},
        {"plan": []},
        {
            "checks": [
                {
                    "command": ["python", "-m", "compileall", "."],
                    "status": "passed",
                    "exit_code": 0,
                    "output": "",
                    "duration_ms": 2,
                }
            ]
        },
    ],
)
def test_completion_cannot_be_only_a_model_claim(updates: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        ProjectResult.model_validate(project_result(**updates))


def test_project_digest_and_base_bind_every_file_and_preserve_multifile_imports() -> None:
    result = ProjectResult.model_validate(project_result())
    digest = project_digest(result.files)
    payload = {
        "objective": "Extend the application",
        "conversation": [],
        "files": FILES,
        "plan": [],
        "checks": [],
        "iteration": 2,
        "base_revision_id": "revision_1",
        "base_sha256": digest,
    }
    assert ProjectPayload.model_validate(payload).files[1].path == "tests/test_app.py"
    assert digest == project_digest(list(reversed(FILES)))
    with pytest.raises(ValueError, match="digest"):
        ProjectPayload.model_validate(
            {**payload, "files": [*FILES, {"path": "new.py", "content": "new"}]}
        )
    assert "Private project" not in summarize_untrusted_worker_output(project_result())


def test_atomic_publication_is_complete_and_never_overwrites(tmp_path: Path) -> None:
    manifest = ProjectWriteArguments(
        project_id="project_test",
        revision_id="revision_test",
        sha256=project_digest(FILES),
        files=FILES,
    )
    fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        receipt = publish_project(fd, manifest)
        assert receipt["sha256"] == project_digest(FILES)
        target = tmp_path / manifest.revision_id
        assert sorted(
            str(path.relative_to(target)) for path in target.rglob("*") if path.is_file()
        ) == sorted(file["path"] for file in FILES)
        for file in FILES:
            assert (target / file["path"]).read_text() == file["content"]
        with pytest.raises(FileExistsError):
            publish_project(fd, manifest)
        assert not list(tmp_path.glob(".project-stage-*"))
    finally:
        os.close(fd)


def test_failed_publication_exposes_no_partial_revision(tmp_path: Path) -> None:
    manifest = ProjectWriteArguments(
        project_id="project_test",
        revision_id="revision_test",
        sha256=project_digest(FILES),
        files=FILES,
    )
    fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with (
            patch(
                "app.services.project_publication._rename_exclusive",
                side_effect=OSError("disk failure"),
            ),
            pytest.raises(OSError),
        ):
            publish_project(fd, manifest)
        assert list(tmp_path.iterdir()) == []
    finally:
        os.close(fd)


async def prepared_project(
    tmp_path: Path,
) -> tuple[GoalManager, ExecutionEngine, str, dict[str, Any]]:
    objective = "Build a complete tested application"
    plan = _worker_plan(objective=objective)
    plan.nodes[0] = plan.nodes[0].model_copy(
        update={"required_skill": "code.build_project", "objective": objective}
    )
    manager = await _manager(
        tmp_path / "project.db",
        plan,
        evaluator=DeterministicEvaluatorProvider(
            EvaluationDecision(
                schema_version="1.0",
                status="done",
                reason_summary="Requested project saved with check receipts.",
                missing_requirements=[],
                invalid_results=[],
                suggested_new_nodes=[],
            )
        ),
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    engine = ExecutionEngine(manager.db_path, workspace, manager.permission_policy)
    manager.project_applications = GoalProjectService(manager.db_path, engine)
    goal = await manager.create_goal(GoalCreateRequest(objective=objective), actor_id=REQUESTER.id)
    detail = await manager.start_goal(goal["id"], GoalStartRequest())
    worker = await manager.state_service.register_agent(
        AgentCreate(
            name="project-builder", endpoint="https://worker.invalid", skills=["code.build_project"]
        ),
        REQUESTER.id,
    )
    await manager.state_service.heartbeat_agent(worker["id"], "online", worker["credential"])
    claimed = await manager.agent_dispatcher.claim(worker["id"])
    assert claimed is not None and claimed["required_skill"] == "code.build_project"
    job, _ = await manager.agent_dispatcher.submit_result(
        worker["id"],
        claimed["id"],
        claimed["claim_token"],
        status="completed",
        result=project_result(),
        error=None,
        lease_id=claimed["lease_id"],
        lease_generation=claimed["lease_generation"],
    )
    await manager.on_job_result(job)
    preview = await manager.project_applications.get_project(goal["id"])
    assert preview and preview["state"] == "ready", detail
    return manager, engine, goal["id"], preview


@pytest.mark.asyncio
async def test_project_review_publication_is_private_bound_and_one_use(tmp_path: Path) -> None:
    manager, engine, goal_id, preview = await prepared_project(tmp_path)
    service = manager.project_applications
    assert service is not None
    assert not list(engine.workspace_root.iterdir())
    assert "Private project" not in json.dumps(await manager.get_goal(goal_id))
    assert "Private project" not in json.dumps(await manager.state_service.bootstrap())
    calls = await asyncio.gather(
        *[
            service.apply(goal_id, preview["revision_id"], REQUESTER, preview["sha256"])
            for _ in range(2)
        ]
    )
    assert calls[0]["id"] == calls[1]["id"]
    call = calls[0]
    assert call["arguments"] == {"file_count": 3, "arguments_redacted": True}
    assert not list(engine.workspace_root.iterdir())
    approval = ApprovalGateway(manager.db_path)
    await approval.decide(call["approval_id"], "approve", actor_id=REQUESTER.id)
    executed = await engine.execute(call["id"])
    assert executed["status"] == "completed"
    target = engine.workspace_root / executed["result"]["path"]
    assert (target / "app/main.py").read_text() == FILES[0]["content"]
    changed = await service.synchronize(call["id"])
    assert changed == {goal_id}
    assert (await service.get_project(goal_id))["state"] == "applied"
    assert (await service.apply(goal_id, preview["revision_id"], REQUESTER, preview["sha256"]))[
        "id"
    ] == call["id"]


@pytest.mark.asyncio
async def test_project_review_rejects_stale_digest_and_newer_reply(tmp_path: Path) -> None:
    manager, engine, goal_id, preview = await prepared_project(tmp_path)
    service = manager.project_applications
    assert service is not None
    with pytest.raises(GoalProjectConflict):
        await service.apply(goal_id, preview["revision_id"], REQUESTER, "0" * 64)
    async with aiosqlite.connect(manager.db_path) as db:
        await db.execute(
            "UPDATE goal_runs SET conversation_revision=conversation_revision+1 WHERE id=?",
            (goal_id,),
        )
        await db.commit()
    with pytest.raises(GoalProjectConflict):
        await service.apply(goal_id, preview["revision_id"], REQUESTER, preview["sha256"])
    assert not list(engine.workspace_root.iterdir())


@pytest.mark.asyncio
async def test_project_publication_rejects_symlinked_host_parent(tmp_path: Path) -> None:
    manager, engine, goal_id, preview = await prepared_project(tmp_path)
    service = manager.project_applications
    assert service is not None
    outside = tmp_path / "outside"
    outside.mkdir()
    (engine.workspace_root / "generated").symlink_to(outside, target_is_directory=True)
    call = await service.apply(goal_id, preview["revision_id"], REQUESTER, preview["sha256"])
    await ApprovalGateway(manager.db_path).decide(
        call["approval_id"], "approve", actor_id=REQUESTER.id
    )
    with pytest.raises(ExecutionError):
        await engine.execute(call["id"])
    receipt = await engine.get(call["id"])
    assert receipt["status"] == "failed"
    assert list(outside.iterdir()) == []


@pytest.mark.asyncio
async def test_project_application_recovers_after_child_link_before_call(tmp_path: Path) -> None:
    manager, engine, goal_id, preview = await prepared_project(tmp_path)
    service = manager.project_applications
    assert service is not None
    with (
        patch.object(engine, "create_tool_call", new=AsyncMock(side_effect=OSError("interrupted"))),
        pytest.raises(OSError),
    ):
        await service.apply(goal_id, preview["revision_id"], REQUESTER, preview["sha256"])
    assert (await service.get_project(goal_id))["state"] == "ready"
    assert (await service.get_project(goal_id))["task_id"] is None
    tasks = await service.application_task_ids(goal_id)
    assert len(tasks) == 1
    recovered = GoalProjectService(manager.db_path, engine)
    call = await recovered.apply(goal_id, preview["revision_id"], REQUESTER, preview["sha256"])
    assert call["task_id"] == tasks[0]
    assert len(await recovered.application_task_ids(goal_id)) == 1


@pytest.mark.asyncio
async def test_cancelled_project_cannot_publish_a_prepared_revision(tmp_path: Path) -> None:
    manager, engine, goal_id, preview = await prepared_project(tmp_path)
    service = manager.project_applications
    assert service is not None
    call = await service.apply(goal_id, preview["revision_id"], REQUESTER, preview["sha256"])
    await manager.cancel_goal(goal_id, actor_id=REQUESTER.id)
    record = await engine.get(call["id"])
    assert record["status"] == "cancelled"
    assert not list(engine.workspace_root.iterdir())


@pytest.mark.asyncio
async def test_project_receipt_must_match_full_manifest(tmp_path: Path) -> None:
    manager, engine, goal_id, preview = await prepared_project(tmp_path)
    service = manager.project_applications
    assert service is not None
    call = await service.apply(goal_id, preview["revision_id"], REQUESTER, preview["sha256"])
    async with aiosqlite.connect(manager.db_path) as db:
        await db.execute(
            "UPDATE tool_calls SET status='completed',result_json=? WHERE id=?",
            (
                json.dumps(
                    {"path": "elsewhere", "sha256": preview["sha256"], "files": 3, "bytes": 1}
                ),
                call["id"],
            ),
        )
        await db.commit()
    assert await service.synchronize(call["id"]) == {goal_id}
    nodes = await manager.graph.list_nodes(goal_id)
    assert nodes[0]["status"] == "failed"
    assert not list(engine.workspace_root.iterdir())
