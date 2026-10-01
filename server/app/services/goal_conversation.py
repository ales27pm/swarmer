from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

import aiosqlite

from app.models import TaskCreate, TaskMode, TaskRecord, TaskStatus
from app.services.audit_log import append_audit_event
from app.services.context_builder import safe_context_text
from app.services.goal_limits import RESUME_RUNTIME_SQL
from app.services.project_identity import (
    MAX_IDENTITY_LINEAGE,
    ProjectIdentityConflict,
    continuation_project_locked,
    has_project_artifact_locked,
    require_idle_lineage_locked,
    seed_legacy_proposal_locked,
)
from app.services.state_service import StateService


class GoalConversationConflict(RuntimeError):
    pass


class GoalConversationService:
    """Private durable input, independent of model and worker request lifetimes."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path

    @staticmethod
    async def create_locked(
        db: aiosqlite.Connection,
        goal_id: str,
        objective: str,
        now: str,
        *,
        history: Sequence[Mapping[str, Any]] = (),
    ) -> None:
        conversation_id = f"gconv_{goal_id}"
        await db.execute(
            "INSERT INTO goal_conversations(id,active_goal_id) VALUES(?,?)",
            (conversation_id, goal_id),
        )
        await db.execute(
            "INSERT INTO goal_conversation_links(goal_run_id,conversation_id) VALUES(?,?)",
            (goal_id, conversation_id),
        )
        # This is a snapshot of bounded chat context, never execution authority.
        # Original chat messages remain available; assistant claims keep their role.
        for message in history[-40:]:
            if message["role"] not in {"user", "assistant", "agent"}:
                continue
            content = safe_context_text(str(message["content"]), max_chars=4_000)
            if not content.strip():
                continue
            await db.execute(
                """INSERT INTO goal_messages(id,conversation_id,goal_run_id,role,content,created_at)
                VALUES(?,?,?,?,?,?)""",
                (
                    f"gmsg_{uuid4().hex}",
                    conversation_id,
                    goal_id,
                    "user" if message["role"] == "user" else "assistant",
                    content,
                    str(message["created_at"]),
                ),
            )
        await db.execute(
            """INSERT INTO goal_messages(id,conversation_id,goal_run_id,role,content,created_at)
            VALUES(?,?,?,'user',?,?)""",
            (f"gmsg_initial_{goal_id}", conversation_id, goal_id, objective, now),
        )

    @staticmethod
    async def assistant_locked(
        db: aiosqlite.Connection, goal_id: str, content: str, *, question: bool, now: str
    ) -> str:
        message_id = f"gmsg_{uuid4().hex}"
        await db.execute(
            """INSERT INTO goal_messages(
            id,conversation_id,goal_run_id,role,content,is_question,created_at)
            SELECT ?,conversation_id,?,'assistant',?,?,? FROM goal_conversation_links
            WHERE goal_run_id=?""",
            (message_id, goal_id, content[:4_000], int(question), now, goal_id),
        )
        return message_id

    async def messages(self, goal_id: str, *, limit: int = 100) -> dict[str, Any]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            link = await (
                await db.execute(
                    """SELECT c.id,c.active_goal_id,p.project_id FROM goal_conversation_links l
                    JOIN goal_conversations c ON c.id=l.conversation_id
                    LEFT JOIN goal_project_links p ON p.goal_run_id=c.active_goal_id
                    WHERE l.goal_run_id=?""",
                    (goal_id,),
                )
            ).fetchone()
            if link is None:
                raise GoalConversationConflict("goal not found")
            rows = await (
                await db.execute(
                    """SELECT id,goal_run_id,role,content,created_at FROM (
                    SELECT rowid AS sequence,* FROM goal_messages WHERE conversation_id=?
                    ORDER BY rowid DESC LIMIT ?) ORDER BY sequence ASC""",
                    (link["id"], max(1, min(limit, 100))),
                )
            ).fetchall()
            question = await (
                await db.execute(
                    """SELECT m.id FROM goal_messages m JOIN goal_runs g ON g.id=m.goal_run_id
                    WHERE m.goal_run_id=? AND m.is_question=1
                    AND m.answered_by_message_id IS NULL
                    AND g.status='waiting_permission' AND g.current_phase='needs_user'
                    ORDER BY m.rowid DESC LIMIT 1""",
                    (link["active_goal_id"],),
                )
            ).fetchone()
        return {
            "messages": [dict(row) for row in rows],
            "active_goal_id": str(link["active_goal_id"]),
            "project_id": str(link["project_id"]) if link["project_id"] else None,
            "pending_question_id": str(question[0]) if question else None,
        }

    async def append(
        self,
        goal_id: str,
        *,
        message: str,
        client_message_id: str,
        reply_to_message_id: str | None,
        actor_id: str,
        planning_mode: Literal["automatic", "iphone_local"] = "automatic",
        internal_checkpoint: bool = False,
        expected_replan_state: tuple[str, int, int] | None = None,
    ) -> str:
        now = datetime.now(UTC).isoformat()
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            link = await (
                await db.execute(
                    """SELECT c.id,c.active_goal_id FROM goal_conversation_links l
                    JOIN goal_conversations c ON c.id=l.conversation_id WHERE l.goal_run_id=?""",
                    (goal_id,),
                )
            ).fetchone()
            if link is None:
                raise GoalConversationConflict("goal not found")
            replay = await (
                await db.execute(
                    """SELECT id,goal_run_id,role,content,reply_to_message_id FROM goal_messages
                    WHERE conversation_id=? AND actor_id=? AND client_message_id=?""",
                    (link["id"], actor_id, client_message_id),
                )
            ).fetchone()
            if replay is not None:
                accepted = await (
                    await db.execute(
                        """SELECT json_extract(payload_json,'$.planning_mode') FROM audit_events
                        WHERE event_type='goal.message.accepted' AND trace_id=?
                        AND json_extract(payload_json,'$.message_id')=? ORDER BY id DESC LIMIT 1""",
                        (replay["goal_run_id"], replay["id"]),
                    )
                ).fetchone()
                original_mode = accepted[0] if accepted and accepted[0] else "automatic"
                if (
                    replay["content"] != message
                    or replay["role"] != ("assistant" if internal_checkpoint else "user")
                    or replay["reply_to_message_id"] != reply_to_message_id
                    or original_mode != planning_mode
                ):
                    raise GoalConversationConflict(
                        "client_message_id is bound to a different reply"
                    )
                return str(replay["goal_run_id"])
            active_id = str(link["active_goal_id"])
            goal = await (
                await db.execute("SELECT * FROM goal_runs WHERE id=?", (active_id,))
            ).fetchone()
            if goal is None:
                raise GoalConversationConflict("active goal not found")
            if expected_replan_state is not None and (
                goal["status"] not in {"running", "waiting_permission"}
                or (
                    active_id,
                    int(goal["conversation_revision"]),
                    int(goal["replan_count"]),
                )
                != expected_replan_state
            ):
                raise GoalConversationConflict("goal changed before deferred replan")
            audit_task_id = str(goal["root_task_id"])
            terminal = goal["status"] in {"completed", "failed", "cancelled", "budget_exhausted"}
            awaiting_local_plan = (
                goal["status"] == "planning"
                and goal["started_at"] is None
                and goal["current_phase"] == "awaiting_local_plan"
            )
            if planning_mode == "iphone_local" and not (terminal or awaiting_local_plan):
                raise GoalConversationConflict(
                    "local continuation requires an existing project with no active work"
                )
            waiting_for_reply = (
                goal["status"] == "waiting_permission" and goal["current_phase"] == "needs_user"
            )
            question = await (
                await db.execute(
                    """SELECT m.id FROM goal_messages m JOIN goal_runs g ON g.id=m.goal_run_id
                    WHERE m.goal_run_id=? AND m.is_question=1
                    AND m.answered_by_message_id IS NULL
                    AND g.status='waiting_permission' AND g.current_phase='needs_user'
                    ORDER BY m.rowid DESC LIMIT 1""",
                    (active_id,),
                )
            ).fetchone()
            if reply_to_message_id is not None and (
                terminal or question is None or question["id"] != reply_to_message_id
            ):
                raise GoalConversationConflict("the clarification question is no longer current")
            try:
                project_id, lineage = await continuation_project_locked(
                    db, active_id, str(link["id"]), now, actor_id=actor_id
                )
                if terminal and len(lineage) >= MAX_IDENTITY_LINEAGE:
                    raise GoalConversationConflict(
                        "project lineage exceeds its reconciliation limit"
                    )
                await seed_legacy_proposal_locked(db, project_id, lineage, now)
                if planning_mode == "iphone_local":
                    await require_idle_lineage_locked(db, lineage)
                    if not await has_project_artifact_locked(db, project_id):
                        raise GoalConversationConflict(
                            "local continuation requires an existing project artifact with no active work"
                        )
                    awaiting_local_plan = True
            except ProjectIdentityConflict as exc:
                raise GoalConversationConflict(str(exc)) from exc
            if terminal:
                active_id = f"goal_{uuid4().hex}"
                root = TaskRecord.new(
                    TaskCreate(
                        input=str(goal["objective"]),
                        mode=TaskMode.AUTONOME
                        if goal["autonomy_profile"] == "autonomous"
                        else TaskMode.NORMAL,
                    ),
                    source=actor_id,
                ).model_copy(update={"status": TaskStatus.PLANNED})
                await StateService._insert_task(db, root)
                audit_task_id = root.id
                await db.execute(
                    """INSERT INTO goal_runs(
                    id,root_task_id,objective,status,autonomy_profile,planner_source,max_steps,
                    max_parallelism,max_replans,max_runtime_seconds,max_model_calls,
                    completion_criteria_json,current_phase,created_at,updated_at,started_at)
                    SELECT ?,?,objective,'planning',autonomy_profile,planner_source,max_steps,
                    max_parallelism,max_replans,max_runtime_seconds,max_model_calls,
                    completion_criteria_json,?,?,?,? FROM goal_runs WHERE id=?""",
                    (
                        active_id,
                        root.id,
                        "awaiting_local_plan" if awaiting_local_plan else "continuation_pending",
                        now,
                        now,
                        None if awaiting_local_plan else now,
                        goal["id"],
                    ),
                )
                if awaiting_local_plan:
                    await db.execute(
                        "UPDATE goal_runs SET planner_source='iphone_local' WHERE id=?",
                        (active_id,),
                    )
                await db.execute(
                    "INSERT INTO goal_conversation_links VALUES(?,?,?)",
                    (active_id, link["id"], goal["id"]),
                )
                await db.execute(
                    "UPDATE goal_conversations SET active_goal_id=? WHERE id=?",
                    (active_id, link["id"]),
                )
                await db.execute(
                    "INSERT INTO goal_project_links(goal_run_id,project_id) VALUES(?,?)",
                    (active_id, project_id),
                )
                await append_audit_event(
                    db,
                    "goal.continued",
                    {"parent_goal_id": goal["id"], "goal_run_id": active_id},
                    actor_type="control-plane" if internal_checkpoint else "device",
                    actor_id=actor_id,
                    task_id=root.id,
                    trace_id=active_id,
                    created_at=now,
                )
            message_id = f"gmsg_{uuid4().hex}"
            await db.execute(
                """INSERT INTO goal_messages(id,conversation_id,goal_run_id,role,content,
                actor_id,client_message_id,reply_to_message_id,created_at)
                VALUES(?,?,?,?,?,?,?,?,?)""",
                (
                    message_id,
                    link["id"],
                    active_id,
                    "assistant" if internal_checkpoint else "user",
                    message,
                    actor_id,
                    client_message_id,
                    reply_to_message_id,
                    now,
                ),
            )
            if waiting_for_reply:
                if question is not None:
                    await db.execute(
                        "UPDATE goal_messages SET answered_by_message_id=? WHERE id=?",
                        (message_id, question["id"]),
                    )
                # Only a fixed application SQL fragment is interpolated; values are bound.
                await db.execute(
                    f"""UPDATE goal_runs SET status='running',current_phase='continuation_pending',
                    {RESUME_RUNTIME_SQL} WHERE id=?""",  # nosec B608
                    (now, active_id),
                )
            await db.execute(
                """UPDATE goal_runs SET conversation_revision=conversation_revision+1,
                pending_message_revision=CASE WHEN ? THEN 0 ELSE conversation_revision+1 END,
                reply_dispatch_credit=?,updated_at=? WHERE id=?""",
                (awaiting_local_plan, 0 if awaiting_local_plan else 1, now, active_id),
            )
            # A model reading an older conversation cannot publish a new plan/evaluation.
            await db.execute(
                """UPDATE goal_model_calls SET status='failed',completed_at=?,
                error_category='conversation_changed' WHERE goal_run_id=? AND status='started'""",
                (now, active_id),
            )
            await append_audit_event(
                db,
                "goal.message.accepted",
                {
                    "goal_run_id": active_id,
                    "message_id": message_id,
                    "planning_mode": planning_mode,
                },
                actor_type="control-plane" if internal_checkpoint else "device",
                actor_id=actor_id,
                task_id=audit_task_id,
                trace_id=active_id,
                created_at=now,
            )
            await db.commit()
        return active_id
