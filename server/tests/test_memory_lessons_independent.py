"""Independent boundaries using accepted reports and separate SQLite connections.

All workers, profiles, oracle declarations and outputs in these tests are synthetic.
Passing these tests does not qualify an actual runtime or attest worker execution.
"""

import asyncio
import json

import aiosqlite
import pytest
from fastapi.testclient import TestClient

from app.services import memory_lessons as lessons
from app.services.memory_lessons_contracts import (
    LessonAssessment,
    LessonProposal,
    LessonWithdrawal,
    ProfileApproval,
    ProfileWithdrawal,
)
from tests.conftest import OPERATOR_TOKEN
from tests.test_memory_lessons import lesson_fixture


async def assessed_fixture(tmp_path):
    service, manager, project, approval, proposal, accepted = await lesson_fixture(tmp_path)
    await service.approve_profile(
        "python-fixture", ProfileApproval.model_validate(approval), "operator"
    )
    candidate = await service.propose(project, LessonProposal.model_validate(proposal), "device")
    request = LessonAssessment(
        request_id="independent-assess", expected_version=1, acceptance_ids=[accepted["id"]]
    )
    assessed = await service.assess(project, candidate.lesson_id, request, "device")
    assert assessed.observation == "passed"
    assert assessed.applicability == "reported_conditions_match"
    return service, manager, project, candidate, accepted, request


@pytest.mark.asyncio
async def test_get_snapshot_survives_concurrent_evidence_erasure_then_requalifies(
    tmp_path, monkeypatch
):
    service, manager, project, candidate, accepted, _ = await assessed_fixture(tmp_path)
    async with aiosqlite.connect(manager.db_path) as db:
        assert (await (await db.execute("PRAGMA journal_mode")).fetchone())[0] == "wal"
    reached, proceed = asyncio.Event(), asyncio.Event()
    original = lessons._binding_reasons

    async def pause_after_snapshot(*args):
        result = await original(*args)
        reached.set()
        await asyncio.wait_for(proceed.wait(), 5)
        return result

    monkeypatch.setattr(lessons, "_binding_reasons", pause_after_snapshot)
    reading = asyncio.create_task(service.get(project, candidate.lesson_id))
    try:
        await asyncio.wait_for(reached.wait(), 5)
        async with aiosqlite.connect(manager.db_path) as db:
            await db.execute("PRAGMA foreign_keys=ON")
            await db.execute(
                "DELETE FROM project_execution_acceptances WHERE id=?", (accepted["id"],)
            )
            await db.commit()
    finally:
        proceed.set()
    old_snapshot = await asyncio.wait_for(reading, 5)
    assert old_snapshot.observation == "passed"
    assert old_snapshot.applicability == "reported_conditions_match"
    new_snapshot = await lessons.MemoryLessonService(manager.db_path).get(
        project, candidate.lesson_id
    )
    assert new_snapshot.observation == "unknown"
    assert new_snapshot.applicability == "needs_revalidation"
    assert new_snapshot.evidence[0].acceptance_id == accepted["id"]
    assert "evidence_unavailable" in new_snapshot.reasons
    async with aiosqlite.connect(manager.db_path) as db:
        assert await (
            await db.execute("SELECT COUNT(*) FROM project_execution_acceptances")
        ).fetchone() == (0,)
        assert await (
            await db.execute("SELECT COUNT(*) FROM memory_procedure_evidence")
        ).fetchone() == (1,)


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["current_source", "profile_body", "evidence_selection"])
async def test_current_conditions_and_complete_selection_cannot_be_silently_tampered(
    tmp_path, mutation
):
    service, manager, project, candidate, _, _ = await assessed_fixture(tmp_path)
    async with aiosqlite.connect(manager.db_path) as db:
        if mutation == "current_source":
            await db.execute("UPDATE project_revisions SET sha256=?", ("0" * 64,))
        elif mutation == "profile_body":
            raw = (
                await (
                    await db.execute("SELECT body_json FROM memory_execution_profiles")
                ).fetchone()
            )[0]
            body = json.loads(raw)
            body["qualification_kind"] = "synthetic_only"
            await db.execute(
                "UPDATE memory_execution_profiles SET body_json=?", (json.dumps(body),)
            )
        else:
            await db.execute("DELETE FROM memory_procedure_evidence")
        await db.commit()
    if mutation == "current_source":
        view = await service.get(project, candidate.lesson_id)
        assert view.applicability == "needs_revalidation" and view.observation == "unknown"
    else:
        with pytest.raises(lessons.LessonError) as error:
            await service.get(project, candidate.lesson_id)
        assert error.value.status_code == 503


@pytest.mark.asyncio
async def test_two_versioned_writers_have_one_winner_and_replay_cannot_reapprove(tmp_path):
    service, manager, project, candidate, _, assessment = await assessed_fixture(tmp_path)
    requests = [
        LessonWithdrawal(request_id=f"competing-{index}", expected_version=2) for index in range(2)
    ]
    results = await asyncio.gather(
        *(
            service.withdraw(project, candidate.lesson_id, request, "device")
            for request in requests
        ),
        return_exceptions=True,
    )
    assert sum(not isinstance(value, Exception) for value in results) == 1
    rejected = next(value for value in results if isinstance(value, Exception))
    assert isinstance(rejected, lessons.LessonError) and rejected.code == "lesson_version_changed"
    restarted = lessons.MemoryLessonService(manager.db_path)
    replay = await restarted.assess(project, candidate.lesson_id, assessment, "device")
    assert replay.version == 3 and replay.lifecycle == "withdrawn"
    await restarted.revoke_profile(
        "python-fixture",
        ProfileWithdrawal(request_id="independent-revoke", expected_version=1),
        "operator",
    )
    view = await restarted.get(project, candidate.lesson_id)
    assert view.applicability == "withdrawn" and "profile_changed" in view.reasons
    async with aiosqlite.connect(manager.db_path) as db:
        assert await (
            await db.execute("SELECT COUNT(*) FROM memory_procedure_lessons")
        ).fetchone() == (3,)


def test_profile_operator_auth_is_separate_from_device_agent_and_remote_host(
    client, paired_headers, test_app
):
    # Authentication must reject before payload validation or database mutations.
    route = "/memory/execution-profiles/independent-auth"
    assert client.put(route, json={}, headers=paired_headers).status_code == 403
    registered = client.post(
        "/agents/register",
        headers=paired_headers,
        json={
            "name": "independent-synthetic-worker",
            "endpoint": "https://worker.invalid",
            "skills": ["code.build_project"],
        },
    )
    assert registered.status_code == 201
    token = registered.json()["credential"]
    assert (
        client.put(route, json={}, headers={"Authorization": "Bearer " + token}).status_code == 403
    )
    operator = {"X-Mongars-Operator-Token": OPERATOR_TOKEN}
    assert client.put(route, json={}, headers=operator).status_code == 422
    remote = TestClient(test_app, client=("198.51.100.7", 50123))
    try:
        assert remote.put(route, json={}, headers=operator).status_code == 403
    finally:
        remote.close()
