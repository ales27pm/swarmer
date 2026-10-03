"""Real paired dispatch for standalone tasks, general scope only, no provider calls."""

from __future__ import annotations

import hashlib
import json

import aiosqlite
import pytest

from app.models import AgentCreate, MemoryUpdate
from app.services.agent_dispatcher import AgentDispatchConflict
from app.services.memory_symbolic_contracts import SymbolicCatalog
from tests.test_memory_symbolic_store import memory, propose


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    [
        None,
        "input",
        "source",
        "after_claim",
        "missing_binding",
        "caller_empty_available",
        "caller_empty_omitted_budget",
    ],
)
async def test_manual_task_selection_is_general_and_fenced_through_result(
    client, paired_headers, test_app, change
):
    state = test_app.state.state_service
    item, source = await memory(state)
    observation = await propose(state, source.bindings)
    other, private_source = await memory(state, scope="project:unrelated")
    private = await propose(state, private_source.bindings, scope="project:unrelated")
    dispatcher = test_app.state.agent_dispatcher
    dispatcher.symbolic_catalogs = (SymbolicCatalog(namespace="software", scheme_id="engineering"),)
    agent = await state.register_agent(
        AgentCreate(name="reader", endpoint="http://127.0.0.1", skills=["workspace.list_dir"]),
        "test",
    )
    await state.heartbeat_agent(agent["id"], "online", agent["credential"])
    response = client.post("/tasks", headers=paired_headers, json={"input": "Inspect cache"})
    assert response.status_code == 201, response.text
    task_id = response.json()["id"]
    supplied_payload = {"path": "."}
    if change and change.startswith("caller_empty_"):
        async with aiosqlite.connect(state.db_path) as db:
            db.row_factory = aiosqlite.Row
            task = await (await db.execute("SELECT * FROM tasks WHERE id=?", (task_id,))).fetchone()
        identity = {
            key: task[key] for key in ("input", "mode", "source", "conversation_id", "created_at")
        }
        digest = hashlib.sha256(
            json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        supplied_payload.update(
            symbolic_context={
                "schema_version": "symbolic-context-v1",
                "evidence": [],
                "status": change.removeprefix("caller_empty_"),
                "grants_authority": False,
            },
            symbolic_context_binding={
                "task_id": task_id,
                "task_revision_sha256": digest,
                "catalogs": [{"namespace": "software", "scheme_id": "engineering"}],
            },
        )
    response = client.post(
        f"/tasks/{task_id}/dispatch",
        headers=paired_headers,
        json={"required_skill": "workspace.list_dir", "payload": supplied_payload},
    )
    assert response.status_code == 201, response.text
    queued = response.json()
    payload = queued["payload"]
    [evidence] = payload["symbolic_context"]["evidence"]
    assert evidence["proposal"] == observation.model_dump()
    assert private.proposal_id not in json.dumps(payload) and other["id"] not in json.dumps(payload)
    assert payload["path"] == "."
    assert set(payload["symbolic_context_binding"]) == {
        "task_id",
        "task_revision_sha256",
        "catalogs",
    }
    assert payload["symbolic_context_binding"]["task_id"] == task_id
    if change == "input":
        async with aiosqlite.connect(state.db_path) as db:
            await db.execute(
                "UPDATE tasks SET input=? WHERE id=?", ("Different requirement.", task_id)
            )
            await db.commit()
    if change == "source":
        await state.update_memory(item["id"], MemoryUpdate(content="Changed."), "test")
    if change == "missing_binding":
        async with aiosqlite.connect(state.db_path) as db:
            await db.execute(
                "UPDATE agent_jobs SET payload_json=? WHERE id=?",
                (json.dumps({"path": "."}), queued["id"]),
            )
            await db.commit()
    claimed = await dispatcher.claim(agent["id"])
    if change in {"input", "source", "missing_binding"}:
        assert claimed is None
        record = await dispatcher.get_job(queued["id"])
        assert record["status"] == "cancelled" and record["error"] == "symbolic_context_changed"
        return
    assert claimed and claimed["payload"] == payload
    if change == "after_claim":
        await state.delete_memory(item["id"], "test")
        with pytest.raises(AgentDispatchConflict, match="^symbolic_context_changed$"):
            await dispatcher.submit_result(
                agent["id"],
                claimed["id"],
                claimed["claim_token"],
                status="completed",
                result={"entries": []},
                error=None,
                lease_id=claimed["lease_id"],
                lease_generation=claimed["lease_generation"],
            )
        assert (await dispatcher.get_job(queued["id"]))["status"] == "cancelled"
    else:
        completed, first = await dispatcher.submit_result(
            agent["id"],
            claimed["id"],
            claimed["claim_token"],
            status="completed",
            result={"entries": []},
            error=None,
            lease_id=claimed["lease_id"],
            lease_generation=claimed["lease_generation"],
        )
        assert completed["status"] == "completed" and first
    async with aiosqlite.connect(state.db_path) as db:
        assert await (await db.execute("SELECT COUNT(*) FROM goal_model_calls")).fetchone() == (0,)


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["queue", "claim", "result"])
async def test_catalog_revocation_during_audit_rolls_back_transition(tmp_path, monkeypatch, stage):
    from app.models import TaskCreate, TaskRecord
    from app.services import agent_dispatcher as module
    from app.services.agent_dispatcher import AgentDispatcher
    from app.services.message_board import SQLiteMessageBoard
    from tests.test_memory_symbolic_store import service

    state = await service(tmp_path)
    _, source = await memory(state)
    await propose(state, source.bindings)
    task = await state.create_task(
        TaskRecord.new(TaskCreate(input="Inspect cache"), source="test-phone")
    )
    agent = await state.register_agent(
        AgentCreate(name="reader", endpoint="http://127.0.0.1", skills=["workspace.list_dir"]),
        "test",
    )
    await state.heartbeat_agent(agent["id"], "online", agent["credential"])
    dispatcher = AgentDispatcher(
        state.db_path,
        SQLiteMessageBoard(state.db_path),
        symbolic_catalogs=(SymbolicCatalog(namespace="software", scheme_id="engineering"),),
    )
    _queued = (
        None
        if stage == "queue"
        else await dispatcher.queue_job(task.id, "workspace.list_dir", {"path": "."})
    )
    claimed = await dispatcher.claim(agent["id"]) if stage == "result" else None

    async def snapshot():
        async with aiosqlite.connect(state.db_path) as db:
            return (
                await (
                    await db.execute("SELECT status FROM tasks WHERE id=?", (task.id,))
                ).fetchall(),
                await (
                    await db.execute(
                        "SELECT status,attempt_count,lease_id,result_json FROM agent_jobs"
                    )
                ).fetchall(),
            )

    before = await snapshot()
    target = {
        "queue": "agent.job.queued",
        "claim": "agent.job.claimed",
        "result": "agent.job.completed",
    }[stage]
    original = module.append_audit_event
    revoked = []

    async def revoke(db, event, *args, **kwargs):
        value = await original(db, event, *args, **kwargs)
        if event == target:
            dispatcher.symbolic_catalogs = ()
            revoked.append(event)
        return value

    monkeypatch.setattr(module, "append_audit_event", revoke)
    with pytest.raises(AgentDispatchConflict, match="^symbolic_context_changed$"):
        if stage == "queue":
            await dispatcher.queue_job(task.id, "workspace.list_dir", {"path": "."})
        elif stage == "claim":
            await dispatcher.claim(agent["id"])
        else:
            await dispatcher.submit_result(
                agent["id"],
                claimed["id"],
                claimed["claim_token"],
                status="completed",
                result={"entries": []},
                error=None,
                lease_id=claimed["lease_id"],
                lease_generation=claimed["lease_generation"],
            )
    assert revoked == [target]
    assert await snapshot() == before


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", [800, 100])
async def test_optional_transport_budget_omits_whole_evidence_or_reports_size(
    tmp_path, monkeypatch, limit
):
    from app.models import TaskCreate, TaskRecord
    from app.services import agent_dispatcher as module
    from app.services.agent_dispatcher import AgentDispatcher
    from app.services.message_board import SQLiteMessageBoard
    from tests.test_memory_symbolic_store import service

    state = await service(tmp_path)
    _, source = await memory(state)
    await propose(state, source.bindings)
    task = await state.create_task(
        TaskRecord.new(TaskCreate(input="Inspect cache"), source="test-phone")
    )
    dispatcher = AgentDispatcher(
        state.db_path,
        SQLiteMessageBoard(state.db_path),
        symbolic_catalogs=(SymbolicCatalog(namespace="software", scheme_id="engineering"),),
    )
    monkeypatch.setattr(module, "MAX_REMOTE_PAYLOAD_BYTES", limit)
    if limit == 100:
        with pytest.raises(AgentDispatchConflict, match="^job payload is too large$"):
            await dispatcher.queue_job(task.id, "workspace.list_dir", {"path": "."})
        async with aiosqlite.connect(state.db_path) as db:
            assert await (await db.execute("SELECT COUNT(*) FROM agent_jobs")).fetchone() == (0,)
        return
    queued = await dispatcher.queue_job(task.id, "workspace.list_dir", {"path": "."})
    assert queued["payload"]["path"] == "."
    assert queued["payload"]["symbolic_context"] == {
        "schema_version": "symbolic-context-v1",
        "status": "omitted_budget",
        "evidence": [],
        "grants_authority": False,
    }
    assert queued["payload"]["symbolic_context_binding"]["task_id"] == task.id
    assert len(json.dumps(queued["payload"], separators=(",", ":")).encode()) <= limit
