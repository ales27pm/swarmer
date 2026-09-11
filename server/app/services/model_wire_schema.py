from __future__ import annotations

from typing import Any

from pydantic import BaseModel


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
