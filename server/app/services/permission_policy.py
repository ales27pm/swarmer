from __future__ import annotations

import fnmatch
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Any, Literal

import yaml


class PermissionPolicyError(RuntimeError):
    pass


@dataclass(frozen=True)
class ProcessPolicy:
    backend: str
    binary: Path
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

    def __init__(
        self,
        *,
        protected_paths: tuple[str, ...],
        process: ProcessPolicy,
        tool_rules: Mapping[str, ToolPermissionRule],
    ) -> None:
        missing = self.SUPPORTED_TOOLS - set(tool_rules)
        extra = set(tool_rules) - self.SUPPORTED_TOOLS
        if missing or extra:
            raise PermissionPolicyError(
                f"tool_rules must define exactly the supported tools; missing={sorted(missing)}, "
                f"extra={sorted(extra)}"
            )
        self.protected_paths = protected_paths
        self.process = process
        self.tool_rules = MappingProxyType(dict(tool_rules))

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
        if not isinstance(protected_paths, list) or not all(
            isinstance(pattern, str) and pattern for pattern in protected_paths
        ):
            raise PermissionPolicyError("protected_paths must be a non-empty string list")
        if not isinstance(execution, dict):
            raise PermissionPolicyError("execution policy is required")
        if not isinstance(tool_rules, dict):
            raise PermissionPolicyError("tool_rules policy is required")

        process = cls._parse_process_policy(execution)
        return cls(
            protected_paths=tuple(protected_paths),
            process=process,
            tool_rules=cls._parse_tool_rules(tool_rules),
        )

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

    @staticmethod
    def _parse_process_policy(raw: dict[str, Any]) -> ProcessPolicy:
        backend = raw.get("backend")
        binary = raw.get("binary")
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
