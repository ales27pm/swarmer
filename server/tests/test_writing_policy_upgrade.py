from __future__ import annotations

import sqlite3
from pathlib import Path

import aiosqlite
import pytest

from app.services.permission_policy import PermissionPolicy
from app.services.state_service import StateService
from app.services.worker_skill_policy import WorkerSkillPolicyStateError, WorkerSkillPolicyStore

PERMISSIONS_PATH = Path(__file__).resolve().parents[2] / "configs" / "permissions.yaml"


@pytest.mark.asyncio
async def test_historic_epoch_denies_writing_until_explicit_fenced_reload(tmp_path: Path) -> None:
    state = StateService(tmp_path / "state.db")
    await state.initialize()
    candidate = PermissionPolicy.from_yaml(PERMISSIONS_PATH)
    historic_rules = dict(candidate.worker_skill_rules)
    historic_rules.pop("writing.draft")
    encoded, digest = WorkerSkillPolicyStore.encode_rules(historic_rules)
    now = "2026-09-21T00:00:00+00:00"
    async with aiosqlite.connect(state.db_path) as db:
        await db.execute(
            "INSERT INTO worker_skill_policy_state "
            "(singleton_id,epoch,rules_json,rules_digest,updated_at) VALUES (1,7,?,?,?)",
            (encoded, digest, now),
        )
        await db.commit()

    store = WorkerSkillPolicyStore(state.db_path, None)
    async with aiosqlite.connect(state.db_path) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("PRAGMA query_only=ON")
        await db.execute("BEGIN")
        historic = await store.load_locked(db, now=now)
        row = await (await db.execute("SELECT * FROM worker_skill_policy_state")).fetchone()
        await db.rollback()
    assert historic is not None and historic.epoch == 7
    assert historic.is_allowed("workspace.list_dir")
    assert not historic.is_allowed("writing.draft")
    assert not historic.can_auto_redistribute("writing.draft")
    assert row is not None and row["rules_json"] == encoded and row["rules_digest"] == digest
    assert await store.current_epoch() == 7

    updated, changed = await store.replace(candidate, expected_epoch=7)
    assert changed and updated.epoch == 8
    assert updated.is_allowed("writing.draft")
    assert not updated.can_auto_redistribute("writing.draft")
    assert updated.is_allowed("workspace.list_dir")


@pytest.mark.parametrize("corruption", ["missing_original", "extra_skill", "digest"])
def test_historic_writing_compatibility_does_not_accept_corrupt_policy(corruption: str) -> None:
    rules = dict(PermissionPolicy.from_yaml(PERMISSIONS_PATH).worker_skill_rules)
    rules.pop("writing.draft")
    if corruption == "missing_original":
        rules.pop("workspace.list_dir")
    elif corruption == "extra_skill":
        rules["writing.execute"] = rules["workspace.list_dir"]
    encoded, digest = WorkerSkillPolicyStore.encode_rules(rules)
    if corruption == "digest":
        digest = "sha256:" + "0" * 64
    with sqlite3.connect(":memory:") as db:
        db.row_factory = sqlite3.Row
        row = db.execute(
            "SELECT 7 AS epoch,? AS rules_json,? AS rules_digest", (encoded, digest)
        ).fetchone()
    with pytest.raises(WorkerSkillPolicyStateError):
        WorkerSkillPolicyStore._snapshot_from_row(row)
