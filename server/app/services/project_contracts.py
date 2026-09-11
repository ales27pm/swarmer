from __future__ import annotations

import hashlib
import json
import re
from pathlib import PurePosixPath
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

PROJECT_SKILL = "code.build_project"
MAX_PROJECT_BYTES = 1_000_000
MAX_PROJECT_FILES = 80
MAX_FILE_BYTES = 64_000
Digest = Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{64}$")]
Identifier = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")]
Text = Annotated[str, StringConstraints(max_length=4_000)]


def validate_project_path(value: str) -> str:
    if (
        not value
        or len(value) > 240
        or not re.fullmatch(r"[A-Za-z0-9_.@/-]+", value)
        or any(part in {"", ".", ".."} for part in value.split("/"))
        or PurePosixPath(value).is_absolute()
    ):
        raise ValueError("project path must be a canonical relative path")
    protected = {
        ".git",
        ".ssh",
        ".aws",
        ".codex",
        ".venv",
        "node_modules",
        ".npmrc",
        ".pypirc",
        "auth.json",
    }
    for part in value.lower().split("/"):
        if part in protected or part in {".env", "id_rsa", "id_ed25519", "credentials.json"}:
            raise ValueError("project path is reserved")
        if part.startswith(".env") and part not in {".env.example", ".env.sample", ".env.template"}:
            raise ValueError("project environment secrets are not accepted")
        if part.endswith((".pem", ".p12", ".pfx", ".key")):
            raise ValueError("project credential files are not accepted")
    return value


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ProjectFile(StrictModel):
    path: str = Field(min_length=1, max_length=240)
    content: str = Field(max_length=MAX_FILE_BYTES)

    @model_validator(mode="after")
    def validate_file(self) -> ProjectFile:
        validate_project_path(self.path)
        if len(self.content.encode("utf-8")) > MAX_FILE_BYTES:
            raise ValueError("project file exceeds UTF-8 byte limit")
        if any(ord(c) < 32 and c not in "\t\r\n" for c in self.content):
            raise ValueError("project file contains unsupported control characters")
        return self


class ProjectCheck(StrictModel):
    command: list[Annotated[str, StringConstraints(min_length=1, max_length=500)]] = Field(
        min_length=1, max_length=24
    )
    status: Literal["passed", "failed", "skipped"]
    exit_code: int | None = Field(ge=-255, le=255)
    output: str = Field(max_length=8_000)
    duration_ms: int = Field(ge=0, le=900_000)

    @model_validator(mode="after")
    def validate_status(self) -> ProjectCheck:
        if self.status == "passed" and self.exit_code != 0:
            raise ValueError("passing check requires a successful exit code")
        if self.status == "failed" and self.exit_code == 0:
            raise ValueError("failed check cannot have a successful exit code")
        return self


def validate_files(files: list[ProjectFile]) -> None:
    if len(files) > MAX_PROJECT_FILES:
        raise ValueError("project contains too many files")
    paths: set[str] = set()
    for file in files:
        path = file.path.casefold()
        if path in paths:
            raise ValueError("project contains duplicate or case-colliding paths")
        paths.add(path)
    for path in paths:
        if any(parent.as_posix() in paths for parent in PurePosixPath(path).parents):
            raise ValueError("project file conflicts with a directory")
    if sum(len(file.content.encode("utf-8")) for file in files) > MAX_PROJECT_BYTES:
        raise ValueError("project exceeds its UTF-8 byte limit")


def project_digest(files: list[ProjectFile] | list[dict[str, Any]]) -> str:
    normalized = [
        file if isinstance(file, ProjectFile) else ProjectFile.model_validate(file)
        for file in files
    ]
    validate_files(normalized)
    raw = json.dumps(
        [file.model_dump() for file in sorted(normalized, key=lambda item: item.path)],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


class ProjectResult(StrictModel):
    schema_version: Literal["1.0"]
    action: Literal["clarify", "continue", "complete"]
    message: Text = Field(min_length=1)
    plan: list[Annotated[str, StringConstraints(min_length=1, max_length=500)]] = Field(
        max_length=20
    )
    files: list[ProjectFile] = Field(max_length=MAX_PROJECT_FILES)
    checks: list[ProjectCheck] = Field(max_length=12)
    run_instructions: Text
    runtime: Literal["python", "node", "python_node"]
    base_revision_id: Identifier | None
    base_sha256: Digest | None
    focus_paths: list[Annotated[str, StringConstraints(min_length=1, max_length=240)]] = Field(
        default_factory=list, max_length=8
    )

    @model_validator(mode="after")
    def validate_project(self) -> ProjectResult:
        validate_files(self.files)
        if any(path not in {file.path for file in self.files} for path in self.focus_paths):
            raise ValueError("project inspection requires an existing file")
        if (self.base_revision_id is None) != (self.base_sha256 is None):
            raise ValueError("project base revision and digest must be supplied together")
        if self.action == "complete":
            if self.focus_paths:
                raise ValueError("completed project cannot require further inspection")
            if not self.files or not self.plan or not self.run_instructions.strip():
                raise ValueError("completed project requires files, plan, and run instructions")
            if not any(file.path.lower().startswith("readme") for file in self.files):
                raise ValueError("completed project requires a README")
            if not self.checks or any(check.status != "passed" for check in self.checks):
                raise ValueError("completed project requires successful executed checks")
            if not any(
                any(
                    word in {"pytest", "unittest", "test", "--test", "test:ci"}
                    for word in check.command
                )
                for check in self.checks
            ):
                raise ValueError("completed project requires an executed test command")
        return self


class ProjectMemoryItem(StrictModel):
    id: Identifier
    summary: str = Field(min_length=1, max_length=1_200)
    score: float = Field(ge=-1, le=1, allow_inf_nan=False)
    source_id: Identifier


class ProjectMemoryContext(StrictModel):
    mode: Literal["semantic", "lexical"]
    reason: str = Field(min_length=1, max_length=100)
    items: list[ProjectMemoryItem] = Field(max_length=4)


class ProjectPayload(StrictModel):
    objective: Text = Field(min_length=1)
    conversation: list[dict[str, str]] = Field(max_length=40)
    files: list[ProjectFile] = Field(max_length=MAX_PROJECT_FILES)
    plan: list[Annotated[str, StringConstraints(min_length=1, max_length=500)]] = Field(
        max_length=20
    )
    checks: list[ProjectCheck] = Field(max_length=12)
    iteration: int = Field(ge=1, le=100)
    base_revision_id: Identifier | None
    base_sha256: Digest | None
    focus_paths: list[Annotated[str, StringConstraints(min_length=1, max_length=240)]] = Field(
        default_factory=list, max_length=8
    )
    memory: ProjectMemoryContext | None = None

    @model_validator(mode="after")
    def validate_payload(self) -> ProjectPayload:
        validate_files(self.files)
        if any(path not in {file.path for file in self.files} for path in self.focus_paths):
            raise ValueError("project inspection requires an existing file")
        for message in self.conversation:
            if (
                set(message) != {"role", "content"}
                or message["role"] not in {"user", "assistant"}
                or not message["content"].strip()
                or len(message["content"]) > 4_000
            ):
                raise ValueError("project conversation message is invalid")
        if (self.base_revision_id is None) != (self.base_sha256 is None):
            raise ValueError("project base revision and digest must be supplied together")
        if self.base_sha256 is not None and self.base_sha256 != project_digest(self.files):
            raise ValueError("project base digest does not match the supplied files")
        return self


class ProjectPreview(StrictModel):
    project_id: Identifier
    revision_id: Identifier
    revision: int = Field(ge=1)
    sha256: Digest
    state: Literal["building", "needs_user", "ready", "waiting_permission", "applied", "failed"]
    message: Text
    plan: list[str]
    files: list[ProjectFile]
    checks: list[ProjectCheck]
    run_instructions: Text
    runtime: Literal["python", "node", "python_node"]
    task_id: str | None


class ProjectApplyRequest(StrictModel):
    revision_id: Identifier
    sha256: Digest


class ProjectApplication(StrictModel):
    task_id: str
    tool_call_id: str
    approval_id: str


class ProjectWriteArguments(StrictModel):
    project_id: Identifier
    revision_id: Identifier
    sha256: Digest
    files: list[ProjectFile] = Field(min_length=1, max_length=MAX_PROJECT_FILES)

    @model_validator(mode="after")
    def validate_manifest(self) -> ProjectWriteArguments:
        if self.sha256 != project_digest(self.files):
            raise ValueError("project manifest digest does not match its files")
        return self

    @property
    def path(self) -> str:
        return f"generated/{self.project_id}/revisions/{self.revision_id}"
