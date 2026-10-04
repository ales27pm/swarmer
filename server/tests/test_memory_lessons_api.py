"""HTTP integration exposes conditional reports without granting execution authority."""

import asyncio
import sqlite3

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.main import create_app
from app.settings import Settings
from tests.conftest import OPERATOR_TOKEN, REPO_ROOT
from tests.conftest import paired_headers as pair_fixture
from tests.test_memory_lessons import lesson_fixture


def test_real_http_propose_assess_list_withdraw_and_operator_revoke(tmp_path):
    _service, manager, project, approval, proposal, accepted = asyncio.run(lesson_fixture(tmp_path))
    app = create_app(
        Settings(
            db_path=manager.db_path,
            workspace_root=tmp_path / "api-workspace",
            permissions_path=REPO_ROOT / "configs/permissions.yaml",
            pairing_bootstrap_token=SecretStr(OPERATOR_TOKEN),
        )
    )
    with TestClient(app, client=("127.0.0.1", 50111)) as client:
        headers = pair_fixture.__wrapped__(client)
        operator = {"X-Mongars-Operator-Token": OPERATOR_TOKEN}
        profile_path = "/memory/execution-profiles/python-fixture"
        approved = client.put(profile_path, json=approval, headers=operator)
        assert approved.status_code == 200, approved.text
        assert approved.json()["execution_attested"] is False
        path = f"/projects/{project}/lessons"
        proposed = client.post(path, json=proposal, headers=headers)
        assert proposed.status_code == 200, proposed.text
        lesson = proposed.json()["lesson_id"]
        path += "/" + lesson
        request = {
            "request_id": "assess-http",
            "expected_version": 1,
            "acceptance_ids": [accepted["id"]],
        }
        assessed = client.post(path + "/assess", json=request, headers=headers)
        assert assessed.status_code == 200, assessed.text
        assert assessed.json()["observation"] == "passed"
        assert assessed.json()["applicability"] == "reported_conditions_match"
        assert assessed.json()["grants_authority"] is False
        assert assessed.json()["evidence_scope"] == "explicit_selection"
        assert "value=1" not in assessed.text and "synthetic private output" not in assessed.text
        assert (
            client.get(f"/projects/{project}/lessons", headers=headers).json()["items"][0][
                "lesson_id"
            ]
            == lesson
        )
        revoked = client.post(
            profile_path + "/withdraw",
            headers=operator,
            json={"request_id": "revoke-http", "expected_version": 1},
        )
        assert revoked.status_code == 200
        stale = client.get(path, headers=headers)
        assert stale.status_code == 200 and stale.json()["applicability"] == "needs_revalidation"
        assert "profile_changed" in stale.json()["reasons"]
        withdrawn = client.post(
            path + "/withdraw",
            headers=headers,
            json={"request_id": "withdraw-http", "expected_version": 2},
        )
        assert withdrawn.status_code == 200 and withdrawn.json()["lifecycle"] == "withdrawn"
        replay = client.post(path + "/assess", json=request, headers=headers)
        assert replay.json()["version"] == 3 and replay.json()["lifecycle"] == "withdrawn"
        for response in (approved, proposed, assessed, revoked, stale, withdrawn, replay):
            assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("params", [{"limit": 0}, {"limit": 51}, {"after_id": "../secret"}])
def test_bounded_query_validation_is_uncached(client, paired_headers, params):
    response = client.get("/projects/project_none/lessons", params=params, headers=paired_headers)
    assert response.status_code == 422 and response.headers["cache-control"] == "no-store"


def test_sql_error_is_sanitized_and_no_store(client, paired_headers):
    with sqlite3.connect(client.app.state.state_service.db_path) as db:
        db.execute("DROP TABLE coding_projects")
    response = client.get("/projects/project_none/lessons", headers=paired_headers)
    assert response.status_code == 503
    assert response.json() == {"detail": "lesson_unavailable"}
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize(
    "payload,status", [(b" " * 65537, 413), ((b"[" * 1100) + (b"]" * 1100), 422)]
)
def test_oversized_and_deep_json_body_are_bounded_and_uncached(
    client, paired_headers, payload, status
):
    response = client.post(
        "/projects/project_none/lessons",
        content=payload,
        headers={**paired_headers, "Content-Type": "application/json"},
    )
    assert response.status_code == status
    assert response.headers["cache-control"] == "no-store"
    assert "Traceback" not in response.text


def test_client_cannot_supply_shell_commands_or_claim_approval(client, paired_headers):
    response = client.post(
        "/projects/project_none/lessons",
        headers=paired_headers,
        json={"argv": ["sh", "-c", "false"], "approved": True},
    )
    assert response.status_code == 422 and response.headers["cache-control"] == "no-store"
