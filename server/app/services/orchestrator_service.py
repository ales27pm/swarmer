from __future__ import annotations

import json
from typing import Any

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
Do not claim an action already happened. You only propose the next action.
"""

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
        tool_name = proposal.get("tool_name")
        arguments = proposal.get("arguments", {})
        summary = proposal.get("summary", "")
        if not isinstance(tool_name, str) or not isinstance(arguments, dict) or not isinstance(summary, str):
            raise OrchestratorError("orchestrator proposal has invalid fields")
        return {"tool_name": tool_name, "arguments": arguments, "summary": summary}
