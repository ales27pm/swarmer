from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from urllib.parse import urlsplit

from app.models import AgentCard, AgentCreate

SUPPORTED_AGENT_PROTOCOL = "mongars-worker-v0.9"
WORKSPACE_SKILLS = frozenset({"workspace.list_dir", "workspace.read_text"})
RESEARCH_SKILLS = frozenset({"research.query"})
CODE_REVIEW_SKILLS = frozenset(
    {
        "code_review.git_status",
        "code_review.git_diff",
        "code_review.git_show",
        "code_review.static_analysis",
    }
)
SUPPORTED_AGENT_SKILLS = WORKSPACE_SKILLS | RESEARCH_SKILLS | CODE_REVIEW_SKILLS

_NAME_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,98}[a-z0-9])?$")
_VERSION_RE = re.compile(r"^\d{1,4}\.\d{1,4}\.\d{1,4}$")
_MODEL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,499}$")
_CAPABILITY_METADATA_RANGES: Mapping[str, tuple[int, int]] = MappingProxyType(
    {
        "max_concurrency": (1, 32),
        "memory_mb": (1, 4_194_304),
        "max_results": (1, 10),
        "max_query_characters": (1, 2_000),
        "max_result_bytes": (1, 524_288),
        "max_operation_seconds": (1, 120),
        "max_paths": (1, 50),
        "max_selected_files": (1, 100),
    }
)
_BASE_METADATA = frozenset({"max_concurrency", "memory_mb", "max_result_bytes"})
_FAMILY_METADATA: Mapping[str, frozenset[str]] = MappingProxyType(
    {
        "workspace": _BASE_METADATA | {"max_operation_seconds"},
        "research": _BASE_METADATA
        | {"max_operation_seconds", "max_results", "max_query_characters"},
        "code_review": _BASE_METADATA
        | {"max_operation_seconds", "max_paths", "max_selected_files"},
    }
)


class AgentCardPolicyError(ValueError):
    """An agent declaration exceeds the control plane's worker policy."""


@dataclass(frozen=True)
class AgentCardPolicy:
    protocol: str
    skills: tuple[str, ...]
    capability_metadata: Mapping[str, int]
    policy: Mapping[str, str | bool]


def _validate_protocol(protocol: object) -> str:
    if protocol != SUPPORTED_AGENT_PROTOCOL:
        raise AgentCardPolicyError("agent protocol is incompatible")
    return SUPPORTED_AGENT_PROTOCOL


def _normalize_skills(raw: object) -> tuple[str, ...]:
    if not isinstance(raw, list) or len(raw) > len(SUPPORTED_AGENT_SKILLS):
        raise AgentCardPolicyError("agent skills must be a bounded list")
    skills: list[str] = []
    for skill in raw:
        if not isinstance(skill, str) or skill not in SUPPORTED_AGENT_SKILLS:
            raise AgentCardPolicyError("agent declares an unsupported or privileged skill")
        if skill in skills:
            raise AgentCardPolicyError("agent skills must be unique")
        skills.append(skill)
    return tuple(skills)


def _skill_families(skills: tuple[str, ...]) -> frozenset[str]:
    families: set[str] = set()
    if set(skills) & WORKSPACE_SKILLS:
        families.add("workspace")
    if set(skills) & RESEARCH_SKILLS:
        families.add("research")
    if set(skills) & CODE_REVIEW_SKILLS:
        families.add("code_review")
    return frozenset(families)


def _normalize_capability_metadata(
    raw: object,
    skills: tuple[str, ...],
) -> Mapping[str, int]:
    if not isinstance(raw, dict) or len(raw) > len(_CAPABILITY_METADATA_RANGES):
        raise AgentCardPolicyError("agent capability metadata must be a bounded object")
    families = _skill_families(skills)
    allowed_keys = set(_BASE_METADATA)
    for family in families:
        allowed_keys.update(_FAMILY_METADATA[family])
    normalized: dict[str, int] = {}
    for key, value in raw.items():
        if not isinstance(key, str) or key not in allowed_keys:
            raise AgentCardPolicyError("agent capability metadata contains an unsafe field")
        if not isinstance(value, int) or isinstance(value, bool):
            raise AgentCardPolicyError("agent capability metadata values must be integers")
        minimum, maximum = _CAPABILITY_METADATA_RANGES[key]
        if not minimum <= value <= maximum:
            raise AgentCardPolicyError("agent capability metadata value is outside policy")
        normalized[key] = value
    return MappingProxyType(dict(sorted(normalized.items())))


def _validate_registration_endpoint(request: AgentCreate) -> None:
    parsed = urlsplit(str(request.endpoint))
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise AgentCardPolicyError("agent endpoint contains unsafe metadata")


def validate_agent_registration(
    request: AgentCreate,
    *,
    protocol: str | None = None,
) -> AgentCardPolicy:
    """Validate the policy-bearing fields already present on ``AgentCreate``."""

    validated_protocol = _validate_protocol(
        request.supported_protocol_version if protocol is None else protocol
    )
    skills = _normalize_skills(request.skills)
    if skills and len(_skill_families(skills)) != 1:
        raise AgentCardPolicyError("agent skills must belong to one capability family")
    _validate_registration_endpoint(request)
    if not _VERSION_RE.fullmatch(request.version):
        raise AgentCardPolicyError("agent version must be a bounded semantic version")
    if request.model_id is not None and (
        _MODEL_ID_RE.fullmatch(request.model_id) is None or "://" in request.model_id
    ):
        raise AgentCardPolicyError("agent model id contains unsafe metadata")
    metadata = _normalize_capability_metadata(request.capacity, skills)
    return AgentCardPolicy(
        protocol=validated_protocol,
        skills=skills,
        capability_metadata=metadata,
        policy=MappingProxyType({}),
    )


def public_agent_card(agent: Mapping[str, object]) -> AgentCard:
    """Project a persisted, server-approved agent into its public card."""

    protocol = _validate_protocol(agent.get("supported_protocol_version", SUPPORTED_AGENT_PROTOCOL))
    skills = _normalize_skills(agent.get("skills"))
    if skills and len(_skill_families(skills)) != 1:
        raise AgentCardPolicyError("agent skills must belong to one capability family")
    capabilities = _normalize_capability_metadata(agent.get("capacity", {}), skills)
    runtime = agent.get("runtime", "python")
    if runtime != "python":
        raise AgentCardPolicyError("agent runtime is unsupported")
    return AgentCard.model_validate(
        {
            "agent_id": agent["id"],
            "name": agent["name"],
            "version": agent["version"],
            "skills": skills,
            "model_id": agent.get("model_id"),
            "runtime": runtime,
            "max_concurrency": agent["max_concurrency"],
            "supported_protocol_version": protocol,
            "capabilities": dict(capabilities),
        }
    )


def _manifest_skills(raw: object) -> tuple[str, ...]:
    if not isinstance(raw, list):
        raise AgentCardPolicyError("agent card skills must be a list")
    identifiers: list[str] = []
    for item in raw:
        if not isinstance(item, dict) or set(item) != {"id", "risk", "result_trust"}:
            raise AgentCardPolicyError("agent card skill metadata is unsafe")
        if item.get("risk") != "low" or item.get("result_trust") != "untrusted":
            raise AgentCardPolicyError("agent card skill metadata exceeds policy")
        identifier = item.get("id")
        if not isinstance(identifier, str):
            raise AgentCardPolicyError("agent card skill id must be a string")
        identifiers.append(identifier)
    skills = _normalize_skills(identifiers)
    if not skills:
        raise AgentCardPolicyError("agent card must declare at least one skill")
    return skills


def _manifest_policy(raw: object, skills: tuple[str, ...]) -> Mapping[str, str | bool]:
    if not isinstance(raw, dict) or set(raw) not in (
        {"filesystem", "network", "writes"},
        {"filesystem", "network", "writes", "shell"},
    ):
        raise AgentCardPolicyError("agent card execution policy has unsafe metadata")
    if raw.get("writes") is not False or raw.get("shell", False) is not False:
        raise AgentCardPolicyError("agent card requests write or shell capability")
    families = _skill_families(skills)
    if len(families) != 1:
        raise AgentCardPolicyError("agent card must describe exactly one capability family")
    family = next(iter(families))
    expected = {
        "workspace": ("configured-workspace-read-only", "control-plane-only"),
        "research": ("none", "configured-research-adapter-only"),
        "code_review": ("configured-repository-read-only", "control-plane-only"),
    }[family]
    if raw.get("filesystem") != expected[0] or raw.get("network") != expected[1]:
        raise AgentCardPolicyError("agent card execution policy is incompatible with its skills")
    if family == "code_review" and "shell" not in raw:
        raise AgentCardPolicyError("code review agent card must explicitly deny shell access")
    normalized: dict[str, str | bool] = {
        "filesystem": expected[0],
        "network": expected[1],
        "writes": False,
    }
    if "shell" in raw:
        normalized["shell"] = False
    return MappingProxyType(normalized)


def validate_agent_card_manifest(card: object) -> AgentCardPolicy:
    """Validate a static metadata-only worker card without trusting its claims."""

    required_fields = {
        "manifest_version",
        "name",
        "version",
        "protocol",
        "skills",
        "limits",
        "policy",
    }
    if not isinstance(card, dict) or set(card) != required_fields:
        raise AgentCardPolicyError("agent card fields do not match the metadata-only contract")
    if card.get("manifest_version") != "1":
        raise AgentCardPolicyError("agent card manifest version is incompatible")
    name = card.get("name")
    version = card.get("version")
    if not isinstance(name, str) or _NAME_RE.fullmatch(name) is None:
        raise AgentCardPolicyError("agent card name is invalid")
    if not isinstance(version, str) or _VERSION_RE.fullmatch(version) is None:
        raise AgentCardPolicyError("agent card version is invalid")
    protocol = _validate_protocol(card.get("protocol"))
    skills = _manifest_skills(card.get("skills"))
    metadata = _normalize_capability_metadata(card.get("limits"), skills)
    policy = _manifest_policy(card.get("policy"), skills)
    return AgentCardPolicy(
        protocol=protocol,
        skills=skills,
        capability_metadata=metadata,
        policy=policy,
    )
