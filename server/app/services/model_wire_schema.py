from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel

from app.services.plan_validation import PlanValidationError


def model_wire_schema(model: type[BaseModel]) -> dict[str, Any]:
    """Build a local-LLM grammar schema without expanded string repetitions.

    Ollama/llama.cpp can reject otherwise valid schemas when ``maxLength``
    expands into too many grammar rules. Omit that generation hint only;
    callers still validate every response with the unchanged Pydantic model.
    """

    def normalize(value: Any) -> Any:
        if isinstance(value, list):
            return [normalize(item) for item in value]
        if isinstance(value, dict):
            return {
                key: normalize(item)
                for key, item in value.items()
                if not (key == "maxLength" and value.get("type") == "string")
            }
        return value

    schema: dict[str, Any] = normalize(model.model_json_schema())
    return schema


def decode_research_query_nodes(
    value: Mapping[str, object], *, node_field: str
) -> dict[str, object]:
    """Translate the model-only query name without relaxing public validation.

    Callers first use the bounded, duplicate-safe JSON reader, then validate the
    returned object with the existing public model. Complete legacy objective
    fields stay compatible; aliases are never accepted on other worker types.
    """
    result = dict(value)
    nodes = value.get(node_field)
    if not isinstance(nodes, list):
        return result
    decoded: list[object] = []
    for node in nodes:
        if not isinstance(node, dict) or "search_query" not in node:
            decoded.append(node)
            continue
        if (
            node.get("node_type") != "worker"
            or node.get("required_skill") != "research.query"
            or "objective" in node
        ):
            raise PlanValidationError("research query wire fields are ambiguous or misplaced")
        translated = dict(node)
        translated["objective"] = translated.pop("search_query")
        decoded.append(translated)
    result[node_field] = decoded
    return result


def encode_model_wire_response(value: Mapping[str, object]) -> str:
    """Keep translated JSON compact and classify non-finite decoded numbers."""
    try:
        return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise PlanValidationError("model wire response contains an invalid JSON value") from exc
