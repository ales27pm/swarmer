import json
import sqlite3

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.services.feedback_dataset import redact_dataset_text


@pytest.mark.parametrize(
    "label",
    [
        "PRIVATE KEY",
        "RSA PRIVATE KEY",
        "EC PRIVATE KEY",
        "OPENSSH PRIVATE KEY",
        "ENCRYPTED PRIVATE KEY",
    ],
)
def test_dataset_redactor_removes_complete_private_key_blocks(label: str) -> None:
    pem = (
        f"-----BEGIN {label}-----\r\n"
        "Proc-Type: 4,ENCRYPTED\r\n"
        "DEK-Info: AES-256-CBC,fixture-encryption-metadata\r\n"
        "c3ludGhldGljS2V5TWF0ZXJpYWxMaW5lT25l\r\n"
        "c3ludGhldGljS2V5TWF0ZXJpYWxMaW5lVHdv\r\n"
        f"-----END {label}-----"
    )

    assert redact_dataset_text(f"Before {pem} after") == "Before <redacted-secret> after"


def test_dataset_redactor_fails_closed_for_unterminated_private_key_blocks() -> None:
    assert (
        redact_dataset_text(
            "Before -----BEGIN PRIVATE KEY-----\n"
            "c3ludGhldGljS2V5TWF0ZXJpYWxMaW5lT25l\n"
            "truncated private key input"
        )
        == "Before <redacted-secret>"
    )


def test_feedback_correction_does_not_persist_or_export_private_key_bodies(
    client: TestClient, paired_headers: dict[str, str]
) -> None:
    body = "c3ludGhldGljS2V5TWF0ZXJpYWxQZXJzaXN0ZW5jZUZpeHR1cmU="
    pem = f"-----BEGIN PRIVATE KEY-----\n{body}\n-----END PRIVATE KEY-----"
    task = client.post("/tasks", headers=paired_headers, json={"input": "Review fixture"}).json()

    correction = client.post(
        "/feedback/correction",
        headers=paired_headers,
        json={"task_id": task["id"], "corrected_behavior": f"Before {pem} after"},
    )

    assert correction.status_code == 201
    assert correction.json()["corrected_behavior"] == "Before <redacted-secret> after"
    exported = client.get("/feedback/dataset/export", headers=paired_headers)
    assert exported.status_code == 200
    assert body not in exported.json()["data"]


def test_goal_feedback_and_episode_do_not_persist_private_key_bodies(
    client: TestClient, paired_headers: dict[str, str], test_app: FastAPI
) -> None:
    body = "c3ludGhldGljS2V5TWF0ZXJpYWxFcGlzb2RlRml4dHVyZQ=="
    pem = f"-----BEGIN PRIVATE KEY-----\n{body}\n-----END PRIVATE KEY-----"
    created = client.post("/goals", headers=paired_headers, json={"objective": f"Inspect {pem}"})
    assert created.status_code == 201
    goal_id = created.json()["goal"]["id"]
    cancelled = client.post(f"/goals/{goal_id}/cancel", headers=paired_headers, json={})
    assert cancelled.status_code == 200

    feedback = client.post(
        f"/goals/{goal_id}/feedback",
        headers=paired_headers,
        json={"score": 5, "reviewed": True, "corrected_plan_summary": f"Before {pem} after"},
    )

    assert feedback.status_code == 201
    assert feedback.json()["corrected_plan_summary"] == "Before <redacted-secret> after"
    with sqlite3.connect(test_app.state.settings.db_path) as db:
        episode = db.execute(
            "SELECT objective_summary FROM episodes WHERE goal_run_id=?", (goal_id,)
        ).fetchone()
        stored_feedback = db.execute(
            "SELECT corrected_plan_summary FROM goal_feedback WHERE goal_run_id=?", (goal_id,)
        ).fetchone()
    assert episode == ("Inspect <redacted-secret>",)
    assert stored_feedback == ("Before <redacted-secret> after",)
    exported = client.get("/feedback/dataset/export?dataset=planner", headers=paired_headers)
    assert exported.status_code == 200
    assert body not in exported.json()["data"]
    assert json.loads(exported.json()["data"])["objective"] == "Inspect <redacted-secret>"


def test_feedback_correction_exports_redacted_jsonl(
    client: TestClient, paired_headers: dict[str, str]
) -> None:
    task = client.post(
        "/tasks", headers=paired_headers, json={"input": "inspect /home/user/private"}
    ).json()
    correction = client.post(
        "/feedback/correction",
        headers=paired_headers,
        json={
            "task_id": task["id"],
            "corrected_behavior": "Never expose token=abc123 from /home/user/private",
            "notes": "Use a safe summary",
        },
    )
    assert correction.status_code == 201
    exported = client.get("/feedback/dataset/export", headers=paired_headers)
    assert exported.status_code == 200
    row = json.loads(exported.json()["data"].strip())
    assert row["task_id"] == task["id"]
    assert "abc123" not in exported.json()["data"]
    assert "/home/user" not in exported.json()["data"]
