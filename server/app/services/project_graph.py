"""Consistent, bounded SQLite graph projection; no reconciliation or model calls."""

import hashlib
import json
import re
from datetime import UTC, datetime
from graphlib import TopologicalSorter
from pathlib import Path
from typing import Any

import aiosqlite

from app.services.feedback_dataset import redact_dataset_text
from app.services.goal_state import PUBLIC_NODE_FIELDS, public_goal, public_plan_node
from app.services.project_contracts import ProjectCheck, ProjectFile, project_digest
from app.services.project_graph_contracts import (
    GraphCheck,
    GraphCoverage,
    GraphCriterion,
    GraphDependency,
    GraphEvaluation,
    GraphFile,
    GraphPlanningDecision,
    GraphRevision,
    ProjectGraph,
)
from app.services.swarm_contracts import GoalRecord, PlanNode

# Match the existing activity projection: arbitrary command arguments may contain secrets.
_PUBLIC_COMMANDS = (
    ["python", "-m", "compileall", "-q", "."],
    ["python", "-m", "pytest", "-q"],
    ["npm", "run", "build"],
    ["node", "--test"],
    ["python", "-m", "pip", "install", "-r", "requirements.txt"],
    ["npm", "install"],
    ["npm", "ci"],
)


def _safe(value: str, limit: int = 4_000) -> str:
    return (redact_dataset_text(value) or "Texte non disponible après masquage.")[:limit]


def _goal(row: aiosqlite.Row) -> GoalRecord:
    record = dict(row)
    record["completion_criteria"] = json.loads(record["completion_criteria_json"])
    goal = GoalRecord.model_validate(public_goal(record))
    safe = goal.model_dump()
    for key in ("objective", "evaluator_summary", "failure_reason"):
        if safe[key] is not None:
            safe[key] = _safe(safe[key])
    safe["completion_criteria"] = [_safe(text, 500) for text in goal.completion_criteria]
    return GoalRecord.model_validate(safe)


async def _read_nodes(db: aiosqlite.Connection, goal_id: str) -> list[PlanNode]:
    # The fields come from a fixed application whitelist, never request data.
    columns = ",".join(
        "depends_on_json" if field == "depends_on" else field for field in PUBLIC_NODE_FIELDS
    )
    rows = list(
        await (
            await db.execute(
                f"SELECT {columns} FROM plan_nodes WHERE goal_run_id=? ORDER BY created_at,id LIMIT 21",  # nosec B608
                (goal_id,),
            )
        ).fetchall()
    )
    if len(rows) > 20:
        raise ProjectGraphEvidenceError("project graph exceeds node bound")
    nodes = []
    for row in rows:
        record = dict(row)
        record["depends_on"] = json.loads(record.pop("depends_on_json"))
        node = PlanNode.model_validate(public_plan_node(record))
        safe = node.model_dump()
        for field in ("title", "objective", "expected_output", "result_summary", "error_summary"):
            if safe[field] is not None:
                safe[field] = _safe(safe[field], 500 if field == "title" else 4_000)
        nodes.append(PlanNode.model_validate(safe))
    return nodes


async def _read_dependencies(
    db: aiosqlite.Connection,
    goal_id: str,
    nodes: list[PlanNode],
) -> list[GraphDependency]:
    rows = list(
        await (
            await db.execute(
                """SELECT from_node_id,to_node_id,dependency_type FROM plan_edges
        WHERE goal_run_id=? ORDER BY from_node_id,to_node_id LIMIT 191""",
                (goal_id,),
            )
        ).fetchall()
    )
    if len(rows) > 190:
        raise ProjectGraphEvidenceError("project graph exceeds dependency bound")
    dependencies = [GraphDependency.model_validate(dict(row)) for row in rows]
    by_id = {node.id: node for node in nodes}
    actual: dict[str, set[str]] = {node.id: set() for node in nodes}
    for edge in dependencies:
        if edge.from_node_id not in by_id or edge.to_node_id not in by_id:
            raise ProjectGraphEvidenceError("project graph has an unknown dependency")
        actual[edge.to_node_id].add(edge.from_node_id)
    for node in nodes:
        if (
            len(set(node.depends_on)) != len(node.depends_on)
            or set(node.depends_on) != actual[node.id]
        ):
            raise ProjectGraphEvidenceError("project graph dependency records disagree")
    tuple(TopologicalSorter(actual).static_order())  # Raises for corrupt self/cyclic edges.
    return dependencies


async def _read_revision(db: aiosqlite.Connection, project_id: str | None) -> GraphRevision | None:
    if project_id is None:
        return None
    row = await (
        await db.execute(
            """SELECT id,project_id,goal_run_id,node_id,worker_job_id,revision,sha256,created_at,
        CASE WHEN length(snapshot_json)<=8000000 THEN snapshot_json END AS snapshot_json
        FROM project_revisions WHERE project_id=? ORDER BY revision DESC LIMIT 1""",
            (project_id,),
        )
    ).fetchone()
    if row is None:
        return None
    origin = await (
        await db.execute(
            """SELECT n.id FROM plan_nodes n JOIN agent_jobs j ON j.id=n.worker_job_id
        AND j.task_id=n.task_id JOIN goal_project_links l ON l.goal_run_id=n.goal_run_id
        WHERE n.id=? AND n.goal_run_id=? AND n.worker_job_id=? AND l.project_id=?""",
            (row["node_id"], row["goal_run_id"], row["worker_job_id"], project_id),
        )
    ).fetchone()
    if origin is None:
        raise ProjectGraphEvidenceError("revision producer does not match recorded project")
    snapshot = json.loads(row["snapshot_json"])
    if not isinstance(snapshot, dict):
        raise ProjectGraphEvidenceError("invalid revision snapshot")
    raw_files, raw_checks = snapshot.get("files"), snapshot.get("checks")
    if not isinstance(raw_files, list) or not isinstance(raw_checks, list) or len(raw_checks) > 12:
        raise ProjectGraphEvidenceError("invalid revision evidence")
    files = [ProjectFile.model_validate(file) for file in raw_files]
    if project_digest(files) != row["sha256"]:
        raise ProjectGraphEvidenceError("revision digest does not match its file snapshot")
    checks = [ProjectCheck.model_validate(check) for check in raw_checks]
    metadata = dict(row)
    del metadata["snapshot_json"]
    return GraphRevision(
        **metadata,
        files=[
            GraphFile(
                id=f"file:{row['id']}:{hashlib.sha256(file.path.encode()).hexdigest()[:16]}",
                path=file.path,
                sha256=hashlib.sha256(file.content.encode("utf-8")).hexdigest(),
                bytes=len(file.content.encode("utf-8")),
            )
            for file in sorted(files, key=lambda file: file.path)
        ],
        checks=[
            GraphCheck(
                id=f"check:{row['id']}:{index}",
                index=index,
                command=check.command if check.command in _PUBLIC_COMMANDS else None,
                status=check.status,
                exit_code=check.exit_code,
                duration_ms=check.duration_ms,
            )
            for index, check in enumerate(checks)
        ],
    )


def _reported_items(value: Any) -> list[str]:
    parsed = json.loads(value)
    if not isinstance(parsed, list) or len(parsed) > 20:
        raise ProjectGraphEvidenceError("invalid evaluator summary items")
    if any(not isinstance(item, str) or not item.strip() or len(item) > 500 for item in parsed):
        raise ProjectGraphEvidenceError("invalid evaluator summary text")
    return [_safe(item, 500) for item in parsed]


async def _read_evaluations(
    db: aiosqlite.Connection,
    goal_id: str,
) -> tuple[list[GraphEvaluation], bool]:
    rows = list(
        await (
            await db.execute(
                """SELECT id,goal_run_id,sequence,status,substr(reason_summary,1,4000) AS reason_summary,
        created_at,json_extract(decision_json,'$.missing_requirements') AS missing_requirements,
        json_extract(decision_json,'$.invalid_results') AS invalid_results
        FROM goal_evaluations WHERE goal_run_id=? ORDER BY sequence DESC LIMIT 6""",
                (goal_id,),
            )
        ).fetchall()
    )
    evaluations = []
    for row in rows[:5]:
        record = dict(row)
        record["reason_summary"] = _safe(record["reason_summary"])
        for field in ("missing_requirements", "invalid_results"):
            record[field] = _reported_items(record[field])
        evaluations.append(GraphEvaluation.model_validate(record))
    return evaluations, len(rows) > 5


async def _read_planning_decisions(
    db: aiosqlite.Connection,
    goal: GoalRecord,
    nodes: list[PlanNode],
) -> tuple[list[GraphPlanningDecision], bool]:
    # Select only explicitly recorded public summaries; legacy events have no summary.
    rows = list(
        await (
            await db.execute(
                """SELECT id AS audit_event_id,event_type,created_at,
        json_extract(payload_json,'$.goal_run_id') AS goal_run_id,
        json_extract(payload_json,'$.planner_source') AS planner_source,
        json_extract(payload_json,'$.rationale_summary') AS rationale_summary,
        json_extract(payload_json,'$.node_ids') AS node_ids,
        json_extract(payload_json,'$.conversation_revision') AS conversation_revision,
        json_extract(payload_json,'$.model_call_id') AS model_call_id
        FROM audit_events WHERE trace_id=? AND task_id=? AND actor_type='control-plane'
        AND actor_id='goal-manager' AND event_type IN ('goal.plan.accepted','goal.replan.accepted')
        AND json_extract(payload_json,'$.goal_run_id')=?
        AND json_type(payload_json,'$.rationale_summary') IS NOT NULL
        ORDER BY id DESC LIMIT 6""",
                (goal.id, goal.root_task_id, goal.id),
            )
        ).fetchall()
    )
    decisions = []
    known_nodes = {node.id for node in nodes}
    for row in rows[:5]:
        record = dict(row)
        record["id"] = f"planning:{row['audit_event_id']}"
        record["node_ids"] = json.loads(row["node_ids"])
        record["rationale_summary"] = _safe(row["rationale_summary"])
        record["model_id"] = None
        if row["model_call_id"] is not None:
            call = await (
                await db.execute(
                    """SELECT model_id FROM goal_model_calls WHERE id=? AND goal_run_id=?
                AND role='planner' AND status='completed' AND provider_source=?
                AND conversation_revision=?""",
                    (
                        row["model_call_id"],
                        goal.id,
                        row["planner_source"],
                        row["conversation_revision"],
                    ),
                )
            ).fetchone()
            if call is None:
                raise ProjectGraphEvidenceError("accepted plan has no matching model call")
            candidate = call["model_id"]
            if isinstance(candidate, str) and re.fullmatch(
                r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,499}", candidate
            ):
                record["model_id"] = candidate
        decision = GraphPlanningDecision.model_validate(record)
        if (
            len(set(decision.node_ids)) != len(decision.node_ids)
            or not set(decision.node_ids) <= known_nodes
        ):
            raise ProjectGraphEvidenceError("accepted plan nodes do not match this goal")
        decisions.append(decision)
    return decisions, len(rows) > 5


class ProjectGraphEvidenceError(ValueError):
    """Stored evidence cannot be projected without inventing or dropping facts."""


async def read_project_graph(db_path: Path, goal_id: str) -> ProjectGraph | None:
    # Do not use GoalManager.get_goal or context.refresh: those reads may write/reconcile.
    async with aiosqlite.connect(db_path.resolve().as_uri() + "?mode=ro", uri=True) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("PRAGMA query_only=ON")
        await db.execute("BEGIN")
        try:
            row = await (
                await db.execute(
                    """SELECT g.*,l.project_id FROM goal_runs g LEFT JOIN goal_project_links l
                ON l.goal_run_id=g.id WHERE g.id=?""",
                    (goal_id,),
                )
            ).fetchone()
            if row is None:
                return None
            goal = _goal(row)
            nodes = await _read_nodes(db, goal_id)
            dependencies = await _read_dependencies(db, goal_id, nodes)
            revision = await _read_revision(db, row["project_id"])
            evaluations, more = await _read_evaluations(db, goal_id)
            planning, planning_more = await _read_planning_decisions(db, goal, nodes)
            return ProjectGraph(
                observed_at=datetime.now(UTC).isoformat(),
                goal=goal,
                conversation_revision=row["conversation_revision"],
                project_id=row["project_id"],
                criteria=[
                    GraphCriterion(id=f"criterion:{goal_id}:{index}", index=index, text=text)
                    for index, text in enumerate(goal.completion_criteria)
                ],
                nodes=nodes,
                dependencies=dependencies,
                latest_revision=revision,
                evaluations=evaluations,
                evaluations_has_more=more,
                planning_decisions=planning,
                planning_decisions_has_more=planning_more,
                coverage=GraphCoverage(
                    planner_rationale="recorded" if planning else "not_recorded"
                ),
            )
        except (ValueError, TypeError, KeyError) as exc:
            raise ProjectGraphEvidenceError("stored project graph evidence is invalid") from exc
        finally:
            await db.rollback()
