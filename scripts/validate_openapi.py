from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator
from openapi_spec_validator import validate_spec

REPO_ROOT = Path(__file__).resolve().parents[1]
SERVER_ROOT = REPO_ROOT / "server"
sys.path.insert(0, str(SERVER_ROOT))

from app.main import API_VERSION, create_app
from app.settings import Settings

HTTP_METHODS = {"delete", "get", "head", "options", "patch", "post", "put", "trace"}


def operations(document: dict[str, Any]) -> set[tuple[str, str]]:
    return {
        (path, method)
        for path, item in document["paths"].items()
        for method in item
        if method in HTTP_METHODS
    }


def operation(document: dict[str, Any], path: str, method: str) -> dict[str, Any]:
    value = document["paths"][path][method]
    if not isinstance(value, dict):
        raise TypeError(f"OpenAPI operation {method.upper()} {path} must be an object")
    return value


def media_schema(container: dict[str, Any] | None) -> dict[str, Any] | None:
    if not container:
        return None
    content = container.get("content", {})
    if not isinstance(content, dict):
        return None
    media = content.get("application/json", {})
    if not isinstance(media, dict):
        return None
    schema = media.get("schema")
    return schema if isinstance(schema, dict) else None


def reference_name(schema: dict[str, Any] | None) -> str | None:
    if not schema:
        return None
    reference = schema.get("$ref")
    return reference.rsplit("/", 1)[-1] if isinstance(reference, str) else None


def resolved_schema(document: dict[str, Any], schema: dict[str, Any]) -> dict[str, Any]:
    reference = schema.get("$ref")
    if isinstance(reference, str):
        resolved = resolve_local_reference(document, reference)
        if not isinstance(resolved, dict):
            raise TypeError(f"schema reference must resolve to an object: {reference}")
        schema = {
            **resolved,
            **{key: value for key, value in schema.items() if key != "$ref"},
        }
    alternatives = schema.get("anyOf")
    if isinstance(alternatives, list) and len(alternatives) == 2:
        concrete = [
            option
            for option in alternatives
            if isinstance(option, dict) and option.get("type") != "null"
        ]
        if len(concrete) == 1:
            merged = {
                **concrete[0],
                **{key: value for key, value in schema.items() if key != "anyOf"},
            }
            return resolved_schema(document, merged)
    return schema


def successful_response(operation_value: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    responses = operation_value.get("responses", {})
    if not isinstance(responses, dict):
        raise TypeError("operation responses must be an object")
    successes = sorted(
        (str(code), response)
        for code, response in responses.items()
        if str(code).startswith("2") and isinstance(response, dict)
    )
    if len(successes) != 1:
        raise ValueError(f"operation must declare exactly one success response, got {successes}")
    return successes[0]


def effective_security(
    document: dict[str, Any], operation_value: dict[str, Any]
) -> list[dict[str, list[Any]]]:
    value = operation_value.get("security", document.get("security", []))
    if not isinstance(value, list):
        raise TypeError("operation security must be an array")
    return value


def validate_operation_contracts(spec: dict[str, Any], generated: dict[str, Any]) -> None:
    for path, method in sorted(operations(spec)):
        declared = operation(spec, path, method)
        actual = operation(generated, path, method)

        declared_code, declared_response = successful_response(declared)
        actual_code, actual_response = successful_response(actual)
        if declared_code != actual_code:
            raise ValueError(
                f"success status mismatch for {method.upper()} {path}: "
                f"OpenAPI={declared_code}, FastAPI={actual_code}"
            )
        declared_statuses = {str(code) for code in declared.get("responses", {})}
        actual_statuses = {str(code) for code in actual.get("responses", {})}
        if not actual_statuses.issubset(declared_statuses):
            raise ValueError(
                f"undocumented FastAPI statuses for {method.upper()} {path}: "
                f"{sorted(actual_statuses - declared_statuses)}"
            )
        if declared_code != "204" and media_schema(declared_response) is None:
            raise ValueError(f"missing JSON response schema for {method.upper()} {path}")

        declared_request = declared.get("requestBody")
        actual_request = actual.get("requestBody")
        if bool(declared_request) != bool(actual_request):
            raise ValueError(f"request-body presence mismatch for {method.upper()} {path}")
        if isinstance(actual_request, dict) and isinstance(declared_request, dict):
            if bool(declared_request.get("required")) != bool(actual_request.get("required")):
                raise ValueError(f"request-body required mismatch for {method.upper()} {path}")
            declared_model = reference_name(media_schema(declared_request))
            actual_model = reference_name(media_schema(actual_request))
            if declared_model != actual_model:
                raise ValueError(
                    f"request schema mismatch for {method.upper()} {path}: "
                    f"OpenAPI={declared_model}, FastAPI={actual_model}"
                )

        actual_response_model = reference_name(media_schema(actual_response))
        declared_response_model = reference_name(media_schema(declared_response))
        if actual_response_model and actual_response_model != declared_response_model:
            raise ValueError(
                f"response schema mismatch for {method.upper()} {path}: "
                f"OpenAPI={declared_response_model}, FastAPI={actual_response_model}"
            )

        if (path, method) in {("/health", "get"), ("/pairing/complete", "post")}:
            expected_security: list[dict[str, list[Any]]] = []
        elif (path, method) == ("/pairing/code", "post"):
            expected_security = [{"operatorToken": []}]
        elif (path, method) in {
            ("/pairing/finalize", "post"),
            ("/sync/bootstrap", "get"),
        }:
            expected_security = [{"bearerAuth": []}, {"pairingCandidateBearer": []}]
        elif path in {
            "/agents/{agent_id}/heartbeat",
            "/agents/{agent_id}/claim",
            "/agents/{agent_id}/jobs",
            "/agents/{agent_id}/jobs/{job_id}/heartbeat",
            "/agents/{agent_id}/jobs/{job_id}/result",
            "/agents/{agent_id}/jobs/{job_id}/capability-requests",
            "/agents/{agent_id}/jobs/{job_id}/capability-requests/{request_id}/poll",
        }:
            expected_security = [{"agentBearer": []}]
        else:
            expected_security = [{"bearerAuth": []}]
        if effective_security(spec, declared) != expected_security:
            raise ValueError(f"security declaration mismatch for {method.upper()} {path}")

        required_runtime_statuses: set[str] = set()
        if expected_security in (
            [{"bearerAuth": []}],
            [{"agentBearer": []}],
            [{"bearerAuth": []}, {"pairingCandidateBearer": []}],
        ):
            required_runtime_statuses.add("426")
        if (path, method) == ("/pairing/finalize", "post"):
            required_runtime_statuses.add("409")
        if (path, method) == ("/tasks/{task_id}/tool-calls", "post"):
            required_runtime_statuses.add("409")
        missing_runtime_statuses = required_runtime_statuses - declared_statuses
        if missing_runtime_statuses:
            raise ValueError(
                f"missing runtime statuses for {method.upper()} {path}: "
                f"{sorted(missing_runtime_statuses)}"
            )

        declared_parameters = parameters(spec, path, declared)
        actual_parameters = parameters(generated, path, actual)
        if set(declared_parameters) != set(actual_parameters):
            raise ValueError(
                f"parameter mismatch for {method.upper()} {path}: "
                f"OpenAPI={sorted(declared_parameters)}, FastAPI={sorted(actual_parameters)}"
            )
        for key, actual_parameter in actual_parameters.items():
            declared_parameter = declared_parameters[key]
            if bool(declared_parameter.get("required")) != bool(actual_parameter.get("required")):
                raise ValueError(f"parameter required mismatch for {method.upper()} {path} {key}")
            actual_schema = resolved_schema(generated, actual_parameter.get("schema", {}))
            declared_schema = resolved_schema(spec, declared_parameter.get("schema", {}))
            for constraint in ("type", "minimum", "maximum", "default", "enum"):
                if actual_schema.get(constraint) != declared_schema.get(constraint):
                    raise ValueError(
                        f"parameter {constraint} mismatch for {method.upper()} {path} {key}"
                    )


def validate_component_contracts(spec: dict[str, Any], generated: dict[str, Any]) -> None:
    declared_endpoint = spec["components"]["schemas"]["AgentCreate"]["properties"]["endpoint"]
    actual_endpoint = generated["components"]["schemas"]["AgentCreate"]["properties"]["endpoint"]
    for constraint in ("type", "format", "minLength", "maxLength", "pattern"):
        if declared_endpoint.get(constraint) != actual_endpoint.get(constraint):
            raise ValueError(
                f"component constraint mismatch for AgentCreate.endpoint {constraint}: "
                f"OpenAPI={declared_endpoint.get(constraint)!r}, "
                f"FastAPI={actual_endpoint.get(constraint)!r}"
            )

    response_credentials = {
        "PairingCandidateResponse.candidate_token": spec["components"]["schemas"][
            "PairingCandidateResponse"
        ]["properties"].get("candidate_token"),
        "AgentRegistration.credential": spec["components"]["schemas"]["AgentRegistration"]["allOf"][
            1
        ]["properties"].get("credential"),
    }
    for credential_name, value in response_credentials.items():
        if not isinstance(value, dict) or value.get("readOnly") is not True:
            raise ValueError(f"response-only credential {credential_name} must be readOnly")
        if value.get("writeOnly") is True:
            raise ValueError(f"response-only credential {credential_name} cannot be writeOnly")


def validate_json_schemas() -> int:
    count = 0
    for schema_path in sorted((REPO_ROOT / "schemas").glob("*.schema.json")):
        loaded = json.loads(schema_path.read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(loaded)
        count += 1
    return count


def references(value: Any) -> list[str]:
    found: list[str] = []
    if isinstance(value, dict):
        reference = value.get("$ref")
        if isinstance(reference, str):
            found.append(reference)
        for child in value.values():
            found.extend(references(child))
    elif isinstance(value, list):
        for child in value:
            found.extend(references(child))
    return found


def resolve_local_reference(document: dict[str, Any], reference: str) -> Any:
    if not reference.startswith("#/"):
        raise ValueError(f"only local OpenAPI references are supported: {reference}")
    current: Any = document
    for raw_part in reference[2:].split("/"):
        part = raw_part.replace("~1", "/").replace("~0", "~")
        if not isinstance(current, dict) or part not in current:
            raise ValueError(f"unresolved OpenAPI reference: {reference}")
        current = current[part]
    return current


def parameters(
    document: dict[str, Any], path: str, operation_value: dict[str, Any]
) -> dict[tuple[str, str], dict[str, Any]]:
    path_value = document["paths"][path]
    raw_parameters = [
        *path_value.get("parameters", []),
        *operation_value.get("parameters", []),
    ]
    result: dict[tuple[str, str], dict[str, Any]] = {}
    for raw in raw_parameters:
        if not isinstance(raw, dict):
            raise TypeError(f"parameter on {path} must be an object")
        reference = raw.get("$ref")
        value = resolve_local_reference(document, reference) if isinstance(reference, str) else raw
        if not isinstance(value, dict):
            raise TypeError(f"parameter reference on {path} must resolve to an object")
        key = (str(value.get("name")), str(value.get("in")))
        if key[1] == "header" and key[0].lower() in {
            "authorization",
            "x-mongars-operator-token",
        }:
            continue
        result[key] = value
    return result


def main() -> None:
    spec_path = REPO_ROOT / "api" / "openapi.yaml"
    loaded = yaml.safe_load(spec_path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        raise TypeError("OpenAPI document must be an object")
    spec: dict[str, Any] = loaded
    validate_spec(spec)

    declared_version = spec.get("info", {}).get("version")
    if declared_version != API_VERSION:
        raise ValueError(
            f"OpenAPI version {declared_version!r} does not match server {API_VERSION!r}"
        )

    refs = references(spec)
    for reference in refs:
        resolve_local_reference(spec, reference)

    generated = create_app(
        Settings(
            db_path=REPO_ROOT / "data" / "openapi-validation.db",
            workspace_root=REPO_ROOT / "workspace",
            permissions_path=REPO_ROOT / "configs" / "permissions.yaml",
        )
    ).openapi()
    expected = operations(spec)
    actual = operations(generated)
    if expected != actual:
        missing = sorted(actual - expected)
        stale = sorted(expected - actual)
        raise ValueError(f"OpenAPI route mismatch; missing={missing}, stale={stale}")

    validate_operation_contracts(spec, generated)
    validate_component_contracts(spec, generated)
    schema_count = validate_json_schemas()

    task_create = spec["components"]["schemas"]["TaskCreate"]
    if task_create.get("additionalProperties") is not False:
        raise ValueError("TaskCreate must reject undeclared caller-controlled fields")
    tool_proposal = spec["components"]["schemas"]["ToolProposal"]
    if set(tool_proposal.get("required", [])) != {"tool_name", "arguments", "summary"}:
        raise ValueError("ToolProposal must require the complete structured proposal")

    print(
        f"OpenAPI valid: {len(spec['paths'])} paths, "
        f"{len(expected)} operations, {len(refs)} references, "
        f"{schema_count} JSON schemas"
    )


if __name__ == "__main__":
    main()
