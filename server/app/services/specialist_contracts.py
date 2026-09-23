"""Bounded arguments for explicitly parameterized specialist operations."""

from __future__ import annotations

import json
import re
from typing import Any

from app.services.project_contracts import validate_project_path
from app.services.swift_contracts import SWIFT_SKILLS, swift_argument_schema, validate_swift_payload

SQLITE_SKILLS = frozenset(
    f"database.sqlite.{op}" for op in ("inspect", "query", "create", "backup", "migrate")
)
PERSONAL_SKILLS = frozenset({"crm.command", "documents.extract"})
SPECIALIST_SKILLS = SQLITE_SKILLS | PERSONAL_SKILLS | SWIFT_SKILLS


def specialist_argument_schema(skill: str) -> dict[str, Any]:
    """Guide constrained decoding; the semantic validator remains authoritative."""
    text = {"type": "string", "minLength": 1, "maxLength": 500}
    path = {**text, "description": "Observed relative path within the operator workspace"}

    def obj(fields: dict[str, Any], required: list[str]) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": fields,
            "required": required,
            "additionalProperties": False,
        }

    if skill in SQLITE_SKILLS:
        fields: dict[str, Any] = {"path": path}
        required = ["path"]
        operation = skill.rsplit(".", 1)[1]
        sql = {"type": "string", "minLength": 1, "maxLength": 16000}
        parameters = {"anyOf": [{"type": "array"}, {"type": "object"}]}
        if operation == "query":
            fields.update(sql=sql, parameters=parameters)
            required.append("sql")
        if operation == "backup":
            fields["destination"] = path
            required.append("destination")
        if operation in {"create", "migrate"}:
            fields.update(
                migration_id=text,
                statements={
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 32,
                    "items": obj({"sql": sql, "parameters": parameters}, ["sql"]),
                },
            )
            required += ["migration_id", "statements"]
        return obj(fields, required)
    if skill in SWIFT_SKILLS:
        return swift_argument_schema()
    if skill == "documents.extract":
        return obj(
            {
                "path": path,
                "max_characters": {"type": "integer", "minimum": 1, "maximum": 100000},
                "max_pages": {"type": "integer", "minimum": 1, "maximum": 200},
            },
            ["path"],
        )
    if skill == "crm.command":
        branches = []
        for action in ("search", "read", "create", "update"):
            fields = {
                "operation": {
                    "type": "string",
                    "enum": [
                        f"{r}.{action}"
                        for r in ("organizations", "contacts", "deals", "tasks", "documents")
                    ],
                }
            }
            required = ["operation"]
            if action == "search":
                fields.update(
                    query={"type": "string", "maxLength": 200},
                    limit={"type": "integer", "minimum": 1, "maximum": 50},
                )
            if action in {"read", "update"}:
                fields["id"] = {**text, "maxLength": 200}
                required.append("id")
            if action in {"create", "update"}:
                fields.update(
                    data={"type": "object"},
                    idempotency_key={"type": "string", "pattern": "^[a-zA-Z0-9._:-]{8,128}$"},
                )
                required += ["data", "idempotency_key"]
            branches.append(obj(fields, required))
        return {"anyOf": branches}
    raise ValueError("unsupported specialist")


def validate_specialist_payload(skill: str, payload: dict[str, Any]) -> dict[str, Any]:
    if len(json.dumps(payload, allow_nan=False).encode()) > 32000:
        raise ValueError("specialist payload exceeds budget")
    if skill in SQLITE_SKILLS:
        operation = skill.rsplit(".", 1)[1]
        fields = {"path"}
        required = {"path"}
        if operation == "query":
            fields |= {"sql", "parameters"}
            required.add("sql")
        if operation in {"create", "migrate"}:
            fields |= {"migration_id", "statements"}
            required |= {"migration_id", "statements"}
        if operation == "backup":
            fields.add("destination")
            required.add("destination")
        if not required <= payload.keys() or payload.keys() - fields:
            raise ValueError("SQLite operation fields invalid")
        validate_project_path(payload["path"])
        if "destination" in payload:
            validate_project_path(payload["destination"])
        if operation == "query" and (
            not isinstance(payload["sql"], str) or not payload["sql"].strip()
        ):
            raise ValueError("SQL required")
        if "parameters" in payload and not isinstance(payload["parameters"], (list, dict)):
            raise ValueError("SQL parameters invalid")
        if operation in {"create", "migrate"}:
            if not isinstance(payload["migration_id"], str) or not payload["migration_id"]:
                raise ValueError("stable migration ID required")
            statements = payload["statements"]
            if not isinstance(statements, list) or not 1 <= len(statements) <= 32:
                raise ValueError("bounded statements required")
            for statement in statements:
                if not isinstance(statement, dict) or statement.keys() - {"sql", "parameters"}:
                    raise ValueError("invalid statement")
                if not isinstance(statement.get("sql"), str) or not statement["sql"].strip():
                    raise ValueError("SQL required")
    elif skill in SWIFT_SKILLS:
        return validate_swift_payload(payload)
    elif skill == "documents.extract":
        if "path" not in payload or payload.keys() - {"path", "max_characters", "max_pages"}:
            raise ValueError("document path required")
        validate_project_path(payload["path"])
        for key, limit in (("max_characters", 100000), ("max_pages", 200)):
            if key in payload and (type(payload[key]) is not int or not 1 <= payload[key] <= limit):
                raise ValueError("document extraction limit invalid")
        if not payload["path"].lower().endswith((".pdf", ".docx", ".txt", ".md")):
            raise ValueError("unsupported document format")
    elif skill == "crm.command":
        if payload.keys() - {"operation", "id", "query", "limit", "data", "idempotency_key"}:
            raise ValueError("CRM command fields invalid")
        operation = payload.get("operation", "")
        allowed = {
            f"{resource}.{action}"
            for resource in ("organizations", "contacts", "deals", "tasks", "documents")
            for action in ("search", "read", "create", "update")
        }
        if not isinstance(operation, str) or operation not in allowed:
            raise ValueError("CRM operation not permitted")
        writing = operation.endswith((".create", ".update"))
        idempotency_key = payload.get("idempotency_key")
        if (writing or idempotency_key is not None) and (
            not isinstance(idempotency_key, str)
            or not re.fullmatch(r"[a-zA-Z0-9._:-]{8,128}", idempotency_key)
        ):
            raise ValueError("stable idempotency key required")
        if operation.endswith((".read", ".update")) and not payload.get("id"):
            raise ValueError("CRM entity ID required")
        if "id" in payload and (
            not isinstance(payload["id"], str) or not 1 <= len(payload["id"]) <= 200
        ):
            raise ValueError("CRM entity ID invalid")
        if writing and not isinstance(payload.get("data"), dict):
            raise ValueError("CRM write data required")
        if "query" in payload and (
            not isinstance(payload["query"], str) or len(payload["query"]) > 200
        ):
            raise ValueError("CRM query invalid")
        if "limit" in payload and (
            type(payload["limit"]) is not int or not 1 <= payload["limit"] <= 50
        ):
            raise ValueError("CRM result limit invalid")
    else:
        raise ValueError("unsupported specialist")
    return payload
