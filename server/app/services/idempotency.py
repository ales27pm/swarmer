from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import aiosqlite
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.models import ChatCreate, FeedbackCreate, MemoryUpdate
from app.services.audit_log import append_audit_event

IDEMPOTENCY_KEY = re.compile(r"^[A-Za-z0-9._:-]{20,200}$")
SafeMutationOperation = Literal[
    "feedback.create",
    "memory.metadata.update",
    "chat.message.create",
]


class SafeMutationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operation: SafeMutationOperation
    resource_id: str | None = Field(default=None, min_length=1, max_length=500)
    payload: dict[str, Any]

    @model_validator(mode="after")
    def validate_operation_payload(self) -> SafeMutationRequest:
        if self.operation == "feedback.create":
            if self.resource_id is not None:
                raise ValueError("feedback creation does not accept resource_id")
            feedback = FeedbackCreate.model_validate(self.payload)
            self.payload = feedback.model_dump(mode="json", exclude_none=True)
            return self
        if self.operation == "memory.metadata.update":
            if self.resource_id is None:
                raise ValueError("memory metadata update requires resource_id")
            memory = MemoryUpdate.model_validate(self.payload)
            if memory.model_fields_set != {"pinned"} or memory.pinned is None:
                raise ValueError("offline memory mutation may only update pinned metadata")
            self.payload = memory.model_dump(mode="json", exclude_none=True)
            return self
        chat = ChatCreate.model_validate(self.payload)
        if chat.start_task:
            raise ValueError("offline chat mutation cannot create a task")
        if (
            self.resource_id is not None
            and chat.conversation_id is not None
            and self.resource_id != chat.conversation_id
        ):
            raise ValueError("chat conversation binding does not match resource_id")
        if self.resource_id is not None:
            chat.conversation_id = self.resource_id
        self.payload = chat.model_dump(mode="json", exclude_none=True)
        return self


class IdempotencyConflict(RuntimeError):
    pass


class IdempotencyService:
    """Atomically applies the small allowlist of replay-safe mobile mutations."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path

    @staticmethod
    def validate_key(value: str) -> str:
        if not IDEMPOTENCY_KEY.fullmatch(value):
            raise IdempotencyConflict("invalid Idempotency-Key")
        return value

    @staticmethod
    def _digest(request: SafeMutationRequest) -> str:
        encoded = json.dumps(
            request.model_dump(mode="json"),
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return f"sha256:{hashlib.sha256(encoded).hexdigest()}"

    async def apply(
        self,
        *,
        actor_id: str,
        idempotency_key: str,
        request: SafeMutationRequest,
    ) -> dict[str, Any]:
        key = self.validate_key(idempotency_key)
        request_digest = self._digest(request)
        now = datetime.now(UTC).isoformat()
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            receipt = await (
                await db.execute(
                    """SELECT operation,request_digest,response_json
                    FROM idempotency_receipts
                    WHERE actor_id=? AND idempotency_key=?""",
                    (actor_id, key),
                )
            ).fetchone()
            if receipt is not None:
                if (
                    str(receipt["operation"]) != request.operation
                    or str(receipt["request_digest"]) != request_digest
                ):
                    await db.rollback()
                    raise IdempotencyConflict(
                        "Idempotency-Key is already bound to a different mutation"
                    )
                response = json.loads(str(receipt["response_json"]))
                await db.rollback()
                if not isinstance(response, dict):
                    raise RuntimeError("stored idempotency response is invalid")
                return {"idempotent_replay": True, "result": response}

            if request.operation == "feedback.create":
                result = await self._create_feedback_locked(
                    db, actor_id=actor_id, key=key, payload=request.payload, now=now
                )
            elif request.operation == "memory.metadata.update":
                if request.resource_id is None:
                    raise RuntimeError("validated mutation lost its resource binding")
                result = await self._update_memory_locked(
                    db,
                    actor_id=actor_id,
                    memory_id=request.resource_id,
                    payload=request.payload,
                    now=now,
                )
            else:
                result = await self._create_chat_message_locked(
                    db,
                    actor_id=actor_id,
                    key=key,
                    resource_id=request.resource_id,
                    payload=request.payload,
                    now=now,
                )
            response_json = json.dumps(
                result,
                allow_nan=False,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            await db.execute(
                """INSERT INTO idempotency_receipts(
                    actor_id,idempotency_key,operation,request_digest,response_json,
                    created_at,completed_at
                ) VALUES(?,?,?,?,?,?,?)""",
                (actor_id, key, request.operation, request_digest, response_json, now, now),
            )
            await db.commit()
        return {"idempotent_replay": False, "result": result}

    @staticmethod
    async def _create_feedback_locked(
        db: aiosqlite.Connection,
        *,
        actor_id: str,
        key: str,
        payload: dict[str, Any],
        now: str,
    ) -> dict[str, Any]:
        request = FeedbackCreate.model_validate(payload)
        if request.task_id is not None:
            task = await (
                await db.execute("SELECT 1 FROM tasks WHERE id=?", (request.task_id,))
            ).fetchone()
            if task is None:
                raise IdempotencyConflict("task not found")
        if request.agent_id is not None:
            agent = await (
                await db.execute("SELECT 1 FROM agents WHERE id=?", (request.agent_id,))
            ).fetchone()
            if agent is None:
                raise IdempotencyConflict("agent not found")
        stable = hashlib.sha256(f"{actor_id}\0{key}".encode()).hexdigest()[:32]
        feedback_id = f"fbk_{stable}"
        await db.execute(
            """INSERT INTO feedback_events(
                id,task_id,agent_id,type,label,score,notes,payload_json,created_at
            ) VALUES(?,?,?,?,?,?,?,?,?)""",
            (
                feedback_id,
                request.task_id,
                request.agent_id,
                request.type,
                request.label,
                request.score,
                request.notes,
                "{}",
                now,
            ),
        )
        if request.agent_id is not None and request.score is not None:
            await db.execute(
                """INSERT INTO agent_scores(agent_id,sample_count,score,updated_at)
                VALUES(?,1,?,?)
                ON CONFLICT(agent_id) DO UPDATE SET
                  score=((agent_scores.score * agent_scores.sample_count) + excluded.score)
                        / (agent_scores.sample_count + 1),
                  sample_count=agent_scores.sample_count + 1,
                  updated_at=excluded.updated_at""",
                (request.agent_id, request.score, now),
            )
        await append_audit_event(
            db,
            "feedback.created",
            {"feedback_id": feedback_id, "score": request.score, "idempotent": True},
            actor_type="device",
            actor_id=actor_id,
            task_id=request.task_id,
            trace_id=request.task_id,
            created_at=now,
        )
        return {
            "id": feedback_id,
            **request.model_dump(mode="json"),
            "payload": {},
            "created_at": now,
        }

    @staticmethod
    async def _update_memory_locked(
        db: aiosqlite.Connection,
        *,
        actor_id: str,
        memory_id: str,
        payload: dict[str, Any],
        now: str,
    ) -> dict[str, Any]:
        request = MemoryUpdate.model_validate(payload)
        cursor = await db.execute(
            "UPDATE memory_items SET pinned=?,updated_at=? WHERE id=?",
            (int(bool(request.pinned)), now, memory_id),
        )
        if cursor.rowcount != 1:
            raise IdempotencyConflict("memory not found")
        await append_audit_event(
            db,
            "memory.updated",
            {"memory_id": memory_id, "fields": ["pinned"], "idempotent": True},
            actor_type="device",
            actor_id=actor_id,
            created_at=now,
        )
        db.row_factory = aiosqlite.Row
        row = await (
            await db.execute("SELECT * FROM memory_items WHERE id=?", (memory_id,))
        ).fetchone()
        if row is None:
            raise RuntimeError("updated memory disappeared")
        value = dict(row)
        value["pinned"] = bool(value["pinned"])
        raw_metadata = value.pop("metadata_json", None)
        value["metadata"] = json.loads(raw_metadata) if raw_metadata else None
        return value

    @staticmethod
    async def _create_chat_message_locked(
        db: aiosqlite.Connection,
        *,
        actor_id: str,
        key: str,
        resource_id: str | None,
        payload: dict[str, Any],
        now: str,
    ) -> dict[str, Any]:
        request = ChatCreate.model_validate(payload)
        stable = hashlib.sha256(f"{actor_id}\0{key}".encode()).hexdigest()[:32]
        conversation_id = resource_id or request.conversation_id or f"cnv_{stable}"
        existing = await (
            await db.execute("SELECT 1 FROM conversations WHERE id=?", (conversation_id,))
        ).fetchone()
        if existing is None:
            await db.execute(
                "INSERT INTO conversations(id,title,created_at,updated_at) VALUES(?,?,?,?)",
                (conversation_id, request.content.strip()[:80], now, now),
            )
        else:
            await db.execute(
                "UPDATE conversations SET updated_at=? WHERE id=?", (now, conversation_id)
            )
        message = {
            "id": f"msg_{stable}",
            "conversation_id": conversation_id,
            "task_id": None,
            "role": "user",
            "agent_id": None,
            "content": request.content,
            "metadata": {"offline_replay": True},
            "created_at": now,
        }
        await db.execute(
            """INSERT INTO messages(
                id,conversation_id,task_id,role,agent_id,content,metadata_json,created_at
            ) VALUES(?,?,?,?,?,?,?,?)""",
            (
                message["id"],
                conversation_id,
                None,
                "user",
                None,
                request.content,
                json.dumps(message["metadata"], separators=(",", ":"), sort_keys=True),
                now,
            ),
        )
        await append_audit_event(
            db,
            "chat.message.created",
            {"offline_replay": True},
            actor_type="device",
            actor_id=actor_id,
            trace_id=conversation_id,
            created_at=now,
        )
        return {"conversation_id": conversation_id, "task": None, "message": message}
