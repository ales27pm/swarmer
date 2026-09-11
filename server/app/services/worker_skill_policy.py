from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType

import aiosqlite

from app.services.agent_card import CODE_GENERATION_SKILLS, SUPPORTED_AGENT_SKILLS
from app.services.permission_policy import (
    PermissionPolicy,
    PermissionPolicyError,
    WorkerPermissionRule,
)


class WorkerSkillPolicyStateError(PermissionPolicyError):
    """The authoritative worker-skill policy projection is invalid."""


class WorkerSkillPolicyFenceError(WorkerSkillPolicyStateError):
    """A policy candidate was parsed against an obsolete durable epoch."""


@dataclass(frozen=True)
class WorkerSkillPolicySnapshot:
    epoch: int
    rules: Mapping[str, WorkerPermissionRule]
    digest: str

    @property
    def allowed_skills(self) -> frozenset[str]:
        return frozenset(skill for skill, rule in self.rules.items() if rule.decision == "allow")

    @property
    def denied_skills(self) -> tuple[str, ...]:
        return tuple(
            sorted(skill for skill, rule in self.rules.items() if rule.decision != "allow")
        )

    def is_allowed(self, skill: str) -> bool:
        rule = self.rules.get(skill)
        return rule is not None and rule.decision == "allow"

    def can_auto_redistribute(self, skill: str) -> bool:
        rule = self.rules.get(skill)
        return rule is not None and rule.decision == "allow" and rule.auto_redistribute


class WorkerSkillPolicyStore:
    """SQLite-authoritative worker-skill policy with monotonic epochs.

    Claims, queueing, and lease recovery load this projection while holding the
    same ``BEGIN IMMEDIATE`` lock as their domain transition. An in-memory
    ``PermissionPolicy`` is only a validated bootstrap/cache and is never the
    authoritative decision once a persisted epoch exists.
    """

    def __init__(
        self,
        db_path: Path,
        permission_policy: PermissionPolicy | None,
    ) -> None:
        self.db_path = db_path
        self.permission_policy = permission_policy

    @staticmethod
    def _now() -> str:
        return datetime.now(UTC).isoformat()

    @staticmethod
    def encode_rules(rules: Mapping[str, WorkerPermissionRule]) -> tuple[str, str]:
        raw = {
            skill: {
                "id": rule.id,
                "description": rule.description,
                "decision": rule.decision,
                "risk": rule.risk,
                "auto_redistribute": rule.auto_redistribute,
            }
            for skill, rule in sorted(rules.items())
        }
        encoded = json.dumps(raw, allow_nan=False, separators=(",", ":"), sort_keys=True)
        digest = f"sha256:{hashlib.sha256(encoded.encode('utf-8')).hexdigest()}"
        return encoded, digest

    @classmethod
    def _snapshot_from_row(cls, row: aiosqlite.Row) -> WorkerSkillPolicySnapshot:
        encoded = str(row["rules_json"])
        expected_digest = f"sha256:{hashlib.sha256(encoded.encode('utf-8')).hexdigest()}"
        if not hmac.compare_digest(str(row["rules_digest"]), expected_digest):
            raise WorkerSkillPolicyStateError("authoritative worker policy digest is invalid")
        try:
            raw = json.loads(encoded)
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            raise WorkerSkillPolicyStateError(
                "authoritative worker policy is not valid JSON"
            ) from exc
        if not isinstance(raw, dict):
            raise WorkerSkillPolicyStateError("authoritative worker policy must be an object")
        try:
            rules = PermissionPolicy._parse_worker_skill_rules(raw)
        except PermissionPolicyError as exc:
            raise WorkerSkillPolicyStateError(
                "authoritative worker policy failed validation"
            ) from exc
        missing = SUPPORTED_AGENT_SKILLS - set(rules)
        extra = set(rules) - SUPPORTED_AGENT_SKILLS
        # A pre-code-generation epoch remains authoritative during upgrade.
        # Absent new skills are denied by is_allowed; only an explicit policy
        # reload can enable them. Missing original skills still fail closed.
        if missing - CODE_GENERATION_SKILLS or extra:
            raise WorkerSkillPolicyStateError(
                "authoritative worker policy does not cover the supported skill set"
            )
        epoch = int(row["epoch"])
        if epoch < 1:
            raise WorkerSkillPolicyStateError("authoritative worker policy epoch is invalid")
        return WorkerSkillPolicySnapshot(
            epoch=epoch,
            rules=MappingProxyType(dict(rules)),
            digest=expected_digest,
        )

    def _sync_local_projection(self, snapshot: WorkerSkillPolicySnapshot) -> None:
        if self.permission_policy is not None:
            self.permission_policy.worker_skill_rules = MappingProxyType(dict(snapshot.rules))

    async def load_locked(
        self,
        db: aiosqlite.Connection,
        *,
        now: str,
    ) -> WorkerSkillPolicySnapshot | None:
        """Load the current epoch inside the caller's authoritative transaction."""

        row = await (
            await db.execute(
                """
                SELECT epoch,rules_json,rules_digest
                FROM worker_skill_policy_state WHERE singleton_id=1
                """
            )
        ).fetchone()
        if row is None:
            if self.permission_policy is None:
                return None
            encoded, digest = self.encode_rules(self.permission_policy.worker_skill_rules)
            await db.execute(
                """
                INSERT INTO worker_skill_policy_state(
                    singleton_id,epoch,rules_json,rules_digest,updated_at
                ) VALUES(1,1,?,?,?)
                """,
                (encoded, digest, now),
            )
            row = await (
                await db.execute(
                    """
                    SELECT epoch,rules_json,rules_digest
                    FROM worker_skill_policy_state WHERE singleton_id=1
                    """
                )
            ).fetchone()
            if row is None:  # pragma: no cover - protected by the primary key insert
                raise WorkerSkillPolicyStateError("authoritative worker policy disappeared")
        snapshot = self._snapshot_from_row(row)
        self._sync_local_projection(snapshot)
        return snapshot

    async def replace_locked(
        self,
        db: aiosqlite.Connection,
        candidate: PermissionPolicy,
        *,
        now: str,
        expected_epoch: int,
    ) -> tuple[WorkerSkillPolicySnapshot, bool]:
        """Install a validated policy while the caller holds ``BEGIN IMMEDIATE``."""

        encoded, digest = self.encode_rules(candidate.worker_skill_rules)
        row = await (
            await db.execute(
                """
                SELECT epoch,rules_json,rules_digest
                FROM worker_skill_policy_state WHERE singleton_id=1
                """
            )
        ).fetchone()
        if row is None:
            if expected_epoch != 0:
                raise WorkerSkillPolicyFenceError(
                    "worker policy epoch changed while candidate was being validated"
                )
            epoch = 1
            await db.execute(
                """
                INSERT INTO worker_skill_policy_state(
                    singleton_id,epoch,rules_json,rules_digest,updated_at
                ) VALUES(1,?,?,?,?)
                """,
                (epoch, encoded, digest, now),
            )
            changed = True
        elif hmac.compare_digest(str(row["rules_digest"]), digest):
            snapshot = self._snapshot_from_row(row)
            return snapshot, False
        else:
            # Validate the previously authoritative row before replacing it. A
            # corrupt projection must fail closed instead of being silently
            # overwritten and hiding the integrity failure.
            current = self._snapshot_from_row(row)
            if current.epoch != expected_epoch:
                raise WorkerSkillPolicyFenceError(
                    "worker policy epoch changed while candidate was being validated"
                )
            epoch = current.epoch + 1
            cursor = await db.execute(
                """
                UPDATE worker_skill_policy_state
                SET epoch=?,rules_json=?,rules_digest=?,updated_at=?
                WHERE singleton_id=1 AND epoch=?
                """,
                (epoch, encoded, digest, now, current.epoch),
            )
            if cursor.rowcount != 1:  # pragma: no cover - writer lock prevents this race
                raise WorkerSkillPolicyStateError("worker policy epoch changed during reload")
            changed = True
        snapshot = WorkerSkillPolicySnapshot(
            epoch=epoch,
            rules=MappingProxyType(dict(candidate.worker_skill_rules)),
            digest=digest,
        )
        return snapshot, changed

    async def current_epoch(self) -> int:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            row = await (
                await db.execute(
                    """
                    SELECT epoch,rules_json,rules_digest
                    FROM worker_skill_policy_state WHERE singleton_id=1
                    """
                )
            ).fetchone()
        if row is None:
            return 0
        return self._snapshot_from_row(row).epoch

    async def replace(
        self,
        candidate: PermissionPolicy,
        *,
        expected_epoch: int | None = None,
    ) -> tuple[WorkerSkillPolicySnapshot, bool]:
        if expected_epoch is None:
            expected_epoch = await self.current_epoch()
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            try:
                snapshot, changed = await self.replace_locked(
                    db,
                    candidate,
                    now=self._now(),
                    expected_epoch=expected_epoch,
                )
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
        self._sync_local_projection(snapshot)
        return snapshot, changed

    async def reload_from_path(self, path: Path) -> tuple[WorkerSkillPolicySnapshot, bool]:
        # Parse and validate the complete policy before opening a writer
        # transaction. The previously observed epoch fences a candidate if a
        # newer policy commits while parsing is in flight.
        expected_epoch = await self.current_epoch()
        candidate = PermissionPolicy.from_yaml(path)
        return await self.replace(candidate, expected_epoch=expected_epoch)
