from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import uuid4

from pydantic import AnyHttpUrl, BaseModel, ConfigDict, Field


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


class TaskMode(StrEnum):
    NORMAL = "normal"
    COMMANDANT = "commandant"
    REVIEW = "review"
    AUTONOME = "autonome"


class TaskCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    input: str = Field(min_length=1, max_length=32_000)
    mode: TaskMode = TaskMode.NORMAL
    conversation_id: str | None = None
    priority: int = Field(default=0, ge=-100, le=100)


class TaskRecord(BaseModel):
    id: str
    title: str
    input: str
    mode: TaskMode
    source: str
    conversation_id: str | None
    status: TaskStatus
    priority: int = 0
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None = None
    error_json: dict[str, Any] | None = None

    @classmethod
    def new(cls, request: TaskCreate, *, source: str) -> "TaskRecord":
        now = datetime.now(UTC)
        return cls(
            id=f"tsk_{uuid4().hex}",
            title=request.input.strip()[:80],
            input=request.input,
            mode=request.mode,
            source=source,
            conversation_id=request.conversation_id,
            status=TaskStatus.CREATED,
            priority=request.priority,
            created_at=now,
            updated_at=now,
        )


class ChatCreate(BaseModel):
    content: str = Field(min_length=1, max_length=32_000)
    conversation_id: str | None = None
    mode: TaskMode = TaskMode.NORMAL


class MemoryCreate(BaseModel):
    content: str = Field(min_length=1, max_length=32_000)
    summary: str | None = Field(default=None, max_length=2_000)
    scope: str = Field(default="general", min_length=1, max_length=100)
    kind: str = Field(default="fact", min_length=1, max_length=100)
    sensitivity: str = Field(default="normal", min_length=1, max_length=50)
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    pinned: bool = False


class MemoryUpdate(BaseModel):
    content: str | None = Field(default=None, min_length=1, max_length=32_000)
    summary: str | None = Field(default=None, max_length=2_000)
    pinned: bool | None = None


class MemorySearch(BaseModel):
    query: str = Field(min_length=1, max_length=2_000)
    scope: str | None = Field(default=None, max_length=100)
    kind: str | None = Field(default=None, max_length=100)


class AgentCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    version: str = Field(default="0.1.0", min_length=1, max_length=100)
    endpoint: AnyHttpUrl = Field(
        max_length=2_083,
        json_schema_extra={"pattern": r"^[Hh][Tt][Tt][Pp][Ss]?://"},
    )
    model_id: str | None = Field(default=None, max_length=500)
    skills: list[str] = Field(default_factory=list, max_length=200)


class AgentHeartbeat(BaseModel):
    status: str = Field(default="online", pattern="^(online|offline|busy)$")


class FeedbackCreate(BaseModel):
    task_id: str | None = None
    agent_id: str | None = None
    type: str = Field(default="rating", min_length=1, max_length=100)
    label: str | None = Field(default=None, max_length=200)
    score: float | None = Field(default=None, ge=0, le=5)
    notes: str | None = Field(default=None, max_length=4_000)


class HealthResponse(BaseModel):
    status: str
    service: str
    version: str
