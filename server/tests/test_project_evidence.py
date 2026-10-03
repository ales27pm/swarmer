from __future__ import annotations

import asyncio
import json
import sqlite3

import aiosqlite
import pytest

from app.services.project_evidence import (
    EvidenceMappingConflict,
    read_project_evidence,
    save_project_evidence,
)
from app.services.project_evidence_contracts import EvidenceMappingRequest, RequirementEvidenceView
from app.services.project_graph import read_project_graph
from app.services.state_service import SCHEMA_VERSION, StateService
from app.services.swarm_contracts import GoalCreateRequest
from tests.test_agent_leases import restore_schema30_memory_fixture
from tests.test_project_graph import graph_evidence


def mapping_request(view, **changes):
    revision = view.current_revision
    assert revision is not None
    return EvidenceMappingRequest.model_validate(
        {
            "request_id": "request_one",
            "expected_version": 0,
            "context_sha256": view.context_sha256,
            "conversation_revision": view.conversation_revision,
            "criterion_sha256": view.criteria[0].sha256,
            "project_id": view.project_id,
            "node_id": revision.node_id,
            "revision_id": revision.id,
            "revision_sha256": revision.sha256,
            "file_ids": [revision.files[0].id],
            "check_ids": [revision.checks[0].id],
            "review_status": "linked",
            "public_explanation": "Fichier et contrôle liés.",
            **changes,
        }
    )


@pytest.mark.asyncio
async def test_mapping_persists_exact_requirement_and_evidence_without_inferred_review(tmp_path):
    path = tmp_path / "state.db"
    _, goal, _ = await graph_evidence(path)
    view = await read_project_evidence(path, goal["id"])
    assert view is not None and view.criteria[0].status == "unmapped"
    linked = await save_project_evidence(path, goal["id"], 0, mapping_request(view), "phone")
    assert linked.criteria[0].status == "linked"
    assert linked.criteria[1].status == "unmapped"
    mapping = linked.criteria[0].mapping
    assert mapping is not None and mapping.reviewed_at is None
    assert mapping.criterion_text == "Conserver les clients"
    assert mapping.node_id == "node_02" and mapping.worker_job_id == "job_2"
    assert mapping.revision_id == "revision_one"
    assert mapping.files[0].path == "crm.py" and mapping.checks[0].status == "passed"
    reopened = await read_project_evidence(path, goal["id"])
    assert reopened is not None and reopened.criteria == linked.criteria
    serialized = reopened.model_dump_json()
    assert "private-source" not in serialized and "private-runner-output" not in serialized
    async with aiosqlite.connect(path) as db:
        before = [line async for line in db.iterdump()]
    await read_project_evidence(path, goal["id"])
    async with aiosqlite.connect(path) as db:
        assert [line async for line in db.iterdump()] == before


@pytest.mark.asyncio
async def test_explicit_review_is_versioned_idempotent_redacted_and_never_updates_old_snapshot(
    tmp_path,
):
    path = tmp_path / "state.db"
    _, goal, _ = await graph_evidence(path)
    view = await read_project_evidence(path, goal["id"])
    first = mapping_request(view)
    linked = await save_project_evidence(path, goal["id"], 0, first, "phone")
    reviewed = await save_project_evidence(
        path,
        goal["id"],
        0,
        mapping_request(
            linked,
            request_id="review_request",
            expected_version=1,
            review_status="reviewed",
            public_explanation="Export relu. token=private-review-secret",
        ),
        "phone",
    )
    assert reviewed.criteria[0].status == "reviewed"
    assert reviewed.criteria[0].mapping.version == 2
    assert reviewed.criteria[0].mapping.reviewed_at is not None
    assert "private-review-secret" not in reviewed.model_dump_json()
    replay = await save_project_evidence(path, goal["id"], 0, first, "phone")
    assert replay.criteria[0].mapping.version == 2
    async with aiosqlite.connect(path) as db:
        rows = await (
            await db.execute(
                "SELECT version,mapping_json FROM project_requirement_evidence ORDER BY version"
            )
        ).fetchall()
        assert len(rows) == 2
        assert json.loads(rows[0][1])["review_status"] == "linked"
        assert json.loads(rows[1][1])["review_status"] == "reviewed"
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            await db.execute("UPDATE project_requirement_evidence SET criterion_text='changed'")
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            await db.execute("DELETE FROM project_requirement_evidence")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "forgery",
    [
        {"project_id": "another_project"},
        {"node_id": "node_00"},
        {"revision_id": "another_revision"},
        {"revision_sha256": "0" * 64},
        {"criterion_sha256": "0" * 64},
        {"context_sha256": "0" * 64},
        {"conversation_revision": 3},
        {"expected_version": 1},
        {"file_ids": ["file:other_revision:forged"]},
        {"check_ids": ["check:other_revision:0"]},
    ],
)
async def test_forged_or_cross_project_inputs_cannot_create_links(tmp_path, forgery):
    path = tmp_path / "state.db"
    _, goal, _ = await graph_evidence(path)
    view = await read_project_evidence(path, goal["id"])
    with pytest.raises(EvidenceMappingConflict):
        await save_project_evidence(path, goal["id"], 0, mapping_request(view, **forgery), "phone")
    fresh = await read_project_evidence(path, goal["id"])
    assert fresh.criteria[0].mapping is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change,expected",
    [
        ("UPDATE goal_runs SET conversation_revision=3", "goal_changed"),
        ("UPDATE goal_runs SET completion_criteria_json='[\"Nouveau besoin\"]'", "goal_changed"),
        ("UPDATE goal_runs SET replan_count=replan_count+1", "goal_changed"),
        (
            "UPDATE project_revisions SET snapshot_json=json_set(snapshot_json,'$.checks[0].output','new')",
            "evidence_changed",
        ),
    ],
)
async def test_context_or_receipt_changes_never_reuse_positive_coverage(tmp_path, change, expected):
    path = tmp_path / "state.db"
    _, goal, _ = await graph_evidence(path)
    view = await read_project_evidence(path, goal["id"])
    await save_project_evidence(
        path, goal["id"], 0, mapping_request(view, review_status="reviewed"), "phone"
    )
    async with aiosqlite.connect(path) as db:
        await db.execute(change)
        await db.commit()
    stale = await read_project_evidence(path, goal["id"])
    assert stale.criteria[0].status == "stale"
    assert expected in stale.criteria[0].mapping.stale_reasons
    assert stale.criteria[0].mapping.criterion_text == "Conserver les clients"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "first_objective,second_objective",
    [
        ("Exporter vers /private/client-a", "Exporter vers /private/client-b"),
        ("Exporter avec token=synthetic-first", "Exporter avec token=synthetic-second"),
    ],
)
async def test_raw_plan_change_invalidates_review_even_when_public_objectives_match(
    tmp_path, first_objective, second_objective
):
    path = tmp_path / "state.db"
    _, goal, _ = await graph_evidence(path)
    async with aiosqlite.connect(path) as db:
        await db.execute(
            "UPDATE plan_nodes SET objective=? WHERE id='node_00'",
            (first_objective,),
        )
        await db.commit()
    graph_before = await read_project_graph(path, goal["id"])
    view = await read_project_evidence(path, goal["id"])
    reviewed = await save_project_evidence(
        path, goal["id"], 0, mapping_request(view, review_status="reviewed"), "phone"
    )
    async with aiosqlite.connect(path) as db:
        await db.execute(
            "UPDATE plan_nodes SET objective=? WHERE id='node_00'",
            (second_objective,),
        )
        await db.commit()
    graph_after = await read_project_graph(path, goal["id"])
    # The public projection is deliberately identical despite a different execution target.
    assert graph_before.nodes[0].objective == graph_after.nodes[0].objective
    fresh = await read_project_evidence(path, goal["id"])
    assert fresh.criteria[0].status == "stale"
    assert fresh.criteria[0].mapping.stale_reasons == ["goal_changed"]
    assert fresh.context_sha256 != reviewed.context_sha256
    with pytest.raises(EvidenceMappingConflict, match="changed"):
        await save_project_evidence(
            path,
            goal["id"],
            0,
            mapping_request(
                reviewed,
                request_id="after_hidden_plan_change",
                expected_version=1,
                review_status="reviewed",
            ),
            "phone",
        )
    payload = fresh.model_dump_json() + graph_after.model_dump_json()
    private_values = [value.rsplit(" ", 1)[-1] for value in (first_objective, second_objective)]
    assert all(value not in payload for value in private_values)
    async with aiosqlite.connect(path) as db:
        rows = await (
            await db.execute("SELECT goal_sha256,mapping_json FROM project_requirement_evidence")
        ).fetchall()
    assert len(rows) == 1  # A stale write never creates another immutable review version.
    assert len(rows[0][0]) == 64 and all(char in "0123456789abcdef" for char in rows[0][0])
    assert all(value not in rows[0][1] for value in private_values)


@pytest.mark.asyncio
async def test_new_revision_requires_explicit_remapping_even_for_identical_sources(tmp_path):
    path = tmp_path / "state.db"
    _, goal, _ = await graph_evidence(path)
    view = await read_project_evidence(path, goal["id"])
    await save_project_evidence(
        path, goal["id"], 0, mapping_request(view, review_status="reviewed"), "phone"
    )
    async with aiosqlite.connect(path) as db:
        await db.execute("""INSERT INTO project_revisions(id,project_id,goal_run_id,node_id,
            worker_job_id,revision,snapshot_json,sha256,created_at)
            SELECT 'revision_two',project_id,goal_run_id,'node_01','job_1',2,snapshot_json,
            sha256,created_at FROM project_revisions WHERE id='revision_one'""")
        await db.commit()
    stale = await read_project_evidence(path, goal["id"])
    assert stale.criteria[0].status == "stale"
    assert "revision_changed" in stale.criteria[0].mapping.stale_reasons
    with pytest.raises(EvidenceMappingConflict):
        await save_project_evidence(
            path,
            goal["id"],
            0,
            mapping_request(
                view,
                request_id="stale_update",
                expected_version=1,
            ),
            "phone",
        )
    remapped = await save_project_evidence(
        path,
        goal["id"],
        0,
        mapping_request(
            stale,
            request_id="new_mapping",
            expected_version=1,
        ),
        "phone",
    )
    assert remapped.criteria[0].status == "linked"
    assert remapped.criteria[0].mapping.revision_id == "revision_two"


@pytest.mark.asyncio
@pytest.mark.parametrize("status,exit_code", [("failed", 1), ("skipped", None)])
async def test_failed_or_skipped_receipt_can_be_linked_but_not_reviewed_as_validation(
    tmp_path,
    status,
    exit_code,
):
    path = tmp_path / "state.db"
    _, goal, snapshot = await graph_evidence(path)
    snapshot["checks"][0].update(status=status, exit_code=exit_code)
    async with aiosqlite.connect(path) as db:
        await db.execute("UPDATE project_revisions SET snapshot_json=?", (json.dumps(snapshot),))
        await db.commit()
    view = await read_project_evidence(path, goal["id"])
    with pytest.raises(EvidenceMappingConflict, match="passing"):
        await save_project_evidence(
            path, goal["id"], 0, mapping_request(view, review_status="reviewed"), "phone"
        )
    linked = await save_project_evidence(path, goal["id"], 0, mapping_request(view), "phone")
    assert linked.criteria[0].status == "linked"
    assert linked.criteria[0].mapping.checks[0].status == status


@pytest.mark.asyncio
async def test_removed_criterion_keeps_immutable_snapshot_in_unmatched_history(tmp_path):
    path = tmp_path / "state.db"
    _, goal, _ = await graph_evidence(path)
    view = await read_project_evidence(path, goal["id"])
    await save_project_evidence(path, goal["id"], 0, mapping_request(view), "phone")
    async with aiosqlite.connect(path) as db:
        await db.execute("UPDATE goal_runs SET completion_criteria_json='[]'")
        await db.commit()
    stale = await read_project_evidence(path, goal["id"])
    assert not stale.criteria
    assert stale.unmatched_mappings[0].criterion_text == "Conserver les clients"
    assert "goal_changed" in stale.unmatched_mappings[0].stale_reasons


@pytest.mark.asyncio
async def test_successor_goal_does_not_inherit_evidence_and_fences_predecessor_mapping(tmp_path):
    path = tmp_path / "state.db"
    manager, goal, _ = await graph_evidence(path)
    view = await read_project_evidence(path, goal["id"])
    await save_project_evidence(path, goal["id"], 0, mapping_request(view), "phone")
    successor = await manager.create_goal(
        GoalCreateRequest(objective="Suite du projet"), actor_id="phone"
    )
    async with aiosqlite.connect(path) as db:
        await db.execute(
            "UPDATE goal_project_links SET project_id='project_one' WHERE goal_run_id=?",
            (successor["id"],),
        )
        await db.execute(
            "UPDATE goal_runs SET completion_criteria_json='[\"Conserver les clients\"]' WHERE id=?",
            (successor["id"],),
        )
        await db.execute(
            "UPDATE goal_conversations SET active_goal_id=? WHERE active_goal_id=?",
            (successor["id"], goal["id"]),
        )
        await db.commit()
    old = await read_project_evidence(path, goal["id"])
    new = await read_project_evidence(path, successor["id"])
    assert old.criteria[0].status == "stale"
    assert new.criteria[0].status == "unmapped"
    assert new.current_revision.goal_run_id == goal["id"]
    with pytest.raises(EvidenceMappingConflict):
        await save_project_evidence(path, successor["id"], 0, mapping_request(new), "phone")


@pytest.mark.asyncio
async def test_concurrent_updates_and_request_reuse_do_not_overwrite_mapping(tmp_path):
    path = tmp_path / "state.db"
    _, goal, _ = await graph_evidence(path)
    view = await read_project_evidence(path, goal["id"])
    first = mapping_request(view)
    results = await asyncio.gather(
        save_project_evidence(path, goal["id"], 0, first, "phone"),
        save_project_evidence(
            path, goal["id"], 0, mapping_request(view, request_id="request_two"), "other_phone"
        ),
        return_exceptions=True,
    )
    assert sum(isinstance(result, EvidenceMappingConflict) for result in results) == 1
    fresh = await read_project_evidence(path, goal["id"])
    assert fresh.criteria[0].mapping.version == 1
    async with aiosqlite.connect(path) as db:
        used = (
            await (
                await db.execute("SELECT request_id FROM project_requirement_evidence")
            ).fetchone()
        )[0]
    with pytest.raises(EvidenceMappingConflict, match="identifier"):
        await save_project_evidence(
            path,
            goal["id"],
            0,
            mapping_request(
                view,
                request_id=used,
                public_explanation="Changed request",
            ),
            "phone",
        )


async def remove_v27(path):
    async with aiosqlite.connect(path) as db:
        await restore_schema30_memory_fixture(db)
        await db.execute("DROP TABLE project_requirement_evidence")
        await db.execute("PRAGMA user_version=26")
        await db.commit()


@pytest.mark.asyncio
async def test_schema26_migrates_idempotently_without_synthesizing_coverage(tmp_path):
    path = tmp_path / "state.db"
    _, goal, _ = await graph_evidence(path)
    await remove_v27(path)
    state = StateService(path)
    await state.initialize()
    await state.initialize()
    view = await read_project_evidence(path, goal["id"])
    assert view.criteria[0].status == "unmapped"
    async with aiosqlite.connect(path) as db:
        assert (await (await db.execute("PRAGMA user_version")).fetchone())[0] == SCHEMA_VERSION


@pytest.mark.asyncio
async def test_migration_failure_rolls_back_table_and_version(tmp_path, monkeypatch):
    from app.services import state_service
    from app.services.project_evidence_schema import migrate_project_evidence

    path = tmp_path / "state.db"
    await StateService(path).initialize()
    await remove_v27(path)

    async def broken(db):
        await migrate_project_evidence(db)
        raise RuntimeError("injected migration failure")

    monkeypatch.setattr(state_service, "migrate_project_evidence", broken)
    with pytest.raises(RuntimeError, match="injected"):
        await StateService(path).initialize()
    async with aiosqlite.connect(path) as db:
        assert (await (await db.execute("PRAGMA user_version")).fetchone())[0] == 26
        assert (
            await (
                await db.execute(
                    "SELECT name FROM sqlite_master WHERE name='project_requirement_evidence'"
                )
            ).fetchone()
            is None
        )


def test_authenticated_api_creates_reviews_and_reads_mapping_with_legacy_graph_compatibility(
    test_app,
    client,
    paired_headers,
):
    _, goal, _ = client.portal.call(graph_evidence, test_app.state.settings.db_path)
    url = f"/goals/{goal['id']}/evidence"
    assert client.get(url).status_code == 401
    response = client.get(url, headers=paired_headers)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "private, no-store"
    view = RequirementEvidenceView.model_validate(response.json())
    request = mapping_request(view).model_dump()
    assert client.put(url + "/0", json=request).status_code == 401
    created = client.put(url + "/0", json=request, headers=paired_headers)
    assert created.status_code == 200
    assert created.json()["criteria"][0]["status"] == "linked"
    linked = RequirementEvidenceView.model_validate(created.json())
    reviewed = client.put(
        url + "/0",
        headers=paired_headers,
        json=mapping_request(
            linked,
            request_id="explicit_review",
            expected_version=1,
            review_status="reviewed",
        ).model_dump(),
    )
    assert reviewed.status_code == 200
    assert reviewed.json()["criteria"][0]["status"] == "reviewed"
    assert reviewed.headers["cache-control"] == "private, no-store"
    assert "actor_id" not in reviewed.text and "private-runner-output" not in reviewed.text
    conflict = client.put(
        url + "/0",
        headers=paired_headers,
        json={
            **request,
            "request_id": "different_request",
        },
    )
    assert conflict.status_code == 409
    assert client.get(f"/goals/{goal['id']}/graph", headers=paired_headers).status_code == 200
    assert client.get("/goals/missing/evidence", headers=paired_headers).status_code == 404
    assert client.put(url + "/20", headers=paired_headers, json=request).status_code == 422


def test_corrupt_evidence_api_returns_safe_unavailability(test_app, client, paired_headers):
    path = test_app.state.settings.db_path
    _, goal, _ = client.portal.call(graph_evidence, path)

    async def damage():
        async with aiosqlite.connect(path) as db:
            await db.execute("UPDATE project_revisions SET snapshot_json='not-json'")
            await db.commit()

    view = client.portal.call(read_project_evidence, path, goal["id"])
    request = mapping_request(view).model_dump()
    client.portal.call(damage)
    url = f"/goals/{goal['id']}/evidence"
    for response in (
        client.get(url, headers=paired_headers),
        client.put(url + "/0", headers=paired_headers, json=request),
    ):
        assert response.status_code == 503
        assert response.json() == {"detail": "requirement evidence unavailable"}


@pytest.mark.asyncio
async def test_receipt_changed_after_read_is_fenced_even_if_revision_and_file_hash_match(tmp_path):
    path = tmp_path / "state.db"
    _, goal, _ = await graph_evidence(path)
    view = await read_project_evidence(path, goal["id"])
    async with aiosqlite.connect(path) as db:
        await db.execute("""UPDATE project_revisions SET snapshot_json=json_set(snapshot_json,
            '$.checks[0].command',json('["node","--test"]'))""")
        await db.commit()
    fresh = await read_project_evidence(path, goal["id"])
    assert fresh.current_revision.id == view.current_revision.id
    assert fresh.current_revision.sha256 == view.current_revision.sha256
    assert fresh.context_sha256 != view.context_sha256
    with pytest.raises(EvidenceMappingConflict, match="changed"):
        await save_project_evidence(
            path, goal["id"], 0, mapping_request(view, review_status="reviewed"), "phone"
        )
    assert fresh.criteria[0].mapping is None


@pytest.mark.asyncio
async def test_failed_audit_rolls_back_mapping_and_allows_explicit_retry(tmp_path, monkeypatch):
    from app.services import project_evidence

    path = tmp_path / "state.db"
    _, goal, _ = await graph_evidence(path)
    view = await read_project_evidence(path, goal["id"])
    original = project_evidence.append_audit_event

    async def fail(*args, **kwargs):
        raise RuntimeError("injected audit failure")

    monkeypatch.setattr(project_evidence, "append_audit_event", fail)
    with pytest.raises(RuntimeError, match="injected"):
        await save_project_evidence(path, goal["id"], 0, mapping_request(view), "phone")
    unchanged = await read_project_evidence(path, goal["id"])
    assert unchanged.criteria[0].status == "unmapped"
    monkeypatch.setattr(project_evidence, "append_audit_event", original)
    retry = await save_project_evidence(path, goal["id"], 0, mapping_request(view), "phone")
    assert retry.criteria[0].mapping.version == 1


@pytest.mark.asyncio
async def test_one_passing_receipt_cannot_hide_another_selected_failed_check(tmp_path):
    path = tmp_path / "state.db"
    _, goal, snapshot = await graph_evidence(path)
    snapshot["checks"].append(
        {
            "command": ["npm", "run", "build"],
            "status": "failed",
            "exit_code": 1,
            "duration_ms": 30,
            "output": "private build error",
        }
    )
    async with aiosqlite.connect(path) as db:
        await db.execute("UPDATE project_revisions SET snapshot_json=?", (json.dumps(snapshot),))
        await db.commit()
    view = await read_project_evidence(path, goal["id"])
    selected = [check.id for check in view.current_revision.checks]
    with pytest.raises(EvidenceMappingConflict, match="passing"):
        await save_project_evidence(
            path,
            goal["id"],
            0,
            mapping_request(
                view,
                check_ids=selected,
                review_status="reviewed",
            ),
            "phone",
        )
    linked = await save_project_evidence(
        path,
        goal["id"],
        0,
        mapping_request(
            view,
            check_ids=selected,
        ),
        "phone",
    )
    assert linked.criteria[0].status == "linked"
    assert len(linked.criteria[0].mapping.checks) == 2
