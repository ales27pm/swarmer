from __future__ import annotations

import ast
import sys
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

CODE_GENERATION_SKILL = "code.generate_python"
MAX_CODE_BYTES = 64_000


class CodeProposalPreview(BaseModel):
    model_config = ConfigDict(extra="forbid")
    node_id: str
    path: str
    content: str = Field(max_length=MAX_CODE_BYTES)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    summary: str = Field(max_length=500)
    status: Literal["proposal", "waiting_permission", "applied", "failed"]
    task_id: str | None


class CodeProposalApplyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class CodeProposalApplication(BaseModel):
    model_config = ConfigDict(extra="forbid")
    task_id: str
    tool_call_id: str
    approval_id: str


def validate_code_proposal_result(value: object) -> dict[str, Any]:
    """Accept one bounded Python proposal, without importing or executing it.

    Syntax validity is only an admission check. It grants no filesystem or
    process authority and is not evidence that the generated application works.
    """

    if not isinstance(value, dict) or set(value) != {
        "schema_version",
        "path",
        "content",
        "summary",
    }:
        raise ValueError("code proposal fields are invalid")
    if value["schema_version"] != "1.0" or value["path"] != "app.py":
        raise ValueError("code proposal version or path is invalid")
    content, summary = value["content"], value["summary"]
    if not isinstance(content, str) or not content.strip():
        raise ValueError("code proposal content is missing")
    if not isinstance(summary, str) or not summary.strip() or len(summary) > 500:
        raise ValueError("code proposal summary is invalid")
    try:
        if len(content.encode("utf-8")) > MAX_CODE_BYTES:
            raise ValueError("code proposal exceeds its size limit")
        module = ast.parse(content, filename="app.py")
    except (SyntaxError, UnicodeError, RecursionError, MemoryError) as exc:
        raise ValueError("code proposal is not valid bounded Python") from exc
    if not module.body:
        raise ValueError("code proposal contains no Python statements")
    for node in ast.walk(module):
        modules: list[str] = []
        if isinstance(node, ast.Import):
            modules = [alias.name.split(".", 1)[0] for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            if node.level or node.module is None:
                raise ValueError("code proposal must be a standalone Python file")
            modules = [node.module.split(".", 1)[0]]
        if any(name not in sys.stdlib_module_names for name in modules):
            raise ValueError("code proposal imports an unavailable third-party dependency")
    return dict(value)
