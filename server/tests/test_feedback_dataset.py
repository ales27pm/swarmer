import json

from fastapi.testclient import TestClient


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
