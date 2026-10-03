"""Bounded handoffs from verified jobs; worker text never grants authority."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import aiosqlite

from app.services.agent_capsule import (
    MAX_SYMBOLIC_CONTEXT_BYTES,
    symbolic_transport,
    validate_symbolic_context,
)
from app.services.context_builder import safe_context_text
from app.services.memory_symbolic_contracts import SymbolicCatalog, SymbolicEvidence
from app.services.memory_symbolic_store import SymbolicStoreError
from app.services.research_contracts import (
    project_research_collect_sources,
    valid_research_collect_receipt,
)
from app.services.research_source_requirements import _timestamp, explicit_read_urls
from app.services.result_aggregator import (
    summarize_untrusted_worker_output,
    validate_worker_evidence,
)
from app.services.swift_contracts import SWIFT_SKILLS, valid_swift_receipt
from app.services.writing_contracts import (
    MAX_DEPENDENCY_BYTES,
    MAX_DEPENDENCY_ITEMS,
    DependencyContextItem,
    WritingResearchSource,
    research_source_limits,
)


def context_bytes(value: object) -> int:
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode())


async def _symbolic_context_locked(
    db: aiosqlite.Connection,
    query: str,
    scopes: tuple[str, ...],
    catalogs: tuple[SymbolicCatalog, ...],
) -> dict[str, Any]:
    from app.services.memory_symbolic_search import search_symbolic_evidence

    omitted = False
    try:
        evidence = await search_symbolic_evidence(
            db, query, allowed_scopes=scopes, catalogs=catalogs, limit=4
        )
    except SymbolicStoreError as exc:
        if exc.code != "symbolic_result_too_large":
            raise
        evidence, omitted = [], True
    context = {
        "schema_version": "symbolic-context-v1",
        "status": "omitted_budget" if omitted else "available",
        "grants_authority": False,
        "evidence": [e.model_dump() for e in evidence],
    }
    if context_bytes(context) > MAX_SYMBOLIC_CONTEXT_BYTES:
        context.update(evidence=[], status="omitted_budget")
    return validate_symbolic_context(context)


async def attach_symbolic_native_context(
    db_path: Path,
    payload: dict[str, Any],
    *,
    validation_id: str,
    catalogs: tuple[SymbolicCatalog, ...],
) -> dict[str, Any]:
    """Bind explicitly approved native jobs, which have a grant instead of a plan node."""
    if not catalogs:
        return payload
    from app.services.swift_project_validation import require_project_grant_locked

    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("PRAGMA query_only=ON")
        await db.execute("BEGIN")
        row = await (
            await db.execute(
                """SELECT v.*,g.objective FROM swift_project_validations v
            JOIN goal_runs g ON g.id=v.goal_id WHERE v.id=?""",
                (validation_id,),
            )
        ).fetchone()
        if row is None:
            raise ValueError("symbolic worker context unavailable")
        await require_project_grant_locked(
            db, payload, task_id=row["task_id"], skill="code.swift." + row["operation"]
        )
        scopes = ("general", f"project:{row['project_id']}")
        context = await _symbolic_context_locked(db, row["objective"], scopes, catalogs)
        result = {
            **payload,
            "symbolic_context": context,
            "symbolic_context_binding": {
                "goal_id": row["goal_id"],
                "node_id": validation_id,
                "conversation_revision": row["conversation_revision"],
                "project_id": row["project_id"],
                "catalogs": [c.model_dump() for c in catalogs],
            },
        }
        symbolic_transport(result)
        return result


def _task_revision(row: aiosqlite.Row) -> str:
    # Tasks have no integer revision. Pin immutable request identity, not mutable
    # scheduling timestamps/status, which legitimately advance with the lease.
    value = {key: row[key] for key in ("input", "mode", "source", "conversation_id", "created_at")}
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


async def attach_symbolic_task_context_locked(
    db: aiosqlite.Connection,
    payload: dict[str, Any],
    *,
    task_id: str,
    catalogs: tuple[SymbolicCatalog, ...],
) -> dict[str, Any]:
    """Cover manual task dispatch without borrowing any project scope."""
    if not catalogs:
        return payload
    cursor = await db.execute("SELECT * FROM tasks WHERE id=?", (task_id,))
    cursor.row_factory = aiosqlite.Row
    row = await cursor.fetchone()
    if row is None or row["status"] not in ("created", "planned"):
        raise ValueError("symbolic_context_changed")
    if str(row["source"]).startswith("goal:") or "project_revision" in payload:
        return payload  # Those SQL-bound selections are prepared by their owning controller.
    operation, _ = symbolic_transport(payload)
    context = await _symbolic_context_locked(db, row["input"], ("general",), catalogs)
    return {
        **operation,
        "symbolic_context": context,
        "symbolic_context_binding": {
            "task_id": task_id,
            "task_revision_sha256": _task_revision(row),
            "catalogs": [c.model_dump() for c in catalogs],
        },
    }


async def attach_symbolic_worker_context(
    db_path: Path,
    payload: dict[str, Any],
    *,
    goal_id: str,
    node_id: str,
    conversation_revision: int,
    catalogs: tuple[SymbolicCatalog, ...],
) -> dict[str, Any]:
    """Select optional observations by SQL only, after the required job is built."""
    if not catalogs:
        return payload
    try:
        async with aiosqlite.connect(db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("PRAGMA query_only=ON")
            await db.execute("BEGIN")
            row = await (
                await db.execute(
                    """SELECT g.conversation_revision,p.project_id,n.objective
                FROM goal_runs g JOIN plan_nodes n ON n.goal_run_id=g.id
                LEFT JOIN goal_project_links p ON p.goal_run_id=g.id
                WHERE g.id=? AND n.id=? AND n.node_type='worker'
                AND g.status='running' AND n.conversation_revision=g.conversation_revision""",
                    (goal_id, node_id),
                )
            ).fetchone()
            if row is None or row["conversation_revision"] != conversation_revision:
                raise ValueError("symbolic worker context changed")
            project_id = row["project_id"]
            scopes = ("general",) + ((f"project:{project_id}",) if project_id else ())
            context = await _symbolic_context_locked(db, str(row["objective"]), scopes, catalogs)
            await db.rollback()
        result = {
            **payload,
            "symbolic_context": validate_symbolic_context(context),
            "symbolic_context_binding": {
                "goal_id": goal_id,
                "node_id": node_id,
                "conversation_revision": conversation_revision,
                "project_id": project_id,
                "catalogs": [c.model_dump() for c in catalogs],
            },
        }
        symbolic_transport(result)
        return result
    except (ValueError, TypeError, aiosqlite.Error) as exc:
        raise ValueError("symbolic worker context unavailable") from exc


async def require_symbolic_worker_context_locked(
    db: aiosqlite.Connection,
    payload: dict[str, Any],
    *,
    task_id: str,
    required_skill: str,
    catalogs: tuple[SymbolicCatalog, ...],
    task_statuses: tuple[str, ...] = ("created", "planned", "queued", "running"),
) -> None:
    """Fence every selected source and selection identity before queue/claim."""
    if "symbolic_context" not in payload and "symbolic_context_binding" not in payload:
        if catalogs:
            raise ValueError("symbolic_context_changed")
        return
    from app.services.memory_symbolic_search import revalidate_symbolic_evidence

    try:
        _, advisory = symbolic_transport(payload)
        binding = advisory["symbolic_context_binding"]
        if binding["catalogs"] != [c.model_dump() for c in catalogs] or not catalogs:
            raise ValueError("changed configuration")
        if "task_id" in binding:
            cursor = await db.execute("SELECT * FROM tasks WHERE id=?", (task_id,))
            cursor.row_factory = aiosqlite.Row
            task = await cursor.fetchone()
            if (
                task is None
                or task["status"] not in task_statuses
                or str(task["source"]).startswith("goal:")
                or "project_revision" in payload
                or binding["task_id"] != task_id
                or binding["task_revision_sha256"] != _task_revision(task)
            ):
                raise ValueError("changed task")
            scopes: tuple[str, ...] = ("general",)
            for raw in advisory["symbolic_context"]["evidence"]:
                if not await revalidate_symbolic_evidence(
                    db,
                    SymbolicEvidence.model_validate(raw),
                    allowed_scopes=scopes,
                    catalogs=catalogs,
                ):
                    raise ValueError("changed source")
            return
        if "project_revision" in payload and required_skill in SWIFT_SKILLS:
            cursor = await db.execute(
                """SELECT g.id AS goal_id,g.conversation_revision,l.project_id,v.id AS node_id
                FROM swift_project_validations v JOIN goal_runs g ON g.id=v.goal_id
                JOIN goal_project_links l ON l.goal_run_id=g.id AND l.project_id=v.project_id
                WHERE v.task_id=? AND 'code.swift.' || v.operation=? AND v.status='approved'
                AND g.status NOT IN ('completed','failed','cancelled','budget_exhausted')
                AND g.conversation_revision=v.conversation_revision""",
                (task_id, required_skill),
            )
        else:
            cursor = await db.execute(
                """SELECT g.id AS goal_id,g.conversation_revision,p.project_id,n.id AS node_id
            FROM tasks t JOIN goal_runs g ON t.source='goal:' || g.id
            JOIN plan_nodes n ON n.goal_run_id=g.id AND n.task_id=t.id
            LEFT JOIN goal_project_links p ON p.goal_run_id=g.id
            WHERE t.id=? AND n.required_skill=? AND n.node_type='worker'
              AND g.status='running' AND n.conversation_revision=g.conversation_revision
              AND n.status IN ('dispatched','running')""",
                (task_id, required_skill),
            )
        cursor.row_factory = aiosqlite.Row
        row = await cursor.fetchone()
        if row is None or any(
            row[key] != binding[key]
            for key in ("goal_id", "node_id", "conversation_revision", "project_id")
        ):
            raise ValueError("changed binding")
        scopes = ("general",) + ((f"project:{row['project_id']}",) if row["project_id"] else ())
        for raw in advisory["symbolic_context"]["evidence"]:
            if not await revalidate_symbolic_evidence(
                db,
                SymbolicEvidence.model_validate(raw),
                allowed_scopes=scopes,
                catalogs=catalogs,
            ):
                raise ValueError("changed source")
    except (ValueError, TypeError, KeyError, aiosqlite.Error) as exc:
        raise ValueError("symbolic_context_changed") from exc


async def read_worker_context(
    db_path: Path, goal_id: str, consumer_id: str
) -> tuple[list[dict[str, str]], list[dict[str, Any]]]:
    """Keep explicit dependency and user-requested page provenance across handoffs."""
    context: list[dict[str, str]] = []
    sources: list[dict[str, Any]] = []
    seen_urls: set[str] = set()
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("PRAGMA query_only=ON")
        await db.execute("BEGIN")
        consumer = await (
            await db.execute(
                """SELECT n.*,g.objective AS goal_objective FROM plan_nodes n
                JOIN goal_runs g ON g.id=n.goal_run_id
                WHERE n.id=? AND n.goal_run_id=? AND n.node_type='worker'
                AND n.required_skill IN ('code.build_project','writing.draft')
                AND n.conversation_revision=g.conversation_revision""",
                (consumer_id, goal_id),
            )
        ).fetchone()
        if consumer is None:
            raise ValueError("dependency context consumer is unavailable")
        # A replan may intentionally omit a new reader because these exact
        # user-requested pages were already read. Rehydrate those receipts only,
        # never arbitrary same-project search results or failed worker output.
        _append_sources(
            sources,
            seen_urls,
            await _explicit_read_sources(db, goal_id, consumer),
            required=True,
        )
        reusable_read_jobs = frozenset(
            str(source["worker_job_id"]) for source in sources if source.get("evidence")
        )
        for node in await _dependency_lineage(
            db, goal_id, consumer_id, consumer, reusable_read_jobs=reusable_read_jobs
        ):
            dependency = str(node["id"])
            # Syntheses have no independent evidence; only their validated
            # worker ancestors can supply research sources.
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
            if skill == "research.collect" and not valid_research_collect_receipt(
                result, json.loads(row["payload_json"])
            ):
                raise ValueError("research dependency receipt does not match its input")
            if skill in SWIFT_SKILLS and not valid_swift_receipt(
                skill, result, json.loads(row["payload_json"])
            ):
                raise ValueError("native dependency receipt does not match its input")
            summary = safe_context_text(summarize_untrusted_worker_output(result), max_chars=2_000)
            if summary.strip() and node["direct_context"]:
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
                elif node["dependency_type"] != "optional":
                    # A hard dependency cannot disappear merely because earlier
                    # summaries consumed the transport budget. Split the task or
                    # reduce optional context before admitting this operation.
                    raise ValueError("required dependency context exceeds its budget")
            if skill not in {"research.query", "research.collect"}:
                continue
            candidates = (
                project_research_collect_sources(result, str(row["id"]))
                if skill == "research.collect"
                else [
                    {
                        "content_trust": "untrusted",
                        "worker_job_id": str(row["id"]),
                        "title": item["title"],
                        "url": item["url"],
                        "snippet": item["snippet"],
                    }
                    for item in result["results"]
                ]
            )
            _append_sources(sources, seen_urls, candidates)
        await db.rollback()
    return context, sources


async def _dependency_lineage(
    db: aiosqlite.Connection,
    goal_id: str,
    consumer_id: str,
    consumer: Any,
    *,
    reusable_read_jobs: frozenset[str] = frozenset(),
) -> list[dict[str, Any]]:
    """Traverse named ancestry only; failed drafts are never evidence."""
    roots = json.loads(consumer["depends_on_json"])
    if not isinstance(roots, list) or len(roots) > 20:
        raise ValueError("dependency context is invalid")
    links: list[tuple[str, str, bool]] = [(item, consumer_id, True) for item in roots]
    metadata = json.loads(consumer["planner_metadata_json"] or "{}")
    if not isinstance(metadata, dict):
        raise TypeError("dependency metadata is invalid")
    if metadata.get("retry_of_node_id") is not None:
        from app.services.writing_drafts import _retry_digest, _writing_retry_record_locked

        reference = metadata["retry_of_node_id"]
        if not isinstance(reference, str) or consumer["required_skill"] != "writing.draft":
            raise ValueError("research retry identity is invalid")
        feedback, previous = await _writing_retry_record_locked(
            db, goal_id, consumer["conversation_revision"], reference
        )
        if metadata.get("source") != "evaluator" or metadata.get("writing_retry") != {
            "node_id": reference,
            "worker_job_id": feedback["worker_job_id"],
            "feedback_sha256": _retry_digest(feedback),
            "payload_sha256": _retry_digest(previous),
        }:
            raise ValueError("research retry lineage is invalid")
        parent = await (
            await db.execute("SELECT depends_on_json FROM plan_nodes WHERE id=?", (reference,))
        ).fetchone()
        if parent is None:
            raise ValueError("research retry parent is unavailable")
        inherited = json.loads(parent[0])
        if not isinstance(inherited, list) or len(inherited) > 20:
            raise ValueError("research retry dependencies are invalid")
        links.extend((item, reference, False) for item in inherited)
    ordered: list[dict[str, Any]] = []
    visited: set[str] = set()
    reused_reads: set[str] = set()

    async def visit(identity: str, child: str, direct: bool, ancestry: set[str]) -> None:
        if not isinstance(identity, str):
            raise TypeError("dependency identity is invalid")
        if identity in ancestry or identity == consumer_id:
            raise ValueError("dependency context contains a cycle")
        if identity in reused_reads:
            return
        if identity in visited:
            if direct:
                next(item for item in ordered if item["id"] == identity)["direct_context"] = True
            return
        if len(visited) + len(reused_reads) >= 100:
            raise ValueError("dependency lineage exceeds its bound")
        row = await (
            await db.execute(
                """SELECT n.*,e.dependency_type FROM plan_nodes n LEFT JOIN plan_edges e
                ON e.goal_run_id=n.goal_run_id AND e.from_node_id=n.id AND e.to_node_id=?
                WHERE n.id=? AND n.goal_run_id=?""",
                (child, identity, goal_id),
            )
        ).fetchone()
        if row is None:
            raise ValueError("dependency belongs to no matching goal")
        if row["status"] != "completed":
            if row["dependency_type"] == "optional":
                return
            raise ValueError("required dependency is incomplete")
        if row["conversation_revision"] != consumer["conversation_revision"]:
            if (
                row["conversation_revision"] < consumer["conversation_revision"]
                and row["node_type"] == "worker"
                and row["required_skill"] == "research.collect"
                and row["worker_job_id"] in reusable_read_jobs
            ):
                matching_job = await (
                    await db.execute(
                        """SELECT j.id FROM agent_jobs j
                        JOIN tasks t ON t.id=j.task_id AND t.source=?
                        WHERE j.id=? AND j.task_id=? AND j.required_skill='research.collect'
                          AND j.status='completed'""",
                        (f"goal:{goal_id}", row["worker_job_id"], row["task_id"]),
                    )
                ).fetchone()
                if matching_job is not None:
                    # Only the exact still-requested pages already revalidated
                    # in this read snapshot are reusable across revisions. Do
                    # not import this job's old summary, other pages, or ancestry.
                    reused_reads.add(identity)
                    return
            raise ValueError("dependency belongs to an older instruction revision")
        visited.add(identity)
        ordered.append({**dict(row), "direct_context": direct})
        ancestors = json.loads(row["depends_on_json"])
        if not isinstance(ancestors, list) or len(ancestors) > 20:
            raise ValueError("dependency context is invalid")
        for ancestor in ancestors:
            await visit(ancestor, identity, False, ancestry | {identity})

    for identity, child, direct in links:
        await visit(identity, child, direct, set())
    return ordered


async def _explicit_read_sources(
    db: aiosqlite.Connection, goal_id: str, consumer: Any
) -> list[dict[str, Any]]:
    """Reuse exact current user page requests, not goal-wide research selection."""
    messages = list(
        await (
            await db.execute(
                """SELECT m.role,m.content,m.created_at FROM goal_messages m
        JOIN goal_conversation_links source
          ON source.goal_run_id=m.goal_run_id AND source.conversation_id=m.conversation_id
        LEFT JOIN goal_project_links source_project ON source_project.goal_run_id=m.goal_run_id
        JOIN goal_conversation_links target ON target.conversation_id=m.conversation_id
        LEFT JOIN goal_project_links target_project ON target_project.goal_run_id=target.goal_run_id
        WHERE target.goal_run_id=? AND m.role='user'
          AND (source_project.project_id=target_project.project_id OR
            (source_project.project_id IS NULL AND target_project.project_id IS NULL))
        ORDER BY m.rowid LIMIT 10001""",
                (goal_id,),
            )
        ).fetchall()
    )
    if len(messages) > 10000:
        raise ValueError("source requirement history exceeds its bound")
    history = [dict(message) for message in messages]
    urls = explicit_read_urls(str(consumer["goal_objective"]), history)
    if not urls:
        return []
    requested_at: dict[str, Any] = {}
    for message in history:
        for url in explicit_read_urls("", [message]):
            requested_at[url] = _timestamp(message["created_at"])
    if any(url not in requested_at or requested_at[url] is None for url in urls):
        raise ValueError("explicit page requirement has no authoritative dated source")
    rows = list(
        await (
            await db.execute(
                """SELECT j.id,j.payload_json,j.result_json,j.created_at FROM plan_nodes n
        JOIN agent_jobs j ON j.id=n.worker_job_id AND j.task_id=n.task_id
          AND j.required_skill=n.required_skill
        JOIN tasks t ON t.id=j.task_id AND t.source='goal:' || n.goal_run_id
        WHERE n.goal_run_id=? AND n.node_type='worker' AND n.required_skill='research.collect'
          AND n.status='completed' AND j.status='completed'
          AND n.conversation_revision<=? AND length(CAST(j.result_json AS BLOB))<=524288
          AND length(CAST(j.payload_json AS BLOB))<=32000
        ORDER BY j.created_at DESC,j.id DESC LIMIT 101""",
                (goal_id, consumer["conversation_revision"]),
            )
        ).fetchall()
    )
    if len(rows) > 100:
        raise ValueError("source receipt history exceeds its bound")
    selected: dict[str, dict[str, Any]] = {}
    for row in rows:
        try:
            payload, result = json.loads(row["payload_json"]), json.loads(row["result_json"])
        except (TypeError, ValueError):
            continue
        started = _timestamp(row["created_at"])
        if started is None or not valid_research_collect_receipt(result, payload):
            continue
        for source in project_research_collect_sources(result, str(row["id"])):
            evidence = source.get("evidence")
            if evidence is None:
                continue
            url = evidence["requested_url"]
            if url not in urls or url in selected:
                continue
            # The same rule as replan source binding: a repeated/new read request
            # cannot be satisfied by a receipt whose job predates that request.
            if url not in requested_at or requested_at[url] is None or started <= requested_at[url]:
                continue
            selected[url] = source
    return [selected[url] for url in urls if url in selected]


def _append_sources(
    sources: list[dict[str, Any]],
    seen_urls: set[str],
    candidates: list[dict[str, Any]],
    *,
    required: bool = False,
) -> None:
    for item in candidates:
        url = item["url"]
        if safe_context_text(url, max_chars=1_001) != url:
            continue
        existing = next((index for index, old in enumerate(sources) if old["url"] == url), None)
        if existing is not None and (sources[existing].get("evidence") or not item.get("evidence")):
            continue
        item["title"] = safe_context_text(item["title"], max_chars=240)
        item["snippet"] = safe_context_text(item["snippet"], max_chars=700)
        evidence = item.get("evidence")
        if evidence is not None and (
            safe_context_text(evidence["text"], max_chars=4_001)
            != " ".join(evidence["text"].split())
            or safe_context_text(evidence["requested_url"], max_chars=1_001)
            != evidence["requested_url"]
        ):
            item.pop("evidence")
            if required:
                raise ValueError("required page evidence cannot preserve its provenance")
        try:
            source = WritingResearchSource.model_validate(item).model_dump()
        except ValueError:
            if required:
                raise ValueError("required page evidence is invalid") from None
            continue
        proposed = (
            [*sources, source]
            if existing is None
            else [source if index == existing else old for index, old in enumerate(sources)]
        )
        count_limit, byte_limit = research_source_limits(proposed)
        if len(proposed) <= count_limit and context_bytes(proposed) <= byte_limit:
            sources[:] = proposed
            seen_urls.add(url)
        elif required:
            raise ValueError("required research source context exceeds its budget")
