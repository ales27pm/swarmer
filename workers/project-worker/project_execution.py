"""Bounded worker-reported measurements; no producer trust or promotion is implied."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

MAX_RECEIPT_BYTES = 16_384
PROFILE_COMMANDS = {
    "python_build": ["python", "-m", "compileall", "-q", "."],
    "python_test": ["python", "-m", "pytest", "-q"],
    "node_build": ["npm", "run", "build"],
    "node_test": ["node", "--test"],
}
OBSERVATION_HASHES = (
    "source_before_sha256",
    "source_after_sha256",
    "workspace_before_sha256",
    "workspace_after_sha256",
    "dependency_before_sha256",
    "dependency_after_sha256",
    "harness_sha256",
)
MEASUREMENT_ERRORS = {
    "source_unavailable",
    "workspace_unavailable",
    "environment_unbound",
    "harness_unavailable",
}
INCOMPLETE_REASONS = MEASUREMENT_ERRORS | {
    "legacy_harness",
    "invalid_measurement",
    "profile_not_executed",
    "source_mismatch",
    "runner_identity_unavailable",
    "profile_interrupted",
}


def canonical_sha(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode()
    ).hexdigest()


def _require(condition: bool) -> None:
    if not condition:
        raise ValueError("invalid project execution measurement")


def _hash(value: object, *, nullable: bool = False) -> None:
    _require(
        (nullable and value is None)
        or (isinstance(value, str) and re.fullmatch(r"[a-f0-9]{64}", value) is not None)
    )


def _integer(value: object, maximum: int, minimum: int = 0) -> None:
    _require(type(value) is int and minimum <= value <= maximum)


def observation_value(value: object) -> dict[str, Any]:
    _require(isinstance(value, dict))
    assert isinstance(value, dict)
    _require(
        set(value)
        == {
            "schema_version",
            *OBSERVATION_HASHES,
            "source_unchanged",
            "environment_unchanged",
            "errors",
        }
    )
    _require(value["schema_version"] == "project-check-observation-v1")
    for key in OBSERVATION_HASHES:
        _hash(value[key], nullable=True)
    errors = value["errors"]
    _require(
        isinstance(errors, list)
        and len(errors) <= 4
        and all(isinstance(e, str) and e in MEASUREMENT_ERRORS for e in errors)
    )
    _require(errors == sorted(set(errors)))
    for prefix, reason in (
        ("source", "source_unavailable"),
        ("workspace", "workspace_unavailable"),
        ("dependency", "environment_unbound"),
    ):
        absent = any(value[f"{prefix}_{when}_sha256"] is None for when in ("before", "after"))
        _require(absent == (reason in errors))
    _require((value["harness_sha256"] is None) == ("harness_unavailable" in errors))
    for prefix, key in (
        ("source", "source_unchanged"),
        ("dependency", "environment_unchanged"),
    ):
        before, after = (value[f"{prefix}_{when}_sha256"] for when in ("before", "after"))
        expected = None if before is None or after is None else before == after
        _require(value[key] is expected)
    return json.loads(json.dumps(value))  # type: ignore[no-any-return]


def incomplete_reasons(receipt: dict[str, Any]) -> list[str]:
    reasons = set()
    if receipt["runner_sha256"] is None:
        reasons.add("runner_identity_unavailable")
    if [p["profile"] for p in receipt["profiles"]] != receipt["profiles_expected"]:
        reasons.add("profile_not_executed")
    for profile in receipt["profiles"]:
        observation = profile["observation"]
        if observation is None:
            reasons.add(profile["measurement_error"])
        else:
            reasons.update(observation["errors"])
            if observation["source_before_sha256"] not in (
                None,
                receipt["source_sha256"],
            ):
                reasons.add("source_mismatch")
    return sorted(reasons)


def execution_receipt_value(value: object) -> dict[str, Any]:
    _require(isinstance(value, dict))
    assert isinstance(value, dict)
    _require(
        set(value)
        == {
            "schema_version",
            "origin",
            "run_id",
            "runtime",
            "source_sha256",
            "runtime_image_id",
            "runner_sha256",
            "policy_sha256",
            "profiles_expected",
            "profiles",
            "observation_status",
            "incomplete_reasons",
        }
    )
    _require(value["schema_version"] == "project-execution-receipt-v1")
    _require(value["origin"] == "worker_reported_measurement")
    _require(
        isinstance(value["run_id"], str)
        and re.fullmatch(r"[a-f0-9]{32}", value["run_id"]) is not None
    )
    _require(value["runtime"] in ("python", "node", "python_node"))
    _hash(value["source_sha256"])
    _hash(value["runner_sha256"], nullable=True)
    _hash(value["policy_sha256"])
    _require(
        isinstance(value["runtime_image_id"], str)
        and re.fullmatch(r"sha256:[a-f0-9]{64}", value["runtime_image_id"]) is not None
    )
    expected = (["python_build", "python_test"] if value["runtime"] != "node" else []) + (
        ["node_build", "node_test"] if value["runtime"] != "python" else []
    )
    _require(value["profiles_expected"] == expected)
    profiles = value["profiles"]
    _require(isinstance(profiles, list) and len(profiles) <= len(expected))
    indices = []
    for index, profile in enumerate(profiles):
        _require(
            isinstance(profile, dict)
            and set(profile)
            == {
                "profile",
                "check_index",
                "exit_code",
                "tests_executed",
                "test_failures",
                "duration_ms",
                "observation",
                "measurement_error",
            }
        )
        _require(profile["profile"] == expected[index])
        _integer(profile["check_index"], 11)
        indices.append(profile["check_index"])
        interrupted = profile["measurement_error"] == "profile_interrupted"
        if interrupted:
            _require(
                all(
                    profile[key] is None for key in ("exit_code", "tests_executed", "test_failures")
                )
            )
        else:
            _integer(profile["exit_code"], 255, -255)
            _integer(profile["tests_executed"], 100_000)
            _integer(profile["test_failures"], 100_000)
        _integer(profile["duration_ms"], 900_000)
        if profile["exit_code"] == 0 and profile["profile"].endswith("_test"):
            _require(profile["tests_executed"] > 0 and profile["test_failures"] == 0)
        if profile["profile"].endswith("_build") and not interrupted:
            _require(profile["tests_executed"] == 0)
        if profile["observation"] is None:
            _require(
                profile["measurement_error"]
                in ("legacy_harness", "invalid_measurement", "profile_interrupted")
            )
        else:
            observation_value(profile["observation"])
            _require(profile["measurement_error"] is None)
    _require(indices == sorted(set(indices)))
    reasons = incomplete_reasons(value)
    _require(value["incomplete_reasons"] == reasons)
    _require(value["observation_status"] == ("incomplete" if reasons else "complete"))
    raw = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    _require(len(raw.encode()) <= MAX_RECEIPT_BYTES)
    return json.loads(raw)  # type: ignore[no-any-return]


def bind_execution_receipt(
    value: object, *, source_sha256: str, runtime: str, checks: list[dict[str, Any]]
) -> dict[str, Any]:
    receipt = execution_receipt_value(value)
    _require(receipt["source_sha256"] == source_sha256 and receipt["runtime"] == runtime)
    for profile in receipt["profiles"]:
        _require(profile["check_index"] < len(checks))
        check = checks[profile["check_index"]]
        _require(check["command"] == PROFILE_COMMANDS[profile["profile"]])
        _require(check["duration_ms"] == profile["duration_ms"])
        if profile["measurement_error"] == "profile_interrupted":
            _require(check["status"] == "failed")
        else:
            _require(check["exit_code"] == profile["exit_code"])
            _require(check["status"] == ("passed" if profile["exit_code"] == 0 else "failed"))
    return receipt
