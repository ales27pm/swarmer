import copy
import hashlib
import importlib.util
import json
import sqlite3
from pathlib import Path

import pytest

from app.models import AgentCreate, TaskCreate, TaskRecord
from app.services.agent_dispatcher import AgentDispatchConflict, AgentDispatcher
from app.services.execution_engine import ExecutionEngine, ExecutionError
from app.services.message_board import MessageBoardService
from app.services.result_aggregator import validate_worker_evidence
from app.services.state_service import StateService
from tests.test_execution_engine import REQUESTER, permission_policy

ROOT = Path(__file__).resolve().parents[2]


def worker():
    spec = importlib.util.spec_from_file_location(
        "read_receipt_file_worker", ROOT / "workers/file-worker/file_worker.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def expected(text, *, path="notes.txt", provenance="worker_reported_measurement", truncated=False):
    raw = text.encode("utf-8")
    return {
        "schema_version": "workspace-read-v1",
        "path": path,
        "encoding": "utf-8",
        "byte_scope": "returned_utf8_text",
        "returned_bytes": len(raw),
        "returned_sha256": hashlib.sha256(raw).hexdigest(),
        "complete": not truncated,
        "truncated": truncated,
        "provenance": provenance,
        "source_snapshot": "not_captured",
        "execution_attested": False,
    }


@pytest.mark.asyncio
async def test_real_local_read_and_utf8_cut_measure_only_returned_bytes(tmp_path):
    engine = ExecutionEngine(tmp_path / "unused.db", tmp_path, permission_policy())
    text = "é\r\n🍁"
    (tmp_path / "notes.txt").write_bytes(text.encode())
    full = await engine._dispatch("workspace.read_text", {"path": "./notes.txt"})
    assert full == {
        "text": text,
        "truncated": False,
        "read_receipt": expected(text, provenance="local_executor_measurement"),
    }
    engine.MAX_READ_BYTES = 5  # Withholds the incomplete emoji; no replacement character.
    short = await engine._dispatch("workspace.read_text", {"path": "notes.txt"})
    assert short == {
        "text": "é\r\n",
        "truncated": True,
        "read_receipt": expected("é\r\n", provenance="local_executor_measurement", truncated=True),
    }
    engine.MAX_READ_BYTES = 131_072
    (tmp_path / "notes.txt").write_bytes(b"not utf8\xff")
    with pytest.raises(ExecutionError, match="UTF-8"):
        await engine._dispatch("workspace.read_text", {"path": "notes.txt"})


def test_real_worker_receipt_is_not_attestation_and_preserves_crlf(tmp_path):
    text = "é\r\n🍁"
    (tmp_path / "notes.txt").write_bytes(text.encode())
    result = worker().execute(
        tmp_path,
        {
            "required_skill": "workspace.read_text",
            "payload": {"path": "./notes.txt"},
        },
    )
    assert result == {"content": text, "read_receipt": expected(text)}
    assert validate_worker_evidence("workspace.read_text", result)


async def claimed_reader(tmp_path):
    state = StateService(tmp_path / "state.db")
    await state.initialize()
    task = await state.create_task(TaskRecord.new(TaskCreate(input="read"), source="device"))
    agent = await state.register_agent(
        AgentCreate(
            name="reader",
            endpoint="http://127.0.0.1:1",
            skills=["workspace.read_text"],
        ),
        "device",
    )
    await state.heartbeat_agent(agent["id"], "online", agent["credential"])
    dispatcher = AgentDispatcher(state.db_path, MessageBoardService(state.db_path))
    await dispatcher.queue_job(task.id, "workspace.read_text", {"path": "notes.txt"})
    job = await dispatcher.claim(agent["id"])
    assert job is not None
    return state, dispatcher, agent, job


async def submit(dispatcher, agent, job, result):
    return await dispatcher.submit_result(
        agent["id"],
        job["id"],
        job["claim_token"],
        lease_id=job["lease_id"],
        lease_generation=job["lease_generation"],
        status="completed",
        result=result,
        error=None,
    )


@pytest.mark.asyncio
async def test_remote_binding_is_atomic_and_exact_replay_does_not_add_evidence(tmp_path):
    state, dispatcher, agent, job = await claimed_reader(tmp_path)
    (tmp_path / "notes.txt").write_text("bonjour", encoding="utf-8")
    result = worker().execute(tmp_path, job)
    completed, changed = await submit(dispatcher, agent, job, result)
    assert changed and completed["result"] == result
    with sqlite3.connect(state.db_path) as db:
        event = json.loads(
            db.execute(
                "SELECT payload_json FROM audit_events WHERE event_type='agent.job.completed'"
            ).fetchone()[0]
        )
        count = db.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0]
    binding = event["read_receipt_binding"]
    assert binding["job_id"] == job["id"]
    assert binding["task_id"] == job["task_id"]
    assert binding["agent_id"] == agent["id"]
    assert binding["lease_id"] == job["lease_id"]
    assert binding["lease_generation"] == job["lease_generation"]
    assert binding["project_id"] is None
    assert binding["identity_evidence"] == "authenticated_lease_only"
    assert binding["execution_attested"] is False
    assert "bonjour" not in json.dumps(binding)
    assert job["claim_token"] not in json.dumps(binding)
    assert agent["credential"] not in json.dumps(binding)
    assert (await submit(dispatcher, agent, job, result))[1] is False
    with sqlite3.connect(state.db_path) as db:
        assert db.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0] == count
    altered = copy.deepcopy(result)
    altered["content"] = "changed"
    with pytest.raises(AgentDispatchConflict):
        await submit(dispatcher, agent, job, altered)
    with pytest.raises(AgentDispatchConflict):
        await submit(dispatcher, agent, job, {"content": result["content"]})


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mutation",
    [
        {"returned_sha256": "0" * 64},
        {"returned_bytes": True},
        {"path": "elsewhere.txt"},
        {"provenance": "local_executor_measurement"},
        {"complete": False},
        {"encoding": "latin-1"},
        {"execution_attested": True},
        {"path": "../notes.txt"},
    ],
)
async def test_invalid_receipt_rejected_before_terminal_effects(tmp_path, mutation):
    state, dispatcher, agent, job = await claimed_reader(tmp_path)
    receipt = expected("bonjour") | mutation
    result = {"content": "bonjour", "read_receipt": receipt}
    with pytest.raises(AgentDispatchConflict, match="invalid_workspace_read_receipt"):
        await submit(dispatcher, agent, job, result)
    with sqlite3.connect(state.db_path) as db:
        assert db.execute("SELECT status FROM agent_jobs").fetchone()[0] == "claimed"
        assert (
            db.execute(
                "SELECT COUNT(*) FROM audit_events WHERE event_type='agent.job.completed'"
            ).fetchone()[0]
            == 0
        )


@pytest.mark.asyncio
async def test_legacy_result_has_no_fabricated_measurement(tmp_path):
    state, dispatcher, agent, job = await claimed_reader(tmp_path)
    result = {"content": "historical worker"}
    assert validate_worker_evidence("workspace.read_text", result)
    assert (await submit(dispatcher, agent, job, result))[0]["result"] == result
    with sqlite3.connect(state.db_path) as db:
        payload = json.loads(
            db.execute(
                "SELECT payload_json FROM audit_events WHERE event_type='agent.job.completed'"
            ).fetchone()[0]
        )
    assert "read_receipt_binding" not in payload


@pytest.mark.asyncio
async def test_local_durable_result_and_audit_are_bound_to_call(tmp_path):
    state = StateService(tmp_path / "state.db")
    await state.initialize()
    task = await state.create_task(TaskRecord.new(TaskCreate(input="read"), source="device"))
    (tmp_path / "notes.txt").write_text("measured", encoding="utf-8")
    engine = ExecutionEngine(state.db_path, tmp_path, permission_policy())
    call = await engine.create_tool_call(
        task_id=task.id,
        tool_name="workspace.read_text",
        arguments={"path": "notes.txt"},
        summary="read",
        requester=REQUESTER,
    )
    completed = await engine.execute(call["id"])
    assert completed["result"]["read_receipt"] == expected(
        "measured", provenance="local_executor_measurement"
    )
    with sqlite3.connect(state.db_path) as db:
        binding = json.loads(
            db.execute(
                "SELECT payload_json FROM audit_events WHERE event_type='tool.completed'"
            ).fetchone()[0]
        )["read_receipt_binding"]
    assert binding["tool_call_id"] == call["id"] and binding["task_id"] == task.id
    assert binding["identity_evidence"] == "local_executor"
    assert binding["execution_attested"] is False


@pytest.mark.parametrize("data", [b"", b"exact ASCII", "français\r\n".encode()])
def test_worker_empty_and_complete_bytes(tmp_path, data):
    (tmp_path / "notes.txt").write_bytes(data)
    result = worker().execute(
        tmp_path,
        {
            "required_skill": "workspace.read_text",
            "payload": {"path": "notes.txt"},
        },
    )
    assert result["content"].encode("utf-8") == data
    assert result["read_receipt"] == expected(data.decode("utf-8"))


def test_worker_invalid_utf8_and_growth_do_not_emit_complete_receipts(tmp_path, monkeypatch):
    module = worker()
    path = tmp_path / "notes.txt"
    job = {"required_skill": "workspace.read_text", "payload": {"path": "notes.txt"}}
    path.write_bytes(b"invalid\xff")
    with pytest.raises(UnicodeError):
        module.execute(tmp_path, job)
    path.write_bytes(b"short")
    original_read = module.os.read
    grew = False

    def read_after_growth(fd, size):
        nonlocal grew
        if not grew:
            path.write_bytes(b"a" * 1_000_001)
            grew = True
        return original_read(fd, size)

    monkeypatch.setattr(module.os, "read", read_after_growth)
    with pytest.raises(ValueError, match="too large"):
        module.execute(tmp_path, job)


@pytest.mark.asyncio
async def test_audit_failure_rolls_back_receipt_task_and_job(tmp_path, monkeypatch):
    state, dispatcher, agent, job = await claimed_reader(tmp_path)
    result = {"content": "measured", "read_receipt": expected("measured")}

    async def fail(*args, **kwargs):
        raise RuntimeError("audit unavailable")

    monkeypatch.setattr("app.services.agent_dispatcher.append_audit_event", fail)
    with pytest.raises(RuntimeError, match="audit unavailable"):
        await submit(dispatcher, agent, job, result)
    with sqlite3.connect(state.db_path) as db:
        assert db.execute("SELECT status,result_json FROM agent_jobs").fetchone() == (
            "claimed",
            None,
        )
        assert db.execute("SELECT status FROM tasks").fetchone()[0] == "running"


@pytest.mark.asyncio
async def test_caller_mutation_after_await_does_not_change_bound_receipt(tmp_path, monkeypatch):
    import app.services.agent_dispatcher as module
    from app.services.workspace_read_receipts import canonical_sha

    state, dispatcher, agent, job = await claimed_reader(tmp_path)
    result = {"content": "measured", "read_receipt": expected("measured")}
    original = copy.deepcopy(result)
    guard = module.require_symbolic_worker_context_locked

    async def mutate(*args, **kwargs):
        result["content"] = "changed after serialization"
        result["read_receipt"]["returned_sha256"] = "0" * 64
        return await guard(*args, **kwargs)

    monkeypatch.setattr(module, "require_symbolic_worker_context_locked", mutate)
    assert (await submit(dispatcher, agent, job, result))[0]["result"] == original
    with sqlite3.connect(state.db_path) as db:
        binding = json.loads(
            db.execute(
                "SELECT payload_json FROM audit_events WHERE event_type='agent.job.completed'"
            ).fetchone()[0]
        )["read_receipt_binding"]
    assert binding["result_sha256"] == canonical_sha(original)


@pytest.mark.asyncio
async def test_goal_scope_comes_from_sql_and_history_survives_source_change(tmp_path):
    from tests.test_agent_dispatcher_goal_revision import queued_goal

    manager, goal, node, agent = await queued_goal(
        tmp_path, "workspace.read_text", {"path": "notes.txt"}
    )
    claim = await manager.agent_dispatcher.claim(agent["id"])
    assert claim is not None
    path = tmp_path / "notes.txt"
    path.write_text("before", encoding="utf-8")
    result = worker().execute(tmp_path, claim)
    completed, _ = await submit(manager.agent_dispatcher, agent, claim, result)
    await manager.on_job_result(completed)
    path.write_text("after", encoding="utf-8")
    stored = await manager.agent_dispatcher.get_job(claim["id"])
    assert stored["result"]["read_receipt"] == expected("before")
    assert (await manager.graph.get_node(node["id"]))["status"] == "completed"
    with sqlite3.connect(manager.db_path) as db:
        binding = json.loads(
            db.execute(
                "SELECT payload_json FROM audit_events WHERE event_type='agent.job.completed'"
            ).fetchone()[0]
        )["read_receipt_binding"]
        linked = db.execute(
            "SELECT project_id FROM goal_project_links WHERE goal_run_id=?", (goal["id"],)
        ).fetchone()
    assert binding["goal_run_id"] == goal["id"] and binding["node_id"] == node["id"]
    assert binding["project_id"] == (linked[0] if linked else None)
    assert stored["result"]["read_receipt"]["source_snapshot"] == "not_captured"


@pytest.mark.asyncio
async def test_wrong_lease_cannot_submit_measurement(tmp_path):
    _state, dispatcher, agent, job = await claimed_reader(tmp_path)
    result = {"content": "read", "read_receipt": expected("read")}
    with pytest.raises(AgentDispatchConflict, match="lease"):
        await submit(
            dispatcher, agent, job | {"lease_generation": job["lease_generation"] + 1}, result
        )


@pytest.mark.parametrize("bad", [None, {}, {"returned_sha256": "0" * 64}])
def test_present_bad_receipt_never_downgrades_to_legacy_evidence(bad):
    assert not validate_worker_evidence(
        "workspace.read_text", {"content": "read", "read_receipt": bad}
    )


@pytest.mark.asyncio
async def test_goal_read_waits_for_job_link_publication_before_claim(tmp_path):
    from tests.test_agent_dispatcher_goal_revision import queued_goal

    manager, _goal, node, agent = await queued_goal(
        tmp_path, "workspace.read_text", {"path": "notes.txt"}
    )
    with sqlite3.connect(manager.db_path) as db:
        db.execute("UPDATE plan_nodes SET worker_job_id=NULL WHERE id=?", (node["id"],))
    assert await manager.agent_dispatcher.claim(agent["id"]) is None
    with sqlite3.connect(manager.db_path) as db:
        assert db.execute(
            "SELECT status,attempt_count,lease_generation FROM agent_jobs"
        ).fetchone() == ("queued", 0, 0)
        db.execute(
            "UPDATE plan_nodes SET worker_job_id=? WHERE id=?", (node["worker_job_id"], node["id"])
        )
    claim = await manager.agent_dispatcher.claim(agent["id"])
    assert claim is not None and claim["id"] == node["worker_job_id"]
    result = {"content": "read", "read_receipt": expected("read")}
    assert (await submit(manager.agent_dispatcher, agent, claim, result))[1] is True
