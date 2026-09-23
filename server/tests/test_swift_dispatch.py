from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import aiosqlite
import pytest
import yaml
from jsonschema import Draft202012Validator

from app.models import AgentCreate
from app.services.goal_manager import GoalManager
from app.services.permission_policy import PermissionPolicy
from app.services.planner_provider import UbuntuSwarmPlannerProvider
from app.services.result_aggregator import validate_worker_evidence
from app.services.state_service import StateService
from app.services.swarm_contracts import GoalCreateRequest, GoalStartRequest, SwarmPlanNodeProposal
from app.services.swift_contracts import valid_swift_receipt
from app.services.worker_skill_policy import WorkerSkillPolicyStateError, WorkerSkillPolicyStore
from tests.test_goal_runtime_recovery import _manager, _worker_plan
from tests.test_project_plan_shape import _node, _plan

ARGS = {"kind": "swiftpm", "source_sha256": "a" * 64}


def receipt(**updates):
    # Handwritten protocol fixture, never claimed as real compiler execution.
    return {
        "operation": "test",
        "kind": "swiftpm",
        "status": "passed",
        "exit_code": 0,
        "source_sha256": "a" * 64,
        "request_sha256": hashlib.sha256(
            json.dumps(ARGS, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
        "source_unchanged": True,
        "tests_executed": 1,
        "test_evidence_format": "swiftpm_xunit",
        "test_failures": 0,
        "duration_ms": 25,
        "artifact_directory": ".swarmer-swift-runs/" + "c" * 32,
        "report_error": None,
        **updates,
    }


@pytest.mark.parametrize(
    "updates",
    [
        {"source_sha256": "b" * 64},
        {"request_sha256": "b" * 64},
        {"tests_executed": 0},
        {"tests_executed": True},
        {"source_unchanged": False},
        {"test_failures": 1},
        {"exit_code": 1},
        {"operation": "build"},
        {"kind": "xcode"},
        {"kind": []},
        {"duration_ms": 120001},
        {"artifact_directory": "../../receipt"},
        {"report_error": "missing"},
    ],
)
def test_invalid_or_unbound_swift_test_receipt_cannot_pass(updates: dict) -> None:
    assert not valid_swift_receipt("code.swift.test", receipt(**updates), ARGS)


def test_swift_receipt_shape_and_binding_are_distinct() -> None:
    assert validate_worker_evidence("code.swift.test", receipt())
    assert valid_swift_receipt("code.swift.test", receipt(), ARGS)
    assert not valid_swift_receipt(
        "code.swift.test", receipt(), {**ARGS, "source_sha256": "b" * 64}
    )
    assert not validate_worker_evidence("code.swift.build", receipt())


def test_xcode_receipt_is_bound_to_the_recorded_scheme_and_destination() -> None:
    args = {
        "kind": "xcode",
        "source_sha256": "a" * 64,
        "project": "Hello.xcodeproj",
        "scheme": "Hello",
        "destination": "simulator",
    }
    value = receipt(
        kind="xcode",
        test_evidence_format="xcresult_summary",
        request_sha256=hashlib.sha256(
            json.dumps(args, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
    )
    assert valid_swift_receipt("code.swift.test", value, args)
    assert not valid_swift_receipt("code.swift.test", value, {**args, "scheme": "Other"})
    assert not valid_swift_receipt("code.swift.test", value, {**args, "destination": "device"})


def test_swift_arguments_survive_constrained_plan_and_dispatch() -> None:
    raw = _node("compile", "code.swift.test")
    with pytest.raises(ValueError):
        SwarmPlanNodeProposal.model_validate(raw)
    raw["worker_arguments"] = ARGS
    node = SwarmPlanNodeProposal.model_validate(raw)
    assert (
        GoalManager._payload_for_node(
            {
                "required_skill": node.required_skill,
                "objective": node.objective,
                "planner_metadata_json": json.dumps({"worker_arguments": node.worker_arguments}),
            }
        )
        == ARGS
    )
    raw["00_required_skill"] = raw.pop("required_skill")
    validator = Draft202012Validator(
        UbuntuSwarmPlannerProvider._response_format()["json_schema"]["schema"]
    )
    assert validator.is_valid(_plan([raw]))
    raw["worker_arguments"] = {"source_sha256": "guess", "shell": "swift test"}
    assert not validator.is_valid(_plan([raw]))


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", [False, True])
async def test_goal_accepts_only_swift_receipt_bound_to_recorded_job(
    tmp_path: Path, invalid: bool
) -> None:
    plan = _worker_plan(objective="Test approved Hello Swift fixture")
    plan.nodes[0] = SwarmPlanNodeProposal.model_validate(
        {
            **plan.nodes[0].model_dump(),
            "required_skill": "code.swift.test",
            "worker_arguments": ARGS,
        }
    )
    manager = await _manager(tmp_path / "swift.db", plan)
    agent = await manager.state_service.register_agent(
        AgentCreate(
            name="swift-fixture-worker",
            endpoint="http://127.0.0.1",
            skills=["code.swift.test"],
        ),
        "test-operator",
    )
    await manager.state_service.heartbeat_agent(agent["id"], "online", agent["credential"])
    goal = await manager.create_goal(GoalCreateRequest(objective=plan.objective), actor_id="phone")
    started = await manager.start_goal(goal["id"], GoalStartRequest())
    assert started["nodes"][0]["status"] == "dispatched"
    job = await manager.agent_dispatcher.claim(agent["id"])
    assert job and job["payload"] == ARGS
    recorded, _ = await manager.agent_dispatcher.submit_result(
        agent["id"],
        job["id"],
        job["claim_token"],
        status="completed",
        result=receipt(source_sha256="b" * 64) if invalid else receipt(),
        error=None,
        lease_id=job["lease_id"],
        lease_generation=job["lease_generation"],
    )
    # A fabricated callback envelope cannot replace the authenticated stored evidence.
    await manager.on_job_result({**recorded, "result": receipt()})
    nodes = await manager.graph.list_nodes(goal["id"])
    assert nodes[0]["status"] == ("failed" if invalid else "completed")


@pytest.mark.asyncio
async def test_old_config_and_durable_epoch_do_not_auto_enable_swift(tmp_path: Path) -> None:
    current = Path(__file__).resolve().parents[2] / "configs/permissions.yaml"
    raw = yaml.safe_load(current.read_text())
    for skill in ("code.swift.build", "code.swift.test"):
        raw["worker_skill_rules"].pop(skill)
    old_config = tmp_path / "old-permissions.yaml"
    old_config.write_text(yaml.safe_dump(raw))
    policy = PermissionPolicy.from_yaml(old_config)
    assert policy.evaluate_worker_skill("code.swift.test").decision == "deny"
    state = StateService(tmp_path / "old.db", permission_policy=policy)
    await state.initialize()
    rules = {
        key: value
        for key, value in policy.worker_skill_rules.items()
        if not key.startswith("code.swift.")
    }
    encoded, digest = WorkerSkillPolicyStore.encode_rules(rules)
    async with aiosqlite.connect(state.db_path) as db:
        await db.execute(
            "INSERT INTO worker_skill_policy_state(singleton_id,epoch,rules_json,rules_digest,updated_at) VALUES(1,1,?,?,?)",
            (encoded, digest, "2026-09-22T00:00:00+00:00"),
        )
        await db.commit()
        before = await (await db.execute("SELECT * FROM worker_skill_policy_state")).fetchone()
    await StateService(
        state.db_path, permission_policy=PermissionPolicy.from_yaml(current)
    ).initialize()
    async with aiosqlite.connect(state.db_path) as db:
        assert (
            await (await db.execute("SELECT * FROM worker_skill_policy_state")).fetchone() == before
        )
        db.row_factory = aiosqlite.Row
        snapshot = await WorkerSkillPolicyStore(state.db_path, None).load_locked(db, now="ignored")
    assert snapshot and not snapshot.is_allowed("code.swift.test")
    assert snapshot.is_allowed("workspace.list_dir")
    # Activation is a separate explicit operator transaction, never an initializer side effect.
    async with aiosqlite.connect(state.db_path) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("BEGIN IMMEDIATE")
        activated, changed = await WorkerSkillPolicyStore(state.db_path, None).replace_locked(
            db,
            PermissionPolicy.from_yaml(current),
            now="2026-09-22T00:01:00+00:00",
            expected_epoch=snapshot.epoch,
        )
        await db.commit()
    assert changed and activated.epoch == snapshot.epoch + 1
    assert activated.is_allowed("code.swift.test")


def test_unknown_or_missing_original_skills_in_old_epoch_still_fail_closed() -> None:
    policy = PermissionPolicy.from_yaml(
        Path(__file__).resolve().parents[2] / "configs/permissions.yaml"
    )
    for unknown in (False, True):
        rules = dict(policy.worker_skill_rules)
        rule = rules.pop("workspace.list_dir")
        if unknown:
            rules["unknown.operation"] = rule
        encoded, digest = WorkerSkillPolicyStore.encode_rules(rules)
        with sqlite3.connect(":memory:") as db:
            db.row_factory = sqlite3.Row
            row = db.execute(
                "SELECT 1 AS epoch,? AS rules_json,? AS rules_digest", (encoded, digest)
            ).fetchone()
        with pytest.raises(WorkerSkillPolicyStateError):
            WorkerSkillPolicyStore._snapshot_from_row(row)


def test_configured_swift_policy_survives_real_api_restart(tmp_path: Path) -> None:
    from fastapi.testclient import TestClient

    from app.main import create_app
    from app.settings import Settings

    source = Path(__file__).resolve().parents[2] / "configs/permissions.yaml"
    current = yaml.safe_load(source.read_text())
    disabled = {**current, "worker_skill_rules": dict(current["worker_skill_rules"])}
    for skill in ("code.swift.build", "code.swift.test"):
        disabled["worker_skill_rules"].pop(skill)
    configured = tmp_path / "operator-policy.yaml"
    configured.write_text(yaml.safe_dump(disabled))
    settings = Settings(
        db_path=tmp_path / "policy-state.db",
        workspace_root=tmp_path / "workspace",
        permissions_path=configured,
    )

    def snapshot():
        with sqlite3.connect(settings.db_path) as db:
            epoch, rules, digest = db.execute(
                "SELECT epoch,rules_json,rules_digest FROM worker_skill_policy_state"
            ).fetchone()
        return epoch, json.loads(rules), digest

    with TestClient(create_app(settings)):
        before = snapshot()
        assert before[1]["code.swift.test"]["decision"] == "deny"
    configured.write_text(yaml.safe_dump(current))
    with TestClient(create_app(settings)):
        activated = snapshot()
        assert activated[0] == before[0] + 1
        assert all(
            activated[1][skill]["decision"] == "allow"
            for skill in ("code.swift.build", "code.swift.test")
        )
        assert all(
            rule == activated[1][skill]
            for skill, rule in before[1].items()
            if not skill.startswith("code.swift.")
        )
    with TestClient(create_app(settings)):
        assert snapshot() == activated
