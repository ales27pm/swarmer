"""New optional capabilities must not rewrite an operator's historical policy."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest
import yaml

from app.models import AgentCreate
from app.services.permission_policy import PermissionPolicy, PermissionPolicyError
from app.services.state_service import StateService
from app.services.worker_skill_policy import (
    WorkerSkillPolicyFenceError,
    WorkerSkillPolicyStateError,
)

POLICY = Path(__file__).resolve().parents[2] / "configs/permissions.yaml"
NEW_CALENDAR = {
    "iphone.calendar.calendars",
    "iphone.calendar.reminders",
    "iphone.calendar.event.create",
    "iphone.calendar.event.update",
    "iphone.calendar.reminder.create",
    "iphone.calendar.reminder.update",
}
NEW_SPECIALISTS = {
    "image.generate",
    "audio.synthesize",
    "database.sqlite.inspect",
    "database.sqlite.query",
    "database.sqlite.create",
    "database.sqlite.backup",
    "database.sqlite.migrate",
    "crm.command",
    "documents.extract",
}


def legacy_raw() -> dict:
    raw = yaml.safe_load(POLICY.read_text())
    for name in NEW_CALENDAR:
        del raw["capability_rules"][name]
    for name in NEW_SPECIALISTS:
        del raw["worker_skill_rules"][name]
    return raw


def write_policy(tmp_path: Path, raw: dict) -> Path:
    path = tmp_path / "legacy.yaml"
    path.write_text(yaml.safe_dump(raw))
    return path


def durable_row(database: Path) -> tuple:
    with sqlite3.connect(database) as db:
        return db.execute("SELECT * FROM worker_skill_policy_state").fetchone()


async def seed_legacy(tmp_path: Path) -> tuple[Path, dict, StateService]:
    raw = legacy_raw()
    path = write_policy(tmp_path, raw)
    state = StateService(tmp_path / "state.db")
    await state.initialize()
    # Seed the bytes of an already authorized epoch independently of policy parsing.
    encoded = json.dumps(raw["worker_skill_rules"], sort_keys=True, separators=(",", ":"))
    digest = "sha256:" + hashlib.sha256(encoded.encode()).hexdigest()
    with sqlite3.connect(state.db_path) as db:
        db.execute(
            "INSERT INTO worker_skill_policy_state VALUES(1,6,?,?,?)",
            (encoded, digest, "2026-09-01T00:00:00+00:00"),
        )
    return path, raw, state


def test_legacy_yaml_keeps_missing_new_rules_unconfigured(tmp_path: Path) -> None:
    raw = legacy_raw()
    path = write_policy(tmp_path, raw)
    before = path.read_bytes()
    policy = PermissionPolicy.from_yaml(path)
    assert set(policy.capability_rules) == set(raw["capability_rules"])
    assert set(policy.worker_skill_rules) == set(raw["worker_skill_rules"])
    assert path.read_bytes() == before
    for name in NEW_CALENDAR:
        with pytest.raises(PermissionPolicyError, match="not covered by policy"):
            policy.evaluate_capability(name)
    for skill in NEW_SPECIALISTS:
        with pytest.raises(PermissionPolicyError, match="not covered by policy"):
            policy.evaluate_worker_skill(skill)


@pytest.mark.parametrize(
    "section,name,change",
    [
        ("capability_rules", "iphone.calendar.events", "missing"),
        ("worker_skill_rules", "workspace.list_dir", "missing"),
        ("capability_rules", "iphone.unrecognized", "extra"),
        ("worker_skill_rules", "database.unrecognized", "extra"),
        ("capability_rules", "iphone.location.current", "malformed"),
        ("worker_skill_rules", "workspace.read_text", "malformed"),
    ],
)
def test_legacy_exceptions_do_not_relax_existing_validation(
    tmp_path: Path, section: str, name: str, change: str
) -> None:
    raw = legacy_raw()
    if change == "missing":
        del raw[section][name]
    elif change == "extra":
        raw[section][name] = dict(next(iter(raw[section].values())))
    else:
        raw[section][name]["decision"] = "unrecognized"
    with pytest.raises(PermissionPolicyError):
        PermissionPolicy.from_yaml(write_policy(tmp_path, raw))


@pytest.mark.asyncio
async def test_initialize_and_reload_preserve_historical_epoch_and_exact_rules(
    tmp_path: Path,
) -> None:
    path, raw, initial = await seed_legacy(tmp_path)
    original_row = durable_row(initial.db_path)
    original_yaml = path.read_bytes()
    expected_allowed = frozenset(
        skill for skill, rule in raw["worker_skill_rules"].items() if rule["decision"] == "allow"
    )
    for _ in range(2):
        policy = PermissionPolicy.from_yaml(path)
        state = StateService(initial.db_path, permission_policy=policy)
        await state.initialize()
        snapshot, changed = await state.worker_skill_policy.reload_from_path(path)
        assert not changed
        assert snapshot.epoch == 6
        assert snapshot.allowed_skills == expected_allowed
        assert set(snapshot.rules) == set(raw["worker_skill_rules"])
        assert snapshot.digest == original_row[3]
        assert durable_row(state.db_path) == original_row
        assert path.read_bytes() == original_yaml
        for skill in NEW_SPECIALISTS:
            assert not snapshot.is_allowed(skill)
            assert not snapshot.can_auto_redistribute(skill)
            with pytest.raises(PermissionPolicyError, match="denied by policy"):
                await state.register_agent(
                    AgentCreate(
                        name="unconfigured-specialist",
                        endpoint="http://127.0.0.1:1",
                        skills=[skill],
                    ),
                    "test-device",
                )
        assert durable_row(state.db_path) == original_row
    with sqlite3.connect(initial.db_path) as db:
        assert db.execute("SELECT count(*) FROM agents").fetchone() == (0,)


@pytest.mark.asyncio
async def test_explicit_new_policy_reload_is_still_fenced(tmp_path: Path) -> None:
    path, _, state = await seed_legacy(tmp_path)
    assert await state.worker_skill_policy.current_epoch() == 6
    legacy = PermissionPolicy.from_yaml(path)
    full = PermissionPolicy.from_yaml(POLICY)
    snapshot, changed = await state.worker_skill_policy.replace(full, expected_epoch=6)
    assert changed and snapshot.epoch == 7
    assert all(snapshot.is_allowed(skill) for skill in NEW_SPECIALISTS)
    for name in NEW_CALENDAR:
        assert full.evaluate_capability(name).decision == "ask"
    original_row = durable_row(state.db_path)
    with pytest.raises(WorkerSkillPolicyFenceError, match="epoch changed"):
        await state.worker_skill_policy.replace(legacy, expected_epoch=6)
    assert durable_row(state.db_path) == original_row
    snapshot, changed = await state.worker_skill_policy.reload_from_path(POLICY)
    assert not changed and snapshot.epoch == 7
    assert durable_row(state.db_path) == original_row


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["missing", "extra", "malformed", "digest", "json"])
async def test_corrupt_historical_rules_still_fail_without_rewriting(
    tmp_path: Path, fault: str
) -> None:
    _, raw, state = await seed_legacy(tmp_path)
    rules = raw["worker_skill_rules"]
    if fault == "missing":
        del rules["workspace.read_text"]
    elif fault == "extra":
        rules["unknown.skill"] = dict(rules["workspace.read_text"])
    elif fault == "malformed":
        rules["workspace.read_text"]["auto_redistribute"] = "yes"
    encoded = "{" if fault == "json" else json.dumps(rules, sort_keys=True)
    digest = "sha256:" + hashlib.sha256(encoded.encode()).hexdigest()
    if fault == "digest":
        digest = "sha256:" + "0" * 64
    with sqlite3.connect(state.db_path) as db:
        db.execute(
            "UPDATE worker_skill_policy_state SET rules_json=?,rules_digest=?",
            (encoded, digest),
        )
    original_row = durable_row(state.db_path)
    with pytest.raises(WorkerSkillPolicyStateError):
        await state.worker_skill_policy.current_epoch()
    with pytest.raises(WorkerSkillPolicyStateError):
        await state.worker_skill_policy.reload_from_path(POLICY)
    assert durable_row(state.db_path) == original_row
