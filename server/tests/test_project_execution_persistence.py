"""Receipt acceptance is server-bound history, never automatic validation."""

import json
from uuid import uuid4

import aiosqlite
import pytest

from app.models import AgentCreate, TaskCreate, TaskRecord
from app.services.agent_dispatcher import AgentDispatchConflict
from app.services.goal_project import GoalProjectConflict
from app.services.project_execution_store import delete_project_execution_records_locked
from tests.test_goal_project_runtime import _project
from tests.test_project_execution_contracts import measured_project


async def receipt_job(tmp_path):
    manager, detail, agent_id = await _project(tmp_path)
    claim = await manager.agent_dispatcher.claim(agent_id)
    assert claim is not None
    result = measured_project()
    result.update(
        action="continue",
        base_revision_id=claim["payload"]["base_revision_id"],
        base_sha256=claim["payload"]["base_sha256"],
    )
    return manager, detail, agent_id, claim, result


async def submit(manager, agent_id, claim, result):
    return await manager.agent_dispatcher.submit_result(
        agent_id,
        claim["id"],
        claim["claim_token"],
        lease_id=claim["lease_id"],
        lease_generation=claim["lease_generation"],
        status="completed",
        result=result,
        error=None,
    )


@pytest.mark.asyncio
async def test_acceptance_is_lease_bound_then_linked_to_actual_accepted_revision(tmp_path):
    manager, detail, agent, claim, result = await receipt_job(tmp_path)
    job, changed = await submit(manager, agent, claim, result)
    assert changed
    async with aiosqlite.connect(manager.db_path) as db:
        db.row_factory = aiosqlite.Row
        accepted = await (
            await db.execute("SELECT * FROM project_execution_acceptances")
        ).fetchone()
        assert accepted["job_id"] == claim["id"]
        assert accepted["producer_agent_id"] == agent
        assert accepted["lease_id"] == claim["lease_id"]
        assert accepted["lease_generation"] == claim["lease_generation"]
        assert accepted["claimed_at"] == claim["claimed_at"]
        assert accepted["node_id"] == detail["nodes"][0]["id"]
        assert accepted["observation_status"] == "complete"
        encoded = json.dumps(dict(accepted))
        assert claim["claim_token"] not in encoded
        assert "auth_token_hash" not in encoded and "lease_token_hash" not in encoded
        assert "value=1" not in encoded
        assert (
            await (
                await db.execute("SELECT COUNT(*) FROM project_execution_revision_links")
            ).fetchone()
        )[0] == 0
    captured = await manager.project_applications.capture_result(
        detail["goal"]["id"], detail["nodes"][0]["id"], job["id"]
    )
    async with aiosqlite.connect(manager.db_path) as db:
        link = await (
            await db.execute(
                "SELECT acceptance_id,revision_id,project_id,source_sha256 FROM project_execution_revision_links"
            )
        ).fetchone()
    assert link == (
        accepted["id"],
        captured["revision_id"],
        captured["project_id"],
        captured["sha256"],
    )
    replay, changed = await submit(manager, agent, claim, result)
    assert not changed and replay["id"] == job["id"]
    assert (
        await manager.project_applications.capture_result(
            detail["goal"]["id"], detail["nodes"][0]["id"], job["id"]
        )
        == captured
    )


@pytest.mark.asyncio
async def test_receipt_mismatch_is_rejected_before_job_task_or_audit_commit(tmp_path):
    manager, _detail, agent, claim, result = await receipt_job(tmp_path)
    result["files"][0]["content"] = "value=2\n"
    with pytest.raises(AgentDispatchConflict, match="project execution"):
        await submit(manager, agent, claim, result)
    job = await manager.agent_dispatcher.get_job(claim["id"])
    assert job["status"] == "claimed" and job["result"] is None


async def counts(manager):
    async with aiosqlite.connect(manager.db_path) as db:
        return tuple(
            [
                (await (await db.execute("SELECT COUNT(*) FROM " + table)).fetchone())[0]
                for table in (
                    "project_execution_acceptances",
                    "project_execution_revision_links",
                    "project_revisions",
                )
            ]
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["claim_token", "lease_id", "lease_generation", "agent_id"])
async def test_stale_or_foreign_lease_cannot_create_receipt(tmp_path, field):
    manager, _detail, agent, claim, result = await receipt_job(tmp_path)
    bad = dict(claim)
    if field == "agent_id":
        agent = "agt_foreign"
    elif field == "lease_generation":
        bad[field] += 1
    else:
        bad[field] += "wrong"
    with pytest.raises(AgentDispatchConflict, match="lease"):
        await submit(manager, agent, bad, result)
    assert await counts(manager) == (0, 0, 0)


@pytest.mark.asyncio
async def test_acceptance_and_job_roll_back_with_failed_audit(tmp_path, monkeypatch):
    from app.services import agent_dispatcher

    manager, _detail, agent, claim, result = await receipt_job(tmp_path)

    async def broken(*args, **kwargs):
        raise RuntimeError("private audit failure")

    with monkeypatch.context() as patch:
        patch.setattr(agent_dispatcher, "append_audit_event", broken)
        with pytest.raises(RuntimeError, match="audit failure"):
            await submit(manager, agent, claim, result)
    assert await counts(manager) == (0, 0, 0)
    job = await manager.agent_dispatcher.get_job(claim["id"])
    assert job["status"] == "claimed" and job["result"] is None
    assert (await manager.state_service.get_task(claim["task_id"])).status.value == "running"
    assert (await submit(manager, agent, claim, result))[1]


@pytest.mark.asyncio
async def test_revision_and_link_roll_back_together_then_recover(tmp_path, monkeypatch):
    from app.services import goal_project

    manager, detail, agent, claim, result = await receipt_job(tmp_path)
    await submit(manager, agent, claim, result)

    async def broken(*args, **kwargs):
        raise RuntimeError("private revision audit failure")

    args = (detail["goal"]["id"], detail["nodes"][0]["id"], claim["id"])
    with monkeypatch.context() as patch:
        patch.setattr(goal_project, "append_audit_event", broken)
        with pytest.raises(RuntimeError, match="audit failure"):
            await manager.project_applications.capture_result(*args)
    assert await counts(manager) == (1, 0, 0)
    # Recreate the service after the interrupted acceptance/capture boundary.
    service = goal_project.GoalProjectService(
        manager.db_path, manager.project_applications.execution_engine
    )
    await service.capture_result(*args)
    assert await counts(manager) == (1, 1, 1)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change", ["source", "base", "foreign_goal", "missing_project", "node_job"]
)
async def test_false_scope_or_binding_is_not_downgraded_to_manual(tmp_path, change):
    manager, detail, agent, claim, result = await receipt_job(tmp_path)
    if change == "base":
        result.update(base_revision_id="revision_foreign", base_sha256="a" * 64)
    else:
        async with aiosqlite.connect(manager.db_path) as db:
            if change == "source":
                await db.execute("UPDATE tasks SET source='manual' WHERE id=?", (claim["task_id"],))
            elif change == "foreign_goal":
                await db.execute(
                    "UPDATE tasks SET source='goal:foreign' WHERE id=?", (claim["task_id"],)
                )
            elif change == "missing_project":
                await db.execute(
                    "DELETE FROM goal_project_links WHERE goal_run_id=?", (detail["goal"]["id"],)
                )
            else:
                await db.execute(
                    "UPDATE plan_nodes SET worker_job_id=NULL WHERE id=?",
                    (detail["nodes"][0]["id"],),
                )
            await db.commit()
    with pytest.raises(AgentDispatchConflict, match="project execution"):
        await submit(manager, agent, claim, result)
    assert await counts(manager) == (0, 0, 0)


@pytest.mark.asyncio
async def test_unchanged_files_new_execution_gets_distinct_revision_and_link(tmp_path):
    manager, detail, agent, claim, result = await receipt_job(tmp_path)
    job, _ = await submit(manager, agent, claim, result)
    await manager.on_job_result(job)
    next_claim = await manager.agent_dispatcher.claim(agent)
    assert next_claim is not None and next_claim["id"] != claim["id"]
    result["base_revision_id"] = next_claim["payload"]["base_revision_id"]
    result["base_sha256"] = next_claim["payload"]["base_sha256"]
    result["execution_receipt"]["run_id"] = uuid4().hex
    job, _ = await submit(manager, agent, next_claim, result)
    node = await manager.graph.node_for_job(job["id"])
    await manager.project_applications.capture_result(detail["goal"]["id"], node["id"], job["id"])
    assert await counts(manager) == (2, 2, 2)
    async with aiosqlite.connect(manager.db_path) as db:
        assert (
            await (
                await db.execute(
                    "SELECT COUNT(DISTINCT source_sha256) FROM project_execution_revision_links"
                )
            ).fetchone()
        )[0] == 1


@pytest.mark.asyncio
async def test_global_run_id_cannot_be_reused_by_a_different_agent_or_task(tmp_path):
    manager, _detail, agent, claim, result = await receipt_job(tmp_path)
    await submit(manager, agent, claim, result)
    second = await manager.state_service.register_agent(
        AgentCreate(
            name="Second project worker",
            endpoint="https://worker.invalid",
            skills=["code.build_project"],
        ),
        "phone",
    )
    await manager.state_service.heartbeat_agent(second["id"], "online", second["credential"])
    async with aiosqlite.connect(manager.db_path) as db:
        await db.execute("UPDATE agents SET status='offline' WHERE id=?", (agent,))
        await db.commit()
    task = await manager.state_service.create_task(
        TaskRecord.new(TaskCreate(input="private test"), source="device")
    )
    await manager.agent_dispatcher.queue_job(task.id, "code.build_project", claim["payload"])
    next_claim = await manager.agent_dispatcher.claim(second["id"])
    assert next_claim is not None
    with pytest.raises(AgentDispatchConflict, match="project execution"):
        await submit(manager, second["id"], next_claim, result)
    assert await counts(manager) == (1, 0, 0)
    result["execution_receipt"]["run_id"] = uuid4().hex
    await submit(manager, second["id"], next_claim, result)
    async with aiosqlite.connect(manager.db_path) as db:
        row = await (
            await db.execute(
                "SELECT project_id,goal_run_id,node_id,conversation_revision FROM project_execution_acceptances WHERE job_id=?",
                (next_claim["id"],),
            )
        ).fetchone()
    assert row == (None, None, None, None)


@pytest.mark.asyncio
@pytest.mark.parametrize("captured", [False, True])
@pytest.mark.parametrize("foreign_keys", [False, True])
async def test_registry_erasure_does_not_resurrect_from_terminal_replay_or_capture(
    tmp_path, captured, foreign_keys
):
    manager, detail, agent, claim, result = await receipt_job(tmp_path)
    await submit(manager, agent, claim, result)
    args = (detail["goal"]["id"], detail["nodes"][0]["id"], claim["id"])
    if captured:
        await manager.project_applications.capture_result(*args)
    project_id = await manager.project_applications.ensure_project(detail["goal"]["id"])
    async with aiosqlite.connect(manager.db_path) as db:
        await db.execute(f"PRAGMA foreign_keys={'ON' if foreign_keys else 'OFF'}")
        with pytest.raises(ValueError):
            await delete_project_execution_records_locked(db, project_id)
        await db.execute("BEGIN IMMEDIATE")
        await delete_project_execution_records_locked(db, project_id)
        await delete_project_execution_records_locked(db, project_id)
        await db.commit()
    with pytest.raises(AgentDispatchConflict, match="project execution"):
        await submit(manager, agent, claim, result)
    if captured:
        old = await manager.project_applications.capture_result(*args)
        assert old["sha256"] == result["execution_receipt"]["source_sha256"]
    else:
        with pytest.raises(GoalProjectConflict, match="project execution"):
            await manager.project_applications.capture_result(*args)
    assert await counts(manager) == (0, 0, int(captured))
    # This internal registry helper is explicitly not full erasure of the job.
    assert (await manager.agent_dispatcher.get_job(claim["id"]))["result"] == result


@pytest.mark.asyncio
async def test_legacy_result_without_receipt_has_no_synthetic_provenance(tmp_path):
    manager, detail, agent, claim, result = await receipt_job(tmp_path)
    del result["execution_receipt"]
    await submit(manager, agent, claim, result)
    await manager.project_applications.capture_result(
        detail["goal"]["id"], detail["nodes"][0]["id"], claim["id"]
    )
    assert await counts(manager) == (0, 0, 1)
    assert not (await submit(manager, agent, claim, result))[1]


@pytest.mark.asyncio
async def test_producer_metadata_is_frozen_and_different_terminal_result_rejected(tmp_path):
    manager, detail, agent, claim, result = await receipt_job(tmp_path)
    await submit(manager, agent, claim, result)
    async with aiosqlite.connect(manager.db_path) as db:
        before = await (
            await db.execute("SELECT producer_json FROM project_execution_acceptances")
        ).fetchone()
        await db.execute("UPDATE agents SET version='9.9.9' WHERE id=?", (agent,))
        await db.commit()
    await manager.project_applications.capture_result(
        detail["goal"]["id"], detail["nodes"][0]["id"], claim["id"]
    )
    async with aiosqlite.connect(manager.db_path) as db:
        assert (
            await (
                await db.execute("SELECT producer_json FROM project_execution_acceptances")
            ).fetchone()
            == before
        )
    result["message"] = "different terminal result"
    with pytest.raises(AgentDispatchConflict, match="differs"):
        await submit(manager, agent, claim, result)


@pytest.mark.asyncio
@pytest.mark.parametrize("target", ["payload", "result", "acceptance", "snapshot"])
async def test_modified_durable_binding_cannot_link_or_replay_as_current(tmp_path, target):
    manager, detail, agent, claim, result = await receipt_job(tmp_path)
    await submit(manager, agent, claim, result)
    args = (detail["goal"]["id"], detail["nodes"][0]["id"], claim["id"])
    if target == "snapshot":
        await manager.project_applications.capture_result(*args)
    async with aiosqlite.connect(manager.db_path) as db:
        if target in {"payload", "result"}:
            column = target + "_json"
            raw = json.loads(
                (
                    await (
                        await db.execute(
                            f"SELECT {column} FROM agent_jobs WHERE id=?", (claim["id"],)
                        )
                    ).fetchone()
                )[0]
            )
            raw["objective" if target == "payload" else "message"] = "changed after acceptance"
            await db.execute(
                f"UPDATE agent_jobs SET {column}=? WHERE id=?", (json.dumps(raw), claim["id"])
            )
        elif target == "acceptance":
            await db.execute(
                "UPDATE project_execution_acceptances SET source_sha256=?", ("0" * 64,)
            )
        else:
            raw = json.loads(
                (
                    await (
                        await db.execute("SELECT snapshot_json FROM project_revisions")
                    ).fetchone()
                )[0]
            )
            raw["message"] = "changed after capture"
            await db.execute("UPDATE project_revisions SET snapshot_json=?", (json.dumps(raw),))
        await db.commit()
    with pytest.raises(GoalProjectConflict, match="project execution"):
        await manager.project_applications.capture_result(*args)
    assert await counts(manager) == (1, int(target == "snapshot"), int(target == "snapshot"))


@pytest.mark.asyncio
async def test_incomplete_measurement_and_failed_check_are_history_not_promoted(tmp_path):
    manager, detail, agent, claim, result = await receipt_job(tmp_path)
    receipt = result["execution_receipt"]
    receipt["profiles"][1].update(exit_code=1, test_failures=1)
    result["checks"][1].update(exit_code=1, status="failed")
    for profile in receipt["profiles"]:
        profile.update(observation=None, measurement_error="legacy_harness")
    receipt.update(observation_status="incomplete", incomplete_reasons=["legacy_harness"])
    await submit(manager, agent, claim, result)
    await manager.project_applications.capture_result(
        detail["goal"]["id"], detail["nodes"][0]["id"], claim["id"]
    )
    async with aiosqlite.connect(manager.db_path) as db:
        assert await (
            await db.execute("SELECT observation_status FROM project_execution_acceptances")
        ).fetchone() == ("incomplete",)
        assert (await (await db.execute("SELECT COUNT(*) FROM memory_items")).fetchone())[0] == 0


@pytest.mark.asyncio
async def test_populated_schema33_restart_preserves_acceptance_link_and_original_reports(tmp_path):
    from app.services.state_service import StateService
    from tests.test_memory_symbolic_integration import NoModelCalls

    manager, detail, agent, claim, result = await receipt_job(tmp_path)
    await submit(manager, agent, claim, result)
    captured = await manager.project_applications.capture_result(
        detail["goal"]["id"], detail["nodes"][0]["id"], claim["id"]
    )

    async def retained_rows():
        async with aiosqlite.connect(manager.db_path) as db:
            return {
                "acceptances": await (
                    await db.execute(
                        "SELECT rowid,* FROM project_execution_acceptances ORDER BY rowid"
                    )
                ).fetchall(),
                "links": await (
                    await db.execute(
                        "SELECT rowid,* FROM project_execution_revision_links ORDER BY rowid"
                    )
                ).fetchall(),
                "job_result": await (
                    await db.execute(
                        "SELECT result_json FROM agent_jobs WHERE id=?", (claim["id"],)
                    )
                ).fetchone(),
                "revision": await (
                    await db.execute(
                        "SELECT rowid,* FROM project_revisions WHERE id=?",
                        (captured["revision_id"],),
                    )
                ).fetchone(),
                "version": await (await db.execute("PRAGMA user_version")).fetchone(),
                "foreign_keys": await (await db.execute("PRAGMA foreign_key_check")).fetchall(),
                "integrity": await (await db.execute("PRAGMA integrity_check")).fetchall(),
            }

    before = await retained_rows()
    assert len(before["acceptances"]) == len(before["links"]) == 1
    assert before["version"] == (33,)
    assert before["foreign_keys"] == [] and before["integrity"] == [("ok",)]
    forbidden = NoModelCalls()
    state = StateService(manager.db_path, forbidden)
    state.memory_normalizer = forbidden
    state.memory_presenter = forbidden
    for _ in range(2):
        await state.initialize()
        assert await retained_rows() == before
        assert forbidden.calls == 0
