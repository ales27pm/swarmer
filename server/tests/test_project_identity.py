from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import aiosqlite
import pytest

from app.services.goal_conversation import GoalConversationService
from app.services.goal_manager import GoalManagerConflict
from app.services.swarm_contracts import GoalCreateRequest, GoalMessageRequest
from tests.test_goal_runtime_recovery import _manager, _worker_plan


async def _rows(path: Path) -> dict[str, list[Any]]:
    async with aiosqlite.connect(path) as db:
        return {
            table: await (await db.execute(f"SELECT * FROM {table} ORDER BY rowid")).fetchall()
            for table in (
                "coding_projects",
                "goal_project_links",
                "goal_runs",
                "tasks",
                "goal_conversations",
                "goal_conversation_links",
                "goal_messages",
                "idempotency_receipts",
                "project_revisions",
                "audit_events",
            )
        }


async def _identity(manager: Any, goal_id: str) -> str | None:
    async with aiosqlite.connect(manager.db_path) as db:
        row = await (
            await db.execute(
                "SELECT project_id FROM goal_project_links WHERE goal_run_id=?", (goal_id,)
            )
        ).fetchone()
    return str(row[0]) if row else None


@pytest.mark.asyncio
async def test_every_new_goal_has_atomic_identity_without_code_or_model(tmp_path: Path) -> None:
    manager = await _manager(tmp_path / "state.db", _worker_plan())
    assert manager.project_applications is None
    request = GoalCreateRequest(
        objective="Compare SQLite and JSON without creating files.", client_request_id="research"
    )
    first, replay = await asyncio.gather(
        *[manager.create_goal(request, actor_id="phone") for _ in range(2)]
    )
    project = await _identity(manager, first["id"])
    assert project is not None
    assert first["id"] == replay["id"]
    restarted = await _manager(manager.db_path, _worker_plan())
    assert (await restarted.create_goal(request, actor_id="phone"))["id"] == first["id"]
    assert await _identity(restarted, first["id"]) == project
    async with aiosqlite.connect(manager.db_path) as db:
        for table in ("coding_projects", "goal_project_links", "goal_runs"):
            assert await (await db.execute(f"SELECT COUNT(*) FROM {table}")).fetchone() == (1,)
        for table in (
            "project_revisions",
            "agent_jobs",
            "goal_model_calls",
            "project_memory_queries",
            "tool_calls",
        ):
            assert await (await db.execute(f"SELECT COUNT(*) FROM {table}")).fetchone() == (0,)
    independent = await manager.create_goal(
        request.model_copy(update={"client_request_id": "other"}), actor_id="phone"
    )
    assert await _identity(manager, independent["id"]) not in {None, project}


@pytest.mark.asyncio
async def test_failure_after_goal_insert_rolls_back_identity_and_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = await _manager(tmp_path / "state.db", _worker_plan())
    before = await _rows(manager.db_path)

    async def fail(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("conversation storage failed")

    monkeypatch.setattr(GoalConversationService, "create_locked", fail)
    with pytest.raises(RuntimeError, match="conversation storage"):
        await manager.create_goal(
            GoalCreateRequest(objective="Research storage", client_request_id="atomic"),
            actor_id="phone",
        )
    assert await _rows(manager.db_path) == before


@pytest.mark.asyncio
async def test_continuations_and_stale_url_share_active_project(tmp_path: Path) -> None:
    manager = await _manager(tmp_path / "state.db", _worker_plan())
    parent = await manager.create_goal(
        GoalCreateRequest(objective="Research storage"), actor_id="phone"
    )
    project = await _identity(manager, parent["id"])
    assert project is not None
    await manager.cancel_goal(parent["id"], actor_id="phone")
    request = GoalMessageRequest(message="Write the comparison", client_message_id="followup")
    child, replay = await asyncio.gather(
        *[manager.reply_goal(parent["id"], request, actor_id="phone") for _ in range(2)]
    )
    child_id = child["goal"]["id"]
    assert child_id != parent["id"] and replay["goal"]["id"] == child_id
    await manager.cancel_goal(child_id, actor_id="phone")
    grandchild = await manager.reply_goal(
        parent["id"], request.model_copy(update={"client_message_id": "next"}), actor_id="phone"
    )
    assert await _identity(manager, grandchild["goal"]["id"]) == project
    assert await _identity(manager, child_id) == project
    assert (await manager.conversation_messages(parent["id"]))["project_id"] == project


@pytest.mark.asyncio
@pytest.mark.parametrize("existing", [False, True])
async def test_explicit_reply_reconciles_only_coherent_legacy_lineage(
    tmp_path: Path, existing: bool
) -> None:
    manager = await _manager(tmp_path / "state.db", _worker_plan())
    parent = await manager.create_goal(
        GoalCreateRequest(objective="Research storage"), actor_id="phone"
    )
    await manager.cancel_goal(parent["id"], actor_id="phone")
    child = await manager.reply_goal(
        parent["id"],
        GoalMessageRequest(message="Write results", client_message_id="first"),
        actor_id="phone",
    )
    child_id = child["goal"]["id"]
    known = await _identity(manager, parent["id"])
    async with aiosqlite.connect(manager.db_path) as db:
        await db.execute("DELETE FROM goal_project_links WHERE goal_run_id=?", (child_id,))
        if not existing:
            await db.execute("DELETE FROM goal_project_links WHERE goal_run_id=?", (parent["id"],))
        await db.commit()
    await manager.cancel_goal(child_id, actor_id="phone")
    before = await _rows(manager.db_path)
    await manager.get_goal(parent["id"])
    await manager.conversation_messages(parent["id"])
    assert await _rows(manager.db_path) == before
    result = await manager.reply_goal(
        parent["id"],
        GoalMessageRequest(message="Add sources", client_message_id="second"),
        actor_id="phone",
    )
    project = await _identity(manager, result["goal"]["id"])
    assert project is not None
    assert await _identity(manager, parent["id"]) == await _identity(manager, child_id) == project
    if existing:
        assert project == known


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "corruption", ["foreign_project", "cycle", "foreign_parent", "active_model"]
)
async def test_ambiguous_or_active_legacy_identity_is_rejected_atomically(
    tmp_path: Path, corruption: str
) -> None:
    manager = await _manager(tmp_path / "state.db", _worker_plan())
    parent = await manager.create_goal(
        GoalCreateRequest(objective="Research storage"), actor_id="phone"
    )
    await manager.cancel_goal(parent["id"], actor_id="phone")
    child = await manager.reply_goal(
        parent["id"],
        GoalMessageRequest(message="Write results", client_message_id="first"),
        actor_id="phone",
    )
    child_id = child["goal"]["id"]
    await manager.cancel_goal(child_id, actor_id="phone")
    async with aiosqlite.connect(manager.db_path) as db:
        if corruption == "foreign_project":
            await db.execute("INSERT INTO coding_projects VALUES('foreign','now','now')")
            await db.execute(
                "INSERT OR REPLACE INTO goal_project_links VALUES(?,'foreign')", (child_id,)
            )
        elif corruption == "cycle":
            await db.execute(
                "UPDATE goal_conversation_links SET parent_goal_id=? WHERE goal_run_id=?",
                (child_id, parent["id"]),
            )
        elif corruption == "foreign_parent":
            await db.execute(
                "UPDATE goal_conversation_links SET parent_goal_id='missing' WHERE goal_run_id=?",
                (child_id,),
            )
        else:
            await db.execute("DELETE FROM goal_project_links")
            await db.execute(
                """INSERT INTO goal_model_calls(id,goal_run_id,role,provider_source,input_digest,status,created_at)
                VALUES('inflight',?,'planner','test','digest','started','now')""",
                (parent["id"],),
            )
        await db.commit()
    before = await _rows(manager.db_path)
    with pytest.raises(GoalManagerConflict, match="(project|lineage|active work)"):
        await manager.reply_goal(
            parent["id"],
            GoalMessageRequest(message="Continue", client_message_id="rejected"),
            actor_id="phone",
        )
    assert await _rows(manager.db_path) == before


@pytest.mark.asyncio
async def test_legacy_create_replay_does_not_migrate_and_identity_does_not_enable_local_files(
    tmp_path: Path,
) -> None:
    manager = await _manager(tmp_path / "state.db", _worker_plan())
    request = GoalCreateRequest(objective="Research storage", client_request_id="legacy")
    goal = await manager.create_goal(request, actor_id="phone")
    await manager.cancel_goal(goal["id"], actor_id="phone")
    before = await _rows(manager.db_path)
    with pytest.raises(GoalManagerConflict, match="existing project"):
        await manager.reply_goal(
            goal["id"],
            GoalMessageRequest(
                message="Continue locally", client_message_id="local", planning_mode="iphone_local"
            ),
            actor_id="phone",
        )
    assert await _rows(manager.db_path) == before
    async with aiosqlite.connect(manager.db_path) as db:
        await db.execute("DELETE FROM goal_project_links")
        await db.commit()
    before = await _rows(manager.db_path)
    restarted = await _manager(manager.db_path, _worker_plan())
    assert (await restarted.create_goal(request, actor_id="phone"))["id"] == goal["id"]
    assert await _rows(manager.db_path) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy_unlinked", [False, True])
async def test_legacy_python_is_retained_with_or_without_preexisting_identity(
    tmp_path: Path, legacy_unlinked: bool
) -> None:
    from app.services.goal_project import GoalProjectService
    from tests.test_goal_code_application import CONTENT, _prepared

    manager, engine, old_id, _ = await _prepared(tmp_path)
    manager.project_applications = GoalProjectService(manager.db_path, engine)
    known = await _identity(manager, old_id)
    assert known is not None
    await manager.cancel_goal(old_id, actor_id="phone")
    if legacy_unlinked:
        async with aiosqlite.connect(manager.db_path) as db:
            await db.execute("DELETE FROM goal_project_links WHERE goal_run_id=?", (old_id,))
            await db.commit()
    request = GoalMessageRequest(
        message="Continue the saved Python proposal", client_message_id="preserve"
    )
    child = await manager.reply_goal(old_id, request, actor_id="phone")
    child_id = child["goal"]["id"]
    project = await _identity(manager, child_id)
    if not legacy_unlinked:
        assert project == known
    assert project == await _identity(manager, old_id)
    latest = await manager.project_applications._latest(child_id)
    assert latest is not None and latest["goal_run_id"] == old_id
    preview = await manager.project_applications.get_project(child_id)
    assert preview is not None and preview["files"] == [{"path": "app.py", "content": CONTENT}]
    before = await _rows(manager.db_path)
    assert (await manager.reply_goal(old_id, request, actor_id="phone"))["goal"]["id"] == child_id
    assert await _rows(manager.db_path) == before
    await manager.cancel_goal(child_id, actor_id="phone")
    grandchild = await manager.reply_goal(
        old_id, request.model_copy(update={"client_message_id": "again"}), actor_id="phone"
    )
    assert await manager.project_applications._latest(grandchild["goal"]["id"]) == latest
    async with aiosqlite.connect(manager.db_path) as db:
        assert await (await db.execute("SELECT COUNT(*) FROM project_revisions")).fetchone() == (1,)
        source = await (
            await db.execute(
                "SELECT node_id,worker_job_id,content FROM goal_code_proposals WHERE goal_run_id=?",
                (old_id,),
            )
        ).fetchone()
        assert source == (latest["node_id"], latest["worker_job_id"], CONTENT)
    assert not (engine.workspace_root / "app.py").exists()


@pytest.mark.asyncio
async def test_invalid_legacy_proposal_rolls_back_reconciliation_and_reply(tmp_path: Path) -> None:
    from tests.test_goal_code_application import _prepared

    manager, _, goal_id, _ = await _prepared(tmp_path)
    await manager.cancel_goal(goal_id, actor_id="phone")
    async with aiosqlite.connect(manager.db_path) as db:
        await db.execute("DELETE FROM goal_project_links WHERE goal_run_id=?", (goal_id,))
        await db.execute(
            "UPDATE goal_code_proposals SET content='print(\"changed\")' WHERE goal_run_id=?",
            (goal_id,),
        )
        await db.commit()
    before = await _rows(manager.db_path)
    with pytest.raises(GoalManagerConflict, match="legacy proposal content"):
        await manager.reply_goal(
            goal_id,
            GoalMessageRequest(message="Continue", client_message_id="invalid"),
            actor_id="phone",
        )
    assert await _rows(manager.db_path) == before


@pytest.mark.asyncio
async def test_existing_files_survive_continuation_exactly(tmp_path: Path) -> None:
    from tests.test_goal_project_runtime import _project, _result

    manager, detail, agent = await _project(tmp_path)
    goal_id = detail["goal"]["id"]
    await _result(manager, agent, action="continue")
    assert manager.project_applications is not None
    revision = await manager.project_applications._latest(goal_id)
    assert revision is not None
    await manager.cancel_goal(goal_id, actor_id="phone")
    child = await manager.reply_goal(
        goal_id, GoalMessageRequest(message="Continue", client_message_id="files"), actor_id="phone"
    )
    assert await manager.project_applications._latest(child["goal"]["id"]) == revision


@pytest.mark.asyncio
async def test_legacy_running_worker_blocks_identity_change_but_normal_reply_remains_available(
    tmp_path: Path,
) -> None:
    from tests.test_goal_project_runtime import _project

    manager, detail, _ = await _project(tmp_path)
    goal_id = detail["goal"]["id"]
    accepted = await manager.reply_goal(
        goal_id,
        GoalMessageRequest(message="Preserve existing files", client_message_id="normal"),
        actor_id="phone",
    )
    assert accepted["goal"]["id"] == goal_id
    async with aiosqlite.connect(manager.db_path) as db:
        await db.execute("DELETE FROM goal_project_links WHERE goal_run_id=?", (goal_id,))
        await db.commit()
    before = await _rows(manager.db_path)
    with pytest.raises(GoalManagerConflict, match="active work"):
        await manager.reply_goal(
            goal_id,
            GoalMessageRequest(message="New guidance", client_message_id="legacy"),
            actor_id="phone",
        )
    assert await _rows(manager.db_path) == before


@pytest.mark.asyncio
async def test_read_side_ensure_project_does_not_create_identity_or_seed_files(
    tmp_path: Path,
) -> None:
    from app.services.goal_project import GoalProjectConflict, GoalProjectService
    from tests.test_goal_code_application import _prepared

    manager, engine, goal_id, _ = await _prepared(tmp_path)
    service = GoalProjectService(manager.db_path, engine)
    before = await _rows(manager.db_path)
    assert await service.ensure_project(goal_id) is not None
    assert await _rows(manager.db_path) == before
    async with aiosqlite.connect(manager.db_path) as db:
        await db.execute("DELETE FROM goal_project_links WHERE goal_run_id=?", (goal_id,))
        await db.commit()
    before = await _rows(manager.db_path)
    with pytest.raises(GoalProjectConflict, match="explicit continuation"):
        await service.ensure_project(goal_id)
    assert await service.get_project(goal_id) is None
    assert await _rows(manager.db_path) == before


@pytest.mark.asyncio
async def test_legacy_receipts_cannot_be_reassigned_to_a_fresh_project(tmp_path: Path) -> None:
    from tests.test_goal_project_runtime import _project, _result

    manager, detail, agent = await _project(tmp_path)
    goal_id = detail["goal"]["id"]
    await _result(manager, agent, action="continue")
    await manager.cancel_goal(goal_id, actor_id="phone")
    async with aiosqlite.connect(manager.db_path) as db:
        await db.execute("DELETE FROM goal_project_links WHERE goal_run_id=?", (goal_id,))
        await db.commit()
    before = await _rows(manager.db_path)
    with pytest.raises(GoalManagerConflict, match="recorded provenance"):
        await manager.reply_goal(
            goal_id,
            GoalMessageRequest(message="Continue", client_message_id="conflict"),
            actor_id="phone",
        )
    assert await _rows(manager.db_path) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("consumer", ["tool", "goal_memory", "project_memory", "compaction"])
async def test_inflight_consumers_prevent_legacy_identity_reconciliation(
    tmp_path: Path, consumer: str
) -> None:
    from app.services.project_compaction import COMPACTION_SCHEMA
    from tests.test_goal_project_runtime import _project

    manager, detail, _ = await _project(tmp_path)
    goal_id = detail["goal"]["id"]
    project_id = await _identity(manager, goal_id)
    await manager.cancel_goal(goal_id, actor_id="phone")
    async with aiosqlite.connect(manager.db_path) as db:
        if consumer == "tool":
            await db.execute(
                """INSERT INTO tool_calls(id,task_id,tool_name,arguments_json,summary,risk,status,created_at,updated_at)
                VALUES('pending_tool',?,'workspace.read_text','{}','Read file','low','running','now','now')""",
                (detail["goal"]["root_task_id"],),
            )
        elif consumer == "goal_memory":
            await db.execute(
                """INSERT INTO goal_memory_queries(id,project_id,goal_run_id,purpose,conversation_revision,provider_identity,
                logical_fingerprint,query_sha256,status,created_at,expires_at)
                VALUES('pending_query',?,?,'planner',0,'test','fingerprint','digest','started','now','later')""",
                (project_id, goal_id),
            )
        elif consumer == "project_memory":
            await db.execute(
                """INSERT INTO project_memory_queries(id,project_id,goal_run_id,node_id,conversation_revision,
                provider_identity,query_sha256,status,created_at,expires_at)
                VALUES('pending_query',?,?,?,0,'test','digest','started','now','later')""",
                (project_id, goal_id, detail["nodes"][0]["id"]),
            )
        else:
            await db.executescript(COMPACTION_SCHEMA)
            await db.execute(
                """INSERT INTO project_context_compactions(cache_key,project_id,goal_run_id,fingerprint,version,
                provider_identity,status,created_at) VALUES('pending',?,?,'fingerprint',1,'test','started','now')""",
                (project_id, goal_id),
            )
        await db.execute("DELETE FROM goal_project_links WHERE goal_run_id=?", (goal_id,))
        await db.commit()
    before = await _rows(manager.db_path)
    with pytest.raises(GoalManagerConflict, match="active work"):
        await manager.reply_goal(
            goal_id,
            GoalMessageRequest(message="Continue", client_message_id="inflight"),
            actor_id="phone",
        )
    assert await _rows(manager.db_path) == before


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "count,terminal,reject", [(129, True, True), (128, True, True), (128, False, False)]
)
async def test_lineage_bound_checks_successor_before_accepting_reply(
    tmp_path: Path, count: int, terminal: bool, reject: bool
) -> None:
    manager = await _manager(tmp_path / "state.db", _worker_plan())
    root = await manager.create_goal(
        GoalCreateRequest(objective="Research storage"), actor_id="phone"
    )
    await manager.cancel_goal(root["id"], actor_id="phone")
    async with aiosqlite.connect(manager.db_path) as db:
        db.row_factory = aiosqlite.Row
        goal = dict(
            await (await db.execute("SELECT * FROM goal_runs WHERE id=?", (root["id"],))).fetchone()
        )
        task = dict(
            await (
                await db.execute("SELECT * FROM tasks WHERE id=?", (root["root_task_id"],))
            ).fetchone()
        )
        conversation = (
            await (
                await db.execute(
                    "SELECT conversation_id FROM goal_conversation_links WHERE goal_run_id=?",
                    (root["id"],),
                )
            ).fetchone()
        )[0]
        parent_id = root["id"]
        for index in range(count - 1):
            goal["id"], task["id"] = f"goal_legacy_{index}", f"task_legacy_{index}"
            goal["root_task_id"] = task["id"]
            for table, row in (("tasks", task), ("goal_runs", goal)):
                await db.execute(
                    f"INSERT INTO {table}({','.join(row)}) VALUES({','.join('?' for _ in row)})",
                    tuple(row.values()),
                )
            await db.execute(
                "INSERT INTO goal_conversation_links VALUES(?,?,?)",
                (goal["id"], conversation, parent_id),
            )
            parent_id = goal["id"]
        await db.execute(
            "UPDATE goal_conversations SET active_goal_id=? WHERE id=?", (parent_id, conversation)
        )
        if not terminal:
            await db.execute(
                "UPDATE goal_runs SET status='planning',current_phase='planning' WHERE id=?",
                (parent_id,),
            )
        await db.execute("DELETE FROM goal_project_links")
        await db.commit()
    before = await _rows(manager.db_path)
    request = GoalMessageRequest(message="Continue", client_message_id="bounded-reply")
    if reject:
        with pytest.raises(GoalManagerConflict, match="reconciliation limit"):
            await manager.reply_goal(root["id"], request, actor_id="phone")
        assert await _rows(manager.db_path) == before
    else:
        result = await manager.reply_goal(root["id"], request, actor_id="phone")
        assert result["goal"]["id"] == parent_id
        after = await _rows(manager.db_path)
        assert len(after["goal_runs"]) == len(after["goal_project_links"]) == count


@pytest.mark.asyncio
async def test_divergent_unversioned_proposals_are_not_arbitrarily_selected(tmp_path: Path) -> None:
    import hashlib
    import json

    from tests.test_goal_code_application import _prepared

    manager, _, goal_id, node_id = await _prepared(tmp_path)
    await manager.cancel_goal(goal_id, actor_id="phone")
    async with aiosqlite.connect(manager.db_path) as db:
        db.row_factory = aiosqlite.Row
        proposal = dict(
            await (
                await db.execute("SELECT * FROM goal_code_proposals WHERE node_id=?", (node_id,))
            ).fetchone()
        )
        node = dict(
            await (await db.execute("SELECT * FROM plan_nodes WHERE id=?", (node_id,))).fetchone()
        )
        job = dict(
            await (
                await db.execute(
                    "SELECT * FROM agent_jobs WHERE id=?", (proposal["worker_job_id"],)
                )
            ).fetchone()
        )
        task = dict(
            await (await db.execute("SELECT * FROM tasks WHERE id=?", (job["task_id"],))).fetchone()
        )
        task["id"] = "task_second"
        content = "print('A different unversioned application')\n"
        proposal.update(
            node_id="node_second",
            worker_job_id="job_second",
            path=f"generated/{goal_id}/node_second/app.py",
            content=content,
            sha256=hashlib.sha256(content.encode()).hexdigest(),
        )
        node.update(id="node_second", worker_job_id="job_second", task_id="task_second")
        result = json.loads(job["result_json"])
        result["content"] = content
        job.update(id="job_second", task_id="task_second", result_json=json.dumps(result))
        for table, row in (
            ("tasks", task),
            ("agent_jobs", job),
            ("plan_nodes", node),
            ("goal_code_proposals", proposal),
        ):
            await db.execute(
                f"INSERT INTO {table}({','.join(row)}) VALUES({','.join('?' for _ in row)})",
                tuple(row.values()),
            )
        await db.commit()
    before = await _rows(manager.db_path)
    with pytest.raises(GoalManagerConflict, match="conflicting unversioned files"):
        await manager.reply_goal(
            goal_id,
            GoalMessageRequest(message="Continue", client_message_id="choose"),
            actor_id="phone",
        )
    assert await _rows(manager.db_path) == before


@pytest.mark.asyncio
async def test_reply_to_inert_python_proposal_keeps_source_without_requiring_cancellation(
    tmp_path: Path,
) -> None:
    from app.services.goal_project import GoalProjectService
    from tests.test_goal_code_application import CONTENT, _prepared

    manager, engine, goal_id, _ = await _prepared(tmp_path)
    manager.project_applications = GoalProjectService(manager.db_path, engine)
    current = await manager.graph.get_goal(goal_id)
    assert current is not None and current["current_phase"] == "code_proposal_ready"
    result = await manager.reply_goal(
        goal_id,
        GoalMessageRequest(
            message="Extend this saved proposal", client_message_id="proposal-followup"
        ),
        actor_id="phone",
    )
    assert result["goal"]["id"] == goal_id
    project = await manager.project_applications.get_project(goal_id)
    assert project is not None
    assert project["files"] == [{"path": "app.py", "content": CONTENT}]
    assert not (engine.workspace_root / "app.py").exists()


@pytest.mark.asyncio
async def test_busy_legacy_proposal_reply_rolls_back_without_accepting_or_cancelling(
    tmp_path: Path,
) -> None:
    from tests.test_goal_code_application import _prepared

    manager, _, goal_id, _ = await _prepared(tmp_path)
    goal = await manager.graph.get_goal(goal_id)
    assert goal is not None
    async with aiosqlite.connect(manager.db_path) as db:
        await db.execute(
            """INSERT INTO tool_calls(id,task_id,tool_name,arguments_json,summary,risk,status,created_at,updated_at)
            VALUES('active_file_read',?,'workspace.read_text','{}','Read file','low','running','now','now')""",
            (goal["root_task_id"],),
        )
        await db.commit()
    before = await _rows(manager.db_path)
    with pytest.raises(GoalManagerConflict, match="active work"):
        await manager.reply_goal(
            goal_id,
            GoalMessageRequest(message="Extend proposal", client_message_id="busy"),
            actor_id="phone",
        )
    assert await _rows(manager.db_path) == before
    async with aiosqlite.connect(manager.db_path) as db:
        assert await (
            await db.execute("SELECT status FROM tool_calls WHERE id='active_file_read'")
        ).fetchone() == ("running",)
