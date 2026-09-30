"""Authoritative continuation context, without copying source files into the planner."""

from __future__ import annotations

import json
from pathlib import Path

import aiosqlite

from app.services.context_builder import ContextCard, safe_context_text


async def continuation_cards(db_path: Path, goal_id: str) -> tuple[ContextCard, ...]:
    """Read the latest instruction and linked revision, including ancestor-goal work.

    These cards must survive context budgeting intact. Source names are untrusted
    labels; counts/revision IDs come from persisted records, not assistant claims.
    No source body, check log or worker-authored success summary is loaded here.
    Message ownership is checked within this read snapshot, before the limit;
    the shared UI conversation is not authority for another project's input.
    """
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("PRAGMA query_only=ON")
        await db.execute("BEGIN")
        message = await (
            await db.execute(
                """SELECT m.id,m.content FROM goal_messages m
                JOIN goal_conversation_links l ON l.conversation_id=m.conversation_id
                LEFT JOIN goal_project_links target_project ON target_project.goal_run_id=l.goal_run_id
                JOIN goal_conversation_links source
                  ON source.goal_run_id=m.goal_run_id AND source.conversation_id=m.conversation_id
                LEFT JOIN goal_project_links source_project ON source_project.goal_run_id=m.goal_run_id
                WHERE l.goal_run_id=? AND m.role='user'
                  AND (source_project.project_id=target_project.project_id OR
                    (target_project.project_id IS NULL AND source_project.project_id IS NULL))
                ORDER BY m.rowid DESC LIMIT 1""",
                (goal_id,),
            )
        ).fetchone()
        checkpoint = await (
            await db.execute(
                """SELECT m.id,m.content FROM goal_messages m
                JOIN goal_conversation_links l ON l.goal_run_id=m.goal_run_id
                  AND l.conversation_id=m.conversation_id
                WHERE m.goal_run_id=? AND m.role='assistant' AND m.actor_id='goal-manager'
                  AND m.client_message_id LIKE 'auto-model-budget:%'
                ORDER BY m.rowid DESC LIMIT 1""",
                (goal_id,),
            )
        ).fetchone()
        revision = await (
            await db.execute(
                """SELECT r.id,r.project_id,r.revision,
                json_extract(r.snapshot_json,'$.runtime') AS runtime,
                json_extract(r.snapshot_json,'$.action') AS last_action,
                json_array_length(r.snapshot_json,'$.files') AS file_count,
                (SELECT json_group_array(json_extract(f.value,'$.path')) FROM (
                    SELECT value FROM json_each(r.snapshot_json,'$.files') LIMIT 8
                ) f) AS paths
                FROM project_revisions r JOIN goal_project_links l ON l.project_id=r.project_id
                WHERE l.goal_run_id=? ORDER BY r.revision DESC LIMIT 1""",
                (goal_id,),
            )
        ).fetchone()
        await db.rollback()

    cards: list[ContextCard] = []
    if checkpoint is not None:
        cards.append(
            ContextCard(
                card_id=f"continuation-checkpoint:{goal_id}",
                kind="continuation_checkpoint",
                summary=safe_context_text(str(checkpoint["content"]), max_chars=4_000),
                provenance_ids=(str(checkpoint["id"]),),
            )
        )
    # The initial objective already has its own card. Subsequent instructions
    # cannot compete for space with earlier assistant diagnostics.
    if message is not None and not str(message["id"]).startswith("gmsg_initial_"):
        instruction = safe_context_text(str(message["content"]), max_chars=64_000)
        if not instruction or len(instruction) > 4_000:
            raise ValueError("latest instruction cannot fit the protected context boundary")
        cards.append(
            ContextCard(
                card_id=f"latest-user:{goal_id}",
                kind="latest_user_message",
                summary=instruction,
                provenance_ids=(str(message["id"]),),
            )
        )
    if revision is not None:
        paths = [
            safe_context_text(str(path), max_chars=60)
            for path in json.loads(str(revision["paths"]))
        ]
        state = {
            "project_id": str(revision["project_id"]),
            "revision_id": str(revision["id"]),
            "revision": int(revision["revision"]),
            "runtime": str(revision["runtime"]),
            "reported_last_action": str(revision["last_action"]),
            "saved_file_count": int(revision["file_count"] or 0),
            "completion": "not inferred from saved files",
            "implementation_skill": "code.build_project",
            "file_names_untrusted": paths,
        }
        # Keep the structured state whole; names are optional routing hints.
        while len(json.dumps(state, ensure_ascii=False)) > 1_000:
            paths.pop()
        cards.append(
            ContextCard(
                card_id=f"saved-project:{goal_id}",
                kind="saved_project_state",
                summary=json.dumps(state, ensure_ascii=False),
                provenance_ids=(str(revision["project_id"]), str(revision["id"])),
            )
        )
    return tuple(cards)
