"""Parent cancellation fences approved Hello snapshots without a worker callback."""

from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests import test_swift_project_transfer as transfer

native_project = transfer.native_project


@pytest.mark.parametrize("job_status", ["queued", "claimed", "running"])
def test_parent_cancel_fences_native_job_without_worker_callback(
    client: TestClient,
    paired_headers: dict[str, str],
    test_app: FastAPI,
    native_project: dict[str, Any],
    job_status: str,
) -> None:
    validation = transfer._approve(client, paired_headers, native_project)
    job = None
    headers: dict[str, str] = {}
    if job_status != "queued":
        job, headers = transfer._claim(client, native_project)
    if job_status == "running":
        assert job is not None
        response = client.post(
            f"/agents/{native_project['agent']['id']}/jobs/{job['id']}/heartbeat",
            headers=headers,
            json=transfer._proof(job),
        )
        assert response.status_code == 200, response.text
    db_path = test_app.state.settings.db_path
    before = transfer._stored(
        db_path, "SELECT id,sha256,snapshot_json FROM project_revisions ORDER BY id"
    )
    assert transfer._stored(
        db_path, "SELECT status FROM agent_jobs WHERE id=?", (validation["job_id"],)
    ) == [(job_status,)]

    response = client.post(
        f"/goals/{native_project['goal_id']}/cancel", headers=paired_headers, json={}
    )
    assert response.status_code == 200, response.text
    assert response.json()["goal"]["status"] == "cancelled"
    # Check persisted cancellation before any claim/source/heartbeat can repair it.
    assert transfer._stored(
        db_path,
        "SELECT j.status,t.status FROM agent_jobs j JOIN tasks t ON t.id=j.task_id WHERE j.id=?",
        (validation["job_id"],),
    ) == [("cancelled", "cancelled")]
    assert transfer._stored(
        db_path,
        "SELECT COUNT(*) FROM outbox_events WHERE aggregate_id=? AND event_type='cancelled'",
        (validation["job_id"],),
    ) == [(1,)]
    assert (
        transfer._stored(
            db_path, "SELECT id,sha256,snapshot_json FROM project_revisions ORDER BY id"
        )
        == before
    )
    if job is not None:
        assert (
            client.post(
                transfer._source_path(native_project, job),
                headers=headers,
                json=transfer._proof(job),
            ).status_code
            == 409
        )
