from __future__ import annotations

import copy
import json
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.main import create_app
from app.models import AgentCreate
from app.services.agent_card import (
    SUPPORTED_AGENT_PROTOCOL,
    SUPPORTED_AGENT_SKILLS,
    AgentCardPolicyError,
    public_agent_card,
    validate_agent_card_manifest,
    validate_agent_registration,
)
from app.services.permission_policy import PermissionPolicy
from app.services.remote_job_policy import RemoteJobPolicyError, validate_remote_job
from app.services.worker_skill_policy import WorkerSkillPolicyStore
from app.settings import Settings

REPO_ROOT = Path(__file__).resolve().parents[2]


def agent_request(**updates: Any) -> AgentCreate:
    raw: dict[str, Any] = {
        "name": "policy-bound-worker",
        "version": "0.10.0",
        "endpoint": "https://worker.internal.example",
        "skills": ["research.query"],
        "max_concurrency": 1,
        "capacity": {
            "max_results": 10,
            "max_query_characters": 2_000,
            "max_result_bytes": 524_288,
            "max_operation_seconds": 60,
        },
    }
    raw.update(updates)
    return AgentCreate.model_validate(raw)


def test_agent_skill_allowlist_is_exact() -> None:
    assert SUPPORTED_AGENT_PROTOCOL == "mongars-worker-v0.9"
    assert SUPPORTED_AGENT_SKILLS == frozenset(
        {
            "workspace.list_dir",
            "workspace.read_text",
            "research.query",
            "code_review.git_status",
            "code_review.git_diff",
            "code_review.git_show",
            "code_review.static_analysis",
        }
    )


@pytest.mark.parametrize("skill", sorted(SUPPORTED_AGENT_SKILLS))
def test_agent_registration_accepts_only_supported_skills(skill: str) -> None:
    request = agent_request(skills=[skill], capacity={"max_result_bytes": 524_288})

    policy = validate_agent_registration(request)

    assert policy.protocol == SUPPORTED_AGENT_PROTOCOL
    assert policy.skills == (skill,)
    assert dict(policy.capability_metadata) == {"max_result_bytes": 524_288}


@pytest.mark.parametrize(
    "skills",
    [
        ["workspace.write_text"],
        ["process.run"],
        ["iphone.location.current"],
        ["code_review.shell"],
        ["research.query", "research.query"],
        ["research.query", "code_review.git_status"],
    ],
)
def test_agent_registration_rejects_unknown_privileged_or_duplicate_skills(
    skills: list[str],
) -> None:
    with pytest.raises(AgentCardPolicyError):
        validate_agent_registration(agent_request(skills=skills))


def test_agent_registration_allows_an_inert_empty_skill_set() -> None:
    policy = validate_agent_registration(agent_request(skills=[], capacity={}))

    assert policy.skills == ()


def test_agent_registration_rejects_incompatible_protocol_and_unsafe_metadata() -> None:
    with pytest.raises(AgentCardPolicyError, match="protocol"):
        validate_agent_registration(agent_request(), protocol="mongars-worker-v1")

    for metadata in (
        {"shell": 1},
        {"network": 1},
        {"max_paths": 51},
        {"max_results": 0},
        {"max_result_bytes": 1_000_001},
    ):
        with pytest.raises(AgentCardPolicyError, match="metadata"):
            validate_agent_registration(agent_request(capacity=metadata))

    for endpoint in (
        "https://user:password@worker.internal.example",
        "https://worker.internal.example/?token=unsafe",
        "https://worker.internal.example/#fragment",
    ):
        with pytest.raises(AgentCardPolicyError, match="endpoint"):
            validate_agent_registration(agent_request(endpoint=endpoint))


def test_agent_registration_rejects_a_supported_skill_revoked_by_current_policy(
    tmp_path: Path,
) -> None:
    raw_policy = yaml.safe_load(
        (REPO_ROOT / "configs" / "permissions.yaml").read_text(encoding="utf-8")
    )
    raw_policy["worker_skill_rules"]["research.query"]["decision"] = "deny"
    raw_policy["worker_skill_rules"]["research.query"]["auto_redistribute"] = False
    permissions_path = tmp_path / "permissions.yaml"
    permissions_path.write_text(yaml.safe_dump(raw_policy), encoding="utf-8")
    operator_token = "agent-card-policy-test-operator-token"
    app = create_app(
        Settings(
            db_path=tmp_path / "state.db",
            workspace_root=tmp_path / "workspace",
            permissions_path=permissions_path,
            pairing_bootstrap_token=SecretStr(operator_token),
        )
    )

    with TestClient(app, client=("127.0.0.1", 50_000)) as client:
        code = client.post(
            "/pairing/code", headers={"X-Mongars-Operator-Token": operator_token}
        ).json()["code"]
        candidate = client.post(
            "/pairing/complete",
            json={"code": code, "device_id": "policy-phone", "name": "pytest"},
        ).json()
        headers = {"Authorization": f"Bearer {candidate['candidate_token']}"}
        assert (
            client.post(
                "/pairing/finalize",
                headers=headers,
                json={
                    "pairing_id": candidate["pairing_id"],
                    "device_id": candidate["device_id"],
                },
            ).status_code
            == 200
        )

        response = client.post(
            "/agents/register",
            headers=headers,
            json=agent_request().model_dump(mode="json"),
        )

    assert response.status_code == 403
    assert response.json() == {"detail": "remote worker skill is denied by policy"}


def _paired_agent_registration_client(
    tmp_path: Path,
) -> tuple[TestClient, FastAPI, dict[str, str], str]:
    operator_token = "agent-policy-fence-test-operator-token"
    app = create_app(
        Settings(
            db_path=tmp_path / "state.db",
            workspace_root=tmp_path / "workspace",
            permissions_path=REPO_ROOT / "configs" / "permissions.yaml",
            pairing_bootstrap_token=SecretStr(operator_token),
        )
    )
    client = TestClient(app, client=("127.0.0.1", 50_000))
    client.__enter__()
    code = client.post(
        "/pairing/code",
        headers={"X-Mongars-Operator-Token": operator_token},
    ).json()["code"]
    candidate = client.post(
        "/pairing/complete",
        json={"code": code, "device_id": "policy-fence-phone", "name": "pytest"},
    ).json()
    headers = {"Authorization": f"Bearer {candidate['candidate_token']}"}
    assert (
        client.post(
            "/pairing/finalize",
            headers=headers,
            json={
                "pairing_id": candidate["pairing_id"],
                "device_id": candidate["device_id"],
            },
        ).status_code
        == 200
    )
    return client, app, headers, operator_token


def test_registration_endpoint_uses_durable_deny_after_waiting_for_writer(
    tmp_path: Path,
) -> None:
    client, app, headers, _ = _paired_agent_registration_client(tmp_path)
    try:
        database = app.state.state_service.db_path
        denied = app.state.agent_dispatcher.permission_policy
        denied = PermissionPolicy(
            protected_paths=denied.protected_paths,
            process=denied.process,
            tool_rules=denied.tool_rules,
            capability_rules=denied.capability_rules,
            worker_skill_rules={
                **denied.worker_skill_rules,
                "research.query": replace(
                    denied.worker_skill_rules["research.query"],
                    decision="deny",
                    auto_redistribute=False,
                ),
            },
        )
        rules_json, rules_digest = WorkerSkillPolicyStore.encode_rules(denied.worker_skill_rules)
        blocker = sqlite3.connect(database, check_same_thread=False)
        blocker.execute("BEGIN IMMEDIATE")
        with ThreadPoolExecutor(max_workers=1) as executor:
            response_future = executor.submit(
                client.post,
                "/agents/register",
                headers=headers,
                json=agent_request(name="stale-allow-worker").model_dump(mode="json"),
            )
            try:
                time.sleep(0.05)
                assert not response_future.done()
                updated = blocker.execute(
                    """
                    UPDATE worker_skill_policy_state
                    SET epoch=epoch+1,rules_json=?,rules_digest=?,updated_at=?
                    WHERE singleton_id=1 AND epoch=1
                    """,
                    (rules_json, rules_digest, datetime.now(UTC).isoformat()),
                )
                assert updated.rowcount == 1
                blocker.commit()
            finally:
                blocker.close()
            response = response_future.result(timeout=1)
    finally:
        client.__exit__(None, None, None)

    assert response.status_code == 403
    assert response.json() == {"detail": "remote worker skill is denied by policy"}


def test_registration_endpoint_ignores_stale_in_memory_deny_after_lock_wait(
    tmp_path: Path,
) -> None:
    client, app, headers, _ = _paired_agent_registration_client(tmp_path)
    try:
        database = app.state.state_service.db_path
        raw_policy = yaml.safe_load(
            (REPO_ROOT / "configs" / "permissions.yaml").read_text(encoding="utf-8")
        )
        raw_policy["worker_skill_rules"]["research.query"].update(
            decision="deny",
            auto_redistribute=False,
        )
        stale_path = tmp_path / "stale-deny.yaml"
        stale_path.write_text(yaml.safe_dump(raw_policy), encoding="utf-8")
        blocker = sqlite3.connect(database, check_same_thread=False)
        blocker.execute("BEGIN IMMEDIATE")
        with ThreadPoolExecutor(max_workers=1) as executor:
            response_future = executor.submit(
                client.post,
                "/agents/register",
                headers=headers,
                json=agent_request(name="durably-allowed-worker").model_dump(mode="json"),
            )
            try:
                time.sleep(0.05)
                assert not response_future.done()
                app.state.agent_dispatcher.permission_policy.reload_worker_skill_rules(stale_path)
                blocker.commit()
            finally:
                blocker.close()
            response = response_future.result(timeout=1)
    finally:
        client.__exit__(None, None, None)

    assert response.status_code == 201
    assert response.json()["skills"] == ["research.query"]


def test_dispatch_endpoint_uses_durable_worker_policy_not_stale_cache(tmp_path: Path) -> None:
    client, app, headers, _ = _paired_agent_registration_client(tmp_path)
    try:
        task = client.post("/tasks", headers=headers, json={"input": "research safely"})
        assert task.status_code == 201
        allowed = app.state.agent_dispatcher.permission_policy
        denied = PermissionPolicy(
            protected_paths=allowed.protected_paths,
            process=allowed.process,
            tool_rules=allowed.tool_rules,
            capability_rules=allowed.capability_rules,
            worker_skill_rules={
                **allowed.worker_skill_rules,
                "research.query": replace(
                    allowed.worker_skill_rules["research.query"],
                    decision="deny",
                    auto_redistribute=False,
                ),
            },
        )
        rules_json, rules_digest = WorkerSkillPolicyStore.encode_rules(denied.worker_skill_rules)
        with sqlite3.connect(app.state.state_service.db_path) as db:
            updated = db.execute(
                """
                UPDATE worker_skill_policy_state
                SET epoch=epoch+1,rules_json=?,rules_digest=?,updated_at=?
                WHERE singleton_id=1
                """,
                (rules_json, rules_digest, datetime.now(UTC).isoformat()),
            )
            assert updated.rowcount == 1

        response = client.post(
            f"/tasks/{task.json()['id']}/dispatch",
            headers=headers,
            json={
                "required_skill": "research.query",
                "payload": {"query": "bounded evidence"},
            },
        )
    finally:
        client.__exit__(None, None, None)

    assert response.status_code == 403
    assert response.json() == {"detail": "remote worker skill is denied by policy"}


def test_dispatch_endpoint_allows_durable_policy_despite_stale_cache(tmp_path: Path) -> None:
    client, app, headers, _ = _paired_agent_registration_client(tmp_path)
    try:
        task = client.post("/tasks", headers=headers, json={"input": "research safely"})
        assert task.status_code == 201
        raw_policy = yaml.safe_load(
            (REPO_ROOT / "configs" / "permissions.yaml").read_text(encoding="utf-8")
        )
        raw_policy["worker_skill_rules"]["research.query"].update(
            decision="deny",
            auto_redistribute=False,
        )
        stale_path = tmp_path / "stale-dispatch-deny.yaml"
        stale_path.write_text(yaml.safe_dump(raw_policy), encoding="utf-8")
        app.state.agent_dispatcher.permission_policy.reload_worker_skill_rules(stale_path)

        response = client.post(
            f"/tasks/{task.json()['id']}/dispatch",
            headers=headers,
            json={
                "required_skill": "research.query",
                "payload": {"query": "bounded evidence"},
            },
        )
    finally:
        client.__exit__(None, None, None)

    assert response.status_code == 201
    assert response.json()["required_skill"] == "research.query"


def test_public_agent_card_is_metadata_only_and_revalidates_persisted_policy() -> None:
    persisted: dict[str, object] = {
        "id": "agt_safe",
        "name": "Safe reviewer",
        "version": "0.10.0",
        "endpoint": "https://private-host.invalid",
        "model_id": None,
        "status": "online",
        "skills": ["code_review.git_status"],
        "max_concurrency": 1,
        "capacity": {"max_result_bytes": 524_288},
        "runtime": "python",
        "supported_protocol_version": SUPPORTED_AGENT_PROTOCOL,
        "auth_token_hash": "must-not-appear",
    }

    card = public_agent_card(persisted).model_dump(mode="json")

    assert set(card) == {
        "agent_id",
        "name",
        "version",
        "skills",
        "model_id",
        "runtime",
        "max_concurrency",
        "supported_protocol_version",
        "capabilities",
    }
    assert not set(card) & {"endpoint", "status", "auth_token_hash", "credential"}

    for key, unsafe in (
        ("skills", ["process.run"]),
        ("runtime", "shell"),
        ("supported_protocol_version", "mongars-worker-v1"),
        ("capacity", {"network": 1}),
    ):
        with pytest.raises(AgentCardPolicyError):
            public_agent_card({**persisted, key: unsafe})


@pytest.mark.parametrize(
    "relative_path",
    [
        "workers/research-worker/agent-card.json",
        "workers/code-review-worker/agent-card.json",
    ],
)
def test_worker_manifests_are_valid_policy_bound_cards(relative_path: str) -> None:
    raw = json.loads((REPO_ROOT / relative_path).read_text(encoding="utf-8"))

    card = validate_agent_card_manifest(raw)

    assert card.protocol == SUPPORTED_AGENT_PROTOCOL
    assert card.skills
    assert set(card.skills) <= SUPPORTED_AGENT_SKILLS
    assert card.policy["writes"] is False


def test_agent_card_manifest_rejects_privilege_or_metadata_escalation() -> None:
    path = REPO_ROOT / "workers/research-worker/agent-card.json"
    valid = json.loads(path.read_text(encoding="utf-8"))
    mutations = []
    for key, value in (
        ("protocol", "mongars-worker-v1"),
        ("endpoint", "https://attacker.invalid"),
    ):
        changed = copy.deepcopy(valid)
        changed[key] = value
        mutations.append(changed)
    changed = copy.deepcopy(valid)
    changed["skills"][0]["id"] = "process.run"
    mutations.append(changed)
    changed = copy.deepcopy(valid)
    changed["skills"][0]["result_trust"] = "trusted"
    mutations.append(changed)
    changed = copy.deepcopy(valid)
    changed["policy"]["writes"] = True
    mutations.append(changed)
    changed = copy.deepcopy(valid)
    changed["policy"]["network"] = "arbitrary"
    mutations.append(changed)
    changed = copy.deepcopy(valid)
    changed["limits"]["shell"] = 1
    mutations.append(changed)

    for mutation in mutations:
        with pytest.raises(AgentCardPolicyError):
            validate_agent_card_manifest(mutation)


@pytest.mark.parametrize(
    ("skill", "payload", "expected"),
    [
        (
            "workspace.list_dir",
            {},
            {"path": "."},
        ),
        (
            "workspace.read_text",
            {"path": "src/module.py"},
            {"path": "src/module.py"},
        ),
        (
            "research.query",
            {"query": "  bounded evidence  "},
            {"query": "bounded evidence", "max_results": 5},
        ),
        (
            "code_review.git_status",
            {},
            {},
        ),
        (
            "code_review.git_diff",
            {"paths": ["src/module.py"], "staged": True, "context_lines": 4},
            {"paths": ["src/module.py"], "staged": True, "context_lines": 4},
        ),
        (
            "code_review.git_show",
            {"revision": "A" * 40},
            {"revision": "a" * 40, "paths": [], "context_lines": 3},
        ),
        (
            "code_review.static_analysis",
            {"paths": ["src/module.py", "src/module.py"]},
            {"paths": ["src/module.py"]},
        ),
    ],
)
def test_remote_job_policy_normalizes_only_supported_payloads(
    skill: str,
    payload: dict[str, Any],
    expected: dict[str, Any],
) -> None:
    assert validate_remote_job(skill, payload) == expected


def test_workspace_payload_allows_only_validated_broker_capability_metadata() -> None:
    payload = {
        "path": ".",
        "capability_request": {
            "capability_name": "iphone.location.current",
            "arguments": {},
        },
    }

    assert validate_remote_job("workspace.list_dir", payload) == payload

    for capability in (
        {"capability_name": "iphone.shell", "arguments": {}},
        {
            "capability_name": "iphone.location.current",
            "arguments": {},
            "callback_url": "https://attacker.invalid",
        },
        {"capability_name": "iphone.contacts.lookup", "arguments": {"query": ""}},
    ):
        with pytest.raises(RemoteJobPolicyError, match="capability"):
            validate_remote_job(
                "workspace.list_dir",
                {"path": ".", "capability_request": capability},
            )


@pytest.mark.parametrize(
    ("skill", "payload"),
    [
        ("workspace.write_text", {"path": "file", "content": "write"}),
        ("process.run", {"argv": ["id"]}),
        ("research.fetch_url", {"url": "https://attacker.invalid"}),
        ("research.query", {"query": "evidence", "url": "https://attacker.invalid"}),
        ("research.query", {"query": "evidence", "headers": {"Authorization": "x"}}),
        ("code_review.git_status", {"command": "git push"}),
        ("code_review.git_diff", {"argv": ["git", "diff"]}),
        ("code_review.git_show", {"revision": "HEAD", "shell": True}),
        ("code_review.static_analysis", {"paths": ["a.py"], "tool": "mypy"}),
    ],
)
def test_remote_job_policy_rejects_unknown_privileged_and_raw_command_fields(
    skill: str,
    payload: dict[str, Any],
) -> None:
    with pytest.raises(RemoteJobPolicyError):
        validate_remote_job(skill, payload)


@pytest.mark.parametrize(
    ("skill", "payload"),
    [
        ("workspace.read_text", {}),
        ("workspace.read_text", {"path": "../secret"}),
        ("workspace.list_dir", {"path": "/etc"}),
        ("workspace.list_dir", {"path": ".env"}),
        ("workspace.list_dir", {"path": "private/secret.txt"}),
        ("research.query", {"query": ""}),
        ("research.query", {"query": "x", "max_results": True}),
        ("code_review.git_diff", {"paths": ["../outside"]}),
        ("code_review.git_diff", {"paths": ["https://attacker.invalid/repo"]}),
        ("code_review.git_diff", {"paths": ["src/token.py"]}),
        ("code_review.git_diff", {"context_lines": 21}),
        ("code_review.git_show", {"revision": "HEAD~1"}),
        ("code_review.git_show", {"revision": "--help"}),
        ("code_review.git_show", {"revision": "a" * 39}),
        ("code_review.static_analysis", {"paths": []}),
        ("code_review.static_analysis", {"paths": ["README.md"]}),
        (
            "code_review.static_analysis",
            {"paths": [f"src/module_{index}.py" for index in range(51)]},
        ),
    ],
)
def test_remote_job_policy_rejects_out_of_contract_values(
    skill: str,
    payload: dict[str, Any],
) -> None:
    with pytest.raises(RemoteJobPolicyError):
        validate_remote_job(skill, payload)


def test_capability_metadata_is_not_available_to_research_or_review_jobs() -> None:
    capability = {
        "capability_request": {
            "capability_name": "iphone.location.current",
            "arguments": {},
        }
    }
    with pytest.raises(RemoteJobPolicyError):
        validate_remote_job("research.query", {"query": "safe", **capability})
    with pytest.raises(RemoteJobPolicyError):
        validate_remote_job("code_review.git_status", capability)
