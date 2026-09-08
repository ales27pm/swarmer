import math
import re
from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Any, Literal
from uuid import uuid4

from pydantic import AnyHttpUrl, BaseModel, ConfigDict, Field, field_validator, model_validator

RFC3339_TIMESTAMP = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})$"
)


def _is_bounded_rfc3339_timestamp(value: Any) -> bool:
    if not isinstance(value, str) or len(value) > 64 or RFC3339_TIMESTAMP.fullmatch(value) is None:
        return False
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return False
    return parsed.tzinfo is not None


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
    model_config = ConfigDict(extra="forbid")

    content: str = Field(min_length=1, max_length=32_000)
    conversation_id: str | None = None
    mode: TaskMode = TaskMode.NORMAL
    start_task: bool = False


class MemoryCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    content: str = Field(min_length=1, max_length=32_000)
    summary: str | None = Field(default=None, max_length=2_000)
    scope: str = Field(default="general", min_length=1, max_length=100)
    kind: str = Field(default="fact", min_length=1, max_length=100)
    sensitivity: str = Field(default="normal", min_length=1, max_length=50)
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    pinned: bool = False


class MemoryUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    content: str | None = Field(default=None, min_length=1, max_length=32_000)
    summary: str | None = Field(default=None, max_length=2_000)
    pinned: bool | None = None


class MemorySearch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str = Field(min_length=1, max_length=2_000)
    scope: str | None = Field(default=None, max_length=100)
    kind: str | None = Field(default=None, max_length=100)


class AgentCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=200)
    version: str = Field(default="0.1.0", min_length=1, max_length=100)
    endpoint: AnyHttpUrl = Field(
        max_length=2_083,
        json_schema_extra={"pattern": r"^[Hh][Tt][Tt][Pp][Ss]?://"},
    )
    model_id: str | None = Field(default=None, max_length=500)
    skills: list[str] = Field(default_factory=list, max_length=200)
    max_concurrency: int = Field(default=1, ge=1, le=32)
    capacity: dict[str, int] = Field(default_factory=dict)


class AgentHeartbeat(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: str = Field(default="online", pattern="^(online|offline|busy|draining)$")


class FeedbackCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    task_id: str | None = None
    agent_id: str | None = None
    type: str = Field(default="rating", min_length=1, max_length=100)
    label: str | None = Field(default=None, max_length=200)
    score: float | None = Field(default=None, ge=0, le=5)
    notes: str | None = Field(default=None, max_length=4_000)


class AgentJobClaim(BaseModel):
    model_config = ConfigDict(extra="forbid")

    wait_seconds: int = Field(default=0, ge=0, le=30)


class AgentJobHeartbeat(BaseModel):
    model_config = ConfigDict(extra="forbid")

    claim_token: str = Field(min_length=20, max_length=500)
    lease_id: str = Field(min_length=10, max_length=200)
    lease_generation: int = Field(ge=1)


class AgentJobResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    claim_token: str = Field(min_length=20, max_length=500)
    lease_id: str = Field(min_length=10, max_length=200)
    lease_generation: int = Field(ge=1)
    status: str = Field(pattern="^(completed|failed)$")
    result: dict[str, Any] | None = None
    error: str | None = Field(default=None, max_length=4_000)


class AgentJobDispatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    required_skill: str = Field(min_length=1, max_length=200, pattern=r"^[A-Za-z0-9._:-]+$")
    payload: dict[str, Any]


IPhoneCapabilityName = Literal[
    "iphone.location.current",
    "iphone.contacts.lookup",
    "iphone.calendar.events",
    "iphone.photos.pick",
    "iphone.mail.compose",
    "iphone.sms.compose",
]


class _EmptyCapabilityArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _ContactsLookupArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str = Field(min_length=1, max_length=200)


class _CalendarEventsArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")
    start: datetime
    end: datetime

    @field_validator("start", "end", mode="before")
    @classmethod
    def validate_timestamp(cls, value: Any) -> Any:
        if not _is_bounded_rfc3339_timestamp(value):
            raise ValueError("calendar timestamp must be bounded RFC3339")
        return value

    @model_validator(mode="after")
    def validate_interval(self) -> "_CalendarEventsArguments":
        if self.end <= self.start:
            raise ValueError("calendar end must be after start")
        return self


CapabilityRecipient = Annotated[str, Field(max_length=1_000)]


class _MailComposeArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")
    recipients: list[CapabilityRecipient] = Field(default_factory=list, max_length=20)
    subject: str | None = Field(default=None, max_length=500)
    body: str | None = Field(default=None, max_length=10_000)


class _SMSComposeArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")
    recipients: list[CapabilityRecipient] = Field(default_factory=list, max_length=20)
    message: str | None = Field(default=None, max_length=2_000)


CAPABILITY_ARGUMENT_MODELS: dict[str, type[BaseModel]] = {
    "iphone.location.current": _EmptyCapabilityArguments,
    "iphone.contacts.lookup": _ContactsLookupArguments,
    "iphone.calendar.events": _CalendarEventsArguments,
    "iphone.photos.pick": _EmptyCapabilityArguments,
    "iphone.mail.compose": _MailComposeArguments,
    "iphone.sms.compose": _SMSComposeArguments,
}


class AgentCapabilityRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    claim_token: str = Field(min_length=20, max_length=500)
    lease_id: str = Field(min_length=10, max_length=200)
    lease_generation: int = Field(ge=1)
    capability_name: IPhoneCapabilityName
    arguments: dict[str, Any]

    @model_validator(mode="after")
    def validate_capability_arguments(self) -> "AgentCapabilityRequest":
        validated = CAPABILITY_ARGUMENT_MODELS[self.capability_name].model_validate(self.arguments)
        self.arguments = validated.model_dump(mode="json", exclude_none=True)
        return self


class AgentCapabilityPoll(BaseModel):
    model_config = ConfigDict(extra="forbid")

    claim_token: str = Field(min_length=20, max_length=500)
    lease_id: str = Field(min_length=10, max_length=200)
    lease_generation: int = Field(ge=1)


class IPhoneCapabilityDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: Literal["approve", "deny"]
    user_note: str | None = Field(default=None, max_length=2_000)


class IPhoneCapabilityExecute(BaseModel):
    model_config = ConfigDict(extra="forbid")

    grant_id: str = Field(min_length=32, max_length=500)
    action_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")


class IPhoneCapabilityNativeResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: IPhoneCapabilityName
    status: Literal["completed", "cancelled", "denied", "failed"]
    value: Any = None
    reason: Literal["permission_denied", "unavailable", "native_error"] | None = None

    @model_validator(mode="after")
    def validate_native_result(self) -> "IPhoneCapabilityNativeResult":
        expected_fields = {"name", "status", "value"}
        if self.status in {"denied", "failed"}:
            expected_fields.add("reason")
        if self.model_fields_set != expected_fields:
            raise ValueError("capability result has an invalid shape")
        if self.status == "denied":
            if self.reason not in {"permission_denied", "unavailable"} or self.value is not None:
                raise ValueError("denied capability result is invalid")
            return self
        if self.status == "failed":
            if self.reason != "native_error" or self.value is not None:
                raise ValueError("failed capability result is invalid")
            return self
        if self.reason is not None:
            raise ValueError("successful or cancelled capability result cannot have a reason")
        if self.status == "cancelled":
            if (
                self.name
                not in {
                    "iphone.photos.pick",
                    "iphone.mail.compose",
                    "iphone.sms.compose",
                }
                or self.value is not None
            ):
                raise ValueError("cancelled capability result is invalid")
            return self
        self._validate_completed_value()
        return self

    def _validate_completed_value(self) -> None:
        if self.name == "iphone.location.current":
            if not isinstance(self.value, dict) or set(self.value) != {
                "latitude",
                "longitude",
                "accuracy",
            }:
                raise ValueError("location result is invalid")
            latitude = self.value["latitude"]
            longitude = self.value["longitude"]
            accuracy = self.value["accuracy"]
            if (
                not isinstance(latitude, (int, float))
                or isinstance(latitude, bool)
                or not math.isfinite(float(latitude))
                or not -90 <= float(latitude) <= 90
                or not isinstance(longitude, (int, float))
                or isinstance(longitude, bool)
                or not math.isfinite(float(longitude))
                or not -180 <= float(longitude) <= 180
                or accuracy is not None
                and (
                    not isinstance(accuracy, (int, float))
                    or isinstance(accuracy, bool)
                    or not math.isfinite(float(accuracy))
                )
            ):
                raise ValueError("location result is invalid")
        elif self.name == "iphone.contacts.lookup":
            if not isinstance(self.value, list) or len(self.value) > 25:
                raise ValueError("contacts result is invalid")
            for contact in self.value:
                if not isinstance(contact, dict) or set(contact) != {
                    "id",
                    "name",
                    "phoneNumbers",
                    "emails",
                }:
                    raise ValueError("contacts result is invalid")
                if (
                    not self._strict_string(contact["id"], 500)
                    or not self._text(contact["name"], 1_000)
                    or not self._string_list(contact["phoneNumbers"], 20, 1_000)
                    or not self._string_list(contact["emails"], 20, 1_000)
                ):
                    raise ValueError("contacts result is invalid")
        elif self.name == "iphone.calendar.events":
            if not isinstance(self.value, list) or len(self.value) > 100:
                raise ValueError("calendar result is invalid")
            for event in self.value:
                if not isinstance(event, dict) or set(event) != {"id", "title", "start", "end"}:
                    raise ValueError("calendar result is invalid")
                if (
                    not self._strict_string(event["id"], 500)
                    or not self._text(event["title"], 2_000)
                    or not self._timestamp(event["start"])
                    or not self._timestamp(event["end"])
                ):
                    raise ValueError("calendar result is invalid")
        elif self.name == "iphone.photos.pick":
            if not isinstance(self.value, dict) or set(self.value) != {"uri", "width", "height"}:
                raise ValueError("photo result is invalid")
            width = self.value["width"]
            height = self.value["height"]
            if (
                not self._strict_string(self.value["uri"], 8_000)
                or not self._positive_finite_number(width)
                or not self._positive_finite_number(height)
            ):
                raise ValueError("photo result is invalid")
        elif self.value != {"composed": True}:
            raise ValueError("composer result is invalid")

    @staticmethod
    def _strict_string(value: Any, maximum: int) -> bool:
        return isinstance(value, str) and 0 < len(value) <= maximum and value.strip() == value

    @staticmethod
    def _text(value: Any, maximum: int) -> bool:
        return isinstance(value, str) and len(value) <= maximum

    @classmethod
    def _string_list(cls, value: Any, maximum_items: int, maximum_length: int) -> bool:
        return (
            isinstance(value, list)
            and len(value) <= maximum_items
            and all(cls._strict_string(item, maximum_length) for item in value)
        )

    @staticmethod
    def _timestamp(value: Any) -> bool:
        return _is_bounded_rfc3339_timestamp(value)

    @staticmethod
    def _positive_finite_number(value: Any) -> bool:
        return (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and math.isfinite(float(value))
            and float(value) > 0
        )


class IPhoneCapabilityResultSubmit(BaseModel):
    model_config = ConfigDict(extra="forbid")

    grant_id: str = Field(min_length=32, max_length=500)
    action_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    result: IPhoneCapabilityNativeResult


class FeedbackCorrection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_id: str = Field(min_length=1, max_length=200)
    corrected_behavior: str = Field(min_length=1, max_length=32_000)
    notes: str | None = Field(default=None, max_length=4_000)


class HealthResponse(BaseModel):
    status: str
    service: str
    version: str
