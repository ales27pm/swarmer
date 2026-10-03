"""Optional symbolic data for direct calls, fenced again at result acceptance."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import aiosqlite

from app.services.audit_log import append_audit_event
from app.services.memory_symbolic_contracts import SymbolicCatalog, SymbolicContext
from app.services.memory_symbolic_search import (
    revalidate_symbolic_evidence,
    search_symbolic_evidence,
)
from app.services.memory_symbolic_store import SymbolicStoreError


class DirectSymbolicConflict(ValueError):
    """A fixed diagnostic; never expose source text or model output."""


async def _revision(
    db: aiosqlite.Connection, *, task_id: str | None, conversation_id: str | None
) -> str:
    if task_id:
        row = await (
            await db.execute(
                "SELECT input,mode,source,conversation_id,status,updated_at FROM tasks WHERE id=?",
                (task_id,),
            )
        ).fetchone()
        if row is None or row[4] != "planned":
            raise DirectSymbolicConflict("symbolic_direct_task_changed")
        values = list(row)
    else:
        rows = await (
            await db.execute(
                "SELECT id,role,content,metadata_json FROM messages WHERE conversation_id=? "
                "ORDER BY created_at DESC,rowid DESC LIMIT 40",
                (conversation_id,),
            )
        ).fetchall()
        if not rows:
            raise DirectSymbolicConflict("symbolic_direct_conversation_changed")
        values = [list(row) for row in rows]
    return hashlib.sha256(
        json.dumps(values, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


@dataclass(frozen=True)
class DirectSymbolicSelection:
    db_path: Path
    context: SymbolicContext
    catalogs: tuple[SymbolicCatalog, ...]
    current_catalogs: Callable[[], tuple[SymbolicCatalog, ...]]
    revision: str
    task_id: str | None
    conversation_id: str | None

    def check_catalogs(self) -> None:
        if self.current_catalogs() != self.catalogs:
            raise DirectSymbolicConflict("symbolic_direct_catalogs_changed")

    async def validate_locked(self, db: aiosqlite.Connection) -> None:
        if not db.in_transaction:
            raise RuntimeError("symbolic acceptance requires a transaction")
        self.check_catalogs()
        if (
            await _revision(db, task_id=self.task_id, conversation_id=self.conversation_id)
            != self.revision
        ):
            raise DirectSymbolicConflict("symbolic_direct_context_changed")
        for proof in self.context.evidence:
            if not await revalidate_symbolic_evidence(
                db, proof, allowed_scopes=("general",), catalogs=self.catalogs
            ):
                raise DirectSymbolicConflict("symbolic_direct_source_changed")
        # Catalogs are in-process configuration, not protected by SQLite's writer lock.
        self.check_catalogs()

    async def validate_current(self) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("BEGIN")
            await self.validate_locked(db)

    async def accept_locked(self, db: aiosqlite.Connection) -> None:
        await self.validate_locked(db)
        await append_audit_event(
            db,
            "memory.symbolic.direct.accepted",
            {
                "status": self.context.status,
                "grants_authority": False,
                "selection_revision": self.revision,
                "proposal_ids": [p.proposal.proposal_id for p in self.context.evidence],
                "read_tokens": [p.read_token for p in self.context.evidence],
            },
            task_id=self.task_id,
            trace_id=self.task_id or self.conversation_id,
        )


async def select_direct_symbolic_context(
    db_path: Path,
    query: str,
    *,
    current_catalogs: Callable[[], tuple[SymbolicCatalog, ...]],
    task_id: str | None = None,
    conversation_id: str | None = None,
    expected_messages: Sequence[Mapping[str, Any]] | None = None,
    expected_mode: str | None = None,
) -> DirectSymbolicSelection | None:
    catalogs = current_catalogs()
    if not catalogs:
        return None
    if (task_id is None) == (conversation_id is None):
        raise ValueError("one direct invocation identity is required")
    # These direct endpoints do not carry an authenticated project binding. A project
    # name in task/chat prose is never permission to retrieve that project's memory.
    async with aiosqlite.connect(db_path) as db:
        await db.execute("BEGIN")
        revision = await _revision(db, task_id=task_id, conversation_id=conversation_id)
        if task_id:
            row = await (
                await db.execute("SELECT input,mode FROM tasks WHERE id=?", (task_id,))
            ).fetchone()
            if row is None or row[0] != query or row[1] != expected_mode:
                raise DirectSymbolicConflict("symbolic_direct_task_changed")
        elif expected_messages is not None:
            current = await (
                await db.execute(
                    "SELECT id,role,content FROM messages WHERE conversation_id=? "
                    "ORDER BY created_at DESC,rowid DESC LIMIT 40",
                    (conversation_id,),
                )
            ).fetchall()
            if {str(row[0]): (row[1], row[2]) for row in current} != {
                str(message["id"]): (message["role"], message["content"])
                for message in expected_messages
            }:
                raise DirectSymbolicConflict("symbolic_direct_conversation_changed")
        try:
            evidence = await search_symbolic_evidence(
                db, query, allowed_scopes=("general",), catalogs=catalogs, limit=4
            )
            raw = {
                "schema_version": "symbolic-context-v1",
                "evidence": [p.model_dump() for p in evidence],
                "status": "available",
                "grants_authority": False,
            }
            if len(json.dumps(raw, ensure_ascii=False, separators=(",", ":")).encode()) > 16 * 1024:
                raw.update(evidence=[], status="omitted_budget")
            context = SymbolicContext.model_validate(raw)
        except SymbolicStoreError as exc:
            if exc.code != "symbolic_result_too_large":
                raise DirectSymbolicConflict("symbolic_direct_unavailable") from exc
            context = SymbolicContext(evidence=[], status="omitted_budget")
    selection = DirectSymbolicSelection(
        db_path, context, catalogs, current_catalogs, revision, task_id, conversation_id
    )
    await selection.validate_current()
    return selection
