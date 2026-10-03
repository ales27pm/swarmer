"""Goal memory call receipts remain visible through the authenticated activity API."""

import json
from unittest.mock import AsyncMock

import aiosqlite
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.services.activity_contracts import ActivityItem
from app.services.model_request_execution import MEMORY_MODEL_ROLES
from tests.test_task_execution import seed


def test_memory_roles_are_visible_in_goal_and_root_task_activity(
    test_app: FastAPI,
    client: TestClient,
    paired_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
):
    assert client.portal is not None
    manager = test_app.state.goal_manager
    goal, children = client.portal.call(seed, manager)

    async def receipts(insert=False):
        async with aiosqlite.connect(manager.db_path) as db:
            if insert:
                for index, role in enumerate(sorted(MEMORY_MODEL_ROLES)):
                    await db.execute(
                        """INSERT INTO goal_model_calls(id,goal_run_id,role,provider_source,
                        model_id,input_digest,output_digest,status,latency_ms,created_at,completed_at,
                        error_category,conversation_revision) VALUES(?,?,?,'ubuntu_local',
                        'local-memory-model','private-input-digest','private-output-digest',?,125,
                        '2026-10-03T10:00:00+00:00','2026-10-03T10:00:00.125+00:00',?,0)""",
                        (
                            "call_" + role,
                            goal["id"],
                            role,
                            "failed" if index == 0 else "completed",
                            "private-error" if index == 0 else None,
                        ),
                    )
                await db.commit()
            return await (
                await db.execute("SELECT rowid,* FROM goal_model_calls ORDER BY rowid")
            ).fetchall()

    before = client.portal.call(receipts, True)
    reconcile = AsyncMock(side_effect=AssertionError("activity read must not execute or reconcile"))
    monkeypatch.setattr(manager, "get_goal", reconcile)
    for path in (f"/goals/{goal['id']}/activity", f"/tasks/{goal['root_task_id']}/activity"):
        assert client.get(path).status_code == 401
        response = client.get(path, headers=paired_headers)
        assert response.status_code == 200
        assert response.headers["cache-control"] == "private, no-store"
        page = response.json()
        calls = {item["role"]: item for item in page["items"] if item["kind"] == "model_call"}
        assert set(calls) == MEMORY_MODEL_ROLES
        for role, item in calls.items():
            assert item["id"] == "model_call:call_" + role
            assert item["model_id"] == "local-memory-model"
            assert item["duration_ms"] == 125
            assert item["status"] == ("failed" if role == min(MEMORY_MODEL_ROLES) else "completed")
        assert page["coverage"]["live_operations"] is False
        assert "private-" not in json.dumps(page)
    child = client.get(f"/tasks/{children[0]}/activity", headers=paired_headers)
    assert child.status_code == 200
    assert all(item["kind"] != "model_call" for item in child.json()["items"])
    assert client.portal.call(receipts) == before
    reconcile.assert_not_called()


def test_activity_still_rejects_unrecognized_model_roles():
    with pytest.raises(ValidationError):
        ActivityItem(
            id="model_call:unknown",
            kind="model_call",
            status="completed",
            title="Appel modèle",
            recorded_at="2026-10-03T10:00:00+00:00",
            role="unrecognized_role",
        )
