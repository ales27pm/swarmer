"""SQL-only local model selections, consumed with the authoritative domain write.

The opaque receipt is a server lookup, never a caller supplied source proof. It
records what was supplied, not model truth, permissions, or an execution grant.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

import aiosqlite

from app.services.audit_log import append_audit_event
from app.services.memory_local_context_contracts import (
    GoalLocalContextRequest,
    IssuedLocalContextReceipt,
    LocalContextReceipt,
    LocalContextResponse,
    ToolLocalContextRequest,
)
from app.services.memory_symbolic_contracts import SymbolicCatalog, SymbolicContext
from app.services.memory_symbolic_search import (
    revalidate_symbolic_evidence,
    search_symbolic_evidence,
)
from app.services.memory_symbolic_store import SymbolicStoreError
from app.services.project_context import ProjectContextConflict, ProjectContextService
from app.services.project_memory import ProjectMemoryConflict, ProjectMemoryService


def canonical(value: object) -> str:
    return json.dumps(
        value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
    )


def context_digest(value: object) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


class LocalContextConflict(ValueError):
    pass


class LocalContextReplay(Exception):
    def __init__(self, target_kind: str, target_id: str) -> None:
        self.target_kind, self.target_id = target_kind, target_id
        super().__init__("local_context_already_accepted")


class LocalContextService:
    def __init__(
        self,
        db_path: Path,
        *,
        current_catalogs: Callable[[], tuple[SymbolicCatalog, ...]],
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.db_path, self.current_catalogs = db_path, current_catalogs
        self.clock = clock or (lambda: datetime.now(UTC))

    def require_receipt(self, receipt: LocalContextReceipt | None) -> None:
        if receipt is None and self.current_catalogs():
            raise LocalContextConflict("local_context_required")

    @staticmethod
    def tool_binding(intent: str, mode: str) -> dict[str, Any]:
        return {
            "purpose": "tool_proposal",
            "input_sha256": context_digest(
                {"intent": intent, "mode": mode, "source": "iphone_local"}
            ),
            "mode": mode,
            "goal_id": None,
            "goal_updated_at": None,
            "project_id": None,
        }

    async def goal_binding(
        self, db: aiosqlite.Connection, goal_id: str
    ) -> tuple[dict[str, Any], str]:
        db.row_factory = aiosqlite.Row
        try:
            goal = await ProjectMemoryService._goal_snapshot(db, goal_id)
            eligible, _ = await ProjectMemoryService._planning_eligibility(db, goal)
        except ProjectMemoryConflict as exc:
            raise LocalContextConflict("local_context_goal_changed") from exc
        if not eligible:
            raise LocalContextConflict("local_context_goal_changed")
        history = await (
            await db.execute(
                """SELECT m.id,m.role,m.content FROM goal_messages m
            JOIN goal_conversation_links l ON l.conversation_id=m.conversation_id
            JOIN goal_conversation_links own ON own.goal_run_id=m.goal_run_id AND own.conversation_id=m.conversation_id
            LEFT JOIN goal_project_links p ON p.goal_run_id=m.goal_run_id
            WHERE l.goal_run_id=? AND ((? IS NULL AND p.project_id IS NULL) OR p.project_id=?)
            ORDER BY m.rowid DESC LIMIT 40""",
                (goal_id, goal["project_id"], goal["project_id"]),
            )
        ).fetchall()
        history = list(history)
        history_values = [dict(row) for row in reversed(history)]
        query = (
            goal["objective"]
            + "\n"
            + next((r["content"] for r in history if r["role"] == "user"), "")
        )
        durable = None
        if goal["project_id"] is not None:
            try:
                durable = (await ProjectContextService(self.db_path)._sources_locked(db, goal_id))[
                    3
                ]
            except ProjectContextConflict as exc:
                raise LocalContextConflict("local_context_goal_changed") from exc
        binding = {
            "purpose": "goal_plan",
            "goal_id": goal_id,
            "goal_updated_at": goal["updated_at"],
            "project_id": goal["project_id"],
            "conversation_revision": goal["conversation_revision"],
            "base_revision_id": goal["revision_id"],
            "base_revision_sha256": goal["revision_sha256"],
            "durable_context_sha256": durable,
            "input_sha256": context_digest(
                {"goal": ProjectMemoryService._logical_goal(goal), "conversation": history_values}
            ),
        }
        return binding, query

    def check_catalogs(self, expected: tuple[SymbolicCatalog, ...]) -> None:
        if self.current_catalogs() != expected:
            raise LocalContextConflict("local_context_catalogs_changed")

    async def prepare(
        self, request: GoalLocalContextRequest | ToolLocalContextRequest, device_id: str
    ) -> LocalContextResponse:
        catalogs = self.current_catalogs()
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            if isinstance(request, GoalLocalContextRequest):
                binding, query = await self.goal_binding(db, request.goal_id)
                if binding["goal_updated_at"] != request.expected_goal_updated_at:
                    raise LocalContextConflict("local_context_goal_changed")
            else:
                if not request.intent.strip():
                    raise LocalContextConflict("local_context_input_invalid")
                binding, query = self.tool_binding(request.intent, request.mode), request.intent
            common = {
                "purpose": request.purpose,
                "goal_id": binding["goal_id"],
                "goal_updated_at": binding["goal_updated_at"],
                "project_id": binding["project_id"],
                "input_sha256": binding["input_sha256"],
            }
            if not catalogs:
                self.check_catalogs(catalogs)
                return LocalContextResponse(
                    enabled=False, symbolic_context=None, receipt=None, **common
                )
            scopes = ("general",) + (
                (f"project:{binding['project_id']}",) if binding["project_id"] else ()
            )
            try:
                evidence = await search_symbolic_evidence(
                    db, query, allowed_scopes=scopes, catalogs=catalogs, limit=4
                )
                raw = {
                    "schema_version": "symbolic-context-v1",
                    "evidence": [p.model_dump(mode="json") for p in evidence],
                    "status": "available",
                    "grants_authority": False,
                }
                context = (
                    SymbolicContext(evidence=[], status="omitted_budget")
                    if len(canonical(raw).encode()) > 16384
                    else SymbolicContext.model_validate(raw)
                )
            except SymbolicStoreError as exc:
                if exc.code != "symbolic_result_too_large":
                    raise LocalContextConflict("local_context_unavailable") from exc
                context = SymbolicContext(evidence=[], status="omitted_budget")
            if len(canonical(context.model_dump(mode="json")).encode()) > request.max_context_bytes:
                context = SymbolicContext(evidence=[], status="omitted_budget")
            # A zero budget permits the small explicit omission marker, never hidden truncation.
            body = context.model_dump(mode="json")
            digest = context_digest(
                {
                    "binding": binding,
                    "catalogs": [c.model_dump() for c in catalogs],
                    "max_context_bytes": request.max_context_bytes,
                    "symbolic_context": body,
                }
            )
            now = self.clock().astimezone(UTC)
            expires = (now + timedelta(minutes=30)).isoformat()
            ident = "lmctx_" + uuid4().hex
            expired = await (
                await db.execute(
                    "SELECT id FROM memory_local_context_receipts WHERE expires_at<=? AND accepted_at IS NULL",
                    (now.isoformat(),),
                )
            ).fetchall()
            for row in expired:
                await db.execute(
                    "DELETE FROM memory_local_context_sources WHERE receipt_id=?", (row[0],)
                )
                await db.execute("DELETE FROM memory_local_context_receipts WHERE id=?", (row[0],))
            await db.execute(
                """INSERT INTO memory_local_context_receipts
                (id,device_id,purpose,goal_id,binding_json,catalogs_json,context_json,context_sha256,max_context_bytes,created_at,expires_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    ident,
                    device_id,
                    request.purpose,
                    binding["goal_id"],
                    canonical(binding),
                    canonical([c.model_dump() for c in catalogs]),
                    canonical(body),
                    digest,
                    request.max_context_bytes,
                    now.isoformat(),
                    expires,
                ),
            )
            proposals = {e.proposal.proposal_id for e in context.evidence}
            for proof in context.evidence:
                for relation in proof.relations:
                    proposals.update((relation.proposal_id, relation.target_proposal_id))
            if proposals:
                sources = await (
                    await db.execute(
                        "SELECT DISTINCT memory_id FROM memory_symbolic_sources WHERE proposal_id IN (SELECT value FROM json_each(?))",
                        (canonical(sorted(proposals)),),
                    )
                ).fetchall()
                await db.executemany(
                    "INSERT INTO memory_local_context_sources(receipt_id,memory_id) VALUES(?,?)",
                    [(ident, r[0]) for r in sources],
                )
            self.check_catalogs(catalogs)
            await db.commit()
            return LocalContextResponse(
                enabled=True,
                symbolic_context=context,
                receipt=IssuedLocalContextReceipt(
                    id=ident, context_sha256=digest, expires_at=expires
                ),
                **common,
            )

    async def _load(
        self, db: aiosqlite.Connection, receipt: LocalContextReceipt, device_id: str, purpose: str
    ) -> tuple[dict[str, Any], tuple[SymbolicCatalog, ...]]:
        db.row_factory = aiosqlite.Row
        row = await (
            await db.execute(
                "SELECT * FROM memory_local_context_receipts WHERE id=?", (receipt.id,)
            )
        ).fetchone()
        if (
            row is None
            or row["device_id"] != device_id
            or row["purpose"] != purpose
            or row["context_sha256"] != receipt.context_sha256
        ):
            raise LocalContextConflict("local_context_invalid")
        try:
            catalogs = tuple(
                SymbolicCatalog.model_validate(c) for c in json.loads(row["catalogs_json"])
            )
            binding = json.loads(row["binding_json"])
            if (
                not isinstance(binding, dict)
                or binding.get("purpose") != purpose
                or binding.get("goal_id") != row["goal_id"]
            ):
                raise ValueError("invalid context binding")
            for field in ("created_at", "expires_at"):
                timestamp = datetime.fromisoformat(row[field])
                if timestamp.utcoffset() is None:
                    raise ValueError("invalid context timestamp")
            if datetime.fromisoformat(row["expires_at"]) != datetime.fromisoformat(
                row["created_at"]
            ) + timedelta(minutes=30):
                raise ValueError("invalid context expiry")
            context = SymbolicContext.model_validate_json(row["context_json"])
            expected = context_digest(
                {
                    "binding": binding,
                    "catalogs": [c.model_dump() for c in catalogs],
                    "max_context_bytes": row["max_context_bytes"],
                    "symbolic_context": context.model_dump(mode="json"),
                }
            )
            if expected != row["context_sha256"] or not catalogs:
                raise ValueError("invalid context fingerprint")
        except (TypeError, ValueError, KeyError) as exc:
            raise LocalContextConflict("local_context_invalid") from exc
        self.check_catalogs(catalogs)
        return dict(row), catalogs

    @staticmethod
    def _replay(
        row: dict[str, Any], target_kind: str, target_id: str, proposal_sha256: str
    ) -> None:
        if row["accepted_at"] is None:
            return
        # Tool receipts bind their first task, and store the generated call as outcome.
        subject = row["accepted_subject_id"]
        if (
            row["accepted_target_kind"] != target_kind
            or subject != target_id
            or row["proposal_sha256"] != proposal_sha256
        ):
            raise LocalContextConflict("local_context_already_used")
        raise LocalContextReplay(target_kind, row["accepted_target_id"])

    async def replay(
        self,
        receipt: LocalContextReceipt,
        device_id: str,
        purpose: str,
        target_kind: str,
        target_id: str,
        proposal_sha256: str,
    ) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("BEGIN")
            row, _ = await self._load(db, receipt, device_id, purpose)
            self._replay(row, target_kind, target_id, proposal_sha256)
            # A pending local plan cannot enter the legacy resume/advance path.
            # This read guards early side effects; the acceptance transaction repeats it.
            if purpose == "goal_plan":
                self._check_expiry(row)
                binding, _ = await self.goal_binding(db, target_id)
                if binding != json.loads(row["binding_json"]):
                    raise LocalContextConflict("local_context_goal_changed")

    def _check_expiry(self, row: dict[str, Any]) -> None:
        if datetime.fromisoformat(row["expires_at"]) <= self.clock().astimezone(UTC):
            raise LocalContextConflict("local_context_expired")

    async def accept_locked(
        self,
        db: aiosqlite.Connection,
        receipt: LocalContextReceipt,
        *,
        device_id: str,
        purpose: Literal["goal_plan", "tool_proposal"],
        subject_id: str,
        target_id: str,
        proposal_sha256: str,
    ) -> tuple[SymbolicCatalog, ...]:
        if not db.in_transaction:
            raise RuntimeError("local acceptance requires transaction")
        row, catalogs = await self._load(db, receipt, device_id, purpose)
        target_kind = "goal" if purpose == "goal_plan" else "tool_call"
        self._replay(row, target_kind, subject_id, proposal_sha256)
        self._check_expiry(row)
        binding = json.loads(row["binding_json"])
        if purpose == "goal_plan":
            current, _ = await self.goal_binding(db, subject_id)
            if current != binding:
                raise LocalContextConflict("local_context_goal_changed")
        else:
            task = await (
                await db.execute("SELECT * FROM tasks WHERE id=?", (subject_id,))
            ).fetchone()
            if (
                task is None
                or task["source"] != device_id
                or task["status"] != "created"
                or task["created_at"] != task["updated_at"]
                or task["created_at"] < row["created_at"]
                or self.tool_binding(task["input"], task["mode"]) != binding
            ):
                raise LocalContextConflict("local_context_task_changed")
            # Preparation has no task/conversation identity. The first target must
            # therefore be a fresh task containing only that reviewed original input.
            messages = list(
                await (
                    await db.execute(
                        "SELECT conversation_id,task_id,role,content FROM messages WHERE task_id=? OR conversation_id=? LIMIT 2",
                        (subject_id, task["conversation_id"]),
                    )
                ).fetchall()
            )
            expected = (
                []
                if task["conversation_id"] is None
                else [(task["conversation_id"], subject_id, "user", task["input"])]
            )
            previous_call = await (
                await db.execute("SELECT id FROM tool_calls WHERE task_id=? LIMIT 1", (subject_id,))
            ).fetchone()
            if [tuple(message) for message in messages] != expected or previous_call is not None:
                raise LocalContextConflict("local_context_task_changed")
        scopes = ("general",) + (
            (f"project:{binding['project_id']}",) if binding["project_id"] else ()
        )
        context = SymbolicContext.model_validate_json(row["context_json"])
        for proof in context.evidence:
            if not await revalidate_symbolic_evidence(
                db, proof, allowed_scopes=scopes, catalogs=catalogs
            ):
                raise LocalContextConflict("local_context_source_changed")
        # The immutable semantic binding remains unchanged; first-task association is separate.
        await db.execute(
            """UPDATE memory_local_context_receipts SET accepted_target_kind=?,accepted_target_id=?,
            accepted_subject_id=?,proposal_sha256=?,accepted_at=? WHERE id=? AND accepted_at IS NULL""",
            (
                target_kind,
                target_id,
                subject_id,
                proposal_sha256,
                self.clock().astimezone(UTC).isoformat(),
                receipt.id,
            ),
        )
        await append_audit_event(
            db,
            "memory.local_context.accepted",
            {
                "receipt_id": receipt.id,
                "context_sha256": receipt.context_sha256,
                "purpose": purpose,
                "target_kind": target_kind,
                "target_id": target_id,
                "subject_id": subject_id,
                "proposal_sha256": proposal_sha256,
                "status": context.status,
                "grants_authority": False,
            },
            actor_type="device",
            actor_id=device_id,
            task_id=subject_id if purpose == "tool_proposal" else None,
        )
        self.check_catalogs(catalogs)
        return catalogs
