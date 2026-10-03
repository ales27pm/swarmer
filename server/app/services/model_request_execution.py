"""Explicit per-request execution scope for goal-owned memory provider calls.

Providers pass the actual HTTP endpoint, model and JSON body. Direct memory API
requests leave this unset and retain their own admission path. No mutable global
provider state or ambient goal identity is used.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, Literal, Protocol, TypeVar

MemoryModelRole = Literal[
    "memory_normalizer",
    "memory_reviewer",
    "memory_presenter",
    "memory_presentation_reviewer",
    "memory_embedder",
]
MEMORY_MODEL_ROLES = frozenset(
    {
        "memory_normalizer",
        "memory_reviewer",
        "memory_presenter",
        "memory_presentation_reviewer",
        "memory_embedder",
    }
)
_Result = TypeVar("_Result")


class ModelExecutionControlError(RuntimeError):
    """Budget, admission or revision control must not become an empty search."""


class ModelRequestBudgetUnavailable(ModelExecutionControlError):
    """Optional retrieval must preserve the next planner call's credit."""


class ModelRequestExecutor(Protocol):
    async def execute(
        self,
        *,
        role: MemoryModelRole,
        model_id: str,
        endpoint: str,
        request_body: dict[str, Any],
        operation: Callable[[], Awaitable[_Result]],
    ) -> _Result: ...
