from __future__ import annotations

import json
import math
import re
import unicodedata
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from itertools import islice
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

import aiosqlite

from app.models import FeedbackCorrection
from app.services.audit_log import append_audit_event

SECRET_PATTERN = re.compile(r"(?i)(bearer\s+\S+|api[_-]?key\s*[:=]\s*\S+|token\s*[:=]\s*\S+)")
PATH_PATTERN = re.compile(r"/(?:Users|home|root|private|etc)/[^\s\"']+")
_PRIVATE_KEY_BLOCK = re.compile(
    r"-----BEGIN(?P<label>(?: [A-Z0-9]+)* PRIVATE KEY)-----.*?"
    r"(?:-----END(?P=label)-----|\Z)",
    re.IGNORECASE | re.DOTALL,
)
_CREDENTIAL_URL = re.compile(r"(?i)\b(?:redis|rediss|https?)://[^/@\s:]+:[^/@\s]+@")
_ADDITIONAL_SECRET = re.compile(
    r"(?i)\b(?:authorization|auth|password|passwd|secret|private[_ -]?key|"
    r"access[_ -]?key|session[_ -]?id|api[_ -]?key|token|grant|lease[_ -]?token)"
    r"\s*[:=]\s*(?:(?:bearer|basic)\s+)?[^\s,;]{4,}|"
    r"\b[A-Za-z0-9_-]{12,}\.[A-Za-z0-9_-]{12,}\.[A-Za-z0-9_-]{8,}\b"
)
_COMMON_SECRET_PREFIX = re.compile(
    r"(?i)\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|"
    r"sk-[A-Za-z0-9_-]{16,}|xox[baprs]-[A-Za-z0-9-]{16,}|AKIA[A-Z0-9]{16})\b"
)
_WINDOWS_PATH = re.compile(r"(?i)\b[A-Z]:\\(?:Users|Windows|ProgramData)\\[^\s\"']+")
_PROTECTED_RELATIVE_PATH = re.compile(
    r"(?i)(?:^|[\s\"'])(?:\.env(?:\.[^\s/\"']+)?|id_rsa|id_ed25519|"
    r"\.git/(?:config|credentials)|\.npmrc|\.pypirc)(?=$|[\s\"'])"
)
_SENSITIVE_KEY_PARTS = frozenset(
    {
        "accesskey",
        "address",
        "apikey",
        "argumentsjson",
        "auth",
        "authorization",
        "bearer",
        "claimtoken",
        "cookie",
        "contact",
        "coordinate",
        "credential",
        "email",
        "emailbody",
        "grant",
        "grantdigest",
        "grantid",
        "leasetoken",
        "nativepayload",
        "nativeresult",
        "password",
        "phone",
        "phonenumber",
        "privatekey",
        "resultjson",
        "secret",
        "sessionid",
        "latitude",
        "longitude",
        "token",
        "tokenhash",
        "smsbody",
    }
)
_MAX_DATASET_DEPTH = 8
_MAX_DATASET_ITEMS = 100
_MAX_DATASET_TEXT_CHARS = 4_000
_MAX_EXPORT_LINE_BYTES = 262_144
_HIGH_CONFIDENCE_SCORE = 4.0

type SafeDatasetValue = (
    None | bool | int | float | str | list["SafeDatasetValue"] | dict[str, "SafeDatasetValue"]
)
GoalDatasetType = Literal["planner", "evaluator", "synthesis", "routing"]


def redact_dataset_text(value: str | None) -> str | None:
    if value is None:
        return None
    # Remove whole blocks before other patterns can consume their BEGIN marker.
    # Incomplete PEM input is secret-bearing through the end of the supplied text.
    redacted = _PRIVATE_KEY_BLOCK.sub("<redacted-secret>", value)
    redacted = SECRET_PATTERN.sub("<redacted-secret>", redacted)
    redacted = _ADDITIONAL_SECRET.sub("<redacted-secret>", redacted)
    redacted = _COMMON_SECRET_PREFIX.sub("<redacted-secret>", redacted)
    redacted = _CREDENTIAL_URL.sub("<redacted-credential-url>", redacted)
    redacted = PATH_PATTERN.sub("<protected-path>", redacted)
    redacted = _WINDOWS_PATH.sub("<protected-path>", redacted)
    return _PROTECTED_RELATIVE_PATH.sub(" <protected-path>", redacted).strip()


def _normalized_dataset_key(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value).casefold()
    normalized = "".join(
        character
        for character in normalized
        if unicodedata.category(character) not in {"Cf", "Mn", "Me"}
    )
    return re.sub(r"[^a-z0-9]+", "", normalized)


def _sensitive_dataset_key(value: str) -> bool:
    normalized = _normalized_dataset_key(value)
    return not value.isascii() or any(part in normalized for part in _SENSITIVE_KEY_PARTS)


def _safe_dataset_text(value: str, *, max_chars: int = _MAX_DATASET_TEXT_CHARS) -> str:
    redacted = redact_dataset_text(value) or ""
    redacted = "".join(max(character, " ") for character in redacted)
    redacted = " ".join(redacted.split())
    if len(redacted) <= max_chars:
        return redacted
    if max_chars <= 1:
        return "…"[:max_chars]
    return redacted[: max_chars - 1].rstrip() + "…"


def sanitize_dataset_value(
    value: object,
    *,
    max_text_chars: int = _MAX_DATASET_TEXT_CHARS,
    _depth: int = 0,
) -> SafeDatasetValue:
    """Return bounded JSON data with secret-bearing fields removed recursively."""

    if _depth >= _MAX_DATASET_DEPTH:
        return "<truncated-depth>"
    if value is None or type(value) is bool or type(value) is int:
        return value
    if type(value) is float:
        return value if math.isfinite(value) else None
    if isinstance(value, str):
        return _safe_dataset_text(value, max_chars=max_text_chars)
    if isinstance(value, Mapping):
        result: dict[str, SafeDatasetValue] = {}
        safe_keys = sorted(
            key
            for key in value
            if isinstance(key, str) and 0 < len(key) <= 128 and not _sensitive_dataset_key(key)
        )[:_MAX_DATASET_ITEMS]
        for key in safe_keys:
            result[key] = sanitize_dataset_value(
                value[key],
                max_text_chars=max_text_chars,
                _depth=_depth + 1,
            )
        return result
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [
            sanitize_dataset_value(
                item,
                max_text_chars=max_text_chars,
                _depth=_depth + 1,
            )
            for item in islice(value, _MAX_DATASET_ITEMS)
        ]
    return f"<{type(value).__name__}-omitted>"


def _decode_safe_json(raw: object) -> SafeDatasetValue:
    if raw is None:
        return None
    try:
        decoded = json.loads(str(raw))
    except (json.JSONDecodeError, TypeError, ValueError):
        return "<invalid-json-omitted>"
    return sanitize_dataset_value(decoded)


class FeedbackDatasetService:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path

    async def add_correction(self, request: FeedbackCorrection, *, actor_id: str) -> dict[str, Any]:
        correction_id = f"cor_{uuid4().hex}"
        example_id = f"eval_{uuid4().hex}"
        now = datetime.now(UTC).isoformat()
        behavior = redact_dataset_text(request.corrected_behavior) or ""
        notes = redact_dataset_text(request.notes)
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            task = await (
                await db.execute("SELECT id FROM tasks WHERE id=?", (request.task_id,))
            ).fetchone()
            if task is None:
                await db.rollback()
                raise ValueError("task not found")
            await db.execute(
                "INSERT INTO corrections(id,task_id,corrected_behavior,notes,created_at) VALUES(?,?,?,?,?)",
                (correction_id, request.task_id, behavior, notes, now),
            )
            await db.execute(
                "INSERT INTO eval_examples(id,task_id,payload_json,created_at) VALUES(?,?,?,?)",
                (
                    example_id,
                    request.task_id,
                    json.dumps({"correction_id": correction_id, "reviewed": True}),
                    now,
                ),
            )
            await append_audit_event(
                db,
                "feedback.corrected",
                {"correction_id": correction_id},
                actor_type="device",
                actor_id=actor_id,
                task_id=request.task_id,
                trace_id=request.task_id,
                created_at=now,
            )
            await db.commit()
        return {
            "id": correction_id,
            "task_id": request.task_id,
            "corrected_behavior": behavior,
            "notes": notes,
            "created_at": now,
        }

    async def export_jsonl(self) -> str:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            rows = await (
                await db.execute(
                    """SELECT t.id,t.input,t.status,t.error_json,c.corrected_behavior,c.notes,
                    (SELECT payload_json FROM audit_events WHERE task_id=t.id AND event_type='orchestrator.proposed' ORDER BY id DESC LIMIT 1) proposal,
                    (SELECT json_object('type',type,'label',label,'score',score,'notes',notes)
                     FROM feedback_events WHERE task_id=t.id ORDER BY created_at DESC LIMIT 1) feedback
                    FROM tasks t JOIN corrections c ON c.task_id=t.id ORDER BY c.created_at"""
                )
            ).fetchall()
        lines: list[str] = []
        for row in rows:
            feedback = _decode_safe_json(row["feedback"]) if row["feedback"] else {}
            proposal = _decode_safe_json(row["proposal"]) if row["proposal"] else None
            lines.append(
                json.dumps(
                    {
                        "task_id": row["id"],
                        "task_input": sanitize_dataset_value(row["input"]),
                        "planner_proposal": proposal,
                        "tool_outcome": {
                            "status": row["status"],
                            "error": sanitize_dataset_value(row["error_json"]),
                        },
                        "user_feedback": feedback,
                        "corrected_behavior": sanitize_dataset_value(row["corrected_behavior"]),
                        "correction_notes": sanitize_dataset_value(row["notes"]),
                    },
                    sort_keys=True,
                )
            )
        return "\n".join(lines) + ("\n" if lines else "")

    async def export_goal_jsonl(
        self,
        dataset_type: GoalDatasetType,
        *,
        minimum_score: float = _HIGH_CONFIDENCE_SCORE,
        successful_only: bool = True,
        reviewed_only: bool = True,
        planner_source: str | None = None,
    ) -> str:
        """Export bounded goal trajectories without promoting unreviewed data to targets."""

        if dataset_type not in {"planner", "evaluator", "synthesis", "routing"}:
            raise ValueError("dataset_type must be planner, evaluator, synthesis, or routing")
        if (
            isinstance(minimum_score, bool)
            or not isinstance(minimum_score, (int, float))
            or not math.isfinite(float(minimum_score))
            or not 0 <= float(minimum_score) <= 5
        ):
            raise ValueError("minimum_score must be between 0 and 5")
        if type(successful_only) is not bool or type(reviewed_only) is not bool:
            raise ValueError("dataset filters must be booleans")
        if planner_source is not None and planner_source not in {
            "iphone_local",
            "ubuntu_local",
            "manual",
            "test",
        }:
            raise ValueError("planner_source is invalid")

        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            rows = await (
                await db.execute(
                    """
                    SELECT gf.id AS feedback_id,gf.score,gf.note,
                           gf.corrected_final_answer,gf.corrected_plan_summary,gf.reviewed,
                           gf.created_at AS feedback_created_at,
                           g.*,t.input AS root_input,r.result_json AS goal_result_json
                    FROM goal_feedback AS gf
                    JOIN goal_runs AS g ON g.id=gf.goal_run_id
                    JOIN tasks AS t ON t.id=g.root_task_id
                    LEFT JOIN goal_results AS r ON r.goal_run_id=g.id
                    WHERE gf.score>=?
                      AND (?=0 OR g.status='completed')
                      AND (?=0 OR gf.reviewed=1)
                      AND (? IS NULL OR g.planner_source=?)
                    ORDER BY gf.created_at ASC,gf.id ASC
                    """,
                    (
                        float(minimum_score),
                        int(successful_only),
                        int(reviewed_only),
                        planner_source,
                        planner_source,
                    ),
                )
            ).fetchall()
            records = [
                await _goal_dataset_record(db, row, dataset_type=dataset_type) for row in rows
            ]

        lines: list[str] = []
        for record in records:
            encoded = json.dumps(
                record,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            if len(encoded.encode("utf-8")) > _MAX_EXPORT_LINE_BYTES:
                raise RuntimeError("bounded goal dataset record exceeded its line limit")
            lines.append(encoded)
        return "\n".join(lines) + ("\n" if lines else "")


async def _goal_dataset_record(
    db: aiosqlite.Connection,
    row: aiosqlite.Row,
    *,
    dataset_type: GoalDatasetType,
) -> dict[str, object]:
    goal_run_id = str(row["id"])
    nodes = await _goal_nodes(db, goal_run_id)
    trajectory: object
    if dataset_type == "planner":
        trajectory = {
            "completion_criteria": _decode_safe_json(row["completion_criteria_json"]),
            "nodes": [
                {
                    key: node[key]
                    for key in (
                        "node_id",
                        "node_type",
                        "title",
                        "objective",
                        "required_skill",
                        "dependencies",
                        "optional_dependencies",
                        "status",
                        "priority",
                        "expected_output",
                    )
                }
                for node in nodes
            ],
            "model_calls": await _goal_model_calls(db, goal_run_id, roles=("planner",)),
        }
    elif dataset_type == "evaluator":
        trajectory = {
            "evaluations": await _goal_evaluations(db, goal_run_id),
            "node_outcomes": [_node_outcome(node) for node in nodes],
            "model_calls": await _goal_model_calls(db, goal_run_id, roles=("evaluator",)),
        }
    elif dataset_type == "synthesis":
        trajectory = {
            "node_outcomes": [_node_outcome(node) for node in nodes],
            "goal_result": _decode_safe_json(row["goal_result_json"]),
            "model_calls": await _goal_model_calls(
                db,
                goal_run_id,
                roles=("summarizer", "synthesizer"),
            ),
        }
    else:
        trajectory = {
            "assignments": [
                {
                    key: node[key]
                    for key in (
                        "node_id",
                        "required_skill",
                        "status",
                        "assigned_agent_id",
                        "worker_job_id",
                    )
                }
                for node in nodes
                if node["node_type"] == "worker"
            ],
            "scheduler_decisions": await _goal_scheduler_decisions(db, goal_run_id),
        }

    reviewed = bool(row["reviewed"])
    score = float(row["score"])
    corrected_target: object = None
    if dataset_type in {"planner", "evaluator"}:
        corrected_target = row["corrected_plan_summary"]
    elif dataset_type == "synthesis":
        corrected_target = row["corrected_final_answer"]
    safe_target = sanitize_dataset_value(corrected_target, max_text_chars=8_000)
    target_text = safe_target if isinstance(safe_target, str) and safe_target else None
    fine_tune_candidate = bool(
        reviewed
        and score >= _HIGH_CONFIDENCE_SCORE
        and str(row["status"]) == "completed"
        and target_text is not None
    )
    record: dict[str, object] = {
        "schema_version": "1.0",
        "dataset_type": dataset_type,
        "goal_run_id": goal_run_id,
        "objective": sanitize_dataset_value(row["objective"], max_text_chars=4_000),
        "root_input": sanitize_dataset_value(row["root_input"], max_text_chars=4_000),
        "planner_source": str(row["planner_source"]),
        "autonomy_profile": str(row["autonomy_profile"]),
        "outcome": {
            "status": str(row["status"]),
            "step_count": int(row["step_count"]),
            "replan_count": int(row["replan_count"]),
            "model_call_count": int(row["model_call_count"]),
            "failure_reason": sanitize_dataset_value(row["failure_reason"]),
        },
        "trajectory": sanitize_dataset_value(trajectory, max_text_chars=1_200),
        "review": {
            "feedback_id": str(row["feedback_id"]),
            "score": score,
            "reviewed": reviewed,
            "note": sanitize_dataset_value(row["note"], max_text_chars=1_200),
            "created_at": str(row["feedback_created_at"]),
        },
        "fine_tune_candidate": fine_tune_candidate,
        "provenance": {
            "goal_run_id": goal_run_id,
            "root_task_id": str(row["root_task_id"]),
            "feedback_id": str(row["feedback_id"]),
            "plan_fingerprint": str(row["plan_fingerprint"] or ""),
            "evaluation_fingerprint": str(row["evaluation_fingerprint"] or ""),
        },
    }
    if fine_tune_candidate and target_text is not None:
        record["training_target"] = target_text
    return record


async def _goal_nodes(db: aiosqlite.Connection, goal_run_id: str) -> list[dict[str, object]]:
    rows = await (
        await db.execute(
            """
            SELECT id,node_type,title,objective,required_skill,status,priority,
                   expected_output,result_summary,error_summary,assigned_agent_id,worker_job_id
            FROM plan_nodes WHERE goal_run_id=?
            ORDER BY priority DESC,created_at ASC,id ASC
            """,
            (goal_run_id,),
        )
    ).fetchall()
    edges = await (
        await db.execute(
            """
            SELECT from_node_id,to_node_id,dependency_type FROM plan_edges
            WHERE goal_run_id=? ORDER BY to_node_id ASC,from_node_id ASC
            """,
            (goal_run_id,),
        )
    ).fetchall()
    dependency_ids: dict[tuple[str, str], list[str]] = {}
    for edge in edges:
        key = (str(edge["to_node_id"]), str(edge["dependency_type"]))
        dependency_ids.setdefault(key, []).append(str(edge["from_node_id"]))
    return [
        {
            "node_id": str(item["id"]),
            "node_type": str(item["node_type"]),
            "title": sanitize_dataset_value(item["title"], max_text_chars=500),
            "objective": sanitize_dataset_value(item["objective"], max_text_chars=1_200),
            "required_skill": str(item["required_skill"] or ""),
            "dependencies": dependency_ids.get((str(item["id"]), "hard"), []),
            "optional_dependencies": dependency_ids.get((str(item["id"]), "optional"), []),
            "status": str(item["status"]),
            "priority": int(item["priority"]),
            "expected_output": sanitize_dataset_value(
                item["expected_output"], max_text_chars=1_200
            ),
            "result_summary": sanitize_dataset_value(item["result_summary"], max_text_chars=1_200),
            "error_summary": sanitize_dataset_value(item["error_summary"], max_text_chars=500),
            "assigned_agent_id": str(item["assigned_agent_id"] or ""),
            "worker_job_id": str(item["worker_job_id"] or ""),
        }
        for item in rows
    ]


def _node_outcome(node: Mapping[str, object]) -> dict[str, object]:
    return {
        key: node[key]
        for key in (
            "node_id",
            "node_type",
            "required_skill",
            "status",
            "result_summary",
            "error_summary",
        )
    }


async def _goal_evaluations(db: aiosqlite.Connection, goal_run_id: str) -> list[dict[str, object]]:
    rows = await (
        await db.execute(
            """
            SELECT id,sequence,status,reason_summary,decision_json,state_fingerprint,
                   decision_fingerprint,created_at
            FROM goal_evaluations WHERE goal_run_id=? ORDER BY sequence ASC,id ASC
            """,
            (goal_run_id,),
        )
    ).fetchall()
    return [
        {
            "evaluation_id": str(item["id"]),
            "sequence": int(item["sequence"]),
            "status": str(item["status"]),
            "reason_summary": sanitize_dataset_value(item["reason_summary"], max_text_chars=1_200),
            "decision": _decode_safe_json(item["decision_json"]),
            "state_fingerprint": str(item["state_fingerprint"]),
            "decision_fingerprint": str(item["decision_fingerprint"]),
            "created_at": str(item["created_at"]),
        }
        for item in rows
    ]


async def _goal_model_calls(
    db: aiosqlite.Connection,
    goal_run_id: str,
    *,
    roles: tuple[str, ...],
) -> list[dict[str, object]]:
    allowed_roles = {"planner", "evaluator", "summarizer", "synthesizer"}
    if not roles or len(roles) > 4 or not set(roles).issubset(allowed_roles):
        raise ValueError("model-call roles are invalid")
    padded_roles = roles + ("__unused__",) * (4 - len(roles))
    rows = await (
        await db.execute(
            """
            SELECT id,node_id,role,provider_source,model_id,context_id,input_digest,
                   output_digest,status,latency_ms,error_category,created_at,completed_at
            FROM goal_model_calls
            WHERE goal_run_id=? AND role IN (?,?,?,?)
            ORDER BY created_at ASC,id ASC
            """,
            (goal_run_id, *padded_roles),
        )
    ).fetchall()
    records: list[dict[str, object]] = []
    for item in rows:
        sanitized = sanitize_dataset_value(dict(item), max_text_chars=500)
        if not isinstance(sanitized, dict):  # pragma: no cover - dict shape is preserved
            raise TypeError("model-call dataset projection failed")
        records.append(dict(sanitized))
    return records


async def _goal_scheduler_decisions(
    db: aiosqlite.Connection, goal_run_id: str
) -> list[dict[str, object]]:
    rows = await (
        await db.execute(
            """
            SELECT s.id,s.job_id,s.selected_agent_id,s.candidates_json,
                   s.scoring_json,s.created_at
            FROM scheduler_decisions AS s
            JOIN plan_nodes AS n ON n.worker_job_id=s.job_id
            WHERE n.goal_run_id=? ORDER BY s.created_at ASC,s.id ASC
            """,
            (goal_run_id,),
        )
    ).fetchall()
    records: list[dict[str, object]] = []
    for item in rows:
        records.append(
            {
                "decision_id": str(item["id"]),
                "job_id": str(item["job_id"]),
                "selected_agent_id": str(item["selected_agent_id"]),
                "candidates": _decode_safe_json(item["candidates_json"]),
                "scoring": _decode_safe_json(item["scoring_json"]),
                "created_at": str(item["created_at"]),
            }
        )
    return records
