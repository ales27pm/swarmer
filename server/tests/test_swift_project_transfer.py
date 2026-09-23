"""Revision-bound native validation through the paired-device and worker APIs.

All sources are handwritten Hello fixtures. No production database, model or
user project is involved; compiler execution is tested separately by the worker.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
import urllib.error
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.main import create_app
from app.models import AgentCreate
from app.services.evaluator_provider import NoopEvaluatorProvider
from app.services.planner_provider import DeterministicSwarmPlannerProvider
from app.services.project_contracts import project_digest
from app.services.swarm_contracts import GoalCreateRequest, GoalStartRequest, SwarmPlanNodeProposal
from tests.conftest import OPERATOR_TOKEN
from tests.test_goal_runtime_recovery import _worker_plan
from tests.test_swift_worker import worker

HELLO_FILES = [
    {
        "path": "Package.swift",
        "content": (
            "// swift-tools-version: 6.0\nimport PackageDescription\n"
            'let package = Package(name: "Hello", targets: [.testTarget(name: "HelloTests")])\n'
        ),
    },
    {
        "path": "Tests/HelloTests/HelloTests.swift",
        "content": (
            "import XCTest\nfinal class HelloTests: XCTestCase {\n"
            'func testGreeting() { XCTAssertEqual(String(2 + 2), "4") }\n}\n'
        ),
    },
    {"path": "README.md", "content": "Private Hello fixture. Run swift test.\n"},
]


def _pair(client: TestClient) -> dict[str, str]:
    response = client.post("/pairing/code", headers={"X-Mongars-Operator-Token": OPERATOR_TOKEN})
    assert response.status_code == 200
    candidate = client.post(
        "/pairing/complete",
        json={"code": response.json()["code"], "device_id": "test-phone", "name": "pytest"},
    )
    assert candidate.status_code == 200
    record = candidate.json()
    headers = {"Authorization": f"Bearer {record['candidate_token']}"}
    assert (
        client.post(
            "/pairing/finalize",
            headers=headers,
            json={"pairing_id": record["pairing_id"], "device_id": record["device_id"]},
        ).status_code
        == 200
    )
    return headers


async def _prepare(app: FastAPI, *, extra_path: str | None = None) -> dict[str, Any]:
    """Persist a native snapshot by the same authenticated result path as production."""
    manager = app.state.goal_manager
    plan = _worker_plan(objective="Create the reviewed Hello Swift test package")
    plan.nodes[0] = plan.nodes[0].model_copy(
        update={
            "required_skill": "code.build_project",
            "objective": plan.objective,
            "title": "Create Hello Swift",
        }
    )
    manager.planner = DeterministicSwarmPlannerProvider(plan)
    manager.evaluator = NoopEvaluatorProvider()
    generator = await app.state.state_service.register_agent(
        AgentCreate(
            name="hello-project-fixture",
            endpoint="http://127.0.0.1",
            skills=["code.build_project"],
        ),
        "test-operator",
    )
    await app.state.state_service.heartbeat_agent(
        generator["id"], "online", generator["credential"]
    )
    goal = await manager.create_goal(
        GoalCreateRequest(objective=plan.objective), actor_id="test-phone"
    )
    await manager.start_goal(goal["id"], GoalStartRequest())
    claimed = await app.state.agent_dispatcher.claim(generator["id"])
    assert claimed is not None
    files = [dict(file) for file in HELLO_FILES]
    if extra_path:
        files.append({"path": extra_path, "content": "inert reserved-path fixture"})
    snapshot = {
        "schema_version": "1.0",
        "action": "continue",
        "message": "Hello source awaits independent native validation.",
        "plan": ["Implement Hello", "Compile and execute its XCTest"],
        "files": files,
        "checks": [],
        "run_instructions": "Run swift test.",
        "runtime": "python",
        "base_revision_id": claimed["payload"]["base_revision_id"],
        "base_sha256": claimed["payload"]["base_sha256"],
    }
    accepted, _ = await app.state.agent_dispatcher.submit_result(
        generator["id"],
        claimed["id"],
        claimed["claim_token"],
        status="completed",
        result=snapshot,
        error=None,
        lease_id=claimed["lease_id"],
        lease_generation=claimed["lease_generation"],
    )
    await manager.on_job_result(accepted)
    preview = await manager.project_applications.get_project(goal["id"])
    assert preview and preview["state"] == "needs_user"
    assert preview["sha256"] == project_digest(files)
    swift = await app.state.state_service.register_agent(
        AgentCreate(
            name="hello-swift-fixture",
            endpoint="http://127.0.0.1",
            skills=["code.swift.build", "code.swift.test"],
            capacity={"max_operation_seconds": 120, "max_result_bytes": 16_384},
        ),
        "test-operator",
    )
    await app.state.state_service.heartbeat_agent(swift["id"], "online", swift["credential"])
    return {
        "goal_id": goal["id"],
        "preview": preview,
        "files": files,
        "agent": swift,
        "path": f"/goals/{goal['id']}/project/swift-validation",
    }


@pytest.fixture
async def native_project(client: TestClient, test_app: FastAPI) -> dict[str, Any]:
    del client  # The fixture starts the app and initializes its private database.
    return await _prepare(test_app)


def _request(project: dict[str, Any], **updates: Any) -> dict[str, Any]:
    return {
        "revision_id": project["preview"]["revision_id"],
        "sha256": project["preview"]["sha256"],
        "agent_id": project["agent"]["id"],
        "operation": "test",
        "target": {"kind": "swiftpm"},
        "execution_consent": True,
        "idempotency_key": str(uuid4()),
        **updates,
    }


def _approve(
    client: TestClient, headers: dict[str, str], project: dict[str, Any], **updates: Any
) -> dict[str, Any]:
    response = client.post(project["path"], headers=headers, json=_request(project, **updates))
    assert response.status_code == 201, response.text
    return response.json()


def _stored(db_path: Path, query: str, parameters: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
    with sqlite3.connect(db_path) as db:
        return db.execute(query, parameters).fetchall()


def _proof(job: dict[str, Any]) -> dict[str, Any]:
    return {key: job[key] for key in ("claim_token", "lease_id", "lease_generation")}


def _claim(client: TestClient, project: dict[str, Any]) -> tuple[dict[str, Any], dict[str, str]]:
    agent = project["agent"]
    headers = {"Authorization": f"Bearer {agent['credential']}"}
    response = client.post(
        f"/agents/{agent['id']}/claim", headers=headers, json={"wait_seconds": 0}
    )
    assert response.status_code == 200, response.text
    assert response.json() is not None
    return response.json(), headers


def _source_path(project: dict[str, Any], job: dict[str, Any]) -> str:
    return f"/agents/{project['agent']['id']}/jobs/{job['id']}/project-source"


def test_transfer_requires_paired_device_and_explicit_execution_consent(
    client: TestClient, paired_headers: dict[str, str], native_project: dict[str, Any]
) -> None:
    project = native_project
    assert client.post(project["path"], json=_request(project)).status_code == 401
    assert client.get(project["path"]).status_code == 401
    agent_headers = {"Authorization": f"Bearer {project['agent']['credential']}"}
    assert (
        client.post(project["path"], headers=agent_headers, json=_request(project)).status_code
        == 401
    )
    for consent in (False, None, "true", 1):
        response = client.post(
            project["path"],
            headers=paired_headers,
            json=_request(project, execution_consent=consent),
        )
        assert response.status_code in {409, 422}, response.text


def test_approval_queues_one_revision_bound_job_without_rewriting_source(
    client: TestClient,
    paired_headers: dict[str, str],
    test_app: FastAPI,
    native_project: dict[str, Any],
) -> None:
    project = native_project
    db_path = test_app.state.settings.db_path
    before = _stored(db_path, "SELECT id,sha256,snapshot_json FROM project_revisions ORDER BY id")
    body = _request(project)
    first = client.post(project["path"], headers=paired_headers, json=body)
    again = client.post(project["path"], headers=paired_headers, json=body)
    assert first.status_code == again.status_code == 201
    value = first.json()
    assert again.json()["validation_id"] == value["validation_id"]
    assert again.json()["job_id"] == value["job_id"]
    assert value["status"] == "queued" and value["receipt"] is None
    assert value["revision_id"] == body["revision_id"] and value["sha256"] == body["sha256"]
    assert value["operation"] == "test" and value["agent_id"] == body["agent_id"]
    assert value["target"] == {"kind": "swiftpm"}
    assert len(value["source_sha256"]) == 64 and value["source_sha256"] != value["sha256"]
    assert "files" not in value and "Private Hello fixture" not in json.dumps(value)
    get = client.get(project["path"], headers=paired_headers)
    assert get.status_code == 200 and get.json()["validation_id"] == value["validation_id"]
    assert get.headers.get("cache-control") == "no-store"
    assert _stored(db_path, "SELECT COUNT(*) FROM swift_project_validations") == [(1,)]
    assert (
        _stored(db_path, "SELECT id,sha256,snapshot_json FROM project_revisions ORDER BY id")
        == before
    )
    job, _ = _claim(client, project)
    assert job["id"] == value["job_id"] and job["max_attempts"] == 1
    assert job["payload"]["project_revision"] == {
        "validation_id": value["validation_id"],
        "project_id": project["preview"]["project_id"],
        "revision_id": body["revision_id"],
        "sha256": body["sha256"],
    }


@pytest.mark.asyncio
async def test_idempotence_survives_actual_api_restart(test_app: FastAPI) -> None:
    with TestClient(test_app, client=("127.0.0.1", 50_000)) as first_client:
        headers = _pair(first_client)
        project = await _prepare(test_app)
        body = _request(project)
        response = first_client.post(project["path"], headers=headers, json=body)
        assert response.status_code == 201, response.text
        first = response.json()
    restarted = create_app(test_app.state.settings)
    with TestClient(restarted, client=("127.0.0.1", 50_000)) as second_client:
        replay = second_client.post(project["path"], headers=headers, json=body)
        assert replay.status_code == 201, replay.text
        assert replay.json()["validation_id"] == first["validation_id"]
        assert replay.json()["job_id"] == first["job_id"]
        assert _stored(
            test_app.state.settings.db_path, "SELECT COUNT(*) FROM swift_project_validations"
        ) == [(1,)]


@pytest.mark.parametrize(
    "updates",
    [
        {"sha256": "0" * 64},
        {"revision_id": "revision_missing"},
        {"target": {"kind": "swiftpm", "shell": "swift test"}},
        {
            "target": {
                "kind": "xcode",
                "project": "../Hello.xcodeproj",
                "scheme": "Hello",
                "destination": "sim",
            }
        },
    ],
)
def test_unreviewed_source_or_unbounded_target_is_rejected(
    client: TestClient,
    paired_headers: dict[str, str],
    native_project: dict[str, Any],
    updates: dict[str, Any],
) -> None:
    response = client.post(
        native_project["path"], headers=paired_headers, json=_request(native_project, **updates)
    )
    assert response.status_code in {409, 422}, response.text


def test_idempotency_key_cannot_authorize_a_different_operation(
    client: TestClient, paired_headers: dict[str, str], native_project: dict[str, Any]
) -> None:
    key = str(uuid4())
    _approve(client, paired_headers, native_project, idempotency_key=key)
    changed = client.post(
        native_project["path"],
        headers=paired_headers,
        json=_request(native_project, idempotency_key=key, operation="build"),
    )
    assert changed.status_code == 409


@pytest.mark.asyncio
@pytest.mark.parametrize("path", [".build/cached.swift", "app/.swarmer-swift-runs/receipt.json"])
async def test_snapshot_cannot_hide_files_in_native_digest_exclusions(
    client: TestClient, paired_headers: dict[str, str], test_app: FastAPI, path: str
) -> None:
    project = await _prepare(test_app, extra_path=path)
    response = client.post(project["path"], headers=paired_headers, json=_request(project))
    assert response.status_code in {409, 422}, response.text
    assert not _stored(test_app.state.settings.db_path, "SELECT * FROM swift_project_validations")


def test_worker_can_fetch_exact_persisted_files_only_with_current_lease(
    client: TestClient, paired_headers: dict[str, str], native_project: dict[str, Any]
) -> None:
    value = _approve(client, paired_headers, native_project)
    job, worker_headers = _claim(client, native_project)
    path = _source_path(native_project, job)
    assert client.post(path, json=_proof(job)).status_code == 401
    assert client.post(path, headers=paired_headers, json=_proof(job)).status_code == 401
    response = client.post(path, headers=worker_headers, json=_proof(job))
    assert response.status_code == 200, response.text
    source = response.json()
    assert source["files"] == native_project["files"]
    assert source["project_revision"] == job["payload"]["project_revision"]
    assert project_digest(source["files"]) == value["sha256"]
    assert source["source_sha256"] == value["source_sha256"]
    assert source["target"] == {"kind": "swiftpm", "source_sha256": value["source_sha256"]}
    assert response.headers.get("cache-control") == "no-store"
    stale = {**_proof(job), "lease_generation": job["lease_generation"] + 1}
    assert client.post(path, headers=worker_headers, json=stale).status_code == 409
    wrong_token = {**_proof(job), "claim_token": "wrong-token-with-enough-characters"}
    assert client.post(path, headers=worker_headers, json=wrong_token).status_code == 409


@pytest.mark.asyncio
async def test_another_worker_cannot_claim_or_fetch_an_approved_revision(
    client: TestClient,
    paired_headers: dict[str, str],
    test_app: FastAPI,
    native_project: dict[str, Any],
) -> None:
    _approve(client, paired_headers, native_project)
    other = await test_app.state.state_service.register_agent(
        AgentCreate(
            name="other-swift-worker", endpoint="http://127.0.0.1", skills=["code.swift.test"]
        ),
        "test-operator",
    )
    await test_app.state.state_service.heartbeat_agent(other["id"], "online", other["credential"])
    headers = {"Authorization": f"Bearer {other['credential']}"}
    claimed = client.post(f"/agents/{other['id']}/claim", headers=headers, json={"wait_seconds": 0})
    assert claimed.status_code == 200 and claimed.json() is None
    job, _ = _claim(client, native_project)
    response = client.post(
        f"/agents/{other['id']}/jobs/{job['id']}/project-source", headers=headers, json=_proof(job)
    )
    assert response.status_code in {403, 409}


def test_cancellation_invalidates_source_and_heartbeat_without_losing_snapshot(
    client: TestClient,
    paired_headers: dict[str, str],
    test_app: FastAPI,
    native_project: dict[str, Any],
) -> None:
    value = _approve(client, paired_headers, native_project)
    job, headers = _claim(client, native_project)
    before = _stored(
        test_app.state.settings.db_path, "SELECT id,sha256,snapshot_json FROM project_revisions"
    )
    path = f"{native_project['path']}/{value['validation_id']}/cancel"
    for _ in range(2):
        response = client.post(path, headers=paired_headers)
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "cancelled"
    assert (
        _stored(
            test_app.state.settings.db_path,
            "SELECT status FROM agent_jobs WHERE id=?",
            (job["id"],),
        )[0][0]
        == "cancelled"
    )
    assert (
        _stored(
            test_app.state.settings.db_path,
            "SELECT status FROM tasks WHERE id=(SELECT task_id FROM swift_project_validations WHERE id=?)",
            (value["validation_id"],),
        )[0][0]
        == "cancelled"
    )
    assert (
        client.post(
            _source_path(native_project, job), headers=headers, json=_proof(job)
        ).status_code
        == 409
    )
    heartbeat = client.post(
        f"/agents/{native_project['agent']['id']}/jobs/{job['id']}/heartbeat",
        headers=headers,
        json=_proof(job),
    )
    assert heartbeat.status_code == 409
    assert (
        _stored(
            test_app.state.settings.db_path, "SELECT id,sha256,snapshot_json FROM project_revisions"
        )
        == before
    )


def test_new_conversation_revision_invalidates_source_authority(
    client: TestClient,
    paired_headers: dict[str, str],
    test_app: FastAPI,
    native_project: dict[str, Any],
) -> None:
    _approve(client, paired_headers, native_project)
    job, headers = _claim(client, native_project)
    # Deterministic simulation of the committed user steering boundary, without
    # dispatching a model or modifying the accepted project source.
    with sqlite3.connect(test_app.state.settings.db_path) as db:
        db.execute(
            "UPDATE goal_runs SET conversation_revision=conversation_revision+1 WHERE id=?",
            (native_project["goal_id"],),
        )
    response = client.post(_source_path(native_project, job), headers=headers, json=_proof(job))
    assert response.status_code == 409


def test_stale_queued_validation_cannot_be_claimed_and_releases_task(
    client: TestClient,
    paired_headers: dict[str, str],
    test_app: FastAPI,
    native_project: dict[str, Any],
) -> None:
    value = _approve(client, paired_headers, native_project)
    with sqlite3.connect(test_app.state.settings.db_path) as db:
        db.execute(
            "UPDATE goal_runs SET conversation_revision=conversation_revision+1 WHERE id=?",
            (native_project["goal_id"],),
        )
    agent = native_project["agent"]
    response = client.post(
        f"/agents/{agent['id']}/claim",
        headers={"Authorization": f"Bearer {agent['credential']}"},
        json={"wait_seconds": 0},
    )
    assert response.status_code == 200 and response.json() is None, response.text
    assert (
        _stored(
            test_app.state.settings.db_path,
            "SELECT status FROM agent_jobs WHERE id=?",
            (value["job_id"],),
        )[0][0]
        == "cancelled"
    )
    assert (
        _stored(
            test_app.state.settings.db_path,
            "SELECT status FROM tasks WHERE id=(SELECT task_id FROM swift_project_validations WHERE id=?)",
            (value["validation_id"],),
        )[0][0]
        == "cancelled"
    )


def test_expired_execution_consent_cannot_deliver_source(
    client: TestClient,
    paired_headers: dict[str, str],
    test_app: FastAPI,
    native_project: dict[str, Any],
) -> None:
    value = _approve(client, paired_headers, native_project)
    job, headers = _claim(client, native_project)
    with sqlite3.connect(test_app.state.settings.db_path) as db:
        db.execute(
            "UPDATE swift_project_validations SET expires_at=? WHERE id=?",
            ("2000-01-01T00:00:00+00:00", value["validation_id"]),
        )
    response = client.post(_source_path(native_project, job), headers=headers, json=_proof(job))
    assert response.status_code == 409


def test_model_arguments_cannot_invent_a_project_execution_grant() -> None:
    node = _worker_plan().nodes[0].model_dump()
    node.update(
        required_skill="code.swift.test",
        worker_arguments={
            "kind": "swiftpm",
            "source_sha256": "a" * 64,
            "project_revision": {
                "validation_id": "validation_invented",
                "project_id": "project_invented",
                "revision_id": "revision_invented",
                "sha256": "b" * 64,
            },
        },
    )
    with pytest.raises(ValueError):
        SwarmPlanNodeProposal.model_validate(node)


@pytest.mark.parametrize("defect", ["revision", "request", "zero_tests"])
def test_unverified_receipt_never_marks_the_revision_validated(
    client: TestClient,
    paired_headers: dict[str, str],
    native_project: dict[str, Any],
    defect: str,
) -> None:
    """Deliberately forged protocol fixtures; no compiler execution is claimed."""
    value = _approve(client, paired_headers, native_project)
    job, headers = _claim(client, native_project)
    receipt = {
        "operation": "test",
        "kind": "swiftpm",
        "status": "passed",
        "exit_code": 0,
        "source_sha256": value["source_sha256"],
        "request_sha256": hashlib.sha256(
            json.dumps(job["payload"], sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
        "project_revision": dict(job["payload"]["project_revision"]),
        "source_unchanged": True,
        "tests_executed": 1,
        "test_evidence_format": "swiftpm_xunit",
        "test_failures": 0,
        "duration_ms": 25,
        "artifact_directory": ".swarmer-swift-runs/" + "c" * 32,
        "report_error": None,
    }
    if defect == "revision":
        receipt["project_revision"]["revision_id"] = "revision_different"
    elif defect == "request":
        receipt["request_sha256"] = "0" * 64
    else:
        receipt["tests_executed"] = 0
    response = client.post(
        f"/agents/{native_project['agent']['id']}/jobs/{job['id']}/result",
        headers=headers,
        json={**_proof(job), "status": "completed", "result": receipt},
    )
    assert response.status_code in {200, 409}, response.text
    metadata = client.get(native_project["path"], headers=paired_headers)
    assert metadata.status_code == 200
    assert metadata.json()["status"] != "passed"
    goal = client.get(f"/goals/{native_project['goal_id']}", headers=paired_headers)
    assert goal.status_code == 200 and goal.json()["goal"]["status"] != "completed"


def test_failed_malformed_result_is_not_exposed_as_a_receipt(
    client: TestClient,
    paired_headers: dict[str, str],
    native_project: dict[str, Any],
) -> None:
    _approve(client, paired_headers, native_project)
    job, headers = _claim(client, native_project)
    response = client.post(
        f"/agents/{native_project['agent']['id']}/jobs/{job['id']}/result",
        headers=headers,
        json={
            **_proof(job),
            "status": "failed",
            "result": {"files": [{"path": "private.swift", "content": "PRIVATE_SOURCE_MARKER"}]},
            "error": "compiler invocation failed",
        },
    )
    assert response.status_code == 200, response.text
    metadata = client.get(native_project["path"], headers=paired_headers)
    assert metadata.status_code == 200
    assert metadata.json()["status"] == "failed"
    assert metadata.json()["receipt"] is None
    assert "PRIVATE_SOURCE_MARKER" not in metadata.text
    assert "files" not in metadata.json()


@pytest.mark.skipif(sys.platform != "darwin", reason="requires the approved local Swift compiler")
async def test_real_native_revision_transfer_compiles_and_retains_verified_receipt(
    client: TestClient,
    paired_headers: dict[str, str],
    test_app: FastAPI,
    native_project: dict[str, Any],
    tmp_path: Path,
) -> None:
    """Real compiler + real authenticated API + persisted, explicitly consented fixture."""
    validation = _approve(client, paired_headers, native_project)
    db_path = test_app.state.settings.db_path
    before = _stored(db_path, "SELECT id,sha256,snapshot_json FROM project_revisions ORDER BY id")
    staging = tmp_path / "swift-project-staging"
    staging.mkdir(mode=0o700)
    seen: list[str] = []

    def asgi_request(origin, path, token, method, body):
        assert origin == "http://127.0.0.1"
        seen.append(path)
        response = client.request(
            method, path, headers={"Authorization": f"Bearer {token}"}, json=body
        )
        if response.status_code >= 400:
            raise urllib.error.HTTPError(origin + path, response.status_code, "rejected", {}, None)
        return response.json() if response.content else None

    workspace = worker.ProjectSnapshotWorkspace(staging)
    agent = native_project["agent"]
    assert worker.run_once(
        "http://127.0.0.1", agent["id"], agent["credential"], workspace, request_fn=asgi_request
    )
    stored = await test_app.state.agent_dispatcher.get_job(validation["job_id"])
    assert stored and stored["status"] == "completed", stored
    receipt = stored["result"]
    assert receipt["status"] == "passed" and receipt["exit_code"] == 0
    assert receipt["source_unchanged"] and receipt["source_sha256"] == validation["source_sha256"]
    assert receipt["tests_executed"] == 1 and receipt["test_failures"] == 0
    assert receipt["project_revision"] == stored["payload"]["project_revision"]
    assert receipt["project_revision"]["revision_id"] == native_project["preview"]["revision_id"]
    assert sum(path.endswith("/project-source") for path in seen) >= 3
    assert any(path.endswith("/result") for path in seen)
    metadata = client.get(native_project["path"], headers=paired_headers)
    assert metadata.status_code == 200, metadata.text
    assert metadata.json()["status"] == "passed"
    assert metadata.json()["receipt"] == receipt
    assert (
        _stored(db_path, "SELECT id,sha256,snapshot_json FROM project_revisions ORDER BY id")
        == before
    )
    goal = await test_app.state.goal_manager.graph.get_goal(native_project["goal_id"])
    assert goal and goal["status"] != "completed"
    artifacts = list(staging.glob("**/receipt.json"))
    assert len(artifacts) == 1 and json.loads(artifacts[0].read_text()) == receipt
