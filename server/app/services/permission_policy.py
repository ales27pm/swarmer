from __future__ import annotations

import fnmatch
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Any, Literal

import yaml

from app.services.agent_card import SUPPORTED_AGENT_SKILLS


class PermissionPolicyError(RuntimeError):
    pass


@dataclass(frozen=True)
class ProcessPolicy:
    backend: str
    binary: Path
    limiter_binary: Path
    network: str
    allowed_commands: frozenset[str]
    max_timeout_seconds: float
    max_output_bytes: int
    max_memory_bytes: int
    max_processes: int
    max_file_bytes: int
    max_open_files: int


PolicyDecision = Literal["allow", "ask", "deny"]
RiskLevel = Literal["low", "medium", "high"]


@dataclass(frozen=True)
class ToolPermissionRule:
    id: str
    description: str
    decision: PolicyDecision
    risk: RiskLevel
    approval_ttl_seconds: int | None = None

    def approval_context(self) -> dict[str, str]:
        return {
            "rule_id": self.id,
            "decision": self.decision,
            "reason": self.description,
        }


@dataclass(frozen=True)
class WorkerPermissionRule:
    id: str
    description: str
    decision: Literal["allow", "deny"]
    risk: Literal["low"]
    auto_redistribute: bool


class PermissionPolicy:
    """Validated, fail-closed projection of the executable permission policy."""

    SUPPORTED_TOOLS = frozenset(
        {
            "workspace.list_dir",
            "workspace.read_text",
            "workspace.write_text",
            "process.run",
        }
    )
    SUPPORTED_IPHONE_CAPABILITIES = frozenset(
        {
            "iphone.location.current",
            "iphone.contacts.lookup",
            "iphone.calendar.events",
            "iphone.photos.pick",
            "iphone.mail.compose",
            "iphone.sms.compose",
        }
    )

    def __init__(
        self,
        *,
        protected_paths: tuple[str, ...],
        process: ProcessPolicy,
        tool_rules: Mapping[str, ToolPermissionRule],
        capability_rules: Mapping[str, ToolPermissionRule] | None = None,
        worker_skill_rules: Mapping[str, WorkerPermissionRule] | None = None,
    ) -> None:
        missing = self.SUPPORTED_TOOLS - set(tool_rules)
        extra = set(tool_rules) - self.SUPPORTED_TOOLS
        if missing or extra:
            raise PermissionPolicyError(
                f"tool_rules must define exactly the supported tools; missing={sorted(missing)}, "
                f"extra={sorted(extra)}"
            )
        effective_capability_rules = capability_rules or {
            name: ToolPermissionRule(
                id=f"deny-unconfigured-{name.replace('.', '-')}",
                description="Native capability policy was not explicitly configured.",
                decision="deny",
                risk="high",
            )
            for name in self.SUPPORTED_IPHONE_CAPABILITIES
        }
        missing_capabilities = self.SUPPORTED_IPHONE_CAPABILITIES - set(effective_capability_rules)
        extra_capabilities = set(effective_capability_rules) - self.SUPPORTED_IPHONE_CAPABILITIES
        if missing_capabilities or extra_capabilities:
            raise PermissionPolicyError(
                "capability_rules must define exactly the supported iPhone capabilities; "
                f"missing={sorted(missing_capabilities)}, extra={sorted(extra_capabilities)}"
            )
        self.protected_paths = protected_paths
        self.process = process
        self.tool_rules = MappingProxyType(dict(tool_rules))
        self.capability_rules = MappingProxyType(dict(effective_capability_rules))
        effective_worker_rules = worker_skill_rules or {
            name: WorkerPermissionRule(
                id=f"deny-unconfigured-{name.replace('.', '-')}",
                description="Remote worker skill was not explicitly configured.",
                decision="deny",
                risk="low",
                auto_redistribute=False,
            )
            for name in SUPPORTED_AGENT_SKILLS
        }
        missing_worker_skills = SUPPORTED_AGENT_SKILLS - set(effective_worker_rules)
        extra_worker_skills = set(effective_worker_rules) - SUPPORTED_AGENT_SKILLS
        if missing_worker_skills or extra_worker_skills:
            raise PermissionPolicyError(
                "worker_skill_rules must define exactly the supported remote skills; "
                f"missing={sorted(missing_worker_skills)}, extra={sorted(extra_worker_skills)}"
            )
        self.worker_skill_rules = MappingProxyType(dict(effective_worker_rules))

    @classmethod
    def from_yaml(cls, path: Path) -> PermissionPolicy:
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as exc:
            raise PermissionPolicyError(f"cannot load permission policy: {path}") from exc

        if not isinstance(raw, dict):
            raise PermissionPolicyError("permission policy must be an object")
        protected_paths = raw.get("protected_paths")
        execution = raw.get("execution")
        tool_rules = raw.get("tool_rules")
        capability_rules = raw.get("capability_rules")
        worker_skill_rules = raw.get("worker_skill_rules")
        if not isinstance(protected_paths, list) or not all(
            isinstance(pattern, str) and pattern for pattern in protected_paths
        ):
            raise PermissionPolicyError("protected_paths must be a non-empty string list")
        if not isinstance(execution, dict):
            raise PermissionPolicyError("execution policy is required")
        if not isinstance(tool_rules, dict):
            raise PermissionPolicyError("tool_rules policy is required")
        if not isinstance(capability_rules, dict):
            raise PermissionPolicyError("capability_rules policy is required")
        if not isinstance(worker_skill_rules, dict):
            raise PermissionPolicyError("worker_skill_rules policy is required")

        process = cls._parse_process_policy(execution)
        return cls(
            protected_paths=tuple(protected_paths),
            process=process,
            tool_rules=cls._parse_tool_rules(tool_rules),
            capability_rules=cls._parse_tool_rules(capability_rules),
            worker_skill_rules=cls._parse_worker_skill_rules(worker_skill_rules),
        )

    @classmethod
    def _parse_worker_skill_rules(cls, raw: dict[str, Any]) -> dict[str, WorkerPermissionRule]:
        parsed: dict[str, WorkerPermissionRule] = {}
        for skill, value in raw.items():
            if not isinstance(skill, str) or not isinstance(value, dict):
                raise PermissionPolicyError("each worker skill rule must be a named object")
            if set(value) != {
                "id",
                "description",
                "decision",
                "risk",
                "auto_redistribute",
            }:
                raise PermissionPolicyError(f"worker skill rule {skill} has invalid fields")
            rule_id = value.get("id")
            description = value.get("description")
            decision = value.get("decision")
            risk = value.get("risk")
            auto_redistribute = value.get("auto_redistribute")
            if not isinstance(rule_id, str) or not rule_id:
                raise PermissionPolicyError(f"worker skill rule {skill} requires a stable id")
            if not isinstance(description, str) or not description.strip():
                raise PermissionPolicyError(f"worker skill rule {skill} requires a description")
            if decision not in {"allow", "deny"} or risk != "low":
                raise PermissionPolicyError(f"worker skill rule {skill} is not fail-closed")
            if not isinstance(auto_redistribute, bool):
                raise PermissionPolicyError(
                    f"worker skill rule {skill} requires an auto_redistribute boolean"
                )
            if auto_redistribute and skill not in {"workspace.list_dir", "workspace.read_text"}:
                raise PermissionPolicyError(
                    f"worker skill rule {skill} cannot be automatically redistributed"
                )
            parsed[skill] = WorkerPermissionRule(
                id=rule_id,
                description=description.strip(),
                decision=decision,
                risk=risk,
                auto_redistribute=auto_redistribute,
            )
        return parsed

    @classmethod
    def _parse_tool_rules(cls, raw: dict[str, Any]) -> dict[str, ToolPermissionRule]:
        parsed: dict[str, ToolPermissionRule] = {}
        for tool_name, value in raw.items():
            if not isinstance(tool_name, str) or not isinstance(value, dict):
                raise PermissionPolicyError("each tool rule must be a named object")
            rule_id = value.get("id")
            description = value.get("description")
            decision = value.get("decision")
            risk = value.get("risk")
            ttl = value.get("approval_ttl_seconds")
            if not isinstance(rule_id, str) or not rule_id:
                raise PermissionPolicyError(f"tool rule {tool_name} requires a stable id")
            if not isinstance(description, str) or not description.strip():
                raise PermissionPolicyError(f"tool rule {tool_name} requires a description")
            if decision not in {"allow", "ask", "deny"}:
                raise PermissionPolicyError(f"tool rule {tool_name} has an invalid decision")
            if risk not in {"low", "medium", "high"}:
                raise PermissionPolicyError(f"tool rule {tool_name} has an invalid risk")
            if decision == "ask":
                if not isinstance(ttl, int) or isinstance(ttl, bool) or not 30 <= ttl <= 900:
                    raise PermissionPolicyError(
                        f"tool rule {tool_name} approval_ttl_seconds must be between 30 and 900"
                    )
            elif ttl is not None:
                raise PermissionPolicyError(
                    f"tool rule {tool_name} cannot set approval_ttl_seconds without decision ask"
                )
            parsed[tool_name] = ToolPermissionRule(
                id=rule_id,
                description=description.strip(),
                decision=decision,
                risk=risk,
                approval_ttl_seconds=ttl,
            )
        return parsed

    def evaluate_tool(self, tool_name: str) -> ToolPermissionRule:
        try:
            return self.tool_rules[tool_name]
        except KeyError as exc:
            raise PermissionPolicyError(f"tool is not covered by policy: {tool_name}") from exc

    def evaluate_capability(self, capability_name: str) -> ToolPermissionRule:
        try:
            return self.capability_rules[capability_name]
        except KeyError as exc:
            raise PermissionPolicyError(
                f"iPhone capability is not covered by policy: {capability_name}"
            ) from exc

    def evaluate_worker_skill(self, skill: str) -> WorkerPermissionRule:
        try:
            return self.worker_skill_rules[skill]
        except KeyError as exc:
            raise PermissionPolicyError(
                f"remote worker skill is not covered by policy: {skill}"
            ) from exc

    def reload_worker_skill_rules(self, path: Path) -> bool:
        """Atomically install worker rules from a complete, valid policy file.

        Long-lived worker leases intentionally retain the authorization under
        which they were issued. New claims and expired-lease redistribution see
        this new immutable mapping immediately. Invalid reloads raise and leave
        the last known-valid mapping installed.
        """

        candidate = type(self).from_yaml(path)
        changed = dict(candidate.worker_skill_rules) != dict(self.worker_skill_rules)
        self.worker_skill_rules = candidate.worker_skill_rules
        return changed

    @staticmethod
    def _parse_process_policy(raw: dict[str, Any]) -> ProcessPolicy:
        backend = raw.get("backend")
        binary = raw.get("binary")
        limiter_binary = raw.get("limiter_binary")
        network = raw.get("network")
        commands = raw.get("allowed_commands")
        timeout = raw.get("max_timeout_seconds")
        output_bytes = raw.get("max_output_bytes")
        memory_bytes = raw.get("max_memory_bytes")
        processes = raw.get("max_processes")
        file_bytes = raw.get("max_file_bytes")
        open_files = raw.get("max_open_files")

        if backend != "bubblewrap":
            raise PermissionPolicyError("execution.backend must be bubblewrap")
        if not isinstance(binary, str) or not Path(binary).is_absolute():
            raise PermissionPolicyError("execution.binary must be an absolute path")
        if (
            not isinstance(limiter_binary, str)
            or not Path(limiter_binary).is_absolute()
            or Path(limiter_binary).name != "prlimit"
        ):
            raise PermissionPolicyError("execution.limiter_binary must be an absolute prlimit path")
        if network != "deny":
            raise PermissionPolicyError("process network access must be denied")
        if not isinstance(commands, list) or not commands:
            raise PermissionPolicyError("execution.allowed_commands must be non-empty")
        if not all(
            isinstance(command, str)
            and command
            and command == Path(command).name
            and "/" not in command
            for command in commands
        ):
            raise PermissionPolicyError("allowed commands must be executable names, not paths")
        if not isinstance(timeout, (int, float)) or not 0.1 <= float(timeout) <= 300:
            raise PermissionPolicyError("max_timeout_seconds must be between 0.1 and 300")
        if not isinstance(output_bytes, int) or not 1024 <= output_bytes <= 1_048_576:
            raise PermissionPolicyError("max_output_bytes must be between 1024 and 1048576")
        if not isinstance(memory_bytes, int) or not 67_108_864 <= memory_bytes <= 4_294_967_296:
            raise PermissionPolicyError("max_memory_bytes must be between 64 MiB and 4 GiB")
        if not isinstance(processes, int) or not 1 <= processes <= 256:
            raise PermissionPolicyError("max_processes must be between 1 and 256")
        if not isinstance(file_bytes, int) or not 1_048_576 <= file_bytes <= 1_073_741_824:
            raise PermissionPolicyError("max_file_bytes must be between 1 MiB and 1 GiB")
        if not isinstance(open_files, int) or not 32 <= open_files <= 1024:
            raise PermissionPolicyError("max_open_files must be between 32 and 1024")

        return ProcessPolicy(
            backend=backend,
            binary=Path(binary),
            limiter_binary=Path(limiter_binary),
            network=network,
            allowed_commands=frozenset(commands),
            max_timeout_seconds=float(timeout),
            max_output_bytes=output_bytes,
            max_memory_bytes=memory_bytes,
            max_processes=processes,
            max_file_bytes=file_bytes,
            max_open_files=open_files,
        )

    def is_protected(self, relative_path: Path | str) -> bool:
        value = PurePosixPath(str(relative_path).replace("\\", "/")).as_posix()
        value = value.removeprefix("./")
        if not value:
            return False
        for pattern in self.protected_paths:
            normalized = pattern.replace("\\", "/")
            candidates = (normalized, normalized.removeprefix("**/"))
            if any(
                fnmatch.fnmatchcase(value, candidate) or PurePosixPath(value).match(candidate)
                for candidate in candidates
            ):
                return True
        return False

    def validate_command(self, argv: list[str]) -> None:
        command = argv[0]
        if command != Path(command).name or "/" in command or "\\" in command:
            raise PermissionPolicyError("process executable must be an allowlisted name")
        if command not in self.process.allowed_commands:
            raise PermissionPolicyError(f"process executable is not allowed: {command}")
