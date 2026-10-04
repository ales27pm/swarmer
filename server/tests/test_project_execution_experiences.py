"""Public context observes qualified history without turning reports into policy."""

import copy
import json

import aiosqlite
import pytest

from app.services.project_context import ProjectContextService
from app.services.project_execution_store import delete_project_execution_records_locked
from app.services.worker_experiences import (
    read_worker_experiences,
    require_selected_worker_experiences_locked,
)
from tests.test_project_execution_persistence import receipt_job, submit


async def finished_measurement(tmp_path, *, incomplete=False, failed=False):
    manager, detail, agent, claim, result = await receipt_job(tmp_path)
    receipt = result["execution_receipt"]
    if failed:
        receipt["profiles"][1].update(exit_code=1, test_failures=1)
        result["checks"][1].update(exit_code=1, status="failed")
    if incomplete:
        for profile in receipt["profiles"]:
            profile.update(observation=None, measurement_error="legacy_harness")
        receipt.update(observation_status="incomplete", incomplete_reasons=["legacy_harness"])
    job, _ = await submit(manager, agent, claim, result)
    await manager.on_job_result(job)
    service = ProjectContextService(manager.db_path)
    state = await service.refresh(detail["goal"]["id"])
    return manager, detail, job, result, service, state


@pytest.mark.asyncio
async def test_claim_result_capture_and_public_context_include_bound_measurements(tmp_path):
    _manager, detail, job, result, service, state = await finished_measurement(tmp_path)
    capsule = service.prompt_state(state)
    (item,) = capsule["experiences"]["items"]
    assert item["observation_kind"] == "accepted_result"
    assert item["content_trust"] == "untrusted" and item["applicability"] == "historical"
    assert item["worker_job_id"] == job["id"]
    assert item["source_sha256"] == result["execution_receipt"]["source_sha256"]
    summary = item["summary"]
    assert "server-bound" in summary
    assert "python_test: exit=0, tests=1, failed=0" in summary
    assert "not attested" in summary and "not proof for current code" in summary
    assert "validated lesson" in summary and len(summary) <= 600
    assert result["files"][0]["content"] not in json.dumps(capsule)
    assert result["checks"][0]["output"] not in summary
    assert result["execution_receipt"]["run_id"] not in summary
    assert (
        capsule["requirements"]
        == service.prompt_state(await service.refresh(detail["goal"]["id"]))["requirements"]
    )


@pytest.mark.asyncio
async def test_erased_acceptance_never_becomes_legacy_checks_or_is_recreated(tmp_path):
    manager, detail, _job, _result, service, before = await finished_measurement(tmp_path)
    async with aiosqlite.connect(manager.db_path) as db:
        await db.execute("BEGIN IMMEDIATE")
        await delete_project_execution_records_locked(db, before["project_id"])
        await db.commit()
    after = await service.refresh(detail["goal"]["id"])
    assert after["experiences"] == {"items": [], "omitted_count": 1}
    assert after["fingerprint"] != before["fingerprint"]
    async with aiosqlite.connect(manager.db_path) as db:
        assert await (
            await db.execute("SELECT COUNT(*) FROM project_execution_acceptances")
        ).fetchone() == (0,)


@pytest.mark.asyncio
@pytest.mark.parametrize("incomplete,failed", [(False, True), (True, False), (True, True)])
async def test_incomplete_or_failed_measurements_remain_untrusted_history(
    tmp_path, incomplete, failed
):
    _manager, _detail, _job, _result, _service, state = await finished_measurement(
        tmp_path, incomplete=incomplete, failed=failed
    )
    (item,) = state["experiences"]["items"]
    assert item["observation_kind"] == "accepted_result" and item["outcome"] == "completed"
    assert item["content_trust"] == "untrusted" and item["applicability"] == "historical"
    assert "not proof for current code" in item["summary"]
    assert f"python_test: exit={int(failed)}, tests=1, failed={int(failed)}" in item["summary"]
    if incomplete:
        assert "incomplete" in item["summary"] and "legacy_harness" in item["summary"]
        assert "source=unknown" in item["summary"] and "deps=unknown" in item["summary"]


async def read_only(manager, project_id):
    async with aiosqlite.connect(f"file:{manager.db_path}?mode=ro", uri=True) as db:
        await db.execute("PRAGMA query_only=ON")
        await db.execute("BEGIN")
        before = db.total_changes
        result = await read_worker_experiences(db, project_id)
        assert db.total_changes == before
        return result


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE project_execution_acceptances SET lease_id='changed'",
        "UPDATE project_execution_acceptances SET accepted_at='changed'",
        "UPDATE project_execution_acceptances SET payload_sha256=printf('%064d',0)",
        "UPDATE project_execution_acceptances SET result_sha256=printf('%064d',0)",
        "UPDATE project_execution_acceptances SET receipt_sha256=printf('%064d',0)",
        "UPDATE project_execution_acceptances SET producer_json='{}'",
        "UPDATE project_execution_revision_links SET snapshot_sha256=printf('%064d',0)",
        "DELETE FROM project_execution_revision_links",
        "UPDATE agent_jobs SET lease_generation=lease_generation+1 WHERE status='completed'",
        "UPDATE agent_jobs SET result_json=json_remove(result_json,'$.execution_receipt') WHERE status='completed'",
        "UPDATE project_revisions SET snapshot_json=json_remove(snapshot_json,'$.execution_receipt') WHERE worker_job_id IS NOT NULL",
        "UPDATE project_revisions SET sha256=printf('%064d',0) WHERE worker_job_id IS NOT NULL",
        "UPDATE tasks SET status='failed' WHERE status='completed'",
        "UPDATE plan_nodes SET conversation_revision=conversation_revision+1 WHERE status='completed'",
    ],
)
async def test_corrupt_binding_is_omitted_without_read_side_repair(tmp_path, sql):
    manager, _detail, _job, _result, _service, state = await finished_measurement(tmp_path)
    async with aiosqlite.connect(manager.db_path) as db:
        await db.execute(sql)
        await db.commit()
    assert await read_only(manager, state["project_id"]) == {"items": [], "omitted_count": 1}


@pytest.mark.asyncio
async def test_read_requires_snapshot_and_cross_project_selection_fails(tmp_path):
    manager, _detail, _job, _result, _service, state = await finished_measurement(tmp_path)
    async with aiosqlite.connect(manager.db_path) as db:
        with pytest.raises(ValueError, match="requires a transaction"):
            await read_worker_experiences(db, state["project_id"])
        with pytest.raises(ValueError, match="^worker_experience_context_changed$"):
            await require_selected_worker_experiences_locked(
                db, project_id=state["project_id"], items=[]
            )
        await db.execute("BEGIN")
        assert await read_worker_experiences(db, "unrelated") == {"items": [], "omitted_count": 0}
        with pytest.raises(ValueError, match="^worker_experience_context_changed$"):
            await require_selected_worker_experiences_locked(
                db, project_id="unrelated", items=state["experiences"]["items"]
            )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode",
    [
        "summary",
        "source_id",
        "worker_job_id",
        "source_revision_id",
        "source_sha256",
        "observation_kind",
        "duplicate",
        "oversize",
        "nested",
        "extra",
    ],
)
async def test_selected_cards_fail_closed_if_malformed_or_changed(tmp_path, mode):
    manager, _detail, _job, _result, _service, state = await finished_measurement(tmp_path)
    items = copy.deepcopy(state["experiences"]["items"])
    if mode == "duplicate":
        items *= 2
    elif mode == "oversize":
        items *= 7
    elif mode == "nested":
        items[0]["summary"] = {"untrusted": "nested"}
    elif mode == "extra":
        items[0]["trusted"] = "yes"
    else:
        items[0][mode] = "changed"
    async with aiosqlite.connect(manager.db_path) as db:
        await db.execute("BEGIN")
        with pytest.raises(ValueError, match="^worker_experience_context_changed$"):
            await require_selected_worker_experiences_locked(
                db, project_id=state["project_id"], items=items
            )


@pytest.mark.asyncio
async def test_registry_and_receipt_erasure_cannot_downgrade_previously_selected_card(tmp_path):
    manager, _detail, job, result, _service, state = await finished_measurement(tmp_path)
    result.pop("execution_receipt")
    async with aiosqlite.connect(manager.db_path) as db:
        await db.execute("BEGIN IMMEDIATE")
        await delete_project_execution_records_locked(db, state["project_id"])
        await db.execute(
            "UPDATE agent_jobs SET result_json=? WHERE id=?", (json.dumps(result), job["id"])
        )
        await db.commit()
        await db.execute("BEGIN")
        assert await read_worker_experiences(db, state["project_id"]) == {
            "items": [],
            "omitted_count": 1,
        }
        # A legacy projection has different caveats; it cannot impersonate the
        # previously issued measured card, even when both registries are gone.
        with pytest.raises(ValueError, match="^worker_experience_context_changed$"):
            await require_selected_worker_experiences_locked(
                db, project_id=state["project_id"], items=state["experiences"]["items"]
            )


@pytest.mark.asyncio
async def test_missing_receipt_cannot_downgrade_an_orphaned_revision_registry(tmp_path):
    manager, _detail, job, result, _service, state = await finished_measurement(tmp_path)
    result.pop("execution_receipt")
    async with aiosqlite.connect(manager.db_path) as db:
        await db.execute("DELETE FROM project_execution_acceptances")
        await db.execute(
            "UPDATE agent_jobs SET result_json=? WHERE id=?", (json.dumps(result), job["id"])
        )
        await db.execute(
            "UPDATE project_revisions SET snapshot_json=json_remove(snapshot_json,'$.execution_receipt') WHERE worker_job_id=?",
            (job["id"],),
        )
        await db.commit()
    assert await read_only(manager, state["project_id"]) == {"items": [], "omitted_count": 1}


@pytest.mark.asyncio
async def test_deleting_every_receipt_copy_cannot_match_a_selected_measured_card(tmp_path):
    manager, _detail, job, result, _service, state = await finished_measurement(tmp_path)
    result.pop("execution_receipt")
    async with aiosqlite.connect(manager.db_path) as db:
        await db.execute("BEGIN IMMEDIATE")
        await delete_project_execution_records_locked(db, state["project_id"])
        await db.execute(
            "UPDATE agent_jobs SET result_json=? WHERE id=?", (json.dumps(result), job["id"])
        )
        await db.execute(
            "UPDATE project_revisions SET snapshot_json=json_remove(snapshot_json,'$.execution_receipt') WHERE worker_job_id=?",
            (job["id"],),
        )
        await db.commit()
        await db.execute("BEGIN")
        with pytest.raises(ValueError, match="^worker_experience_context_changed$"):
            await require_selected_worker_experiences_locked(
                db, project_id=state["project_id"], items=state["experiences"]["items"]
            )


@pytest.mark.asyncio
async def test_deep_json_is_counted_omitted_but_storage_errors_are_visible(tmp_path):
    manager, _detail, job, _result, _service, state = await finished_measurement(tmp_path)
    async with aiosqlite.connect(manager.db_path) as db:
        await db.execute(
            "UPDATE agent_jobs SET result_json=? WHERE id=?",
            ("[" * 1500 + "0" + "]" * 1500, job["id"]),
        )
        await db.commit()
    assert await read_only(manager, state["project_id"]) == {"items": [], "omitted_count": 1}
    async with aiosqlite.connect(manager.db_path) as db:
        await db.execute("DROP TABLE project_execution_acceptances")
        await db.commit()
    with pytest.raises(aiosqlite.OperationalError, match="no such table"):
        await read_only(manager, state["project_id"])


@pytest.mark.asyncio
async def test_read_snapshot_stays_consistent_then_next_snapshot_observes_erasure(tmp_path):
    manager, _detail, _job, _result, _service, state = await finished_measurement(tmp_path)
    async with aiosqlite.connect(manager.db_path) as db:
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("BEGIN")
        first = await read_worker_experiences(db, state["project_id"])
        async with aiosqlite.connect(manager.db_path) as writer:
            await writer.execute("BEGIN IMMEDIATE")
            await delete_project_execution_records_locked(writer, state["project_id"])
            await writer.commit()
        await require_selected_worker_experiences_locked(
            db, project_id=state["project_id"], items=first["items"]
        )
        assert await read_worker_experiences(db, state["project_id"]) == first
        await db.rollback()
        await db.execute("BEGIN")
        assert await read_worker_experiences(db, state["project_id"]) == {
            "items": [],
            "omitted_count": 1,
        }


async def add_newer_legacy_rows(manager, job_id, count):
    """Distinct retained legacy history, with no invented server acceptance."""
    async with aiosqlite.connect(manager.db_path) as db:
        db.row_factory = aiosqlite.Row
        job = dict(
            await (await db.execute("SELECT * FROM agent_jobs WHERE id=?", (job_id,))).fetchone()
        )
        task = dict(
            await (await db.execute("SELECT * FROM tasks WHERE id=?", (job["task_id"],))).fetchone()
        )
        node = dict(
            await (
                await db.execute("SELECT * FROM plan_nodes WHERE worker_job_id=?", (job_id,))
            ).fetchone()
        )
        revision = dict(
            await (
                await db.execute("SELECT * FROM project_revisions WHERE worker_job_id=?", (job_id,))
            ).fetchone()
        )
        result = json.loads(job["result_json"])
        result.pop("execution_receipt")
        snapshot = json.loads(revision["snapshot_json"])
        snapshot.pop("execution_receipt")
        for index in range(count):
            copies = {
                "tasks": {**task, "id": f"legacy_task_{index}"},
                "agent_jobs": {
                    **job,
                    "id": f"legacy_job_{index}",
                    "task_id": f"legacy_task_{index}",
                    "result_json": json.dumps(result),
                    "created_at": f"2099-01-01T00:00:{index:02d}+00:00",
                },
                "plan_nodes": {
                    **node,
                    "id": f"legacy_node_{index}",
                    "task_id": f"legacy_task_{index}",
                    "worker_job_id": f"legacy_job_{index}",
                },
                "project_revisions": {
                    **revision,
                    "id": f"legacy_revision_{index}",
                    "node_id": f"legacy_node_{index}",
                    "worker_job_id": f"legacy_job_{index}",
                    "revision": revision["revision"] + index + 1,
                    "snapshot_json": json.dumps(snapshot),
                },
            }
            for table, values in copies.items():
                await db.execute(
                    f"INSERT INTO {table}({','.join(values)}) VALUES({','.join('?' for _ in values)})",
                    tuple(values.values()),
                )
        await db.commit()


@pytest.mark.asyncio
async def test_exact_selected_ids_survive_newer_results_and_support_mixed_legacy(tmp_path):
    manager, _detail, job, _result, _service, state = await finished_measurement(tmp_path)
    measured = state["experiences"]["items"][0]
    await add_newer_legacy_rows(manager, job["id"], 7)
    async with aiosqlite.connect(manager.db_path) as db:
        await db.execute("PRAGMA query_only=ON")
        await db.execute("BEGIN")
        latest = await read_worker_experiences(db, state["project_id"])
        assert len(latest["items"]) == 6 and latest["omitted_count"] == 2
        assert measured not in latest["items"]
        legacy = latest["items"][0]
        assert "original execution revision is not established" in legacy["summary"]
        await require_selected_worker_experiences_locked(
            db, project_id=state["project_id"], items=[measured, legacy]
        )
        assert db.total_changes == 0


@pytest.mark.asyncio
async def test_all_profile_caveats_are_omitted_whole_when_the_summary_cannot_fit(tmp_path):
    from app.services.project_contracts import ProjectResult
    from app.services.project_execution_read import project_execution_summary
    from app.services.project_execution_receipts import PROFILE_COMMANDS, incomplete_reasons

    manager, detail, agent, claim, result = await receipt_job(tmp_path)
    receipt = result["execution_receipt"]
    result["runtime"] = receipt["runtime"] = "python_node"
    receipt["profiles_expected"] += ["node_build", "node_test"]
    receipt["profiles"] += copy.deepcopy(receipt["profiles"])
    result["checks"] += copy.deepcopy(result["checks"])
    receipt["runner_sha256"] = None
    for index, profile in enumerate(receipt["profiles"]):
        profile.update(profile=receipt["profiles_expected"][index], check_index=index)
        result["checks"][index]["command"] = PROFILE_COMMANDS[profile["profile"]]
        profile["exit_code"] = -255
        result["checks"][index].update(exit_code=-255, status="failed")
        if profile["profile"].endswith("_test"):
            profile.update(tests_executed=100_000, test_failures=100_000)
        observation = profile["observation"]
        for key in observation:
            if key.endswith(("_sha256", "_unchanged")):
                observation[key] = None
        observation["errors"] = [
            "environment_unbound",
            "harness_unavailable",
            "source_unavailable",
            "workspace_unavailable",
        ]
    receipt["profiles"][-1].update(observation=None, measurement_error="legacy_harness")
    receipt.update(observation_status="incomplete", incomplete_reasons=incomplete_reasons(receipt))
    parsed = ProjectResult.model_validate(result)
    assert project_execution_summary(parsed.execution_receipt) is None
    job, _ = await submit(manager, agent, claim, result)
    await manager.on_job_result(job)
    state = await ProjectContextService(manager.db_path).refresh(detail["goal"]["id"])
    assert state["experiences"] == {"items": [], "omitted_count": 1}


def test_paired_context_api_exposes_same_historical_card_without_new_wire_fields(
    tmp_path, client, paired_headers, monkeypatch
):
    history = tmp_path / "history"
    history.mkdir()
    manager, detail, _job, _result, _service, state = client.portal.call(
        finished_measurement, history
    )
    monkeypatch.setattr(client.app.state.settings, "project_context_enabled", True)
    monkeypatch.setattr(client.app.state.project_context, "db_path", manager.db_path)
    monkeypatch.setattr(
        client.app.state.goal_manager.project_applications, "db_path", manager.db_path
    )
    response = client.post(f"/goals/{detail['goal']['id']}/context", headers=paired_headers)
    assert response.status_code == 200, response.text
    assert response.json()["experiences"] == state["experiences"]
    capsule = ProjectContextService.prompt_state(response.json())
    assert capsule["experiences"] == state["experiences"]
    assert all(
        item["evidence_status"] == "recorded_check" for item in response.json()["verified_results"]
    )
    assert all(
        item["tested_revision_status"] == "not_established"
        for item in response.json()["verified_results"]
    )


@pytest.mark.asyncio
async def test_compaction_keeps_measured_summary_complete_or_omits_whole_card(tmp_path):
    from app.services.agent_capsule import validate_agent_capsule
    from app.services.project_compaction import ProjectCompactionService
    from tests.test_project_compaction import Provider

    manager, detail, _job, _result, context, state = await finished_measurement(tmp_path)
    provider = Provider()
    compaction = ProjectCompactionService(
        context,
        manager,
        provider,
        enabled=True,
        context_tokens=20_000,
        output_tokens=500,
        overhead_tokens=300,
    )
    await compaction.initialize()
    roomy = await compaction.prepare(detail["goal"]["id"], {"conversation": []})
    assert validate_agent_capsule(roomy["durable_context"])["experiences"] == state["experiences"]
    mandatory = copy.deepcopy(roomy)
    mandatory["durable_context"]["experiences"] = {"items": [], "omitted_count": 1}
    # Leave enough for the fixed mandatory capsule, but not the full card.
    compaction.context_tokens = compaction._count(mandatory) + 800 + 50
    tight = await compaction.prepare(detail["goal"]["id"], {"conversation": []})
    capsule = validate_agent_capsule(tight["durable_context"])
    assert capsule["experiences"] == {"items": [], "omitted_count": 1}
    assert capsule["requirements"] == roomy["durable_context"]["requirements"]
    assert provider.calls == []
