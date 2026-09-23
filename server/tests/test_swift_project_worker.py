from __future__ import annotations

import hashlib
import json
import urllib.error
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient

from app.services.project_contracts import project_digest
from tests.test_swift_worker import worker


def snapshot_payload(tmp_path: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    files = [
        {"path": "Package.swift", "content": "// an explicitly approved fixture\n"},
        {"path": "Sources/Hello.swift", "content": 'let greeting = "Bonjour é"\n'},
    ]
    source = tmp_path / "approved-fixture"
    source.mkdir()
    for item in files:
        path = source / item["path"]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(item["content"], encoding="utf-8")
    ref = {
        "validation_id": "validation_1",
        "project_id": "project_1",
        "revision_id": "revision_1",
        "sha256": project_digest(files),
    }
    target = {"kind": "swiftpm", "source_sha256": worker.source_digest(source)}
    payload = {**target, "project_revision": ref}
    response = {
        "project_revision": ref,
        "source_sha256": target["source_sha256"],
        "target": target,
        "files": files,
    }
    return payload, response


class SourceAPI:
    """Authenticated HTTP seam; server's durable consent/lease implementation is tested separately."""

    def __init__(self, payload, response):
        self.payload, self.response = payload, response
        self.reads = 0
        self.revoke_on_read: int | None = None
        self.submitted = []
        self.calls = []
        app = FastAPI()

        @app.post("/agents/{agent_id}/{rest:path}")
        async def route(agent_id: str, rest: str, request: Request):
            if (
                request.headers.get("authorization") != "Bearer " + "T" * 43
                or agent_id != "agt_swift"
            ):
                raise HTTPException(401)
            body = await request.json()
            self.calls.append(rest)
            if rest == "claim":
                return {
                    "id": "job_1",
                    "required_skill": "code.swift.build",
                    "payload": self.payload,
                    "claim_token": "claim",
                    "lease_id": "lease",
                    "lease_generation": 1,
                }
            if rest.startswith("jobs/"):
                if any(
                    body.get(key) != value
                    for key, value in {
                        "claim_token": "claim",
                        "lease_id": "lease",
                        "lease_generation": 1,
                    }.items()
                ):
                    raise HTTPException(409)
                if rest.endswith("project-source"):
                    self.reads += 1
                    if self.revoke_on_read is not None and self.reads >= self.revoke_on_read:
                        raise HTTPException(409)
                    return self.response
                if rest.endswith("result"):
                    self.submitted.append(body)
            return {"status": "ok"}

        self.client = TestClient(app)

    def request(self, origin, path, token, method, body):
        result = self.client.request(
            method, path, headers={"Authorization": f"Bearer {token}"}, json=body
        )
        if result.status_code >= 400:
            raise urllib.error.HTTPError(origin + path, result.status_code, "rejected", {}, None)
        return result.json()


def test_authorized_snapshot_stages_and_binds_full_request(tmp_path: Path) -> None:
    payload, response = snapshot_payload(tmp_path)
    api = SourceAPI(payload, response)
    staging = tmp_path / "staging"
    staging.mkdir(mode=0o700)
    roots = []

    def compile_fixture(argv, root, log, timeout, ensure_active):
        ensure_active()
        roots.append(root)
        assert worker.source_digest(root) == payload["source_sha256"]
        assert (root / "Sources/Hello.swift").read_text() == response["files"][1]["content"]
        log.write_text("fixture compiler completed")
        return 0

    workspace = worker.ProjectSnapshotWorkspace(staging, runner=compile_fixture)
    assert worker.run_once(
        "http://127.0.0.1", "agt_swift", "T" * 43, workspace, request_fn=api.request
    )
    assert len(roots) == 1 and roots[0].is_relative_to(staging)
    assert api.reads >= 3  # initial consent, immediately before command, final receipt.
    receipt = api.submitted[0]["result"]
    assert receipt["project_revision"] == payload["project_revision"]
    assert (
        receipt["request_sha256"]
        == hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
    )
    assert receipt["status"] == "passed"
    assert (
        json.loads((roots[0] / receipt["artifact_directory"] / "receipt.json").read_text())
        == receipt
    )


@pytest.mark.parametrize(
    "defect",
    [
        "response_extra",
        "reference_extra",
        "reference_invalid_id",
        "reference_wrong_project",
        "wrong_target",
        "target_extra",
        "source_digest",
        "project_digest",
        "unknown_kind",
        "payload_extra",
        "traversal",
        "absolute",
        "noncanonical",
        "windows",
        "reserved_git",
        "reserved_build",
        "reserved_runs",
        "duplicate",
        "case_collision",
        "directory_case_collision",
        "file_directory_collision",
        "file_extra",
        "file_nontext",
        "file_control",
        "too_many_files",
        "oversized_file",
        "oversized_total",
    ],
)
def test_forged_or_unsafe_source_never_reaches_compiler(tmp_path: Path, defect: str) -> None:
    payload, response = snapshot_payload(tmp_path)
    payload, response = deepcopy(payload), deepcopy(response)
    if defect == "response_extra":
        response["approved"] = True
    elif defect == "reference_extra":
        payload["project_revision"]["approved"] = True
    elif defect == "reference_invalid_id":
        payload["project_revision"]["validation_id"] = "../invalid"
    elif defect == "reference_wrong_project":
        response["project_revision"]["project_id"] = "another"
    elif defect == "wrong_target":
        response["target"]["kind"] = "xcode"
    elif defect == "target_extra":
        response["target"]["flags"] = "--unsafe"
    elif defect == "source_digest":
        payload["source_sha256"] = response["source_sha256"] = response["target"][
            "source_sha256"
        ] = "0" * 64
    elif defect == "project_digest":
        response["files"][0]["content"] += "changed"
    elif defect == "unknown_kind":
        payload["kind"] = "shell"
    elif defect == "payload_extra":
        payload["compiler"] = "/bin/sh"
    elif defect in {
        "traversal",
        "absolute",
        "noncanonical",
        "windows",
        "reserved_git",
        "reserved_build",
        "reserved_runs",
    }:
        response["files"][0]["path"] = {
            "traversal": "a/../outside.swift",
            "absolute": "/tmp/outside.swift",
            "noncanonical": "a//file.swift",
            "windows": "C:\\outside.swift",
            "reserved_git": ".git/config",
            "reserved_build": "a/.BUILD/main.swift",
            "reserved_runs": ".swarmer-swift-runs/fake/receipt.json",
        }[defect]
    elif defect in {
        "duplicate",
        "case_collision",
        "directory_case_collision",
        "file_directory_collision",
    }:
        response["files"].append(
            {
                "path": {
                    "duplicate": "Package.swift",
                    "case_collision": "package.swift",
                    "directory_case_collision": "sources/Other.swift",
                    "file_directory_collision": "Sources",
                }[defect],
                "content": "other",
            }
        )
    elif defect == "file_extra":
        response["files"][0]["symlink"] = "/private/credential"
    elif defect == "file_nontext":
        response["files"][0]["content"] = 1
    elif defect == "file_control":
        response["files"][0]["content"] = "bad\x00text"
    elif defect == "too_many_files":
        response["files"] = [{"path": f"file{i}.swift", "content": ""} for i in range(81)]
    elif defect == "oversized_file":
        response["files"][0]["content"] = "é" * 32_001
    else:
        response["files"] = [{"path": f"file{i}.swift", "content": "x" * 60_000} for i in range(17)]
    api = SourceAPI(payload, response)
    staging = tmp_path / "staging"
    staging.mkdir(mode=0o700)

    def never(*args):
        pytest.fail("unapproved source reached the compiler")

    workspace = worker.ProjectSnapshotWorkspace(staging, runner=never)
    assert worker.run_once(
        "http://127.0.0.1", "agt_swift", "T" * 43, workspace, request_fn=api.request
    )
    assert api.submitted and api.submitted[0]["status"] == "failed"
    assert "result" not in api.submitted[0]


@pytest.mark.parametrize("read_number", [1, 2, 3])
def test_revoked_consent_never_submits_success(tmp_path: Path, read_number: int) -> None:
    payload, response = snapshot_payload(tmp_path)
    api = SourceAPI(payload, response)
    api.revoke_on_read = read_number
    staging = tmp_path / "staging"
    staging.mkdir(mode=0o700)
    executed = []

    def compile_fixture(argv, root, log, timeout, active):
        executed.append(True)
        log.write_text("fixture")
        return 0

    workspace = worker.ProjectSnapshotWorkspace(staging, runner=compile_fixture)
    assert worker.run_once(
        "http://127.0.0.1", "agt_swift", "T" * 43, workspace, request_fn=api.request
    )
    assert not api.submitted
    assert bool(executed) == (read_number == 3)


def test_revocation_during_command_aborts_on_periodic_revalidation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload, response = snapshot_payload(tmp_path)
    api = SourceAPI(payload, response)
    api.revoke_on_read = 3
    staging = tmp_path / "staging"
    staging.mkdir(mode=0o700)
    clock = [100.0]
    monkeypatch.setattr(worker._project_snapshots().time, "monotonic", lambda: clock[0])
    observed = []

    def running_command(argv, root, log, timeout, active):
        active()
        assert api.reads == 2
        clock[0] += 5.1
        with pytest.raises(worker._protocol().LeaseLost):
            active()
        observed.append("revoked")
        raise worker._protocol().LeaseLost("revoked during command")

    workspace = worker.ProjectSnapshotWorkspace(staging, runner=running_command)
    assert worker.run_once(
        "http://127.0.0.1", "agt_swift", "T" * 43, workspace, request_fn=api.request
    )
    assert observed == ["revoked"] and not api.submitted


@pytest.mark.parametrize("defect", ["symlink", "public", "file", "symlink_parent"])
def test_staging_root_must_be_private_regular_directory(tmp_path: Path, defect: str) -> None:
    root = tmp_path / "staging"
    root.mkdir(mode=0o700)
    if defect == "public":
        root.chmod(0o755)
    elif defect == "file":
        root.rmdir()
        root.write_text("not a directory")
    elif defect == "symlink":
        link = tmp_path / "link"
        link.symlink_to(root)
        root = link
    else:
        child = root / "child"
        child.mkdir(mode=0o700)
        link = tmp_path / "link"
        link.symlink_to(root)
        root = link / "child"
    with pytest.raises(ValueError):
        worker.ProjectSnapshotWorkspace(root)


def test_two_jobs_get_independent_stages_without_overwriting(tmp_path: Path) -> None:
    payload, response = snapshot_payload(tmp_path)
    staging = tmp_path / "staging"
    staging.mkdir(mode=0o700)
    roots = []

    def compile_fixture(argv, root, log, timeout, active):
        roots.append(root)
        log.write_text("fixture")
        return 0

    workspace = worker.ProjectSnapshotWorkspace(staging, runner=compile_fixture)
    for _ in range(2):
        api = SourceAPI(payload, response)
        assert worker.run_once(
            "http://127.0.0.1", "agt_swift", "T" * 43, workspace, request_fn=api.request
        )
        assert api.submitted[0]["status"] == "completed"
    assert len(set(roots)) == 2 and all(root.is_dir() for root in roots)


def test_legacy_workspace_does_not_accept_snapshot_authority(tmp_path: Path) -> None:
    payload, response = snapshot_payload(tmp_path)
    api = SourceAPI(payload, response)
    root = tmp_path / "approved-fixture"
    workspace = worker.SwiftWorkspace(root, approved_source_sha256=payload["source_sha256"])
    assert worker.run_once(
        "http://127.0.0.1", "agt_swift", "T" * 43, workspace, request_fn=api.request
    )
    assert api.reads == 0 and api.submitted[0]["status"] == "failed"


def test_source_reader_limit_is_separate_from_standard_control_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    protocol = worker._protocol()
    calls = []

    def request(origin, path, token, method, body, **kwargs):
        calls.append((path, kwargs))
        return {"ok": True}

    monkeypatch.setattr(protocol, "request", request)
    client = protocol.ControlPlaneClient("http://127.0.0.1", "agt_swift", "T" * 43)
    client.heartbeat_agent("online")

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def read(self, limit):
            assert limit == 8_000_001
            return b'{"ok":true}'

    class Opener:
        def open(self, req, timeout):
            assert req.full_url.endswith("/jobs/job_1/project-source")
            assert timeout == 5
            return Response()

    monkeypatch.setattr(worker.urllib.request, "build_opener", lambda *args: Opener())
    lease = protocol.LeaseProof("claim", "lease", 1)
    assert worker._project_source_request(protocol, client, "job_1", lease, None) == {"ok": True}
    assert len(calls) == 1 and calls[0][1] == {}


@pytest.mark.parametrize(
    "mode",
    ["both_sources", "staging_with_pin", "workspace_without_pin", "staging_inside_credentials"],
)
def test_cli_source_authority_modes_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    credentials = tmp_path / "credentials"
    credentials.mkdir(mode=0o700)
    token_file = credentials / "worker.json"
    token_file.write_text(json.dumps({"agent_id": "agt_swift", "credential": "T" * 43}))
    token_file.chmod(0o600)
    staging = (credentials if mode == "staging_inside_credentials" else tmp_path) / "staging"
    staging.mkdir(mode=0o700)
    args = [
        "swift_worker.py",
        "--base-url",
        "http://127.0.0.1",
        "--credential-file",
        str(token_file),
        "--once",
    ]
    if mode == "workspace_without_pin":
        args += ["--workspace", str(staging)]
    else:
        args += ["--project-staging-root", str(staging)]
        if mode == "both_sources":
            args += ["--workspace", str(staging)]
        elif mode == "staging_with_pin":
            args += ["--approved-source-sha256", "0" * 64]
    import sys

    monkeypatch.setattr(sys, "argv", args)
    with pytest.raises(SystemExit) as exc:
        worker.main()
    assert exc.value.code == 2


def test_staging_detects_nonregular_or_symlink_source_before_publication(tmp_path: Path) -> None:
    payload, response = snapshot_payload(tmp_path)
    staging = tmp_path / "staging"
    staging.mkdir(mode=0o700)
    workspace = worker.ProjectSnapshotWorkspace(staging)
    original_digest = workspace.snapshots.source_digest

    def tamper(root):
        path = root / "Package.swift"
        path.unlink()
        path.symlink_to(tmp_path / "approved-fixture/Package.swift")
        return original_digest(root)

    workspace.snapshots.source_digest = tamper
    with pytest.raises(ValueError, match="symlink"):
        workspace.snapshots.stage(response["files"], payload["source_sha256"])
    assert not list(staging.glob("*/source"))
