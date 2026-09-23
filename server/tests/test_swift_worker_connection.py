from __future__ import annotations

import json
import sqlite3
import sys
import urllib.error
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.models import TaskCreate, TaskRecord
from app.services.swift_contracts import valid_swift_receipt
from app.worker_admin import enroll_swift_worker
from tests.test_swift_worker import worker


@pytest.fixture
def approved_hello(tmp_path: Path) -> Path:
    root = tmp_path / "hello"
    root.mkdir()
    (root / "Package.swift").write_text(
        "// swift-tools-version: 6.0\nimport PackageDescription\n"
        'let package = Package(name: "Hello", targets: [.testTarget(name: "HelloTests")])\n'
    )
    tests = root / "Tests/HelloTests"
    tests.mkdir(parents=True)
    (tests / "HelloTests.swift").write_text(
        "import XCTest\nfinal class HelloTests: XCTestCase {\n"
        'func testGreeting() { XCTAssertEqual(String(2 + 2), "4") }\n}\n'
    )
    return root


async def test_swift_enrollment_declares_two_tools_and_private_credentials(
    client: TestClient,
    test_app: FastAPI,
    tmp_path: Path,
) -> None:
    del client
    target = tmp_path / "private" / "worker.json"
    settings = test_app.state.settings
    agent_id = await enroll_swift_worker(
        db_path=settings.db_path,
        permissions_path=settings.permissions_path,
        credential_file=target,
    )
    identity, credential = worker.load_credentials(target, tmp_path / "workspace")
    assert identity == agent_id and len(credential) >= 32
    assert target.stat().st_mode & 0o777 == 0o600
    with sqlite3.connect(settings.db_path) as db:
        row = db.execute(
            "SELECT skills_json,status,capacity_json FROM agents WHERE id=?", (agent_id,)
        ).fetchone()
        assert set(json.loads(row[0])) == {"code.swift.build", "code.swift.test"}
        assert row[1] == "unverified"
        assert json.loads(row[2])["max_operation_seconds"] == worker.MAX_REMOTE_SECONDS
        assert credential not in str(db.execute("SELECT payload_json FROM audit_events").fetchall())
    with pytest.raises(FileExistsError):
        await enroll_swift_worker(
            db_path=settings.db_path,
            permissions_path=settings.permissions_path,
            credential_file=target,
        )


@pytest.mark.parametrize(
    "defect",
    ["public_file", "public_directory", "symlink", "inside_workspace", "oversized", "malformed"],
)
def test_credential_loader_rejects_unsafe_files(tmp_path: Path, defect: str) -> None:
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    registration = private / "worker.json"
    registration.write_text(json.dumps({"agent_id": "agt_test", "credential": "T" * 43}))
    registration.chmod(0o600)
    workspace = tmp_path / "workspace"
    if defect == "public_file":
        registration.chmod(0o644)
    elif defect == "public_directory":
        private.chmod(0o755)
    elif defect == "symlink":
        link = private / "link.json"
        link.symlink_to(registration)
        registration = link
    elif defect == "inside_workspace":
        workspace = private
    elif defect == "oversized":
        registration.write_text("x" * 4097)
    else:
        registration.write_text('{"credential":"secret"}')
    with pytest.raises((worker.SwiftWorkerError, OSError)):
        worker.load_credentials(registration, workspace)


@pytest.mark.skipif(sys.platform != "darwin", reason="requires the approved local Swift compiler")
async def test_authenticated_claim_compiles_operator_fixture_and_returns_real_test_receipt(
    client: TestClient,
    test_app: FastAPI,
    tmp_path: Path,
    approved_hello: Path,
) -> None:
    registration = tmp_path / "credentials" / "worker.json"
    settings = test_app.state.settings
    await enroll_swift_worker(
        db_path=settings.db_path,
        permissions_path=settings.permissions_path,
        credential_file=registration,
    )
    agent_id, credential = worker.load_credentials(registration, approved_hello)
    task = await test_app.state.state_service.create_task(
        TaskRecord.new(
            TaskCreate(input="Test the operator-approved Hello fixture"), source="test-operator"
        )
    )
    digest = worker.source_digest(approved_hello)
    job = await test_app.state.agent_dispatcher.queue_job(
        task.id,
        "code.swift.test",
        {"kind": "swiftpm", "source_sha256": digest},
    )
    seen = []

    def asgi_request(origin, path, token, method, body):
        assert origin == "http://127.0.0.1"
        seen.append(path)
        response = client.request(
            method, path, headers={"Authorization": f"Bearer {token}"}, json=body
        )
        if response.status_code >= 400:
            raise urllib.error.HTTPError(origin + path, response.status_code, "rejected", {}, None)
        return response.json() if response.content else None

    workspace = worker.SwiftWorkspace(approved_hello, approved_source_sha256=digest)
    assert client.post(f"/agents/{agent_id}/claim", json={"wait_seconds": 0}).status_code == 401
    assert worker.run_once(
        "http://127.0.0.1", agent_id, credential, workspace, request_fn=asgi_request
    )
    with sqlite3.connect(settings.db_path) as db:
        stored = db.execute(
            "SELECT status,result_json FROM agent_jobs WHERE id=?", (job["id"],)
        ).fetchone()
        assert stored[0] == "completed"
        receipt = json.loads(stored[1])
    assert receipt["source_sha256"] == digest and receipt["source_unchanged"]
    assert receipt["status"] == "passed" and receipt["tests_executed"] == 1
    assert receipt["test_failures"] == 0 and receipt["exit_code"] == 0
    assert valid_swift_receipt(
        "code.swift.test", receipt, {"kind": "swiftpm", "source_sha256": digest}
    )
    assert any(path.endswith("/heartbeat") and "/jobs/" in path for path in seen)
    assert any(path.endswith("/result") for path in seen)
    assert (approved_hello / receipt["artifact_directory"] / "receipt.json").is_file()
    assert not worker.run_once(
        "http://127.0.0.1", agent_id, credential, workspace, request_fn=asgi_request
    )


def test_compiler_process_does_not_inherit_worker_credential(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SWARMER_WORKER_TOKEN", "must-not-reach-compiler")
    log = tmp_path / "environment.log"
    assert (
        worker.run_command(
            [sys.executable, "-c", "import os; print(os.getenv('SWARMER_WORKER_TOKEN', 'absent'))"],
            tmp_path,
            log,
            10,
            lambda: None,
        )
        == 0
    )
    assert log.read_text().strip() == "absent"
