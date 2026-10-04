"""Included historical cards must remain SQL-bound at each dispatch boundary."""

import copy
import socket
from uuid import uuid4

import aiosqlite
import pytest

from app.models import AgentCreate, TaskCreate, TaskRecord
from app.services.agent_dispatcher import AgentDispatchConflict, AgentDispatcher
from app.services.project_context import ProjectContextService
from app.services.project_execution_store import delete_project_execution_records_locked
from app.services.writing_drafts import writing_payload
from tests.test_agent_capsule import capsule
from tests.test_goal_project_runtime import _project
from tests.test_project_execution_contracts import measured_project
from tests.test_project_execution_persistence import receipt_job, submit

REASON = "worker_experience_context_changed"
CARRIERS = (
    "writing.draft",
    "code.generate_python",
    "code.build_project",
    "image.generate",
    "audio.synthesize",
)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("network calls forbidden in context qualification")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)


async def history(tmp_path):
    manager, detail, agent, claim, result = await receipt_job(tmp_path)
    job, _ = await submit(manager, agent, claim, result)
    node = detail["nodes"][0]
    await manager.project_applications.capture_result(detail["goal"]["id"], node["id"], job["id"])
    await manager.graph.transition_node(
        node["id"], expected="dispatched", target="completed", result_summary="Recorded history."
    )
    context = ProjectContextService(manager.db_path)
    state = await context.refresh(detail["goal"]["id"])
    selected = context.prompt_state(state)
    assert len(selected["experiences"]["items"]) == 1
    return manager, detail, agent, selected, state["project_id"]


async def consumer(manager, detail, selected, skill="code.generate_python"):
    """Create an ordinary goal-bound consumer node; queue through the real dispatcher."""
    task = await manager.state_service.create_task(
        TaskRecord.new(
            TaskCreate(input="Use the original requirements."),
            source="goal:" + detail["goal"]["id"],
        )
    )
    async with aiosqlite.connect(manager.db_path) as db:
        db.row_factory = aiosqlite.Row
        node = dict(
            await (
                await db.execute("SELECT * FROM plan_nodes WHERE id=?", (detail["nodes"][0]["id"],))
            ).fetchone()
        )
        node.update(
            id="node_" + uuid4().hex,
            task_id=task.id,
            worker_job_id=None,
            required_skill=skill,
            status="dispatched",
            result_summary=None,
            error_summary=None,
            completed_at=None,
            assigned_agent_id=None,
        )
        await db.execute(
            f"INSERT INTO plan_nodes ({','.join(node)}) VALUES ({','.join('?' for _ in node)})",
            tuple(node.values()),
        )
        await db.commit()
    if skill == "writing.draft":
        payload = writing_payload("Use the original requirements.", [], durable_context=selected)
    elif skill == "code.build_project":
        payload = await manager.project_applications.payload(detail["goal"]["id"], node, [])
        payload["durable_context"] = selected
    elif skill in {"image.generate", "audio.synthesize"}:
        payload = (
            {"prompt": "A blue landscape", "width": 512, "height": 512, "steps": 4, "seed": 42}
            if skill == "image.generate"
            else {
                "text": "Bonjour",
                "language": "fr-FR",
                "voice": "ff_siwis",
                "max_duration_seconds": 2,
            }
        )
        payload["context"] = {
            "goal_id": detail["goal"]["id"],
            "objective": "Use the original requirements.",
            "completion_criteria": [],
            "step_objective": "Create media",
            "conversation_revision": 0,
            "durable_context": selected,
        }
    else:
        payload = {"objective": "Use the original requirements.", "durable_context": selected}
    return task, node, payload


async def enrolled(manager, skill):
    agent = await manager.state_service.register_agent(
        AgentCreate(name="Context consumer", endpoint="https://worker.invalid", skills=[skill]),
        "phone",
    )
    await manager.state_service.heartbeat_agent(agent["id"], "online", agent["credential"])
    return agent["id"]


async def link(manager, node, job):
    async with aiosqlite.connect(manager.db_path) as db:
        await db.execute(
            "UPDATE plan_nodes SET worker_job_id=? WHERE id=?", (job["id"], node["id"])
        )
        await db.commit()


async def revoke(manager, project_id):
    async with aiosqlite.connect(manager.db_path) as db:
        await db.execute("BEGIN IMMEDIATE")
        await delete_project_execution_records_locked(db, project_id)
        await db.commit()


async def finish_failed(manager, agent, claimed):
    return await manager.agent_dispatcher.submit_result(
        agent,
        claimed["id"],
        claimed["claim_token"],
        lease_id=claimed["lease_id"],
        lease_generation=claimed["lease_generation"],
        status="failed",
        result=None,
        error="invalid_output",
    )


async def assert_cancelled(manager, node, job, *, attempts):
    record = await manager.agent_dispatcher.get_job(job["id"])
    assert record["status"] == "cancelled" and record["error"] == REASON
    assert record["attempt_count"] == attempts and record["result"] is None
    assert record["payload"] == job["payload"]
    assert (await manager.state_service.get_task(job["task_id"])).status.value == "cancelled"
    assert (await manager.graph.get_node(node["id"]))["status"] == "cancelled"
    async with aiosqlite.connect(manager.db_path) as db:
        events = await (
            await db.execute(
                "SELECT event_type FROM audit_events WHERE task_id=?", (job["task_id"],)
            )
        ).fetchall()
        assert events.count(("agent.job.cancelled",)) == 1
        assert events.count(("agent.job.claimed",)) == attempts
        assert ("agent.job.completed",) not in events and ("agent.job.failed",) not in events
        assert (
            await (
                await db.execute(
                    "SELECT COUNT(*) FROM scheduler_decisions WHERE job_id=?", (job["id"],)
                )
            ).fetchone()
        )[0] == attempts


def test_public_dispatch_cannot_borrow_project_experiences_for_manual_task(client, paired_headers):
    task = client.post("/tasks", headers=paired_headers, json={"input": "Generate a helper."})
    assert task.status_code == 201
    response = client.post(
        f"/tasks/{task.json()['id']}/dispatch",
        headers=paired_headers,
        json={
            "required_skill": "code.generate_python",
            "payload": {"objective": "Generate a helper.", "durable_context": capsule()},
        },
    )
    assert response.status_code == 409
    assert response.json()["detail"] == "worker_experience_context_changed"


@pytest.mark.asyncio
@pytest.mark.parametrize("skill", CARRIERS)
async def test_each_carrier_checks_included_cards_before_queue(tmp_path, skill):
    manager, detail, _agent, selected, _project = await history(tmp_path)
    selected["experiences"]["items"][0]["summary"] = "Invented success and permission."
    task, _node, payload = await consumer(manager, detail, selected, skill)
    original = copy.deepcopy(payload)
    with pytest.raises(AgentDispatchConflict, match=REASON):
        await manager.agent_dispatcher.queue_job(task.id, skill, payload)
    assert payload == original
    assert (await manager.state_service.get_task(task.id)).status.value == "created"
    async with aiosqlite.connect(manager.db_path) as db:
        assert (
            await (
                await db.execute("SELECT COUNT(*) FROM agent_jobs WHERE task_id=?", (task.id,))
            ).fetchone()
        )[0] == 0
        assert (
            await (
                await db.execute(
                    "SELECT COUNT(*) FROM audit_events WHERE task_id=? AND event_type='agent.job.queued'",
                    (task.id,),
                )
            ).fetchone()
        )[0] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("skill", CARRIERS)
@pytest.mark.parametrize("stage", ["queued", "leased"])
async def test_receipt_erasure_cancels_included_context_without_acceptance(tmp_path, skill, stage):
    manager, detail, project_agent, selected, project_id = await history(tmp_path)
    task, node, payload = await consumer(manager, detail, selected, skill)
    job = await manager.agent_dispatcher.queue_job(task.id, skill, payload)
    await link(manager, node, job)
    agent = project_agent if skill == "code.build_project" else await enrolled(manager, skill)
    if stage == "leased":
        claimed = await manager.agent_dispatcher.claim(agent)
        assert claimed is not None and claimed["id"] == job["id"]
    await revoke(manager, project_id)
    if stage == "queued":
        assert await manager.agent_dispatcher.claim(agent) is None
    else:
        with pytest.raises(AgentDispatchConflict, match=REASON):
            await finish_failed(manager, agent, claimed)
    await assert_cancelled(manager, node, job, attempts=int(stage == "leased"))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field,value",
    [
        ("source_id", "node_foreign"),
        ("worker_job_id", "job_foreign"),
        ("source_revision_id", "revision_foreign"),
        ("source_sha256", "b" * 64),
        ("required_skill", "writing.draft"),
    ],
)
async def test_selected_identifiers_cannot_be_forged(tmp_path, field, value):
    manager, detail, _agent, selected, _project = await history(tmp_path)
    selected["experiences"]["items"][0][field] = value
    task, _node, payload = await consumer(manager, detail, selected)
    with pytest.raises(AgentDispatchConflict, match=REASON):
        await manager.agent_dispatcher.queue_job(task.id, "code.generate_python", payload)


@pytest.mark.asyncio
async def test_null_node_job_publication_window_accepts_unique_job_but_not_another_id(tmp_path):
    manager, detail, _agent, selected, _project = await history(tmp_path)
    task, node, payload = await consumer(manager, detail, selected)
    job = await manager.agent_dispatcher.queue_job(task.id, "code.generate_python", payload)
    agent = await enrolled(manager, "code.generate_python")
    assert (await manager.graph.get_node(node["id"]))["worker_job_id"] is None
    claimed = await manager.agent_dispatcher.claim(agent)
    assert claimed is not None and claimed["id"] == job["id"]
    finished, changed = await finish_failed(manager, agent, claimed)
    assert changed and finished["status"] == "failed"
    other_task, other_node, other_payload = await consumer(manager, detail, selected)
    other_job = await manager.agent_dispatcher.queue_job(
        other_task.id, "code.generate_python", other_payload
    )
    await link(manager, other_node, job)
    assert await manager.agent_dispatcher.claim(agent) is None
    assert (await manager.agent_dispatcher.get_job(other_job["id"]))["status"] == "cancelled"


@pytest.mark.asyncio
async def test_claim_skips_revoked_candidate_and_claims_valid_legacy_empty_job(tmp_path):
    manager, detail, _agent, selected, project_id = await history(tmp_path)
    task, node, payload = await consumer(manager, detail, selected)
    stale = await manager.agent_dispatcher.queue_job(task.id, "code.generate_python", payload)
    await link(manager, node, stale)
    empty = copy.deepcopy(selected)
    empty["experiences"] = {"items": [], "omitted_count": 1}
    task2, node2, payload2 = await consumer(manager, detail, empty)
    valid = await manager.agent_dispatcher.queue_job(task2.id, "code.generate_python", payload2)
    await link(manager, node2, valid)
    await revoke(manager, project_id)
    agent = await enrolled(manager, "code.generate_python")
    claimed = await manager.agent_dispatcher.claim(agent)
    assert claimed is not None and claimed["id"] == valid["id"]
    await assert_cancelled(manager, node, stale, attempts=0)
    assert (await finish_failed(manager, agent, claimed))[1]


@pytest.mark.asyncio
async def test_terminal_replay_does_not_reaccept_context_after_historical_erasure(tmp_path):
    manager, detail, _agent, selected, project_id = await history(tmp_path)
    task, node, payload = await consumer(manager, detail, selected)
    job = await manager.agent_dispatcher.queue_job(task.id, "code.generate_python", payload)
    await link(manager, node, job)
    agent = await enrolled(manager, "code.generate_python")
    claimed = await manager.agent_dispatcher.claim(agent)
    first, changed = await finish_failed(manager, agent, claimed)
    assert changed
    await revoke(manager, project_id)
    replay, changed = await finish_failed(manager, agent, claimed)
    assert not changed and replay == first


async def legacy_observation(manager, detail, selected, agent):
    empty = copy.deepcopy(selected)
    empty["experiences"] = {"items": [], "omitted_count": 0}
    task, node, payload = await consumer(manager, detail, empty)
    job = await manager.agent_dispatcher.queue_job(task.id, "code.generate_python", payload)
    await link(manager, node, job)
    claimed = await manager.agent_dispatcher.claim(agent)
    assert claimed is not None and claimed["id"] == job["id"]
    await finish_failed(manager, agent, claimed)
    await manager.graph.transition_node(
        node["id"],
        expected="dispatched",
        target="failed",
        error_summary="remote worker reported failure",
    )


@pytest.mark.asyncio
async def test_mixed_included_history_remains_valid_outside_current_top_six(tmp_path):
    manager, detail, _agent, selected, _project = await history(tmp_path)
    agent = await enrolled(manager, "code.generate_python")
    await legacy_observation(manager, detail, selected, agent)
    context = ProjectContextService(manager.db_path)
    mixed = context.prompt_state(await context.refresh(detail["goal"]["id"]))
    assert len(mixed["experiences"]["items"]) == 2
    assert {item["observation_kind"] for item in mixed["experiences"]["items"]} == {
        "accepted_result",
        "reported_failure",
    }
    for _ in range(7):
        await legacy_observation(manager, detail, selected, agent)
    current = await context.refresh(detail["goal"]["id"])
    assert len(current["experiences"]["items"]) == 6
    assert not (
        {item["source_id"] for item in mixed["experiences"]["items"]}
        & {item["source_id"] for item in current["experiences"]["items"]}
    )
    task, node, payload = await consumer(manager, detail, mixed)
    job = await manager.agent_dispatcher.queue_job(task.id, "code.generate_python", payload)
    await link(manager, node, job)
    claimed = await manager.agent_dispatcher.claim(agent)
    assert claimed is not None and claimed["payload"]["durable_context"] == mixed
    assert (await finish_failed(manager, agent, claimed))[1]


@pytest.mark.asyncio
async def test_one_erased_measurement_rejects_entire_mixed_selection(tmp_path):
    manager, detail, _agent, selected, project_id = await history(tmp_path)
    agent = await enrolled(manager, "code.generate_python")
    await legacy_observation(manager, detail, selected, agent)
    context = ProjectContextService(manager.db_path)
    mixed = context.prompt_state(await context.refresh(detail["goal"]["id"]))
    assert len(mixed["experiences"]["items"]) == 2
    task, _node, payload = await consumer(manager, detail, mixed)
    before = copy.deepcopy(payload)
    await revoke(manager, project_id)
    with pytest.raises(AgentDispatchConflict, match=REASON):
        await manager.agent_dispatcher.queue_job(task.id, "code.generate_python", payload)
    assert payload == before


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["queued", "leased"])
@pytest.mark.parametrize("change", ["receipt", "snapshot", "consumer_project", "source_id"])
async def test_selected_binding_tampering_fails_at_claim_and_result(tmp_path, stage, change):
    manager, detail, _agent, selected, project_id = await history(tmp_path)
    task, node, payload = await consumer(manager, detail, selected)
    job = await manager.agent_dispatcher.queue_job(task.id, "code.generate_python", payload)
    await link(manager, node, job)
    agent = await enrolled(manager, "code.generate_python")
    if stage == "leased":
        claimed = await manager.agent_dispatcher.claim(agent)
        assert claimed is not None
    item = selected["experiences"]["items"][0]
    async with aiosqlite.connect(manager.db_path) as db:
        if change == "receipt":
            await db.execute(
                "UPDATE project_execution_acceptances SET receipt_sha256=? WHERE project_id=?",
                ("b" * 64, project_id),
            )
        elif change == "snapshot":
            await db.execute(
                "UPDATE project_execution_revision_links SET snapshot_sha256=? WHERE project_id=?",
                ("b" * 64, project_id),
            )
        elif change == "consumer_project":
            await db.execute("UPDATE tasks SET source='goal:foreign' WHERE id=?", (task.id,))
        else:
            await db.execute(
                "UPDATE plan_nodes SET worker_job_id=NULL WHERE id=?", (item["source_id"],)
            )
        await db.commit()
    if stage == "queued":
        assert await manager.agent_dispatcher.claim(agent) is None
    else:
        with pytest.raises(AgentDispatchConflict, match=REASON):
            await finish_failed(manager, agent, claimed)
    await assert_cancelled(manager, node, job, attempts=int(stage == "leased"))


@pytest.mark.asyncio
async def test_result_cancellation_is_atomic_with_its_audit(tmp_path, monkeypatch):
    from app.services import agent_dispatcher

    manager, detail, _agent, selected, project_id = await history(tmp_path)
    task, node, payload = await consumer(manager, detail, selected)
    job = await manager.agent_dispatcher.queue_job(task.id, "code.generate_python", payload)
    await link(manager, node, job)
    agent = await enrolled(manager, "code.generate_python")
    claimed = await manager.agent_dispatcher.claim(agent)
    await revoke(manager, project_id)

    async def failed_audit(*args, **kwargs):
        raise RuntimeError("disposable audit failure")

    with monkeypatch.context() as patch:
        patch.setattr(agent_dispatcher, "append_audit_event", failed_audit)
        with pytest.raises(RuntimeError, match="disposable audit failure"):
            await finish_failed(manager, agent, claimed)
    persisted = await manager.agent_dispatcher.get_job(job["id"])
    assert persisted["status"] == "claimed" and persisted["result"] is None
    assert (await manager.state_service.get_task(task.id)).status.value == "running"
    assert (await manager.graph.get_node(node["id"]))["status"] == "dispatched"
    with pytest.raises(AgentDispatchConflict, match=REASON):
        await finish_failed(manager, agent, claimed)
    await assert_cancelled(manager, node, job, attempts=1)


@pytest.mark.asyncio
@pytest.mark.parametrize("with_receipt", [False, True])
async def test_real_queue_publication_defers_project_claim_until_linked(
    tmp_path, monkeypatch, with_receipt
):
    queue = AgentDispatcher.queue_job
    observed = []

    async def during_publication(self, task_id, required_skill, payload, **kwargs):
        job = await queue(self, task_id, required_skill, payload, **kwargs)
        if required_skill == "code.build_project" and not observed:
            async with aiosqlite.connect(self.db_path) as db:
                assert (
                    await (
                        await db.execute(
                            "SELECT worker_job_id FROM plan_nodes WHERE task_id=?", (task_id,)
                        )
                    ).fetchone()
                ) == (None,)
                agent = (await (await db.execute("SELECT id FROM agents")).fetchone())[0]
            observed.append(job["id"])
            assert await self.claim(agent) is None
            held = await self.get_job(job["id"])
            assert (
                held["status"] == "queued"
                and held["attempt_count"] == held["lease_generation"] == 0
            )
            async with aiosqlite.connect(self.db_path) as db:
                assert (
                    await (
                        await db.execute(
                            "SELECT COUNT(*) FROM scheduler_decisions WHERE job_id=?", (job["id"],)
                        )
                    ).fetchone()
                )[0] == 0
        return job

    monkeypatch.setattr(AgentDispatcher, "queue_job", during_publication)
    manager, detail, agent = await _project(tmp_path)
    assert observed == [detail["nodes"][0]["worker_job_id"]]
    claimed = await manager.agent_dispatcher.claim(agent)
    assert claimed is not None
    result = measured_project()
    result.update(
        base_revision_id=claimed["payload"]["base_revision_id"],
        base_sha256=claimed["payload"]["base_sha256"],
    )
    if not with_receipt:
        result.pop("execution_receipt")
    finished, changed = await submit(manager, agent, claimed, result)
    assert changed and finished["status"] == "completed"


@pytest.mark.asyncio
async def test_reconcile_publishes_project_job_then_claim_resumes(tmp_path):
    manager, detail, agent = await _project(tmp_path)
    node = detail["nodes"][0]
    job_id = node["worker_job_id"]
    async with aiosqlite.connect(manager.db_path) as db:
        await db.execute("UPDATE plan_nodes SET worker_job_id=NULL WHERE id=?", (node["id"],))
        await db.commit()
    assert await manager.agent_dispatcher.claim(agent) is None
    assert (await manager.agent_dispatcher.get_job(job_id))["attempt_count"] == 0
    await manager.reconcile()
    assert (await manager.graph.get_node(node["id"]))["worker_job_id"] == job_id
    assert (await manager.agent_dispatcher.claim(agent))["id"] == job_id


@pytest.mark.asyncio
async def test_unpublished_project_job_does_not_starve_manual_project_candidate(tmp_path):
    manager, detail, agent = await _project(tmp_path)
    node = detail["nodes"][0]
    waiting = await manager.agent_dispatcher.get_job(node["worker_job_id"])
    async with aiosqlite.connect(manager.db_path) as db:
        await db.execute("UPDATE plan_nodes SET worker_job_id=NULL WHERE id=?", (node["id"],))
        await db.commit()
    task = await manager.state_service.create_task(
        TaskRecord.new(TaskCreate(input="Independent project."), source="device")
    )
    manual = await manager.agent_dispatcher.queue_job(
        task.id, "code.build_project", waiting["payload"]
    )
    claimed = await manager.agent_dispatcher.claim(agent)
    assert claimed is not None and claimed["id"] == manual["id"]
    held = await manager.agent_dispatcher.get_job(waiting["id"])
    assert held["status"] == "queued" and held["attempt_count"] == held["lease_generation"] == 0
    await finish_failed(manager, agent, claimed)
    await link(manager, node, waiting)
    assert (await manager.agent_dispatcher.claim(agent))["id"] == waiting["id"]


@pytest.mark.asyncio
async def test_unpublished_backlog_is_filtered_before_ranked_candidate_limit(tmp_path):
    manager, detail, agent, selected, _project = await history(tmp_path)
    waiting = []
    for _ in range(manager.agent_dispatcher.CLAIM_CANDIDATE_LIMIT + 1):
        task, _node, payload = await consumer(manager, detail, selected, "code.build_project")
        job = await manager.agent_dispatcher.queue_job(task.id, "code.build_project", payload)
        waiting.append(job["id"])
    task, node, payload = await consumer(manager, detail, selected, "code.build_project")
    published = await manager.agent_dispatcher.queue_job(task.id, "code.build_project", payload)
    await link(manager, node, published)
    claimed = await manager.agent_dispatcher.claim(agent)
    assert claimed is not None and claimed["id"] == published["id"]
    async with aiosqlite.connect(manager.db_path) as db:
        rows = await (
            await db.execute(
                "SELECT id,status,attempt_count,lease_generation FROM agent_jobs WHERE status='queued'"
            )
        ).fetchall()
        assert {row[0] for row in rows} == set(waiting)
        assert all(row[1:] == ("queued", 0, 0) for row in rows)


@pytest.mark.asyncio
async def test_stale_unpublished_project_still_uses_existing_retirement(tmp_path):
    manager, detail, agent = await _project(tmp_path)
    node = detail["nodes"][0]
    async with aiosqlite.connect(manager.db_path) as db:
        await db.execute("UPDATE plan_nodes SET worker_job_id=NULL WHERE id=?", (node["id"],))
        await db.execute(
            "UPDATE goal_runs SET conversation_revision=conversation_revision+1 WHERE id=?",
            (detail["goal"]["id"],),
        )
        await db.commit()
    assert await manager.agent_dispatcher.claim(agent) is None
    job = await manager.agent_dispatcher.get_job(node["worker_job_id"])
    assert job["status"] == "cancelled" and job["attempt_count"] == 0
    assert job["error"] == "goal_conversation_changed_before_claim"


@pytest.mark.asyncio
@pytest.mark.parametrize("node_status", ["cancelled", "failed"])
async def test_terminal_unpublished_project_cannot_use_stale_cleanup_exception(
    tmp_path, node_status
):
    manager, detail, agent = await _project(tmp_path)
    node = detail["nodes"][0]
    async with aiosqlite.connect(manager.db_path) as db:
        await db.execute(
            "UPDATE plan_nodes SET worker_job_id=NULL,status=? WHERE id=?",
            (node_status, node["id"]),
        )
        await db.execute(
            "UPDATE goal_runs SET conversation_revision=conversation_revision+1 WHERE id=?",
            (detail["goal"]["id"],),
        )
        await db.commit()
    assert await manager.agent_dispatcher.claim(agent) is None
    job = await manager.agent_dispatcher.get_job(node["worker_job_id"])
    assert job["status"] == "queued" and job["attempt_count"] == job["lease_generation"] == 0
    assert (await manager.graph.get_node(node["id"]))["status"] == node_status


@pytest.mark.parametrize("kind", ["absent", "empty", "no_experiences"])
def test_public_legacy_optional_context_remains_accepted(client, paired_headers, kind):
    task = client.post(
        "/tasks", headers=paired_headers, json={"input": "Generate a helper."}
    ).json()
    payload = {"objective": "Generate a helper."}
    if kind != "absent":
        selected = capsule()
        if kind == "empty":
            selected["experiences"]["items"] = []
        else:
            selected.pop("experiences")
        payload["durable_context"] = selected
    response = client.post(
        f"/tasks/{task['id']}/dispatch",
        headers=paired_headers,
        json={"required_skill": "code.generate_python", "payload": payload},
    )
    assert response.status_code == 201
