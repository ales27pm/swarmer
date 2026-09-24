from __future__ import annotations

import asyncio
import base64
import json
from pathlib import Path
from unittest.mock import AsyncMock

import aiosqlite
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.services.activity_service import (
    ActivityCursorError,
    ActivityCursorStale,
    ActivityEvidenceError,
    read_activity,
)
from tests.test_goal_runtime_recovery import _manager, _worker_plan
from tests.test_task_execution import seed


async def evidence(db_path: Path):
    manager = await _manager(db_path, _worker_plan())
    goal, children = await seed(manager)
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            """INSERT INTO goal_model_calls(id,goal_run_id,role,provider_source,model_id,
            input_digest,status,latency_ms,created_at,completed_at)
            VALUES('call_one',?,'planner','ubuntu_local','safe-model:9b',
            'private-prompt-digest','completed',1250,'2026-09-24T10:00:00+00:00',
            '2026-09-24T10:00:01.250+00:00')""",
            (goal["id"],),
        )
        await db.execute(
            """INSERT INTO tool_calls(id,task_id,tool_name,arguments_json,summary,risk,
            status,result_json,error,created_at,updated_at) VALUES('tool_one',?,
            'process.run','{"argv":["private-secret-command"]}','private-summary',
            'high','completed','{"stdout":"private-output"}','private-error',
            '2026-09-24T11:00:00+00:00','2026-09-24T11:00:03+00:00')""",
            (children[0],),
        )
        await db.execute("INSERT INTO coding_projects VALUES('project_one','now','now')")
        snapshot = {
            "files": [{"path": "private-file", "content": "private-source"}],
            "checks": [
                {
                    "command": ["python", "-m", "pytest", "-q"],
                    "status": "passed",
                    "exit_code": 0,
                    "duration_ms": 325,
                    "output": "private-check-output",
                }
            ],
            "message": "private-model-message",
        }
        await db.execute(
            """INSERT INTO project_revisions(id,project_id,goal_run_id,node_id,worker_job_id,
            revision,snapshot_json,sha256,created_at) VALUES('revision_one','project_one',?,
            'node_00','job_0',1,?,'private-sha','2026-09-24T12:00:00+00:00')""",
            (goal["id"], json.dumps(snapshot)),
        )
        await db.commit()
    return goal, children


@pytest.mark.asyncio
async def test_activity_projects_safe_correlated_evidence_without_writes(tmp_path: Path):
    path = tmp_path / "state.db"
    goal, children = await evidence(path)
    async with aiosqlite.connect(path) as db:
        before = [line async for line in db.iterdump()]
    root = await read_activity(path, "goal", goal["id"])
    assert root is not None
    assert {item.kind for item in root.items} == {
        "model_call",
        "worker_job",
        "tool_call",
        "project_revision",
        "project_check",
    }
    encoded = root.model_dump_json()
    for private in ("private-", "/home/user", "token=", "secret=", "raw-payload", "full-draft"):
        assert private not in encoded
    model = next(item for item in root.items if item.kind == "model_call")
    assert model.model_id == "safe-model:9b" and model.duration_ms == 1250
    check = next(item for item in root.items if item.kind == "project_check")
    assert check.duration_ms == 325 and check.detail.exit_code == 0
    tool = next(item for item in root.items if item.kind == "tool_call")
    assert tool.duration_ms is None and tool.started_at is None
    child = await read_activity(path, "task", children[1])
    assert child is not None and [item.id for item in child.items] == ["worker_job:job_1"]
    assert root.coverage.live_operations is False
    async with aiosqlite.connect(path) as db:
        assert [line async for line in db.iterdump()] == before


@pytest.mark.asyncio
async def test_activity_pagination_is_stable_and_scope_bound(tmp_path: Path):
    path = tmp_path / "state.db"
    goal, children = await evidence(path)
    all_items = await read_activity(path, "goal", goal["id"])
    first = await read_activity(path, "goal", goal["id"], limit=2)
    assert all_items is not None and first is not None and first.has_more
    ids = [item.id for item in first.items]
    cursor = first.next_cursor
    with pytest.raises(ActivityCursorError):
        await read_activity(path, "task", children[1], cursor=cursor)
    async with aiosqlite.connect(path) as db:
        await db.execute(
            """INSERT INTO tool_calls(id,task_id,tool_name,arguments_json,summary,risk,
            status,created_at,updated_at) VALUES('later',?,'workspace.list_dir','{}','safe',
            'low','queued','2020-01-01T00:00:00+00:00','2020-01-01T00:00:00+00:00')""",
            (children[0],),
        )
        await db.commit()
    while cursor:
        page = await read_activity(path, "goal", goal["id"], limit=2, cursor=cursor)
        assert page is not None
        ids.extend(item.id for item in page.items)
        cursor = page.next_cursor
    assert ids == [item.id for item in all_items.items]
    reconnected = await read_activity(path, "goal", goal["id"])
    assert reconnected is not None and "tool_call:later" in [i.id for i in reconnected.items]


@pytest.mark.asyncio
async def test_activity_isolates_unrelated_records_and_cursor_metadata(tmp_path: Path):
    path = tmp_path / "state.db"
    goal, _ = await evidence(path)
    async with aiosqlite.connect(path) as db:
        await db.execute(
            """INSERT INTO tasks(id,title,input,mode,source,status,priority,created_at,updated_at)
            VALUES('unrelated_task','private-title','private-input','chat','phone','created',0,
            '2026-09-24T14:00:00+00:00','2026-09-24T14:00:00+00:00')"""
        )
        await db.execute(
            """INSERT INTO tool_calls(id,task_id,tool_name,arguments_json,summary,risk,status,
            created_at,updated_at) VALUES('unrelated_tool','unrelated_task','workspace.read_text',
            '{}','private-summary','low','completed','2026-09-24T14:00:00+00:00',
            '2026-09-24T14:00:00+00:00')"""
        )
        await db.commit()
    page = await read_activity(path, "goal", goal["id"], limit=2)
    assert page is not None and page.next_cursor is not None
    decoded = base64.urlsafe_b64decode(page.next_cursor + "=" * (-len(page.next_cursor) % 4))
    assert b"unrelated" not in decoded and "unrelated" not in page.model_dump_json()
    root = await read_activity(path, "task", goal["root_task_id"])
    by_goal = await read_activity(path, "goal", goal["id"])
    assert root is not None and by_goal is not None
    assert root.items == by_goal.items
    separate = await read_activity(path, "task", "unrelated_task")
    assert separate is not None and [i.id for i in separate.items] == ["tool_call:unrelated_tool"]


@pytest.mark.asyncio
async def test_cancelled_scope_does_not_claim_stale_model_is_running(tmp_path: Path):
    path = tmp_path / "state.db"
    goal, children = await evidence(path)
    async with aiosqlite.connect(path) as db:
        await db.execute("UPDATE goal_runs SET status='cancelled' WHERE id=?", (goal["id"],))
        await db.execute(
            """UPDATE goal_model_calls SET status='started',completed_at=NULL,latency_ms=NULL,
            lease_expires_at='2099-01-01T00:00:00+00:00' WHERE id='call_one'"""
        )
        await db.execute(
            """UPDATE agent_jobs SET status='running',lease_expires_at='2099-01-01T00:00:00+00:00'
            WHERE id='job_1'"""
        )
        await db.commit()
    page = await read_activity(path, "goal", goal["id"])
    assert page is not None
    item = next(i for i in page.items if i.kind == "model_call")
    assert item.status == "recorded" and item.completed_at is None and item.duration_ms is None
    assert "état final non enregistré" in item.title
    child_page = await read_activity(path, "task", children[1])
    assert child_page is not None and child_page.items[0].status == "recorded"
    async with aiosqlite.connect(path) as db:
        row = await (
            await db.execute("SELECT status FROM goal_model_calls WHERE id='call_one'")
        ).fetchone()
        assert row == ("started",)


@pytest.mark.asyncio
async def test_refresh_updates_same_record_without_fabricating_duration(tmp_path: Path):
    path = tmp_path / "state.db"
    _, children = await evidence(path)
    async with aiosqlite.connect(path) as db:
        await db.execute(
            "UPDATE agent_jobs SET status='running',lease_expires_at='2099-01-01T00:00:00+00:00' WHERE id='job_1'"
        )
        await db.commit()
    before = await read_activity(path, "task", children[1])
    assert before is not None and before.items[0].status == "running"
    async with aiosqlite.connect(path) as db:
        await db.execute(
            "UPDATE agent_jobs SET status='completed',completed_at='2026-09-24T15:00:00+00:00' WHERE id='job_1'"
        )
        await db.commit()
    after = await read_activity(path, "task", children[1])
    assert after is not None and after.items[0].status == "completed"
    assert before.items[0].id == after.items[0].id
    assert before.items[0].recorded_at == after.items[0].recorded_at
    assert after.items[0].duration_ms is None


@pytest.mark.asyncio
async def test_deleted_fence_requires_fresh_page(tmp_path: Path):
    path = tmp_path / "state.db"
    goal, _ = await evidence(path)
    page = await read_activity(path, "goal", goal["id"], limit=1)
    assert page is not None and page.next_cursor
    async with aiosqlite.connect(path) as db:
        await db.execute("DELETE FROM tool_calls WHERE id='tool_one'")
        await db.commit()
    with pytest.raises(ActivityCursorStale):
        await read_activity(path, "goal", goal["id"], cursor=page.next_cursor)
    assert await read_activity(path, "goal", goal["id"]) is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("cursor", ["", "../private", "x" * 4097, "e30"])
async def test_bad_cursor_is_rejected(tmp_path: Path, cursor: str):
    with pytest.raises(ActivityCursorError):
        await read_activity(tmp_path / "never-created.db", "task", "task_one", cursor=cursor)
    assert not (tmp_path / "never-created.db").exists()


@pytest.mark.asyncio
async def test_private_model_identity_and_arbitrary_command_are_not_exposed(tmp_path: Path):
    path = tmp_path / "state.db"
    goal, _ = await evidence(path)
    async with aiosqlite.connect(path) as db:
        await db.execute(
            "UPDATE goal_model_calls SET model_id='Bearer private-key' WHERE id='call_one'"
        )
        await db.execute(
            """UPDATE project_revisions SET snapshot_json=json_set(snapshot_json,
            '$.checks[0].command',json('["curl","https://private-url?token=private-token"]'))"""
        )
        await db.commit()
    page = await read_activity(path, "goal", goal["id"])
    assert page is not None
    assert next(i for i in page.items if i.kind == "model_call").model_id is None
    assert next(i for i in page.items if i.kind == "project_check").detail.command is None
    assert "private-" not in page.model_dump_json()


@pytest.mark.asyncio
async def test_invalid_duration_fails_closed(tmp_path: Path):
    path = tmp_path / "state.db"
    goal, _ = await evidence(path)
    async with aiosqlite.connect(path) as db:
        await db.execute(
            "UPDATE project_revisions SET snapshot_json=json_set(snapshot_json,'$.checks[0].duration_ms',-1)"
        )
        await db.commit()
    with pytest.raises(ActivityEvidenceError):
        await read_activity(path, "goal", goal["id"])


@pytest.mark.asyncio
async def test_nonzero_exit_code_cannot_be_presented_as_success(tmp_path: Path):
    path = tmp_path / "state.db"
    goal, _ = await evidence(path)
    async with aiosqlite.connect(path) as db:
        await db.execute(
            "UPDATE project_revisions SET snapshot_json=json_set(snapshot_json,'$.checks[0].exit_code',1)"
        )
        await db.commit()
    with pytest.raises(ActivityEvidenceError):
        await read_activity(path, "goal", goal["id"])


@pytest.mark.asyncio
@pytest.mark.parametrize("lease", [None, "2020-01-01T00:00:00+00:00"])
async def test_missing_or_expired_lease_never_claims_live_execution(
    tmp_path: Path, lease: str | None
):
    path = tmp_path / "state.db"
    _, children = await evidence(path)
    async with aiosqlite.connect(path) as db:
        await db.execute(
            "UPDATE agent_jobs SET status='running',lease_expires_at=? WHERE id='job_1'", (lease,)
        )
        await db.commit()
    page = await read_activity(path, "task", children[1])
    assert page is not None and page.items[0].status == "recorded"


@pytest.mark.asyncio
async def test_check_receipts_share_revision_time_without_invented_execution_time(tmp_path: Path):
    path = tmp_path / "state.db"
    goal, _ = await evidence(path)
    async with aiosqlite.connect(path) as db:
        await db.execute(
            """UPDATE project_revisions SET snapshot_json=json_set(snapshot_json,'$.checks[1]',
            json('{"command":["npm","run","build"],"status":"failed","exit_code":1,"duration_ms":50}'))"""
        )
        await db.commit()
    result = await read_activity(path, "goal", goal["id"], limit=1)
    assert result is not None
    gathered = result.items[:]
    while result.next_cursor:
        result = await read_activity(path, "goal", goal["id"], limit=1, cursor=result.next_cursor)
        assert result is not None
        gathered.extend(result.items)
    checks = [item for item in gathered if item.kind == "project_check"]
    assert [check.detail.check_index for check in checks] == [1, 0]
    assert all(check.started_at is None and check.completed_at is None for check in checks)
    assert all(check.title.startswith("Reçu") for check in checks)
    assert checks[0].status == "failed" and checks[0].detail.command == ["npm", "run", "build"]


@pytest.mark.asyncio
async def test_cancellation_closes_read_without_writing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from app.services import activity_service

    path = tmp_path / "state.db"
    goal, _ = await evidence(path)
    original = activity_service._scope
    entered = asyncio.Event()
    release = asyncio.Event()

    async def paused(*args):
        entered.set()
        await release.wait()
        return await original(*args)

    async with aiosqlite.connect(path) as db:
        before = [line async for line in db.iterdump()]
    monkeypatch.setattr(activity_service, "_scope", paused)
    pending = asyncio.create_task(read_activity(path, "goal", goal["id"]))
    await entered.wait()
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    async with aiosqlite.connect(path, timeout=0) as db:
        await db.execute("BEGIN IMMEDIATE")
        assert [line async for line in db.iterdump()] == before
        await db.rollback()


def test_activity_api_auth_errors_and_no_business_reconciliation(
    test_app: FastAPI,
    client: TestClient,
    paired_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
):
    assert client.portal is not None
    goal, children = client.portal.call(seed, test_app.state.goal_manager)
    reconcile = AsyncMock(side_effect=AssertionError("activity read must not reconcile goals"))
    monkeypatch.setattr(test_app.state.goal_manager, "get_goal", reconcile)
    for path in (f"/goals/{goal['id']}/activity", f"/tasks/{goal['root_task_id']}/activity"):
        assert client.get(path).status_code == 401
        response = client.get(path, headers=paired_headers)
        assert (
            response.status_code == 200 and response.headers["cache-control"] == "private, no-store"
        )
        assert response.json()["coverage"]["mode"] == "persisted_records"
        assert client.get(path + "?cursor=e30", headers=paired_headers).status_code == 400
        assert client.get(path + "?limit=101", headers=paired_headers).status_code == 422
    assert client.get("/tasks/missing/activity", headers=paired_headers).status_code == 404
    response = client.get(f"/tasks/{children[1]}/activity", headers=paired_headers)
    assert [i["id"] for i in response.json()["items"]] == ["worker_job:job_1"]
    reconcile.assert_not_called()
