"""Pure, paged projection of existing SQLite evidence, without reconciliation.

Ordering is by immutable record creation time and identity, newest first. A cursor
freezes source rowid high-water marks, so new (even backdated) records cannot move
between pages. Mutable statuses are read at page time, not reconstructed history.
Reconnect starts a new first page; consumers replace existing records by stable id.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, cast

import aiosqlite
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.services.activity_contracts import (
    ActivityDetail,
    ActivityItem,
    ActivityKind,
    ActivityPage,
    ActivityScope,
    ActivityStatus,
)
from app.services.feedback_dataset import redact_dataset_text

_TABLES = ("goal_model_calls", "agent_jobs", "tool_calls", "project_revisions")
# Only these fixed application tables participate; no identifier comes from a cursor.
_TERMINAL = {"completed", "failed", "cancelled", "budget_exhausted"}
_METADATA = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$")
_MODEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}$")
_COMMANDS = (
    ["python", "-m", "compileall", "-q", "."],
    ["python", "-m", "pytest", "-q"],
    ["npm", "run", "build"],
    ["node", "--test"],
    ["python", "-m", "pip", "install", "-r", "requirements.txt"],
    ["npm", "install"],
    ["npm", "ci"],
)
_TITLES = {
    "model_call": "Appel modèle",
    "worker_job": "Travail d’agent",
    "tool_call": "Appel d’outil",
    "project_revision": "Révision du projet enregistrée",
    "project_check": "Reçu de vérification du projet",
}


class ActivityCursorError(ValueError):
    pass


class ActivityCursorStale(ActivityCursorError):
    pass


class ActivityEvidenceError(ValueError):
    pass


class _Fence(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    rowid: int = Field(ge=0, le=2**63 - 1)
    anchor: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")


class _Cursor(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    version: Literal[1] = 1
    scope: str = Field(pattern=r"^[a-f0-9]{64}$")
    fences: dict[str, _Fence]
    recorded_at: str = Field(min_length=1, max_length=64)
    id: str = Field(min_length=1, max_length=260, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")


def _decode_cursor(value: str) -> _Cursor:
    try:
        if not value or len(value) > 4096 or not re.fullmatch(r"[A-Za-z0-9_-]+", value):
            raise ValueError
        raw = base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)
        result = _Cursor.model_validate_json(raw)
        if set(result.fences) != set(_TABLES):
            raise ValueError
        _timestamp(result.recorded_at)
        return result
    except (ValueError, TypeError) as exc:
        raise ActivityCursorError("invalid activity cursor") from exc


def _encode_cursor(value: _Cursor) -> str:
    return base64.urlsafe_b64encode(value.model_dump_json().encode()).decode().rstrip("=")


def _anchor(row: aiosqlite.Row) -> str:
    # Global high-water rows may belong to other tasks. Never embed their IDs
    # in a decodable public cursor, even though queries remain scope-bound.
    return hashlib.sha256(json.dumps([row["id"], row["created_at"]]).encode()).hexdigest()


def _timestamp(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > 64:
        raise ActivityEvidenceError("invalid activity timestamp")
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            raise ValueError
    except ValueError as exc:
        raise ActivityEvidenceError("invalid activity timestamp") from exc
    return value


def _metadata(value: Any, *, model: bool = False) -> str | None:
    if not isinstance(value, str) or not (_MODEL if model else _METADATA).fullmatch(value):
        return None
    if model and (value.count("/") > 1 or any(part in {".", ".."} for part in value.split("/"))):
        return None
    if redact_dataset_text(value) != value:
        return None
    if any(word in value.casefold() for word in ("password", "secret", "credential", "bearer")):
        return None
    return value


async def _scope(
    db: aiosqlite.Connection, scope_type: Literal["task", "goal"], scope_id: str
) -> tuple[ActivityScope, dict[str, Any]] | None:
    if not _METADATA.fullmatch(scope_id):
        return None
    if scope_type == "goal":
        row = await (
            await db.execute(
                "SELECT id,root_task_id,status,created_at FROM goal_runs WHERE id=?", (scope_id,)
            )
        ).fetchone()
        if row is None:
            return None
        goal_id, root_id = row["id"], row["root_task_id"]
        goal_status = row["status"]
        include_children = True
    else:
        row = await (
            await db.execute("SELECT id,status,created_at FROM tasks WHERE id=?", (scope_id,))
        ).fetchone()
        if row is None:
            return None
        goal = list(
            await (
                await db.execute(
                    """SELECT g.id,g.root_task_id,g.status FROM goal_runs g WHERE g.root_task_id=?
            OR EXISTS(SELECT 1 FROM plan_nodes n WHERE n.goal_run_id=g.id AND n.task_id=?)
            OR EXISTS(SELECT 1 FROM project_revisions r WHERE r.goal_run_id=g.id AND r.apply_task_id=?)
            OR EXISTS(SELECT 1 FROM goal_code_proposals p WHERE p.goal_run_id=g.id AND p.apply_task_id=?)
            ORDER BY g.id LIMIT 2""",
                    (scope_id,) * 4,
                )
            ).fetchall()
        )
        if len(goal) > 1:
            raise ActivityEvidenceError("ambiguous activity scope")
        goal_id = goal[0]["id"] if goal else None
        root_id = goal[0]["root_task_id"] if goal else None
        goal_status = goal[0]["status"] if goal else None
        include_children = root_id == scope_id
    scope = ActivityScope(type=scope_type, id=scope_id, goal_run_id=goal_id, root_task_id=root_id)
    fingerprint = hashlib.sha256(
        json.dumps([scope.model_dump(), row["created_at"]], sort_keys=True).encode()
    ).hexdigest()
    return scope, {
        "goal_id": goal_id,
        "task_id": scope_id if scope_type == "task" else root_id,
        "children": int(include_children),
        "scope_status": goal_status if goal_status in _TERMINAL else row["status"],
        "fingerprint": fingerprint,
    }


_SCOPE_CTE = """WITH scoped_tasks AS (
    SELECT :task_id AS id
    UNION SELECT task_id FROM plan_nodes WHERE :children=1 AND goal_run_id=:goal_id
    UNION SELECT apply_task_id FROM project_revisions WHERE :children=1 AND goal_run_id=:goal_id
    UNION SELECT apply_task_id FROM goal_code_proposals WHERE :children=1 AND goal_run_id=:goal_id
) """

# SQL projects only known metadata. Raw prompts, arguments, errors, result content,
# file contents and check logs are never fetched into the public projection.
_QUERIES = {
    "goal_model_calls": """SELECT 'model_call:'||c.id AS activity_id,
        'model_call' AS kind,c.created_at,c.status,c.created_at AS started_at,c.completed_at,
        c.latency_ms AS duration_ms,c.model_id,c.role,c.lease_expires_at,
        c.goal_run_id,COALESCE(n.task_id,g.root_task_id) AS task_id,n.id AS node_id,
        g.status AS goal_status FROM goal_model_calls c JOIN goal_runs g ON g.id=c.goal_run_id
        LEFT JOIN plan_nodes n ON n.id=c.node_id AND n.goal_run_id=c.goal_run_id
        WHERE c.rowid<=:goal_model_calls AND
        ((:children=1 AND c.goal_run_id=:goal_id) OR n.task_id=:task_id)""",
    "agent_jobs": """SELECT 'worker_job:'||j.id AS activity_id,
        'worker_job' AS kind,j.created_at,j.status,j.claimed_at AS started_at,j.completed_at,
        j.lease_expires_at,j.claimed_by AS agent_id,j.required_skill AS tool_name,
        :goal_id AS goal_run_id,j.task_id,n.id AS node_id,t.status AS task_status
        FROM agent_jobs j JOIN tasks t ON t.id=j.task_id
        JOIN scoped_tasks s ON s.id=t.id
        LEFT JOIN plan_nodes n ON n.task_id=t.id AND n.goal_run_id=:goal_id
        WHERE j.rowid<=:agent_jobs""",
    "tool_calls": """SELECT 'tool_call:'||c.id AS activity_id,
        'tool_call' AS kind,c.created_at,c.status,c.tool_name,:goal_id AS goal_run_id,
        c.task_id,n.id AS node_id,t.status AS task_status
        FROM tool_calls c JOIN tasks t ON t.id=c.task_id
        JOIN scoped_tasks s ON s.id=t.id
        LEFT JOIN plan_nodes n ON n.task_id=t.id AND n.goal_run_id=:goal_id
        WHERE c.rowid<=:tool_calls""",
    "project_revisions": """SELECT 'project_revision:'||r.id AS activity_id,
        'project_revision' AS kind,r.created_at,'recorded' AS status,r.id AS revision_id,
        r.goal_run_id,n.task_id,n.id AS node_id,
        CASE WHEN json_valid(r.snapshot_json) THEN json_array_length(r.snapshot_json,'$.files')
        END AS file_count FROM project_revisions r
        JOIN plan_nodes n ON n.id=r.node_id AND n.goal_run_id=r.goal_run_id
        WHERE r.rowid<=:project_revisions AND ((:children=1 AND r.goal_run_id=:goal_id)
        OR n.task_id=:task_id OR r.apply_task_id=:task_id)""",
    "project_checks": """SELECT 'project_check:'||r.id||':'||printf('%02d',x.key) AS activity_id,
        'project_check' AS kind,r.created_at,r.id AS revision_id,r.goal_run_id,n.task_id,
        n.id AS node_id,x.key AS check_index,json_extract(x.value,'$.status') AS status,
        json_extract(x.value,'$.duration_ms') AS duration_ms,
        json_extract(x.value,'$.exit_code') AS exit_code,
        CASE WHEN length(json_extract(x.value,'$.command'))<=2048
        THEN json_extract(x.value,'$.command') END AS command_json
        FROM project_revisions r JOIN plan_nodes n ON n.id=r.node_id AND n.goal_run_id=r.goal_run_id,
        json_each(CASE WHEN json_valid(r.snapshot_json) THEN r.snapshot_json ELSE '{}' END,'$.checks') x
        WHERE r.rowid<=:project_revisions AND x.type='object' AND typeof(x.key)='integer'
        AND x.key BETWEEN 0 AND 11 AND ((:children=1 AND r.goal_run_id=:goal_id)
        OR n.task_id=:task_id OR r.apply_task_id=:task_id)""",
}


def _item(row: dict[str, Any], scope_status: str, now: datetime) -> ActivityItem:
    kind = cast(ActivityKind, row["kind"])
    raw_status = str(row.get("status") or "recorded")
    status = cast(
        ActivityStatus,
        {
            "started": "running",
            "claimed": "running",
            "passed": "completed",
            "denied": "cancelled",
            "waiting_permission": "waiting",
            "waiting_capability": "waiting",
        }.get(raw_status, raw_status),
    )
    if status not in {
        "queued",
        "running",
        "waiting",
        "completed",
        "failed",
        "cancelled",
        "skipped",
    }:
        status = "recorded"
    title = _TITLES[kind]
    # A cancelled/finished scope or expired lease cannot prove work is still running.
    # Keep the record without inventing an unrecorded cancellation/completion event.
    if status in {"running", "queued", "waiting"} and (
        scope_status in _TERMINAL
        or row.get("task_status") in _TERMINAL
        or row.get("goal_status") in _TERMINAL
        or (
            row.get("lease_expires_at") is not None
            and datetime.fromisoformat(str(_timestamp(row["lease_expires_at"]))) <= now
        )
    ):
        status, title = "recorded", title + " — état final non enregistré"
    elif (
        status == "running"
        and kind in {"model_call", "worker_job"}
        and not row.get("lease_expires_at")
    ):
        status, title = "recorded", title + " — exécution non confirmée"
    duration = row.get("duration_ms")
    if duration is not None and (type(duration) is not int or duration < 0):
        raise ActivityEvidenceError("invalid activity duration")
    if kind == "project_check" and (
        (raw_status == "passed" and row.get("exit_code") != 0)
        or (raw_status == "failed" and row.get("exit_code") == 0)
    ):
        raise ActivityEvidenceError("inconsistent project check receipt")
    command = None
    if row.get("command_json"):
        candidate = json.loads(row["command_json"])
        if candidate in _COMMANDS:
            command = candidate
    return ActivityItem(
        id=row["activity_id"],
        kind=kind,
        status=status,
        title=title,
        recorded_at=str(_timestamp(row["created_at"])),
        started_at=_timestamp(row.get("started_at")),
        completed_at=_timestamp(row.get("completed_at")),
        duration_ms=duration,
        goal_run_id=row.get("goal_run_id"),
        task_id=row.get("task_id"),
        node_id=row.get("node_id"),
        agent_id=_metadata(row.get("agent_id")),
        model_id=_metadata(row.get("model_id"), model=True),
        role=row.get("role"),
        tool_name=_metadata(row.get("tool_name")),
        detail=ActivityDetail(
            revision_id=row.get("revision_id"),
            check_index=row.get("check_index"),
            exit_code=row.get("exit_code"),
            file_count=row.get("file_count"),
            command=command,
        ),
    )


async def read_activity(
    db_path: Path,
    scope_type: Literal["task", "goal"],
    scope_id: str,
    *,
    limit: int = 50,
    cursor: str | None = None,
) -> ActivityPage | None:
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ActivityCursorError("invalid activity page limit")
    previous = _decode_cursor(cursor) if cursor is not None else None
    async with aiosqlite.connect(db_path.resolve().as_uri() + "?mode=ro", uri=True) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("PRAGMA query_only=ON")
        await db.execute("BEGIN")
        try:
            selected = await _scope(db, scope_type, scope_id)
            if selected is None:
                return None
            scope, params = selected
            if previous is not None and previous.scope != params["fingerprint"]:
                raise ActivityCursorError("activity cursor belongs to another scope")
            fences = {}
            for table in _TABLES:
                if previous is None:
                    row = await (
                        await db.execute(
                            f"SELECT rowid,id,created_at FROM {table} ORDER BY rowid DESC LIMIT 1"  # nosec B608
                        )
                    ).fetchone()
                    fence = _Fence(
                        rowid=int(row[0]) if row else 0, anchor=_anchor(row) if row else None
                    )
                else:
                    fence = previous.fences[table]
                    if fence.rowid:
                        row = await (
                            await db.execute(
                                f"SELECT id,created_at FROM {table} WHERE rowid=?",  # nosec B608
                                (fence.rowid,),
                            )
                        ).fetchone()
                        if row is None or _anchor(row) != fence.anchor:
                            raise ActivityCursorStale(
                                "activity history changed; refresh first page"
                            )
                fences[table] = fence
                params[table] = fence.rowid
            params.update(
                position_date=previous.recorded_at if previous else None,
                position_id=previous.id if previous else None,
                batch=limit + 1,
            )
            rows: list[dict[str, Any]] = []
            for query in _QUERIES.values():
                # Both fragments are fixed application SQL; all request values are bound.
                query = (
                    _SCOPE_CTE
                    + "SELECT * FROM ("  # nosec B608
                    + query
                    + """
                    ) WHERE (:position_date IS NULL OR created_at<:position_date
                    OR (created_at=:position_date AND activity_id<:position_id))
                    ORDER BY created_at DESC,activity_id DESC LIMIT :batch"""
                )
                rows.extend(dict(row) for row in await (await db.execute(query, params)).fetchall())
            rows.sort(key=lambda row: (row["created_at"], row["activity_id"]), reverse=True)
            more = len(rows) > limit
            selected_rows = rows[:limit]
            now = datetime.now(UTC)
            items = [_item(row, params["scope_status"], now) for row in selected_rows]
            next_cursor = (
                _encode_cursor(
                    _Cursor(
                        scope=params["fingerprint"],
                        fences=fences,
                        recorded_at=selected_rows[-1]["created_at"],
                        id=selected_rows[-1]["activity_id"],
                    )
                )
                if more
                else None
            )
            return ActivityPage(scope=scope, items=items, next_cursor=next_cursor, has_more=more)
        except (ValidationError, json.JSONDecodeError) as exc:
            raise ActivityEvidenceError("activity evidence is invalid") from exc
        finally:
            await db.rollback()
