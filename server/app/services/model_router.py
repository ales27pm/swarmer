from __future__ import annotations

from collections.abc import Iterable, Mapping
from types import MappingProxyType

from app.services.swarm_contracts import ModelRole, ModelRoleConfig


class ModelRouterError(ValueError):
    """Model routing metadata is missing, duplicated, or unsafe."""


class ModelRouter:
    """Immutable role metadata with deliberately no inference or execution methods."""

    def __init__(self, routes: Iterable[ModelRoleConfig]) -> None:
        by_role: dict[ModelRole, ModelRoleConfig] = {}
        for route in routes:
            if route.role in by_role:
                raise ModelRouterError(f"duplicate model route for role: {route.role.value}")
            by_role[route.role] = route
        self._routes: Mapping[ModelRole, ModelRoleConfig] = MappingProxyType(by_role)

    def route_for(self, role: ModelRole) -> ModelRoleConfig:
        try:
            return self._routes[role]
        except KeyError as exc:
            raise ModelRouterError(f"no model route configured for role: {role.value}") from exc

    def public_metadata(self) -> tuple[dict[str, str], ...]:
        return tuple(
            {
                "role": role.value,
                "source": route.source.value,
                "model_id": route.model_id,
            }
            for role, route in sorted(self._routes.items(), key=lambda item: item[0].value)
        )
