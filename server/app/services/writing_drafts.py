"""Read-only draft projection and bounded, redacted writing context."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import aiosqlite

from app.services.context_builder import safe_context_text
from app.services.result_aggregator import validate_worker_evidence
from app.services.writing_contracts import (
    MAX_RESEARCH_SOURCE_BYTES,
    MAX_RESEARCH_SOURCES,
    MAX_WRITING_PAYLOAD_BYTES,
    WRITING_SKILL,
    WritingPayload,
    WritingResearchSource,
    WritingResult,
)


class WritingDraftPreview(WritingResult):
    goal_run_id: str
    node_id: str
    worker_job_id: str
    sha256: str


def writing_payload(
    objective: str,
    conversation: Sequence[Mapping[str, str]],
    *,
    research_sources: Sequence[Mapping[str, str]] = (),
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema_version": "1.0",
        "objective": safe_context_text(objective, max_chars=4_000),
        "conversation": [],
    }
    if research_sources:
        selected_sources: list[dict[str, str]] = []
        for source in research_sources[:MAX_RESEARCH_SOURCES]:
            candidate = WritingResearchSource.model_validate(source).model_dump()
            proposed = [*selected_sources, candidate]
            if len(json.dumps(proposed, ensure_ascii=False, separators=(",", ":")).encode()) > (
                MAX_RESEARCH_SOURCE_BYTES
            ):
                break
            selected_sources.append(candidate)
        if selected_sources:
            payload["research_sources"] = selected_sources

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
    return WritingPayload.model_validate(payload).model_dump(exclude_unset=True)


async def read_research_sources(
    db_path: Path, goal_id: str, writer_node_id: str
) -> list[dict[str, str]]:
    """Read only verified completed research dependencies of this writer."""
    sources: list[dict[str, str]] = []
    seen_urls: set[str] = set()
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("PRAGMA query_only=ON")
        await db.execute("BEGIN")
        writer = await (
            await db.execute(
                """SELECT depends_on_json FROM plan_nodes WHERE id=? AND goal_run_id=?
                   AND node_type='worker' AND required_skill=?""",
                (writer_node_id, goal_id, WRITING_SKILL),
            )
        ).fetchone()
        if writer is None:
            raise ValueError("writing node is unavailable")
        dependencies = json.loads(writer["depends_on_json"])
        if not isinstance(dependencies, list) or len(dependencies) > 20:
            raise ValueError("writing dependencies are invalid")
        for dependency in dependencies:
            if not isinstance(dependency, str):
                raise TypeError("writing dependency identity is invalid")
            node = await (
                await db.execute(
                    "SELECT required_skill FROM plan_nodes WHERE id=? AND goal_run_id=?",
                    (dependency, goal_id),
                )
            ).fetchone()
            if node is None:
                raise ValueError("writing dependency belongs to no matching goal")
            if node["required_skill"] != "research.query":
                continue
            row = await (
                await db.execute(
                    """SELECT j.id,j.result_json FROM plan_nodes n
                       JOIN agent_jobs j ON j.id=n.worker_job_id AND j.task_id=n.task_id
                       JOIN tasks t ON t.id=j.task_id AND t.source=?
                       WHERE n.id=? AND n.goal_run_id=? AND n.node_type='worker'
                         AND n.required_skill='research.query' AND j.required_skill='research.query'
                         AND n.status='completed' AND j.status='completed'
                         AND length(CAST(j.result_json AS BLOB))<=524288""",
                    (f"goal:{goal_id}", dependency, goal_id),
                )
            ).fetchone()
            if row is None:
                raise ValueError("completed research evidence is unavailable")
            result = json.loads(row["result_json"])
            if not validate_worker_evidence("research.query", result):
                raise ValueError("completed research evidence is invalid")
            for item in result["results"]:
                url = item["url"]
                # Redact first; never turn a truncated or credential-bearing URL into a citation.
                if safe_context_text(url, max_chars=1_001) != url or url in seen_urls:
                    continue
                try:
                    source = WritingResearchSource(
                        content_trust="untrusted",
                        worker_job_id=str(row["id"]),
                        title=safe_context_text(item["title"], max_chars=240),
                        url=url,
                        snippet=safe_context_text(item["snippet"], max_chars=700),
                    )
                except ValueError:
                    continue
                if len(sources) < MAX_RESEARCH_SOURCES:
                    sources.append(source.model_dump())
                    seen_urls.add(url)
        await db.rollback()
    return sources


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
