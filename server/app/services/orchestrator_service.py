from __future__ import annotations

import json
from typing import Any, ClassVar

import httpx


class OrchestratorError(RuntimeError):
    pass


class OrchestratorService:
    """Adapter for an OpenAI-compatible local orchestrator endpoint.

    The model only proposes structured actions. It never receives an execution handle.
    """

    SYSTEM_PROMPT = """You are the monGARS swarm orchestrator.
Return exactly one JSON object and no prose.
Choose one tool from:
- workspace.list_dir: {path}
- workspace.read_text: {path}
- workspace.write_text: {path, content}
- process.run: {argv, cwd?, timeout_seconds?}
If no tool is appropriate, use tool_name 'none'.
Schema: {"tool_name": string, "arguments": object, "summary": string}.
For a real tool, summary is proposal-only context and the server replaces it with a fixed label.
For tool_name 'none', summary is the proposal-only response shown to the user.
Do not claim an action already happened. You only propose the next action.
All path and cwd values are relative to the configured workspace; never use an absolute path.
The configured project, repository, or workspace root is exactly ".". For example,
"Liste les fichiers du projet" must use workspace.list_dir with {"path": "."}.
Only name another relative path when the user explicitly names that file or directory.
Never invent or translate a directory name for the workspace root, and never add arguments
outside the selected tool's shape above.
"""
    ROOT_LIST_INTENTS: ClassVar[frozenset[str]] = frozenset(
        {
            "liste les fichiers du projet et résume sa structure.",
            "liste les fichiers à la racine du projet.",
        }
    )
    TOOL_NAMES: ClassVar[tuple[str, ...]] = (
        "none",
        "workspace.list_dir",
        "workspace.read_text",
        "workspace.write_text",
        "process.run",
    )
    RESPONSE_FORMAT: ClassVar[dict[str, Any]] = {
        "type": "json_schema",
        "json_schema": {
            "name": "orchestrator_proposal",
            # Tool-specific required fields are validated by ExecutionEngine. Keeping
            # this schema non-strict lets one envelope cover each supported tool. The
            # provider receives the JSON contract; downstream validation remains the
            # authoritative boundary when a provider only partially honors it.
            "strict": False,
            "schema": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "tool_name": {
                        "type": "string",
                        "enum": list(TOOL_NAMES),
                    },
                    "arguments": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "path": {
                                "type": "string",
                                "description": (
                                    "Workspace-relative path. For workspace.list_dir, use '.' "
                                    "for the configured project root; never use an absolute path."
                                ),
                            },
                            "content": {"type": "string"},
                            "argv": {"type": "array", "items": {"type": "string"}},
                            "cwd": {
                                "type": "string",
                                "description": (
                                    "Workspace-relative process directory; use '.' for the "
                                    "configured project root."
                                ),
                            },
                            "timeout_seconds": {"type": "number"},
                        },
                    },
                    "summary": {"type": "string", "minLength": 1},
                },
                "required": ["tool_name", "arguments", "summary"],
            },
        },
    }

    def __init__(self, base_url: str, model: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model

    async def plan(self, task_input: str, mode: str = "normal") -> dict[str, Any]:
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": self.SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": f"Mode: {mode}\nTask: {task_input}",
                },
            ],
            "temperature": 0.1,
            "stream": False,
            "response_format": self.RESPONSE_FORMAT,
        }
        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                response = await client.post(f"{self.base_url}/chat/completions", json=payload)
                response.raise_for_status()
        except httpx.HTTPError as exc:
            raise OrchestratorError(f"local orchestrator unavailable: {exc}") from exc

        body = response.json()
        try:
            content = body["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise OrchestratorError("invalid orchestrator response envelope") from exc

        if not isinstance(content, str):
            raise OrchestratorError("orchestrator content is not text")

        text = content.strip()
        if text.startswith("```"):
            lines = text.splitlines()
            if lines and lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            text = "\n".join(lines).strip()

        try:
            proposal = json.loads(text)
        except json.JSONDecodeError as exc:
            raise OrchestratorError("orchestrator did not return valid JSON") from exc

        if not isinstance(proposal, dict):
            raise OrchestratorError("orchestrator proposal must be an object")
        required_fields = {"tool_name", "arguments", "summary"}
        if set(proposal) != required_fields:
            raise OrchestratorError(
                "orchestrator proposal must contain exactly tool_name, arguments, and summary"
            )
        tool_name = proposal["tool_name"]
        arguments = proposal["arguments"]
        summary = proposal["summary"]
        if (
            not isinstance(tool_name, str)
            or not isinstance(arguments, dict)
            or not isinstance(summary, str)
        ):
            raise OrchestratorError("orchestrator proposal has invalid fields")
        if tool_name not in self.TOOL_NAMES:
            raise OrchestratorError("orchestrator proposal uses an unsupported tool")
        if not summary.strip() or len(summary) > 2_000:
            raise OrchestratorError("orchestrator proposal summary is invalid")
        if tool_name == "none":
            # A no-tool proposal cannot act on arguments. Discarding any generated
            # values keeps this path inert even when a provider only partially
            # implements the requested response schema.
            arguments = {}
        elif tool_name == "workspace.list_dir":
            # These shipped suggestions unambiguously name the configured root. Small
            # local models have translated that concept into nonexistent or absolute
            # directory names. Bind only these product-owned intents to "."; arbitrary
            # user paths (including a real child named "project") remain untouched.
            normalized_input = " ".join(task_input.strip().casefold().split())
            if normalized_input in self.ROOT_LIST_INTENTS:
                arguments = {**arguments, "path": "."}
        return {"tool_name": tool_name, "arguments": arguments, "summary": summary}
