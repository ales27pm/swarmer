"""Read-only draft projection and bounded, redacted writing context."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import aiosqlite

from app.services.context_builder import safe_context_text
from app.services.writing_contracts import (
    MAX_WRITING_PAYLOAD_BYTES,
    WRITING_SKILL,
    WritingPayload,
    WritingResult,
)


class WritingDraftPreview(WritingResult):
    goal_run_id: str
    node_id: str
    worker_job_id: str
    sha256: str


def writing_payload(objective: str, conversation: Sequence[Mapping[str, str]]) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema_version": "1.0",
        "objective": safe_context_text(objective, max_chars=4_000),
        "conversation": [],
    }

    def fits(messages: list[dict[str, str]]) -> bool:
        candidate = {**payload, "conversation": messages}
        return len(json.dumps(candidate, ensure_ascii=False, separators=(",", ":")).encode()) <= (
            MAX_WRITING_PAYLOAD_BYTES
        )

    # Keep newest guidance first when the byte budget cannot retain the whole history.
    selected: list[dict[str, str]] = []
    for message in reversed(conversation[-12:]):
        if message["role"] not in {"user", "assistant"}:
            continue
        content = safe_context_text(message["content"], max_chars=4_000)
        if not content.strip():
            continue
        low, high = 0, len(content)
        while low < high:
            middle = (low + high + 1) // 2
            candidate = {"role": message["role"], "content": content[:middle]}
            if fits([candidate, *selected]):
                low = middle
            else:
                high = middle - 1
        if not content[:low].strip():
            break
        selected.insert(0, {"role": message["role"], "content": content[:low]})
        if low < len(content):
            break
    payload["conversation"] = selected
    return WritingPayload.model_validate(payload).model_dump()


async def read_writing_draft(
    db_path: Path, goal_id: str, node_id: str
) -> WritingDraftPreview | None:
    async with aiosqlite.connect(db_path) as db:
        row = await (
            await db.execute(
                """SELECT j.id,j.result_json FROM plan_nodes AS n
                JOIN agent_jobs AS j ON j.id=n.worker_job_id AND j.task_id=n.task_id
                WHERE n.goal_run_id=? AND n.id=? AND n.required_skill=?
                  AND j.required_skill=? AND n.status='completed' AND j.status='completed'""",
                (goal_id, node_id, WRITING_SKILL, WRITING_SKILL),
            )
        ).fetchone()
    if row is None:
        return None
    # Validate before projection: never expose raw jobs, payloads or invalid terminal output.
    result = WritingResult.model_validate_json(row[1])
    return WritingDraftPreview(
        **result.model_dump(),
        goal_run_id=goal_id,
        node_id=node_id,
        worker_job_id=str(row[0]),
        sha256=hashlib.sha256(result.text.encode("utf-8")).hexdigest(),
    )
