from datetime import UTC, datetime
from enum import StrEnum
from uuid import uuid4

from pydantic import BaseModel, Field


class TaskStatus(StrEnum):
    CREATED = "created"
    PLANNED = "planned"
    WAITING_PERMISSION = "waiting_permission"
    QUEUED = "queued"
    RUNNING = "running"
    BLOCKED = "blocked"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class TaskCreate(BaseModel):
    input: str = Field(min_length=1, max_length=32_000)
    mode: str = "normal"
    source: str = "iphone"
    conversation_id: str | None = None


class TaskRecord(BaseModel):
    id: str
    input: str
    mode: str
    source: str
    conversation_id: str | None
    status: TaskStatus
    created_at: datetime
    updated_at: datetime

    @classmethod
    def new(cls, request: TaskCreate) -> "TaskRecord":
        now = datetime.now(UTC)
        return cls(
            id=f"tsk_{uuid4().hex}",
            input=request.input,
            mode=request.mode,
            source=request.source,
            conversation_id=request.conversation_id,
            status=TaskStatus.CREATED,
            created_at=now,
            updated_at=now,
        )


class HealthResponse(BaseModel):
    status: str
    service: str
    version: str
