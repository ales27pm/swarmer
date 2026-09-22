from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from functools import lru_cache
from importlib.resources import files
from pathlib import Path
from typing import Annotated, Any, Literal, Self

import aiosqlite
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StringConstraints, model_validator

from app.services.agent_card import (
    SPECIALIST_SKILLS,
    SUPPORTED_AGENT_PROTOCOL,
    SUPPORTED_AGENT_SKILLS,
)
from app.services.agent_liveness import DEFAULT_AGENT_OFFLINE_TIMEOUT_SECONDS, agent_is_fresh
from app.services.iphone_capability_service import EXTENDED_AGENDA_CAPABILITIES
from app.services.permission_policy import PermissionPolicy, PermissionPolicyError
from app.services.worker_skill_policy import (
    WorkerSkillPolicySnapshot,
    WorkerSkillPolicyStateError,
    WorkerSkillPolicyStore,
)

CatalogIdentifier = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9._-]{0,127}$")]
CatalogTitle = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)
]
CatalogText = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2_000)
]
CatalogInput = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=1_000)
]
AvailabilityState = Literal[
    "goal_ready",
    "parameters_required",
    "worker_unavailable",
    "iphone_request",
    "planned",
    "policy_denied",
    "unknown",
]

# These two implemented jobs need explicit paths that a generic goal node does
# not carry. The remaining worker targets have a bounded GoalManager mapping.
PARAMETER_BOUND_SKILLS = (
    frozenset({"workspace.read_text", "code_review.static_analysis"}) | SPECIALIST_SKILLS
)
GOAL_READY_SKILLS = frozenset(
    {
        "workspace.list_dir",
        "research.query",
        "code_review.git_status",
        "code_review.git_diff",
        "code_review.git_show",
        "code.generate_python",
        "code.build_project",
        "writing.draft",
    }
)


class CatalogModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ActivityCatalogDomain(CatalogModel):
    id: CatalogIdentifier
    title: CatalogTitle
    description: CatalogText


class ActivityCatalogExecution(CatalogModel):
    kind: Literal["worker", "iphone", "planned"]
    target: CatalogIdentifier | None

    @model_validator(mode="after")
    def validate_target(self) -> Self:
        if self.kind == "planned":
            if self.target is not None:
                raise ValueError("planned catalogue entries cannot declare executable targets")
        elif self.kind == "worker":
            if self.target not in SUPPORTED_AGENT_SKILLS:
                raise ValueError("catalogue worker target is unsupported")
        elif self.target not in PermissionPolicy.SUPPORTED_IPHONE_CAPABILITIES:
            raise ValueError("catalogue iPhone target is unsupported")
        return self


class ActivityCatalogSkill(CatalogModel):
    id: CatalogIdentifier
    title: CatalogTitle
    description: CatalogText
    inputs: list[CatalogInput] = Field(max_length=20)
    output: CatalogText
    execution: ActivityCatalogExecution
    requirements: list[CatalogInput] = Field(max_length=20)

    @model_validator(mode="after")
    def validate_identity(self) -> Self:
        if self.execution.target is not None and self.id != self.execution.target:
            raise ValueError("an executable catalogue skill must use its real target as its id")
        return self


class ActivityCatalogRole(CatalogModel):
    id: CatalogIdentifier
    domain_id: CatalogIdentifier
    title: CatalogTitle
    description: CatalogText
    skill_ids: list[CatalogIdentifier] = Field(min_length=1, max_length=64)
    examples: list[CatalogText] = Field(min_length=1, max_length=12)


class ActivityCatalogDefinition(CatalogModel):
    schema_version: Literal["1.0"]
    domains: list[ActivityCatalogDomain] = Field(min_length=1, max_length=64)
    skills: list[ActivityCatalogSkill] = Field(min_length=1, max_length=512)
    roles: list[ActivityCatalogRole] = Field(min_length=1, max_length=256)

    @model_validator(mode="after")
    def validate_references(self) -> Self:
        for collection in (self.domains, self.skills, self.roles):
            if len({item.id for item in collection}) != len(collection):
                raise ValueError("catalogue identifiers must be unique within each collection")
        domains = {item.id for item in self.domains}
        skills = {item.id for item in self.skills}
        for role in self.roles:
            if role.domain_id not in domains or set(role.skill_ids) - skills:
                raise ValueError("catalogue role references an unknown domain or skill")
            if len(set(role.skill_ids)) != len(role.skill_ids):
                raise ValueError("catalogue role skill references must be unique")
        return self


class ActivityCatalogAvailability(CatalogModel):
    state: AvailabilityState
    agent_ids: list[str] = Field(max_length=250)
    reason: CatalogText


class ActivityCatalogSkillStatus(ActivityCatalogSkill):
    availability: ActivityCatalogAvailability


class ActivityCatalogResponse(CatalogModel):
    schema_version: Literal["1.0"]
    generated_at: AwareDatetime
    domains: list[ActivityCatalogDomain]
    skills: list[ActivityCatalogSkillStatus]
    roles: list[ActivityCatalogRole]


class ActivityCatalogError(ValueError):
    """The packaged catalogue is missing or invalid; it must not be advertised."""


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("catalogue JSON keys must be unique")
        result[key] = value
    return result


def parse_activity_catalog(text: str) -> ActivityCatalogDefinition:
    try:
        if len(text.encode("utf-8")) > 2_000_000:
            raise ValueError("catalogue exceeds its package size budget")
        return ActivityCatalogDefinition.model_validate(
            json.loads(text, object_pairs_hook=_unique_object)
        )
    except (ValueError, UnicodeError) as exc:
        raise ActivityCatalogError("Le catalogue des activités est invalide.") from exc


@lru_cache(maxsize=1)
def load_activity_catalog() -> ActivityCatalogDefinition:
    try:
        text = files("app").joinpath("data/activity_catalog.json").read_text(encoding="utf-8")
    except OSError as exc:
        raise ActivityCatalogError("Le catalogue des activités est indisponible.") from exc
    return parse_activity_catalog(text)


class ActivityCatalogService:
    """Read-only catalogue projection; never enrolls workers or grants authority."""

    def __init__(
        self,
        db_path: Path,
        permission_policy: PermissionPolicy | None,
        *,
        offline_timeout_seconds: int = DEFAULT_AGENT_OFFLINE_TIMEOUT_SECONDS,
        clock: Callable[[], datetime] | None = None,
        extended_agenda_enabled: bool = False,
    ) -> None:
        if offline_timeout_seconds <= 0:
            raise ValueError("agent offline timeout must be positive")
        self.db_path = db_path
        self.extended_agenda_enabled = extended_agenda_enabled
        self.permission_policy = permission_policy
        self.offline_timeout_seconds = offline_timeout_seconds
        self.clock = clock or (lambda: datetime.now(UTC))
        # None prevents load_locked from bootstrapping or updating an in-memory
        # policy projection. Only the existing durable worker epoch is trusted.
        self.worker_policy = WorkerSkillPolicyStore(db_path, None)

    async def get_catalog(self) -> ActivityCatalogResponse:
        catalog = load_activity_catalog()
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("PRAGMA query_only=ON")
            await db.execute("BEGIN")
            try:
                now = self.clock()
                if now.tzinfo is None:
                    raise ValueError("catalogue clock must be timezone-aware")
                try:
                    policy = await self.worker_policy.load_locked(db, now=now.isoformat())
                except WorkerSkillPolicyStateError:
                    policy = None
                rows = await (
                    await db.execute(
                        """SELECT id,status,skills_json,last_seen_at FROM agents
                        WHERE status IN ('online','busy') AND supported_protocol_version=?
                        AND runtime='python' ORDER BY id""",
                        (SUPPORTED_AGENT_PROTOCOL,),
                    )
                ).fetchall()
            finally:
                await db.rollback()
        agents: dict[str, list[str]] = {}
        for row in rows:
            if not agent_is_fresh(dict(row), now=now, timeout_seconds=self.offline_timeout_seconds):
                continue
            try:
                skills = json.loads(row["skills_json"])
            except (ValueError, TypeError):
                continue
            if not isinstance(skills, list) or not all(isinstance(item, str) for item in skills):
                continue
            for skill in set(skills) & SUPPORTED_AGENT_SKILLS:
                agents.setdefault(skill, []).append(str(row["id"]))
        return ActivityCatalogResponse(
            schema_version=catalog.schema_version,
            generated_at=now,
            domains=catalog.domains,
            roles=catalog.roles,
            skills=[
                ActivityCatalogSkillStatus(
                    **skill.model_dump(),
                    availability=self._availability(skill, policy, agents),
                )
                for skill in catalog.skills
            ],
        )

    def _availability(
        self,
        skill: ActivityCatalogSkill,
        policy: WorkerSkillPolicySnapshot | None,
        agents: dict[str, list[str]],
    ) -> ActivityCatalogAvailability:
        def result(
            state: AvailabilityState,
            reason: str,
            agent_ids: list[str] | None = None,
        ) -> ActivityCatalogAvailability:
            return ActivityCatalogAvailability(
                state=state, reason=reason, agent_ids=agent_ids or []
            )

        execution = skill.execution
        if execution.kind == "planned":
            return result("planned", "Activité proposée ; son intégration reste à réaliser.")
        target = execution.target
        if target is None:
            raise ActivityCatalogError("La cible de cette activité est invalide.")
        if execution.kind == "iphone":
            if self.permission_policy is None:
                return result(
                    "unknown", "Les autorisations de cet outil iPhone ne sont pas vérifiables."
                )
            if target in EXTENDED_AGENDA_CAPABILITIES and not self.extended_agenda_enabled:
                return result(
                    "policy_denied",
                    "Cet outil attend l’activation après la mise à jour de l’iPhone.",
                )
            try:
                rule = self.permission_policy.evaluate_capability(target)
            except PermissionPolicyError:
                return result(
                    "unknown", "Les autorisations de cet outil iPhone ne sont pas vérifiables."
                )
            if rule.decision == "deny":
                return result(
                    "policy_denied", "Cet outil iPhone est désactivé dans les réglages du serveur."
                )
            return result(
                "iphone_request",
                "Cet outil iPhone exige une demande, votre accord et les permissions iOS. "
                "Son accès depuis un but reste à intégrer. La connexion à l’iPhone n’est pas vérifiée.",
            )
        if policy is None:
            return result(
                "unknown", "La disponibilité de cette activité ne peut pas être vérifiée."
            )
        if not policy.is_allowed(target):
            return result(
                "policy_denied", "Cette activité est désactivée dans les réglages du serveur."
            )
        # Discovery metadata only: this bounded projection never selects an
        # execution worker or changes the dispatcher's full candidate set.
        agent_ids = agents.get(target, [])[:250]
        if not agent_ids:
            return result("worker_unavailable", "Aucun agent compatible ne répond actuellement.")
        if target in PARAMETER_BOUND_SKILLS:
            return result(
                "parameters_required",
                "Agent disponible ; les fichiers à consulter doivent être précisés dans une action "
                "paramétrée. Les buts ne fournissent pas encore ces paramètres.",
                agent_ids,
            )
        if target in GOAL_READY_SKILLS:
            return result(
                "goal_ready",
                "Agent compatible disponible pour un but. Le plan, les budgets et les autorisations "
                "seront vérifiés au lancement.",
                agent_ids,
            )
        return result("unknown", "Le parcours d’exécution de cette compétence reste à vérifier.")
