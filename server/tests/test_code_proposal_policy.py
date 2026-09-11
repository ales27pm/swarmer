from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.services.agent_card import validate_agent_card_manifest
from app.services.code_proposal import validate_code_proposal_result
from app.services.remote_job_policy import RemoteJobPolicyError, validate_remote_job
from app.services.result_aggregator import validate_worker_evidence
from app.services.worker_skill_policy import WorkerSkillPolicyStore
from app.worker_admin import enroll_python_worker


def proposal(**updates: object) -> dict[str, object]:
    return {
        "schema_version": "1.0",
        "path": "app.py",
        "content": "print('hello')\n",
        "summary": "Proposed application; not executed or tested.",
        **updates,
    }


@pytest.mark.parametrize(
    "updates",
    [
        {"path": "../app.py"},
        {"path": ".env"},
        {"schema_version": "2.0"},
        {"content": "def broken(:"},
        {"content": "# only comments"},
        {"content": "x = '" + "é" * 32_000 + "'"},
        {"summary": "a" * 501},
        {"shell": "python app.py"},
        {"content": "\ud800"},
        {"content": "import flask"},
        {"content": "from .helpers import run"},
    ],
)
def test_untrusted_proposal_rejected_before_review_or_execution(updates: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        validate_code_proposal_result(proposal(**updates))
    assert not validate_worker_evidence("code.generate_python", proposal(**updates))


def test_proposal_validation_never_executes_generated_source(tmp_path: Path) -> None:
    marker = tmp_path / "must-not-exist"
    source = f"from pathlib import Path\nPath({str(marker)!r}).touch()\n"
    assert validate_code_proposal_result(proposal(content=source))["content"] == source
    assert not marker.exists()


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"objective": ""},
        {"objective": "x" * 4_001},
        {"objective": "Make an app", "path": "/tmp/output"},
        {"objective": "Make an app", "model_url": "https://example.invalid"},
    ],
)
def test_code_job_cannot_choose_host_paths_models_or_commands(payload: object) -> None:
    with pytest.raises(RemoteJobPolicyError):
        validate_remote_job("code.generate_python", payload)


def test_code_review_endpoints_require_paired_device(client: TestClient) -> None:
    route = "/goals/goal_any/nodes/node_any/code-proposal"
    assert client.get(route).status_code == 401
    assert client.post(route + "/apply", json={"sha256": "0" * 64}).status_code == 401


def test_deployed_code_worker_manifest_matches_server_capability_policy() -> None:
    path = Path(__file__).resolve().parents[2] / "workers/code-worker/agent-card.json"
    policy = validate_agent_card_manifest(json.loads(path.read_text()))
    assert policy.skills == ("code.generate_python",)
    assert policy.policy["filesystem"] == "none"
    assert policy.policy["writes"] is False
    assert policy.policy["shell"] is False


async def test_operator_enrollment_preserves_identity_and_keeps_credential_private(
    client: TestClient, test_app: FastAPI, tmp_path: Path
) -> None:
    del client
    output = tmp_path / "private" / "worker.json"
    settings = test_app.state.settings
    agent_id = await enroll_python_worker(
        db_path=settings.db_path,
        permissions_path=settings.permissions_path,
        credential_file=output,
        model="qwen2.5-coder:7b",
    )
    secret = json.loads(output.read_text())
    assert secret["agent_id"] == agent_id
    assert len(secret["credential"]) >= 32
    assert output.stat().st_mode & 0o777 == 0o600
    with sqlite3.connect(settings.db_path) as db:
        audit = db.execute(
            "SELECT actor_type,actor_id,payload_json FROM audit_events WHERE event_type='agent.registered'"
        ).fetchone()
        assert audit[0] == "operator" and audit[1].startswith(f"uid:{os.getuid()}@")
        assert secret["credential"] not in audit[2]
        assert db.execute("SELECT status FROM agents WHERE id=?", (agent_id,)).fetchone() == (
            "unverified",
        )
    with pytest.raises(FileExistsError):
        await enroll_python_worker(
            db_path=settings.db_path,
            permissions_path=settings.permissions_path,
            credential_file=output,
            model="qwen2.5-coder:7b",
        )


def test_legacy_policy_epoch_does_not_implicitly_allow_new_codegen_skill() -> None:
    import hashlib

    from app.services.permission_policy import PermissionPolicy

    path = Path(__file__).resolve().parents[2] / "configs" / "permissions.yaml"
    rules = dict(PermissionPolicy.from_yaml(path).worker_skill_rules)
    rules.pop("code.generate_python")
    encoded, digest = WorkerSkillPolicyStore.encode_rules(rules)
    with sqlite3.connect(":memory:") as db:
        db.row_factory = sqlite3.Row
        row = db.execute(
            "SELECT 1 AS epoch,? AS rules_json,? AS rules_digest", (encoded, digest)
        ).fetchone()
    snapshot = WorkerSkillPolicyStore._snapshot_from_row(row)
    assert not snapshot.is_allowed("code.generate_python")
    assert snapshot.is_allowed("workspace.list_dir")
    assert digest == "sha256:" + hashlib.sha256(encoded.encode()).hexdigest()
