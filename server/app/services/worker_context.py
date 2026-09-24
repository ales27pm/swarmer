"""Bounded handoffs from verified jobs; worker text never grants authority."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import aiosqlite

from app.services.context_builder import safe_context_text
from app.services.result_aggregator import (
    summarize_untrusted_worker_output,
    validate_worker_evidence,
)
from app.services.swift_contracts import SWIFT_SKILLS, valid_swift_receipt
from app.services.writing_contracts import (
    MAX_DEPENDENCY_BYTES,
    MAX_DEPENDENCY_ITEMS,
    MAX_RESEARCH_SOURCE_BYTES,
    MAX_RESEARCH_SOURCES,
    DependencyContextItem,
    WritingResearchSource,
)


def context_bytes(value: object) -> int:
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode())


async def read_worker_context(
    db_path: Path, goal_id: str, consumer_id: str
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    """Only direct, same-run/revision dependencies, with authoritative job receipts."""
    context: list[dict[str, str]] = []
    sources: list[dict[str, str]] = []
    seen_urls: set[str] = set()
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("PRAGMA query_only=ON")
        await db.execute("BEGIN")
        consumer = await (
            await db.execute(
                """SELECT depends_on_json,conversation_revision FROM plan_nodes
                WHERE id=? AND goal_run_id=? AND node_type='worker'
                AND required_skill IN ('code.build_project','writing.draft')""",
                (consumer_id, goal_id),
            )
        ).fetchone()
        if consumer is None:
            raise ValueError("dependency context consumer is unavailable")
        dependencies = json.loads(consumer["depends_on_json"])
        if not isinstance(dependencies, list) or len(dependencies) > 20:
            raise ValueError("dependency context is invalid")
        for dependency in dependencies:
            if not isinstance(dependency, str):
                raise TypeError("dependency identity is invalid")
            node = await (
                await db.execute(
                    """SELECT n.*,e.dependency_type FROM plan_nodes n LEFT JOIN plan_edges e
                    ON e.goal_run_id=n.goal_run_id AND e.from_node_id=n.id AND e.to_node_id=?
                    WHERE n.id=? AND n.goal_run_id=?""",
                    (consumer_id, dependency, goal_id),
                )
            ).fetchone()
            if node is None:
                raise ValueError("dependency belongs to no matching goal")
            if node["status"] != "completed":
                if node["dependency_type"] == "optional":
                    continue
                raise ValueError("required dependency is incomplete")
            if node["conversation_revision"] != consumer["conversation_revision"]:
                raise ValueError("dependency belongs to an older instruction revision")
            # Syntheses have no independent job evidence; their actual worker
            # dependencies should be linked directly by the planner.
            if node["node_type"] != "worker":
                continue
            row = await (
                await db.execute(
                    """SELECT j.id,j.result_json,j.payload_json FROM agent_jobs j
                    JOIN tasks t ON t.id=j.task_id AND t.source=?
                    WHERE j.id=? AND j.task_id=? AND j.required_skill=?
                    AND j.status='completed' AND length(CAST(j.result_json AS BLOB))<=524288""",
                    (
                        f"goal:{goal_id}",
                        node["worker_job_id"],
                        node["task_id"],
                        node["required_skill"],
                    ),
                )
            ).fetchone()
            if row is None:
                raise ValueError("completed dependency evidence is unavailable")
            result: Any = json.loads(row["result_json"])
            skill = str(node["required_skill"])
            if not validate_worker_evidence(skill, result):
                raise ValueError("completed dependency evidence is invalid")
            if skill in SWIFT_SKILLS and not valid_swift_receipt(
                skill, result, json.loads(row["payload_json"])
            ):
                raise ValueError("native dependency receipt does not match its input")
            summary = safe_context_text(summarize_untrusted_worker_output(result), max_chars=2_000)
            if summary.strip():
                item = DependencyContextItem(
                    content_trust="untrusted",
                    node_id=dependency,
                    worker_job_id=str(row["id"]),
                    required_skill=skill,
                    summary=summary,
                ).model_dump()
                if (
                    len(context) < MAX_DEPENDENCY_ITEMS
                    and context_bytes([*context, item]) <= MAX_DEPENDENCY_BYTES
                ):
                    context.append(item)
            if skill != "research.query":
                continue
            for item in result["results"]:
                url = item["url"]
                if safe_context_text(url, max_chars=1_001) != url or url in seen_urls:
                    continue
                try:
                    source = WritingResearchSource(
                        content_trust="untrusted",
                        worker_job_id=str(row["id"]),
                        title=safe_context_text(item["title"], max_chars=240),
                        url=url,
                        snippet=safe_context_text(item["snippet"], max_chars=700),
                    ).model_dump()
                except ValueError:
                    continue
                if (
                    len(sources) < MAX_RESEARCH_SOURCES
                    and context_bytes([*sources, source]) <= MAX_RESEARCH_SOURCE_BYTES
                ):
                    sources.append(source)
                    seen_urls.add(url)
        await db.rollback()
    return context, sources
