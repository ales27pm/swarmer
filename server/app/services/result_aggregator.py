from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, Literal

import aiosqlite
from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from app.services.code_proposal import validate_code_proposal_result
from app.services.feedback_dataset import SafeDatasetValue, sanitize_dataset_value
from app.services.swarm_contracts import GoalRunStatus, PlanNodeStatus, PlanNodeType

_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$")
_MAX_NODE_SUMMARY_CHARS = 1_200
_MAX_ERROR_SUMMARY_CHARS = 500
_MAX_GOAL_SUMMARY_CHARS = 8_000
_MAX_RESULT_NODES = 20

SafeIdentifier = Annotated[
    str,
    StringConstraints(
        min_length=1,
        max_length=200,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    ),
]
SafeSummary = Annotated[str, StringConstraints(min_length=1, max_length=_MAX_GOAL_SUMMARY_CHARS)]
SafeNodeSummary = Annotated[
    str,
    StringConstraints(min_length=1, max_length=_MAX_NODE_SUMMARY_CHARS),
]
SafeErrorSummary = Annotated[
    str,
    StringConstraints(min_length=1, max_length=_MAX_ERROR_SUMMARY_CHARS),
]
Sha256Digest = Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{64}$")]


class NodeResultProvenance(BaseModel):
    model_config = ConfigDict(extra="forbid")

    node_id: SafeIdentifier
    worker_job_id: SafeIdentifier | None
    agent_id: SafeIdentifier | None
    required_skill: SafeIdentifier | None
    result_digest: Sha256Digest | None


class SafeNodeResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    node_id: SafeIdentifier
    node_type: PlanNodeType
    status: PlanNodeStatus
    title: SafeNodeSummary
    output_summary: SafeNodeSummary | None
    error_summary: SafeErrorSummary | None
    provenance: NodeResultProvenance


class GoalResultCounts(BaseModel):
    model_config = ConfigDict(extra="forbid")

    total: int = Field(strict=True, ge=0, le=_MAX_RESULT_NODES)
    completed: int = Field(strict=True, ge=0, le=_MAX_RESULT_NODES)
    failed: int = Field(strict=True, ge=0, le=_MAX_RESULT_NODES)
    blocked: int = Field(strict=True, ge=0, le=_MAX_RESULT_NODES)
    cancelled: int = Field(strict=True, ge=0, le=_MAX_RESULT_NODES)
    skipped: int = Field(strict=True, ge=0, le=_MAX_RESULT_NODES)


class GoalResultProvenance(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: Literal["sqlite_authoritative_summaries"]
    root_task_id: SafeIdentifier
    plan_fingerprint: Sha256Digest | None
    evaluation_fingerprint: Sha256Digest | None
    node_ids: list[SafeIdentifier] = Field(max_length=_MAX_RESULT_NODES)


class SafeGoalResult(BaseModel):
    """A bounded projection. It can never contain a worker's raw result object."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0"]
    goal_run_id: SafeIdentifier
    status: GoalRunStatus
    summary: SafeSummary
    counts: GoalResultCounts
    nodes: list[SafeNodeResult] = Field(max_length=_MAX_RESULT_NODES)
    memory_ids: list[SafeIdentifier] = Field(default_factory=list, max_length=100)
    episode_ids: list[SafeIdentifier] = Field(default_factory=list, max_length=100)
    provenance: GoalResultProvenance


class ResultAggregator:
    """Build and persist deterministic summary-only results from untrusted workers."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path

    async def aggregate_goal(self, goal_run_id: str) -> dict[str, Any]:
        goal_run_id = _safe_identifier(goal_run_id, field="goal_run_id")
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            try:
                goal = await (
                    await db.execute(
                        """
                        SELECT id,root_task_id,status,plan_fingerprint,evaluation_fingerprint
                        FROM goal_runs WHERE id=?
                        """,
                        (goal_run_id,),
                    )
                ).fetchone()
                if goal is None:
                    raise ValueError("goal run not found")
                rows = await (
                    await db.execute(
                        """
                        SELECT n.id,n.node_type,n.title,n.required_skill,n.status,
                               n.result_summary,n.error_summary,n.assigned_agent_id,
                               n.worker_job_id,j.result_json AS worker_result_json
                        FROM plan_nodes AS n
                        LEFT JOIN agent_jobs AS j ON j.id=n.worker_job_id
                        WHERE n.goal_run_id=?
                        ORDER BY n.priority DESC,n.created_at ASC,n.id ASC
                        """,
                        (goal_run_id,),
                    )
                ).fetchall()
                context_rows = await (
                    await db.execute(
                        "SELECT provenance_json FROM goal_contexts WHERE goal_run_id=?",
                        (goal_run_id,),
                    )
                ).fetchall()
                source_ids: set[str] = set()
                for context_row in context_rows:
                    try:
                        provenance = json.loads(str(context_row["provenance_json"]))
                    except (json.JSONDecodeError, TypeError, ValueError):
                        continue
                    values = provenance.get("source_ids") if isinstance(provenance, dict) else None
                    if isinstance(values, list):
                        source_ids.update(
                            item
                            for item in values
                            if isinstance(item, str) and _IDENTIFIER.fullmatch(item) is not None
                        )
                memory_ids = sorted(item for item in source_ids if item.startswith("mem_"))[:100]
                episode_ids = sorted(item for item in source_ids if item.startswith("ep_"))[:100]
                result = aggregate_goal_rows(
                    dict(goal),
                    [dict(row) for row in rows],
                    memory_ids=memory_ids,
                    episode_ids=episode_ids,
                )
                encoded = json.dumps(
                    result.model_dump(mode="json"),
                    ensure_ascii=False,
                    allow_nan=False,
                    separators=(",", ":"),
                    sort_keys=True,
                )
                existing = await (
                    await db.execute(
                        "SELECT result_json FROM goal_results WHERE goal_run_id=?",
                        (goal_run_id,),
                    )
                ).fetchone()
                if existing is None:
                    now = datetime.now(UTC).isoformat()
                    await db.execute(
                        """
                        INSERT INTO goal_results(
                            goal_run_id,result_json,created_at,updated_at
                        ) VALUES(?,?,?,?)
                        """,
                        (goal_run_id, encoded, now, now),
                    )
                elif str(existing["result_json"]) != encoded:
                    await db.execute(
                        "UPDATE goal_results SET result_json=?,updated_at=? WHERE goal_run_id=?",
                        (encoded, datetime.now(UTC).isoformat(), goal_run_id),
                    )
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
        return result.model_dump(mode="json")


def aggregate_goal_rows(
    goal: Mapping[str, object],
    rows: Sequence[Mapping[str, object]],
    *,
    memory_ids: Sequence[str] = (),
    episode_ids: Sequence[str] = (),
) -> SafeGoalResult:
    if len(rows) > _MAX_RESULT_NODES:
        raise ValueError("goal contains more nodes than the aggregation limit")
    goal_run_id = _safe_identifier(goal.get("id"), field="goal_run_id")
    root_task_id = _safe_identifier(goal.get("root_task_id"), field="root_task_id")
    nodes = [_safe_node_result(row) for row in rows]
    counts = GoalResultCounts(
        total=len(nodes),
        completed=sum(node.status is PlanNodeStatus.COMPLETED for node in nodes),
        failed=sum(node.status is PlanNodeStatus.FAILED for node in nodes),
        blocked=sum(node.status is PlanNodeStatus.BLOCKED for node in nodes),
        cancelled=sum(node.status is PlanNodeStatus.CANCELLED for node in nodes),
        skipped=sum(node.status is PlanNodeStatus.SKIPPED for node in nodes),
    )
    summary = _goal_summary(nodes, counts)
    return SafeGoalResult(
        schema_version="1.0",
        goal_run_id=goal_run_id,
        status=GoalRunStatus(str(goal.get("status"))),
        summary=summary,
        counts=counts,
        nodes=nodes,
        memory_ids=[_safe_identifier(item, field="memory_id") for item in memory_ids],
        episode_ids=[_safe_identifier(item, field="episode_id") for item in episode_ids],
        provenance=GoalResultProvenance(
            source="sqlite_authoritative_summaries",
            root_task_id=root_task_id,
            plan_fingerprint=_optional_digest(goal.get("plan_fingerprint")),
            evaluation_fingerprint=_optional_digest(goal.get("evaluation_fingerprint")),
            node_ids=[node.node_id for node in nodes],
        ),
    )


def summarize_untrusted_worker_output(
    value: object,
    *,
    max_chars: int = _MAX_NODE_SUMMARY_CHARS,
) -> str:
    if not 1 <= max_chars <= _MAX_GOAL_SUMMARY_CHARS:
        raise ValueError("max_chars is outside the aggregation limit")
    sanitized = sanitize_dataset_value(value, max_text_chars=min(max_chars, 1_000))
    if sanitized in (None, "", [], {}):
        return "Worker output contained no safe summary fields."
    encoded = json.dumps(
        sanitized,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return _bounded_text(encoded, max_chars=max_chars) or "Worker output summary is unavailable."


def validate_worker_evidence(required_skill: object, value: object) -> bool:
    """Validate the concrete result contract for every supported bounded worker.

    AgentDispatcher intentionally accepts generic JSON objects for backwards
    compatibility.  A goal node has stronger evidence semantics: it may become
    complete only when the authoritative worker result matches the result shape
    of the skill that was dispatched.  This function validates shape and types;
    it never treats worker assertions as proof of task completion by itself.
    """

    if not isinstance(required_skill, str) or not isinstance(value, dict):
        return False
    if required_skill == "code.generate_python":
        try:
            validate_code_proposal_result(value)
        except ValueError:
            return False
        return True
    if required_skill == "workspace.list_dir":
        if not _only_result_fields(value, required={"entries"}, optional={"capability_result"}):
            return False
        entries = value.get("entries")
        return (
            isinstance(entries, list)
            and len(entries) <= 10_000
            and all(isinstance(item, str) and len(item) <= 4_096 for item in entries)
        )
    if required_skill == "workspace.read_text":
        if not _only_result_fields(value, required={"content"}, optional={"capability_result"}):
            return False
        return isinstance(value.get("content"), str)
    if required_skill == "research.query":
        if set(value) != {"content_trust", "results"} or value.get("content_trust") != "untrusted":
            return False
        results = value.get("results")
        return (
            isinstance(results, list)
            and len(results) <= 10
            and all(
                isinstance(item, dict)
                and set(item) == {"title", "url", "snippet"}
                and all(isinstance(item[field], str) for field in ("title", "url", "snippet"))
                for item in results
            )
        )
    if required_skill == "code_review.git_status":
        if (
            set(value)
            != {
                "content_trust",
                "entries",
                "protected_entries_omitted",
                "truncated",
            }
            or value.get("content_trust") != "untrusted"
        ):
            return False
        entries = value.get("entries")
        return (
            isinstance(entries, list)
            and len(entries) <= 10_000
            and all(_valid_status_entry(item) for item in entries)
            and _valid_nonnegative_int(value.get("protected_entries_omitted"))
            and isinstance(value.get("truncated"), bool)
        )
    if required_skill in {
        "code_review.git_diff",
        "code_review.git_show",
        "code_review.static_analysis",
    }:
        return (
            set(value)
            == {
                "content_trust",
                "output",
                "exit_code",
                "protected_entries_omitted",
                "truncated",
            }
            and value.get("content_trust") == "untrusted"
            and isinstance(value.get("output"), str)
            and isinstance(value.get("exit_code"), int)
            and not isinstance(value.get("exit_code"), bool)
            and _valid_nonnegative_int(value.get("protected_entries_omitted"))
            and isinstance(value.get("truncated"), bool)
        )
    return False


def _only_result_fields(
    value: Mapping[str, object],
    *,
    required: set[str],
    optional: set[str],
) -> bool:
    return required.issubset(value) and set(value).issubset(required | optional)


def _valid_nonnegative_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _valid_status_entry(value: object) -> bool:
    if not isinstance(value, dict) or not {"status", "path"}.issubset(value):
        return False
    if not set(value).issubset({"status", "path", "original_path"}):
        return False
    return all(isinstance(item, str) for item in value.values())


def _safe_node_result(row: Mapping[str, object]) -> SafeNodeResult:
    node_id = _safe_identifier(row.get("id"), field="node_id")
    raw_result = row.get("worker_result_json")
    output_summary = _safe_optional_text(
        row.get("result_summary"), max_chars=_MAX_NODE_SUMMARY_CHARS
    )
    parsed_result: object | None = None
    if raw_result is not None:
        try:
            parsed_result = json.loads(str(raw_result))
        except (json.JSONDecodeError, TypeError, ValueError):
            parsed_result = None
    if output_summary is None and parsed_result is not None:
        output_summary = summarize_untrusted_worker_output(parsed_result)
    if output_summary is None and raw_result is not None:
        output_summary = "Worker result could not be parsed and was omitted."
    return SafeNodeResult(
        node_id=node_id,
        node_type=PlanNodeType(str(row.get("node_type"))),
        status=PlanNodeStatus(str(row.get("status"))),
        title=_safe_required_text(row.get("title"), max_chars=_MAX_NODE_SUMMARY_CHARS),
        output_summary=output_summary,
        error_summary=_safe_optional_text(
            row.get("error_summary"), max_chars=_MAX_ERROR_SUMMARY_CHARS
        ),
        provenance=NodeResultProvenance(
            node_id=node_id,
            worker_job_id=_optional_identifier(row.get("worker_job_id")),
            agent_id=_optional_identifier(row.get("assigned_agent_id")),
            required_skill=_optional_identifier(row.get("required_skill")),
            result_digest=_raw_result_digest(raw_result),
        ),
    )


def _goal_summary(nodes: Sequence[SafeNodeResult], counts: GoalResultCounts) -> str:
    synthesis = [
        node.output_summary
        for node in nodes
        if node.node_type is PlanNodeType.SYNTHESIS and node.output_summary
    ]
    selected = synthesis or [
        f"{node.title}: {node.output_summary}" for node in nodes if node.output_summary is not None
    ]
    prefix = (
        f"Goal nodes: {counts.completed}/{counts.total} completed; "
        f"{counts.failed} failed; {counts.blocked} blocked; "
        f"{counts.cancelled} cancelled; {counts.skipped} skipped."
    )
    detail = " ".join(selected)
    return (
        _bounded_text(
            f"{prefix} {detail}".strip(),
            max_chars=_MAX_GOAL_SUMMARY_CHARS,
        )
        or "Goal result summary is unavailable."
    )


def _safe_identifier(value: object, *, field: str) -> str:
    if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
        raise ValueError(f"{field} is not a safe identifier")
    return value


def _optional_identifier(value: object) -> str | None:
    if value is None or value == "":
        return None
    return _safe_identifier(value, field="provenance identifier")


def _optional_digest(value: object) -> str | None:
    if value is None or value == "":
        return None
    text = str(value)
    if re.fullmatch(r"[a-f0-9]{64}", text) is None:
        return None
    return text


def _raw_result_digest(raw: object) -> str | None:
    if raw is None:
        return None
    encoded = str(raw).encode("utf-8", errors="replace")
    return hashlib.sha256(encoded).hexdigest()


def _safe_required_text(value: object, *, max_chars: int) -> str:
    result = _safe_optional_text(value, max_chars=max_chars)
    return result or "Summary unavailable."


def _safe_optional_text(value: object, *, max_chars: int) -> str | None:
    if value is None:
        return None
    sanitized: SafeDatasetValue = sanitize_dataset_value(value, max_text_chars=max_chars)
    if not isinstance(sanitized, str) or not sanitized:
        return None
    return _bounded_text(sanitized, max_chars=max_chars)


def _bounded_text(value: str, *, max_chars: int) -> str:
    normalized = " ".join(value.split())
    if len(normalized) <= max_chars:
        return normalized
    if max_chars <= 1:
        return "…"[:max_chars]
    return normalized[: max_chars - 1].rstrip() + "…"
