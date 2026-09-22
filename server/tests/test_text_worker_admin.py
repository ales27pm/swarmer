from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from dataclasses import replace
from pathlib import Path
from types import MappingProxyType
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import worker_admin
from app.services.permission_policy import PermissionPolicy, PermissionPolicyError
from app.services.worker_skill_policy import WorkerSkillPolicyStore


async def test_text_enrollment_keeps_secret_private_and_cannot_overwrite(
    client: TestClient, test_app: FastAPI, tmp_path: Path
) -> None:
    del client
    settings = test_app.state.settings
    credential_file = tmp_path / "private-text" / "registration.json"
    agent_id = await worker_admin.enroll_text_worker(
        db_path=settings.db_path,
        permissions_path=settings.permissions_path,
        credential_file=credential_file,
        model="local-text:3b",
    )
    registration = json.loads(credential_file.read_text())
    assert registration["agent_id"] == agent_id
    assert len(registration["credential"]) >= 32
    assert credential_file.stat().st_mode & 0o777 == 0o600
    assert credential_file.parent.stat().st_mode & 0o777 == 0o700
    with sqlite3.connect(settings.db_path) as db:
        db.row_factory = sqlite3.Row
        row = db.execute("SELECT * FROM agents WHERE id=?", (agent_id,)).fetchone()
        assert row["name"] == "ubuntu-text-draft-worker"
        assert row["status"] == "unverified"
        assert json.loads(row["skills_json"]) == ["writing.draft"]
        assert row["max_concurrency"] == 1
        assert json.loads(row["capacity_json"]) == {
            "max_result_bytes": 160_000,
            "max_operation_seconds": 120,
        }
        assert row["auth_token_hash"] == (
            "sha256:" + hashlib.sha256(registration["credential"].encode()).hexdigest()
        )
        audit = db.execute(
            "SELECT actor_type,actor_id,payload_json FROM audit_events "
            "WHERE event_type='agent.registered'"
        ).fetchone()
        assert audit["actor_type"] == "operator"
        assert audit["actor_id"].startswith(f"uid:{os.getuid()}@")
        assert registration["credential"] not in audit["payload_json"]
    original_bytes = credential_file.read_bytes()
    with pytest.raises(FileExistsError):
        await worker_admin.enroll_text_worker(
            db_path=settings.db_path,
            permissions_path=settings.permissions_path,
            credential_file=credential_file,
            model="local-text:3b",
        )
    assert credential_file.read_bytes() == original_bytes
    with sqlite3.connect(settings.db_path) as db:
        assert db.execute("SELECT count(*) FROM agents").fetchone()[0] == 1


async def test_text_enrollment_obeys_persisted_policy_denial(
    client: TestClient, test_app: FastAPI, tmp_path: Path
) -> None:
    del client
    settings = test_app.state.settings
    candidate = PermissionPolicy.from_yaml(settings.permissions_path)
    rules = dict(candidate.worker_skill_rules)
    rules["writing.draft"] = replace(rules["writing.draft"], decision="deny")
    candidate.worker_skill_rules = MappingProxyType(rules)
    await WorkerSkillPolicyStore(settings.db_path, None).replace(candidate)
    credential_file = tmp_path / "private-denied" / "registration.json"
    with pytest.raises(PermissionPolicyError, match="denied by policy"):
        await worker_admin.enroll_text_worker(
            db_path=settings.db_path,
            permissions_path=settings.permissions_path,
            credential_file=credential_file,
            model="local-text:3b",
        )
    with sqlite3.connect(settings.db_path) as db:
        assert db.execute("SELECT count(*) FROM agents").fetchone()[0] == 0
    assert not credential_file.read_bytes()


@pytest.mark.parametrize(
    "kind,skill", [("python", "code.generate_python"), ("project", "code.build_project")]
)
async def test_existing_enrollment_kinds_preserve_their_skill(
    client: TestClient, test_app: FastAPI, tmp_path: Path, kind: str, skill: str
) -> None:
    del client
    settings = test_app.state.settings
    enroll = (
        worker_admin.enroll_project_worker
        if kind == "project"
        else worker_admin.enroll_python_worker
    )
    agent_id = await enroll(
        db_path=settings.db_path,
        permissions_path=settings.permissions_path,
        credential_file=tmp_path / "private-existing" / "registration.json",
        model="local-code:3b",
    )
    with sqlite3.connect(settings.db_path) as db:
        row = db.execute("SELECT skills_json FROM agents WHERE id=?", (agent_id,)).fetchone()
        assert json.loads(row[0]) == [skill]


def test_text_cli_selects_text_and_prints_no_credential(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    enroll = AsyncMock(return_value="agt_text_test")
    monkeypatch.setattr(worker_admin, "_enroll_worker", enroll)
    monkeypatch.setattr(
        "sys.argv",
        [
            "worker_admin",
            "--kind",
            "text",
            "--db",
            str(tmp_path / "db"),
            "--permissions",
            str(tmp_path / "permissions.yaml"),
            "--credential-file",
            str(tmp_path / "private" / "registration.json"),
            "--model",
            "local-text:3b",
        ],
    )
    worker_admin.main()
    assert enroll.await_args.kwargs["kind"] == "text"
    assert json.loads(capsys.readouterr().out) == {
        "agent_id": "agt_text_test",
        "credential_saved": True,
    }
