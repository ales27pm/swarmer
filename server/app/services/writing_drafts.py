"""Read-only draft projection and bounded, redacted writing context."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, NamedTuple

import aiosqlite

from app.services.agent_capsule import required_capsule_identity, validate_agent_capsule
from app.services.context_builder import safe_context_text
from app.services.project_contracts import (
    PROJECT_SKILL,
    ProjectPayload,
    ProjectResult,
    project_digest,
    validate_project_path,
)
from app.services.writing_contracts import (
    MAX_RESEARCH_SOURCES,
    MAX_WRITING_PAYLOAD_BYTES,
    WRITING_SKILL,
    WritingPayload,
    WritingRequirementsError,
    WritingResearchSource,
    WritingResult,
    derive_writing_requirements,
    research_source_limits,
    unsupported_citation,
    validate_writing_failure_diagnostics,
    validate_writing_requirements,
    validate_writing_result,
    writing_failure_summary,
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
    step_objective: str | None = None,
    research_sources: Sequence[Mapping[str, Any]] = (),
    dependency_context: Sequence[Mapping[str, str]] = (),
    durable_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema_version": "1.0",
        "objective": safe_context_text(objective, max_chars=4_000),
        "conversation": [],
    }
    durable_messages: list[dict[str, str]] = []
    if durable_context is not None:
        payload["durable_context"] = validate_agent_capsule(durable_context)
        durable_messages = [
            {"role": "user", "content": item["text"]}
            for item in payload["durable_context"]["requirements"]
        ]
    requirements = derive_writing_requirements(
        objective, [*durable_messages, *(dict(message) for message in conversation)]
    )
    if requirements:
        payload["requirements"] = requirements
    if dependency_context:
        payload["dependency_context"] = [dict(item) for item in dependency_context]
    # Reserve room for the newest user guidance before adding auxiliary evidence.
    latest_user = next(
        (message for message in reversed(conversation) if message["role"] == "user"), None
    )
    reserved_guidance = (
        [{"role": "user", "content": safe_context_text(latest_user["content"], max_chars=1_000)}]
        if latest_user
        else []
    )
    if research_sources:
        selected_sources: list[dict[str, Any]] = []
        for source in research_sources[:MAX_RESEARCH_SOURCES]:
            candidate = WritingResearchSource.model_validate(source).model_dump()
            proposed = [*selected_sources, candidate]
            with_guidance = {
                **payload,
                "research_sources": proposed,
                "conversation": reserved_guidance,
            }
            if (
                len(json.dumps(with_guidance, ensure_ascii=False, separators=(",", ":")).encode())
                > MAX_WRITING_PAYLOAD_BYTES
            ):
                continue
            count_limit, byte_limit = research_source_limits(proposed)
            if (
                len(proposed) > count_limit
                or len(json.dumps(proposed, ensure_ascii=False, separators=(",", ":")).encode())
                > byte_limit
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
    # The planner's assigned step is subordinate to the saved objective and user
    # instructions. It may use remaining space but cannot evict retained history.
    if step_objective is not None:
        step = safe_context_text(step_objective, max_chars=4_000)
        low, high = 0, len(step)
        while low < high:
            middle = (low + high + 1) // 2
            candidate = {**payload, "step_objective": step[:middle]}
            if (
                len(json.dumps(candidate, ensure_ascii=False, separators=(",", ":")).encode())
                <= MAX_WRITING_PAYLOAD_BYTES
            ):
                low = middle
            else:
                high = middle - 1
        if step[:low].strip():
            payload["step_objective"] = step[:low]
    return WritingPayload.model_validate(payload).model_dump(exclude_unset=True)


def _retry_digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()


async def _writing_retry_record_locked(
    db: aiosqlite.Connection, goal_id: str, revision: int, previous_node_id: str
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Read only the explicitly named failed job, never a latest-failure heuristic."""
    row = await (
        await db.execute(
            """SELECT j.id,j.payload_json,j.result_json,j.error,n.error_summary
            FROM plan_nodes n JOIN goal_runs g ON g.id=n.goal_run_id
            JOIN agent_jobs j ON j.id=n.worker_job_id AND j.task_id=n.task_id
              AND j.required_skill=n.required_skill
            JOIN tasks t ON t.id=j.task_id AND t.source=?
            WHERE n.id=? AND n.goal_run_id=? AND n.node_type='worker'
              AND n.required_skill='writing.draft' AND n.status='failed' AND j.status='failed'
              AND n.conversation_revision=? AND g.conversation_revision=?
              AND length(CAST(j.result_json AS BLOB))<=4096
              AND length(CAST(j.payload_json AS BLOB))<=32000""",
            (f"goal:{goal_id}", previous_node_id, goal_id, revision, revision),
        )
    ).fetchone()
    if row is None:
        raise ValueError("writing retry has no matching failed attempt")
    previous = WritingPayload.model_validate_json(row[1]).model_dump(exclude_unset=True)
    diagnostic = validate_writing_failure_diagnostics(json.loads(row[2]), payload=previous)
    expected = writing_failure_summary(diagnostic)
    if row[3] != expected or row[4] != expected:
        raise ValueError("writing retry measurements are not authoritative")
    feedback = {"node_id": previous_node_id, "worker_job_id": row[0], "diagnostics": diagnostic}
    return feedback, previous


async def writing_retry_provenance_locked(
    db: aiosqlite.Connection, goal_id: str, revision: int, previous_node_id: str
) -> dict[str, str]:
    """Persist the selected receipt identity with an evaluator-created repair node."""
    feedback, previous = await _writing_retry_record_locked(db, goal_id, revision, previous_node_id)
    used = await (
        await db.execute(
            """SELECT 1 FROM plan_nodes WHERE goal_run_id=?
            AND json_extract(planner_metadata_json,'$.retry_of_node_id')=? LIMIT 1""",
            (goal_id, previous_node_id),
        )
    ).fetchone()
    if used is not None:
        raise ValueError("writing attempt already has a repair")
    return {
        "node_id": previous_node_id,
        "worker_job_id": feedback["worker_job_id"],
        "feedback_sha256": _retry_digest(feedback),
        "payload_sha256": _retry_digest(previous),
    }


async def attach_writing_retry_feedback(
    db_path: Path, goal_id: str, consumer_id: str, payload: dict[str, Any]
) -> dict[str, Any]:
    """Recheck explicit same-run/revision lineage and current admission before dispatch."""
    async with aiosqlite.connect(db_path) as db:
        await db.execute("PRAGMA query_only=ON")
        await db.execute("BEGIN")
        consumer = await (
            await db.execute(
                """SELECT n.planner_metadata_json,n.conversation_revision,g.conversation_revision
                FROM plan_nodes n JOIN goal_runs g ON g.id=n.goal_run_id
                WHERE n.id=? AND n.goal_run_id=? AND n.node_type='worker'
                  AND n.required_skill='writing.draft'""",
                (consumer_id, goal_id),
            )
        ).fetchone()
        if consumer is None:
            raise ValueError("writing retry consumer is unavailable")
        metadata = json.loads(consumer[0])
        if not isinstance(metadata, dict):
            raise TypeError("writing retry metadata is invalid")
        previous_id = metadata.get("retry_of_node_id")
        if previous_id is None:
            if "writing_retry" in metadata:
                raise ValueError("writing retry provenance has no reference")
            return payload
        if (
            not isinstance(previous_id, str)
            or re.fullmatch(r"node_[A-Za-z0-9_-]+", previous_id) is None
        ):
            raise ValueError("writing retry reference is invalid")
        if metadata.get("source") != "evaluator" or consumer[1] != consumer[2]:
            raise ValueError("writing retry consumer is not a current evaluator repair")
        feedback, previous = await _writing_retry_record_locked(
            db, goal_id, consumer[1], previous_id
        )
        expected = {
            "node_id": previous_id,
            "worker_job_id": feedback["worker_job_id"],
            "feedback_sha256": _retry_digest(feedback),
            "payload_sha256": _retry_digest(previous),
        }
        if metadata.get("writing_retry") != expected:
            raise ValueError("writing retry receipt changed")
        sibling = await (
            await db.execute(
                """SELECT 1 FROM plan_nodes WHERE goal_run_id=? AND id<>?
                AND json_extract(planner_metadata_json,'$.retry_of_node_id')=? LIMIT 1""",
                (goal_id, consumer_id, previous_id),
            )
        ).fetchone()
        if sibling is not None:
            raise ValueError("writing retry lineage is ambiguous")
        old, current = (
            WritingPayload.model_validate(previous),
            WritingPayload.model_validate(payload),
        )
        if (
            old.objective != current.objective
            or old.requirements != current.requirements
            or old.conversation != current.conversation
            or old.research_sources != current.research_sources
            or required_capsule_identity(old.durable_context)
            != required_capsule_identity(current.durable_context)
        ):
            raise ValueError("writing retry no longer matches admitted inputs")
        checked = WritingPayload.model_validate(
            {**payload, "previous_attempt_feedback": feedback}
        ).model_dump(exclude_unset=True)
        await db.rollback()
        return checked


async def writing_completion_valid_locked(db: aiosqlite.Connection, goal_id: str) -> bool:
    """Compatibility wrapper; a verdict never claims semantic or runtime validity."""
    return await writing_completion_failure_locked(db, goal_id) is None


_DOCUMENT_PATH = re.compile(
    r"(?<![\w./:@-])((?:https?://)?[A-Za-z0-9_.@/-]+\.(?:markdown|md|txt|rst|adoc))"
    r"(?=$|[\s`'\"\]\[(),;:!?.])",
    re.IGNORECASE,
)


class _DocumentTarget(NamedTuple):
    requested: bool
    path: str | None


def _requested_document_path(objective: str, history: list[dict[str, str]]) -> _DocumentTarget:
    """Resolve one literal documentary path, never a URL or a guessed basename.

    This intentionally supports a narrow artifact contract. Multiple named
    documents, unsupported paths and non-output references cannot be disambiguated
    by choosing whichever saved file happens to satisfy the word count.
    """
    selected = _DocumentTarget(False, None)
    for text in [objective, *(message["content"] for message in history)]:
        matches = list(_DOCUMENT_PATH.finditer(text))
        if not matches:
            continue
        match = matches[0]
        prefix, suffix = text[max(0, match.start() - 100) : match.start()], text[match.end() :]
        output_instruction = re.search(
            r"\b(?:create|write|update|produce|save|generate|rédige[rz]?|écri[rs]|écrivez|"
            r"crée[rz]?|modifie[rz]?|produi[rs]|produisez|enregistre[rz]?|mets? à jour)\s+"
            r"(?:(?:the|a|an|new|file|document|le|la|un|une|nouveau|nouvelle|fichier)\s+)*[`'\"]?$",
            prefix,
            re.IGNORECASE,
        ) or re.match(
            r"^[`'\"\s]*(?:doit|devra|must|should|shall)\s+(?:contenir|comporter|contain|include|have)\b",
            suffix,
            re.IGNORECASE,
        )
        # Reading or mentioning another document must not erase an earlier
        # output contract. Only a recognized new output instruction replaces it.
        if not output_instruction or re.search(
            r"\b(?:do not|don't|never)\s+\w+\s+$", prefix, re.IGNORECASE
        ):
            continue
        paths = {match[1] for match in matches}
        if len(paths) != 1:
            selected = _DocumentTarget(True, None)
            continue
        try:
            selected = _DocumentTarget(True, validate_project_path(match[1]))
        except ValueError:
            selected = _DocumentTarget(True, None)
    return selected


async def _document_completion_failure_locked(
    db: aiosqlite.Connection, goal_id: str, path: str, requirements: object
) -> str | None:
    # Select the latest revision before validating its producer. Filtering out an
    # invalid latest revision here would incorrectly revive an older valid file.
    revision = await (
        await db.execute(
            """SELECT r.node_id,r.worker_job_id,r.sha256,
                CASE WHEN length(CAST(r.snapshot_json AS BLOB))<=8000000 THEN r.snapshot_json END,
                r.goal_run_id
            FROM project_revisions r JOIN goal_project_links l ON l.project_id=r.project_id
            WHERE l.goal_run_id=? ORDER BY r.revision DESC LIMIT 1""",
            (goal_id,),
        )
    ).fetchone()
    if revision is None or revision[4] != goal_id:
        return "document_requirement_unverifiable"
    origin = await (
        await db.execute(
            """SELECT j.payload_json,j.result_json FROM plan_nodes n
            JOIN agent_jobs j ON j.id=n.worker_job_id AND j.task_id=n.task_id
            JOIN tasks t ON t.id=j.task_id
            WHERE n.id=? AND n.goal_run_id=? AND n.worker_job_id=?
              AND n.node_type='worker' AND n.required_skill=? AND j.required_skill=?
              AND n.status='completed' AND j.status='completed' AND t.status='completed'
              AND t.source=? AND length(CAST(j.result_json AS BLOB))<=8000000
              AND length(CAST(j.payload_json AS BLOB))<=8000000""",
            (revision[0], goal_id, revision[1], PROJECT_SKILL, PROJECT_SKILL, f"goal:{goal_id}"),
        )
    ).fetchone()
    if origin is None:
        return "document_requirement_unverifiable"
    try:
        snapshot = ProjectResult.model_validate_json(revision[3])
        result = ProjectResult.model_validate_json(origin[1])
        payload = ProjectPayload.model_validate_json(origin[0])
        if (
            project_digest(snapshot.files) != revision[2]
            or project_digest(result.files) != revision[2]
        ):
            return "document_requirement_unverifiable"
        if (snapshot.base_revision_id, snapshot.base_sha256) != (
            result.base_revision_id,
            result.base_sha256,
        ) or (result.base_revision_id, result.base_sha256) != (
            payload.base_revision_id,
            payload.base_sha256,
        ):
            return "document_requirement_unverifiable"
        file = next((file for file in snapshot.files if file.path == path), None)
        if file is None:
            return "document_requirement_unverifiable"
        allowed_urls = {source.url for source in payload.research_sources}
        if allowed_urls and unsupported_citation(file.content, allowed_urls):
            return "writing_requirements_unmet"
        validate_writing_requirements(file.content, requirements, allowed_urls)
    except WritingRequirementsError:
        return "writing_requirements_unmet"
    except (TypeError, ValueError):
        return "document_requirement_unverifiable"
    return None


async def writing_completion_failure_locked(db: aiosqlite.Connection, goal_id: str) -> str | None:
    """Validate accepted receipts and current deterministic document constraints.

    Return a public fixed reason code; never expose private model output. A file
    fallback validates only the named document, not the entire project's quality.
    The caller owns the transaction so revisions and requirements share a snapshot.
    """
    rows = await (
        await db.execute(
            """SELECT j.result_json,j.payload_json,j.status AS job_status,t.status AS task_status,t.source,
                n.conversation_revision
        FROM plan_nodes n LEFT JOIN agent_jobs j ON j.id=n.worker_job_id AND j.task_id=n.task_id AND j.required_skill=n.required_skill
        LEFT JOIN tasks t ON t.id=j.task_id
        WHERE n.goal_run_id=? AND n.required_skill=? AND n.status='completed'""",
            (goal_id, WRITING_SKILL),
        )
    ).fetchall()
    goal = await (
        await db.execute(
            "SELECT objective,conversation_revision FROM goal_runs WHERE id=?", (goal_id,)
        )
    ).fetchone()
    if goal is None:
        return "writing_evidence_invalid"
    history = await (
        await db.execute(
            """SELECT role,content FROM (SELECT m.rowid AS sequence,m.role,m.content
            FROM goal_messages m JOIN goal_conversation_links c ON c.conversation_id=m.conversation_id
            LEFT JOIN goal_project_links target_project ON target_project.goal_run_id=c.goal_run_id
            JOIN goal_conversation_links source
              ON source.goal_run_id=m.goal_run_id AND source.conversation_id=m.conversation_id
            LEFT JOIN goal_project_links source_project ON source_project.goal_run_id=m.goal_run_id
            WHERE c.goal_run_id=? AND m.role='user'
              AND (source_project.project_id=target_project.project_id OR
                (target_project.project_id IS NULL AND source_project.project_id IS NULL))
            ORDER BY m.rowid DESC LIMIT 100) ORDER BY sequence ASC""",
            (goal_id,),
        )
    ).fetchall()
    user_messages = [{"role": row[0], "content": row[1]} for row in history]
    try:
        requirements = derive_writing_requirements(str(goal[0]), user_messages)
    except (TypeError, ValueError):
        return "document_requirement_unverifiable"
    current_receipts = 0
    for row in rows:
        if row[2] != "completed" or row[3] != "completed" or row[4] != f"goal:{goal_id}":
            return "writing_evidence_invalid"
        try:
            payload = json.loads(row[1])
            # Historical receipts retain strict shape, source and citation
            # checks. Their superseded word limits must not poison a corrected
            # deliverable on a newer user revision.
            task = WritingPayload.model_validate(payload)
            result = validate_writing_result(
                json.loads(row[0]),
                payload={
                    **task.model_dump(exclude_unset=True, exclude={"previous_attempt_feedback"}),
                    "requirements": {},
                },
            )
        except (TypeError, ValueError):
            return "writing_evidence_invalid"
        if row[5] > goal[1]:
            return "writing_evidence_invalid"
        if row[5] < goal[1]:
            continue
        current_receipts += 1
        try:
            validate_writing_result(result, payload=payload)
            validate_writing_requirements(
                result["text"],
                requirements,
                {source["url"] for source in payload.get("research_sources", [])},
            )
        except (TypeError, ValueError):
            return "writing_requirements_unmet"
    target = _requested_document_path(str(goal[0]), user_messages)
    if target.requested:
        if target.path is None:
            return "document_requirement_unverifiable"
        return await _document_completion_failure_locked(db, goal_id, target.path, requirements)
    # Without an explicit per-deliverable mapping, multiple current drafts must
    # not be treated as arbitrary replacements or concatenated into one note.
    if requirements and current_receipts != 1:
        return "document_requirement_unverifiable"
    return None


async def read_research_sources(
    db_path: Path, goal_id: str, writer_node_id: str
) -> list[dict[str, Any]]:
    """Use the same exact task/job/revision handoff as writer dispatch."""
    from app.services.worker_context import read_worker_context

    async with aiosqlite.connect(db_path) as db:
        await db.execute("PRAGMA query_only=ON")
        row = await (
            await db.execute(
                "SELECT 1 FROM plan_nodes WHERE id=? AND goal_run_id=? "
                "AND node_type='worker' AND required_skill=?",
                (writer_node_id, goal_id, WRITING_SKILL),
            )
        ).fetchone()
        if row is None:
            raise ValueError("writing node is unavailable")
    _, sources = await read_worker_context(db_path, goal_id, writer_node_id)
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
