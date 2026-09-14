from __future__ import annotations

import hashlib
import json
import sqlite3
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.services.activity_catalog import (
    GOAL_READY_SKILLS,
    PARAMETER_BOUND_SKILLS,
    ActivityCatalogError,
    ActivityCatalogService,
    parse_activity_catalog,
)
from app.services.agent_card import SUPPORTED_AGENT_SKILLS
from app.services.goal_manager import GoalManager, GoalManagerConflict
from app.services.permission_policy import PermissionPolicy

NOW = datetime(2030, 1, 1, 12, tzinfo=UTC)


def _minimal_catalog() -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "domains": [{"id": "coding", "title": "Code", "description": "Développement"}],
        "skills": [
            {
                "id": "workspace.list_dir",
                "title": "Lister",
                "description": "Lire un dossier",
                "inputs": ["Dossier"],
                "output": "Fichiers",
                "requirements": [],
                "execution": {"kind": "worker", "target": "workspace.list_dir"},
            }
        ],
        "roles": [
            {
                "id": "reviewer",
                "domain_id": "coding",
                "title": "Réviseur",
                "description": "Inspecter",
                "skill_ids": ["workspace.list_dir"],
                "examples": ["Lister le projet"],
            }
        ],
    }


def _register(client: TestClient, headers: dict[str, str], skill: str) -> str:
    response = client.post(
        "/agents/register",
        headers=headers,
        json={"name": "Worker catalogue", "endpoint": "https://worker.invalid", "skills": [skill]},
    )
    assert response.status_code == 201, response.text
    return str(response.json()["id"])


def _skills(client: TestClient, headers: dict[str, str]) -> dict[str, Any]:
    response = client.get("/catalog/activities", headers=headers)
    assert response.status_code == 200, response.text
    return {skill["id"]: skill["availability"] for skill in response.json()["skills"]}


def test_activity_catalog_requires_a_paired_device(client: TestClient) -> None:
    assert client.get("/catalog/activities").status_code == 401


def test_activity_catalog_exposes_definitions_and_read_only_availability(
    client: TestClient, paired_headers: dict[str, str]
) -> None:
    response = client.get("/catalog/activities", headers=paired_headers)
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    payload = response.json()
    assert payload["schema_version"] == "1.0" and payload["generated_at"]
    assert payload["domains"] and payload["roles"] and payload["skills"]
    for skill in payload["skills"]:
        assert skill["availability"]["reason"]
        assert skill["availability"]["agent_ids"] == []
        kind = skill["execution"]["kind"]
        assert (
            skill["availability"]["state"]
            == {"worker": "worker_unavailable", "iphone": "iphone_request", "planned": "planned"}[
                kind
            ]
        )
    assert {
        s["execution"]["target"] for s in payload["skills"] if s["execution"]["kind"] == "worker"
    } == SUPPORTED_AGENT_SKILLS
    assert {
        s["execution"]["target"] for s in payload["skills"] if s["execution"]["kind"] == "iphone"
    } == PermissionPolicy.SUPPORTED_IPHONE_CAPABILITIES


@pytest.mark.parametrize(
    "condition", ["stale", "future", "offline", "draining", "protocol", "malformed"]
)
def test_catalogue_does_not_advertise_an_ineligible_worker(
    client: TestClient, paired_headers: dict[str, str], test_app: FastAPI, condition: str
) -> None:
    agent_id = _register(client, paired_headers, "workspace.list_dir")
    test_app.state.activity_catalog.clock = lambda: NOW
    with sqlite3.connect(test_app.state.settings.db_path) as db:
        db.execute(
            "UPDATE agents SET status='online',last_seen_at=? WHERE id=?",
            (NOW.isoformat(), agent_id),
        )
        if condition in {"stale", "future"}:
            age = timedelta(seconds=90) if condition == "stale" else timedelta(seconds=-1)
            db.execute(
                "UPDATE agents SET last_seen_at=? WHERE id=?", ((NOW - age).isoformat(), agent_id)
            )
        elif condition == "protocol":
            db.execute("UPDATE agents SET supported_protocol_version='old' WHERE id=?", (agent_id,))
        elif condition == "malformed":
            db.execute(
                "UPDATE agents SET skills_json=? WHERE id=?",
                ('["workspace.list_dir",false]', agent_id),
            )
        else:
            db.execute("UPDATE agents SET status=? WHERE id=?", (condition, agent_id))
    availability = _skills(client, paired_headers)["workspace.list_dir"]
    assert availability["state"] == "worker_unavailable" and availability["agent_ids"] == []


def test_catalogue_separates_goal_mapping_and_parameters_and_keeps_busy_workers(
    client: TestClient, paired_headers: dict[str, str], test_app: FastAPI
) -> None:
    expected = {}
    for skill in (
        "workspace.list_dir",
        "workspace.read_text",
        "code_review.static_analysis",
        "code.build_project",
    ):
        expected[skill] = _register(client, paired_headers, skill)
    test_app.state.activity_catalog.clock = lambda: NOW
    with sqlite3.connect(test_app.state.settings.db_path) as db:
        db.execute(
            "UPDATE agents SET status='busy',last_seen_at=?",
            (NOW.isoformat(),),
        )
    skills = _skills(client, paired_headers)
    for skill, agent_id in expected.items():
        assert skills[skill]["agent_ids"] == [agent_id]
        assert skills[skill]["state"] == (
            "parameters_required" if skill in PARAMETER_BOUND_SKILLS else "goal_ready"
        )
    with sqlite3.connect(test_app.state.settings.db_path) as db:
        for table in ("goal_runs", "agent_jobs", "goal_model_calls", "plan_nodes"):
            assert db.execute("SELECT COUNT(*) FROM " + table).fetchone() == (0,)


def test_catalogue_caps_discovery_ids_without_changing_registered_workers(
    client: TestClient, paired_headers: dict[str, str], test_app: FastAPI
) -> None:
    test_app.state.activity_catalog.clock = lambda: NOW
    with sqlite3.connect(test_app.state.settings.db_path) as db:
        db.executemany(
            """INSERT INTO agents(id,name,version,endpoint,status,skills_json,last_seen_at,
            created_at,updated_at) VALUES(?,'Catalogue worker','1.0.0','https://worker.invalid',
            'online','["workspace.list_dir"]',?,?,?)""",
            [
                (f"agt_{index:03d}", NOW.isoformat(), NOW.isoformat(), NOW.isoformat())
                for index in range(250, -1, -1)
            ],
        )
    availability = _skills(client, paired_headers)["workspace.list_dir"]
    assert availability["state"] == "goal_ready"
    assert availability["agent_ids"] == [f"agt_{index:03d}" for index in range(250)]
    with sqlite3.connect(test_app.state.settings.db_path) as db:
        assert db.execute("SELECT COUNT(*) FROM agents").fetchone() == (251,)
        assert db.execute("SELECT COUNT(*) FROM agent_jobs").fetchone() == (0,)


def test_catalogue_reloads_durable_worker_denial_and_respects_native_denial(
    client: TestClient, paired_headers: dict[str, str], test_app: FastAPI
) -> None:
    agent_id = _register(client, paired_headers, "workspace.list_dir")
    service = test_app.state.activity_catalog
    service.clock = lambda: NOW
    with sqlite3.connect(test_app.state.settings.db_path) as db:
        db.execute(
            "UPDATE agents SET status='online',last_seen_at=? WHERE id=?",
            (NOW.isoformat(), agent_id),
        )
    assert _skills(client, paired_headers)["workspace.list_dir"]["state"] == "goal_ready"
    with sqlite3.connect(test_app.state.settings.db_path) as db:
        rules = json.loads(
            db.execute("SELECT rules_json FROM worker_skill_policy_state").fetchone()[0]
        )
        rules["workspace.list_dir"]["decision"] = "deny"
        rules["workspace.list_dir"]["auto_redistribute"] = False
        encoded = json.dumps(rules, sort_keys=True, separators=(",", ":"))
        digest = "sha256:" + hashlib.sha256(encoded.encode()).hexdigest()
        db.execute(
            "UPDATE worker_skill_policy_state SET epoch=epoch+1,rules_json=?,rules_digest=?",
            (encoded, digest),
        )
    policy = service.permission_policy
    native_rules = dict(policy.capability_rules)
    native_rules["iphone.mail.compose"] = replace(
        native_rules["iphone.mail.compose"], decision="deny"
    )
    policy.capability_rules = native_rules
    skills = _skills(client, paired_headers)
    assert skills["workspace.list_dir"]["state"] == "policy_denied"
    assert skills["workspace.list_dir"]["agent_ids"] == []
    assert skills["iphone.mail.compose"]["state"] == "policy_denied"


@pytest.mark.parametrize("policy_state", ["missing", "corrupt"])
def test_catalogue_unknown_policy_is_read_only_and_does_not_bootstrap(
    client: TestClient, paired_headers: dict[str, str], test_app: FastAPI, policy_state: str
) -> None:
    _register(client, paired_headers, "workspace.list_dir")
    with sqlite3.connect(test_app.state.settings.db_path) as db:
        if policy_state == "missing":
            db.execute("DELETE FROM worker_skill_policy_state")
        else:
            db.execute("UPDATE worker_skill_policy_state SET rules_digest='corrupt'")
        before = db.execute("SELECT * FROM worker_skill_policy_state").fetchall()
        agent_before = db.execute("SELECT * FROM agents").fetchall()
    availability = _skills(client, paired_headers)["workspace.list_dir"]
    assert availability["state"] == "unknown" and availability["agent_ids"] == []
    with sqlite3.connect(test_app.state.settings.db_path) as db:
        assert db.execute("SELECT * FROM worker_skill_policy_state").fetchall() == before
        assert db.execute("SELECT * FROM agents").fetchall() == agent_before


def test_missing_native_policy_never_claims_iphone_availability(
    client: TestClient, paired_headers: dict[str, str], test_app: FastAPI
) -> None:
    assert client.portal is not None
    service = ActivityCatalogService(test_app.state.settings.db_path, None)
    response = client.portal.call(service.get_catalog)
    native = [skill for skill in response.skills if skill.execution.kind == "iphone"]
    assert native and all(skill.availability.state == "unknown" for skill in native)


@pytest.mark.parametrize(
    "mutation",
    [
        "duplicate_domain",
        "duplicate_skill",
        "duplicate_role",
        "unknown_domain",
        "unknown_skill",
        "duplicate_ref",
        "unsupported_worker",
        "unsupported_iphone",
        "planned_target",
        "wrong_identity",
        "unknown_field",
    ],
)
def test_catalogue_rejects_ambiguous_or_nonexecutable_definitions(mutation: str) -> None:
    raw = _minimal_catalog()
    if mutation.startswith("duplicate_") and mutation != "duplicate_ref":
        collection = {
            "duplicate_domain": "domains",
            "duplicate_skill": "skills",
            "duplicate_role": "roles",
        }[mutation]
        raw[collection].append(deepcopy(raw[collection][0]))
    elif mutation == "unknown_domain":
        raw["roles"][0]["domain_id"] = "missing"
    elif mutation == "unknown_skill":
        raw["roles"][0]["skill_ids"] = ["missing"]
    elif mutation == "duplicate_ref":
        raw["roles"][0]["skill_ids"] *= 2
    elif mutation.startswith("unsupported_"):
        raw["skills"][0]["execution"] = {
            "kind": mutation.removeprefix("unsupported_"),
            "target": "invented.execute",
        }
    elif mutation == "planned_target":
        raw["skills"][0]["execution"]["kind"] = "planned"
    elif mutation == "wrong_identity":
        raw["skills"][0]["id"] = "alias"
    else:
        raw["skills"][0]["approval"] = True
    with pytest.raises(ActivityCatalogError):
        parse_activity_catalog(json.dumps(raw))


def test_catalogue_rejects_duplicate_json_keys_and_accepts_planned_null() -> None:
    text = json.dumps(_minimal_catalog())
    with pytest.raises(ActivityCatalogError):
        parse_activity_catalog(
            text.replace(
                '"schema_version": "1.0"', '"schema_version": "1.0", "schema_version": "1.0"'
            )
        )
    raw = _minimal_catalog()
    raw["skills"][0]["execution"] = {"kind": "planned", "target": None}
    assert parse_activity_catalog(json.dumps(raw)).skills[0].execution.kind == "planned"


def test_catalogue_goal_ready_ids_follow_actual_goal_payload_contract() -> None:
    assert GOAL_READY_SKILLS | PARAMETER_BOUND_SKILLS == SUPPORTED_AGENT_SKILLS
    for skill in GOAL_READY_SKILLS - {"code.build_project"}:
        assert isinstance(
            GoalManager._payload_for_node(
                {"required_skill": skill, "objective": "Inspecter le projet"}
            ),
            dict,
        )
    for skill in PARAMETER_BOUND_SKILLS:
        with pytest.raises(GoalManagerConflict, match="explicit bounded parameters"):
            GoalManager._payload_for_node(
                {"required_skill": skill, "objective": "Inspecter le projet"}
            )
