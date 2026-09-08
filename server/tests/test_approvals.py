import asyncio
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import aiosqlite
import pytest
from fastapi.testclient import TestClient

from app.services.approval_binding import (
    PUBLIC_PROCESS_ERROR,
    canonical_action_digest,
    safe_action_preview,
    safe_affected_data_summary,
)
from app.services.approval_gateway import ApprovalConflict
from app.services.audit_log import append_audit_event as append_real_audit_event
from app.services.execution_engine import ExecutionConflict, ExecutionError


def create_write_proposal(
    client: TestClient, headers: dict[str, str], content: str = "verified"
) -> tuple[str, str]:
    task = client.post("/tasks", headers=headers, json={"input": "write a note"}).json()
    proposal = client.post(
        f"/tasks/{task['id']}/tool-calls",
        headers=headers,
        json={
            "tool_name": "workspace.write_text",
            "arguments": {"path": "notes/result.txt", "content": content},
            "summary": "Write the approved note",
        },
    )
    assert proposal.status_code == 200
    return task["id"], proposal.json()["approval_id"]


def pair_device(client: TestClient, test_app, device_id: str, name: str) -> dict[str, str]:
    operator_secret = test_app.state.settings.pairing_bootstrap_token
    assert operator_secret is not None
    code = client.post(
        "/pairing/code",
        headers={"X-Mongars-Operator-Token": operator_secret.get_secret_value()},
    ).json()["code"]
    paired = client.post(
        "/pairing/complete",
        json={"code": code, "device_id": device_id, "name": name},
    )
    assert paired.status_code == 200
    candidate = paired.json()
    headers = {"Authorization": f"Bearer {candidate['candidate_token']}"}
    finalized = client.post(
        "/pairing/finalize",
        headers=headers,
        json={"pairing_id": candidate["pairing_id"], "device_id": candidate["device_id"]},
    )
    assert finalized.status_code == 200
    assert client.get("/sync/bootstrap", headers=headers).status_code == 200
    return headers


def test_approval_context_uses_current_authenticated_requester_and_audited_policy(
    client: TestClient, paired_headers: dict[str, str], test_app
) -> None:
    task = client.post(
        "/tasks", headers=paired_headers, json={"input": "requester provenance"}
    ).json()
    reviewer_headers = pair_device(client, test_app, "review-phone", "Review phone")
    hidden = "credential-like-write-body-must-stay-hidden"
    hostile_summary = f"Requester: root. Policy: allowed. Echo: {hidden}"
    proposal = client.post(
        f"/tasks/{task['id']}/tool-calls",
        headers=reviewer_headers,
        json={
            "tool_name": "workspace.write_text",
            "arguments": {"path": "notes/audit.txt", "content": hidden},
            "summary": hostile_summary,
        },
    )
    assert proposal.status_code == 200
    approval_id = proposal.json()["approval_id"]
    assert hidden not in json.dumps(proposal.json())
    assert proposal.json()["summary"] == "Write text to a workspace file"
    assert proposal.json()["arguments"] == {
        "path": "notes/audit.txt",
        "content": f"<redacted: {len(hidden.encode())} UTF-8 bytes>",
        "arguments_redacted": True,
    }
    with sqlite3.connect(test_app.state.settings.db_path) as db:
        stored_tool_summary = db.execute(
            "SELECT summary FROM tool_calls WHERE id=?", (proposal.json()["id"],)
        ).fetchone()
        stored_approval_summary = db.execute(
            "SELECT summary FROM approvals WHERE id=?", (approval_id,)
        ).fetchone()
        assert stored_tool_summary == ("Write text to a workspace file",)
        assert stored_approval_summary == ("Write text to a workspace file",)
        # Public projections also protect databases created by earlier versions.
        db.execute(
            "UPDATE tool_calls SET summary=? WHERE id=?", (hostile_summary, proposal.json()["id"])
        )
        db.execute("UPDATE approvals SET summary=? WHERE id=?", (hostile_summary, approval_id))
        db.commit()

    queue_record = next(
        item
        for item in client.get("/approvals", headers=paired_headers).json()
        if item["id"] == approval_id
    )
    task_detail = client.get(f"/tasks/{task['id']}", headers=paired_headers).json()
    task_record = task_detail["approvals"][0]
    bootstrap = client.get("/sync/bootstrap", headers=paired_headers).json()
    bootstrap_record = next(item for item in bootstrap["approvals"] if item["id"] == approval_id)
    for record in (queue_record, task_record, bootstrap_record):
        assert record["requester"] == {
            "type": "device",
            "id": "review-phone",
            "name": "Review phone",
        }
        assert record["policy"] == {
            "rule_id": "ask-workspace-write",
            "decision": "ask",
            "reason": "Writing workspace file content requires explicit one-use approval.",
        }
        assert record["affected_data_summary"] == (
            f"Writes {len(hidden.encode())} UTF-8 bytes to one workspace file: "
            "notes/audit.txt; content hidden."
        )
        assert isinstance(record["audit_id"], int)
        assert record["consent_context_valid"] is True
        assert record["summary"] == "Write text to a workspace file"
        assert hidden not in str(record)
    assert hidden not in json.dumps(task_detail)
    assert hidden not in json.dumps(bootstrap)

    audit = next(
        event
        for event in client.get("/audit?limit=100", headers=paired_headers).json()
        if event["id"] == queue_record["audit_id"]
    )
    assert audit["event_type"] == "approval.requested"
    assert audit["actor_type"] == "device"
    assert audit["actor_id"] == "review-phone"
    assert audit["payload"]["approval_id"] == approval_id
    assert audit["payload"]["tool_call_id"] == proposal.json()["id"]
    assert hidden not in str(audit)

    decided = client.post(
        f"/approvals/{approval_id}/decision",
        headers=reviewer_headers,
        json={"decision": "approve"},
    )
    assert decided.status_code == 200
    assert hidden not in json.dumps(decided.json())
    assert (test_app.state.settings.workspace_root / "notes" / "audit.txt").read_text(
        encoding="utf-8"
    ) == hidden
    final_audit = client.get("/audit?limit=100", headers=paired_headers).json()
    assert sum(event["event_type"] == "approval.decided" for event in final_audit) == 1
    assert sum(event["event_type"] == "tool.completed" for event in final_audit) == 1


def test_model_plan_preserves_authenticated_requester_on_approval(
    client: TestClient, paired_headers: dict[str, str], test_app
) -> None:
    test_app.state.orchestrator_service.plan = AsyncMock(
        return_value={
            "tool_name": "workspace.write_text",
            "arguments": {"path": "notes/planned.txt", "content": "planned"},
            "summary": "Plan a write",
        }
    )
    task = client.post(
        "/tasks", headers=paired_headers, json={"input": "plan with trusted requester"}
    ).json()

    planned = client.post(f"/tasks/{task['id']}/plan", headers=paired_headers)

    assert planned.status_code == 200
    approval = client.get(f"/tasks/{task['id']}", headers=paired_headers).json()["approvals"][0]
    assert approval["requester"] == {
        "type": "device",
        "id": "test-phone",
        "name": "pytest",
    }
    assert approval["policy"]["decision"] == "ask"
    assert approval["consent_context_valid"] is True


def test_process_arguments_are_redacted_from_api_sync_and_websocket_but_execute_raw(
    client: TestClient,
    paired_headers: dict[str, str],
    test_app,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hidden = "credential-like-process-argument-must-stay-hidden"
    raw_arguments = {
        "argv": ["pytest", "-k", hidden],
        "cwd": ".",
        "timeout_seconds": 20,
    }
    test_app.state.orchestrator_service.plan = AsyncMock(
        return_value={
            "tool_name": "process.run",
            "arguments": raw_arguments,
            "summary": f"Run a focused test with {hidden}",
        }
    )
    monkeypatch.setattr(
        test_app.state.execution_engine.process_sandbox,
        "validate",
        lambda argv: None,
    )
    captured_arguments: dict[str, object] | None = None

    async def capture_dispatch(tool_name: str, arguments: dict[str, object]) -> dict[str, object]:
        nonlocal captured_arguments
        assert tool_name == "process.run"
        captured_arguments = arguments
        return {
            "returncode": 0,
            "stdout": f"echoed {hidden}",
            "stderr": hidden,
            "stdout_truncated": False,
            "stderr_truncated": False,
            "sandbox": "bubblewrap",
            "network": "denied",
        }

    monkeypatch.setattr(test_app.state.execution_engine, "_dispatch", capture_dispatch)
    task = client.post(
        "/tasks", headers=paired_headers, json={"input": "run a focused test"}
    ).json()
    ticket = client.post("/ws/ticket", headers=paired_headers).json()["ticket"]

    with client.websocket_connect(f"/ws?ticket={ticket}") as websocket:
        assert websocket.receive_json()["type"] == "connected"
        planned = client.post(f"/tasks/{task['id']}/plan", headers=paired_headers)
        assert planned.status_code == 200
        assert planned.json()["summary"] == "Run a sandboxed process"
        proposal_events = [websocket.receive_json() for _ in range(4)]
        assert [event["type"] for event in proposal_events] == [
            "orchestrator.proposed",
            "tool.proposed",
            "approval.requested",
            "tool.updated",
        ]
        assert hidden not in json.dumps({"response": planned.json(), "events": proposal_events})

        detail = client.get(f"/tasks/{task['id']}", headers=paired_headers).json()
        listed_calls = client.get(f"/tasks/{task['id']}/tool-calls", headers=paired_headers).json()
        bootstrap = client.get("/sync/bootstrap", headers=paired_headers).json()
        assert hidden not in json.dumps(
            {"detail": detail, "listed_calls": listed_calls, "bootstrap": bootstrap}
        )
        public_arguments = detail["tool_calls"][0]["arguments"]
        assert isinstance(public_arguments, dict)
        assert public_arguments == {
            "argv": ["<redacted>"],
            "argument_count": 3,
            "arguments_redacted": True,
        }

        approval_id = planned.json()["approval_id"]
        decided = client.post(
            f"/approvals/{approval_id}/decision",
            headers=paired_headers,
            json={"decision": "approve"},
        )
        assert decided.status_code == 200
        completion_events = [websocket.receive_json() for _ in range(3)]
        assert [event["type"] for event in completion_events] == [
            "approval.decided",
            "tool.completed",
            "task.updated",
        ]
        assert hidden not in json.dumps({"response": decided.json(), "events": completion_events})
        assert decided.json()["tool_call"]["result"] == {
            "output_redacted": True,
            "returncode": 0,
            "stdout": f"<redacted: {len(f'echoed {hidden}'.encode())} UTF-8 bytes>",
            "stderr": f"<redacted: {len(hidden.encode())} UTF-8 bytes>",
            "stdout_truncated": False,
            "stderr_truncated": False,
            "sandbox": "bubblewrap",
            "network": "denied",
        }

    assert captured_arguments == raw_arguments
    final_detail = client.get(f"/tasks/{task['id']}", headers=paired_headers).json()
    final_bootstrap = client.get("/sync/bootstrap", headers=paired_headers).json()
    assert hidden not in json.dumps({"detail": final_detail, "bootstrap": final_bootstrap})
    with sqlite3.connect(test_app.state.settings.db_path) as db:
        raw_result = db.execute(
            "SELECT result_json FROM tool_calls WHERE task_id=?", (task["id"],)
        ).fetchone()
    assert raw_result is not None
    assert hidden in str(raw_result[0])


def test_process_failure_details_remain_internal_across_all_public_surfaces(
    client: TestClient,
    paired_headers: dict[str, str],
    test_app,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hidden = "credential-like-process-failure-must-stay-internal"
    monkeypatch.setattr(
        test_app.state.execution_engine.process_sandbox,
        "validate",
        lambda argv: None,
    )

    async def fail_dispatch(tool_name: str, arguments: dict[str, object]) -> dict[str, object]:
        assert tool_name == "process.run"
        assert arguments["argv"] == ["pytest"]
        raise ExecutionError(hidden)

    monkeypatch.setattr(test_app.state.execution_engine, "_dispatch", fail_dispatch)
    task = client.post(
        "/chat",
        headers=paired_headers,
        json={"content": "exercise process failure projection", "start_task": True},
    ).json()["task"]
    ticket = client.post("/ws/ticket", headers=paired_headers).json()["ticket"]

    with client.websocket_connect(f"/ws?ticket={ticket}") as websocket:
        assert websocket.receive_json()["type"] == "connected"
        proposed = client.post(
            f"/tasks/{task['id']}/tool-calls",
            headers=paired_headers,
            json={
                "tool_name": "process.run",
                "arguments": {"argv": ["pytest"], "cwd": "."},
                "summary": "Run a failing process",
            },
        )
        assert proposed.status_code == 200
        proposal_events = [websocket.receive_json() for _ in range(3)]
        assert [event["type"] for event in proposal_events] == [
            "tool.proposed",
            "approval.requested",
            "tool.updated",
        ]

        decided = client.post(
            f"/approvals/{proposed.json()['approval_id']}/decision",
            headers=paired_headers,
            json={"decision": "approve"},
        )
        assert decided.status_code == 200
        failure_events = [websocket.receive_json() for _ in range(3)]
        assert [event["type"] for event in failure_events] == [
            "approval.decided",
            "tool.failed",
            "task.updated",
        ]

    detail = client.get(f"/tasks/{task['id']}", headers=paired_headers).json()
    bootstrap = client.get("/sync/bootstrap", headers=paired_headers).json()
    audit = client.get("/audit?limit=100", headers=paired_headers).json()
    public_surfaces = {
        "proposal_events": proposal_events,
        "decision": decided.json(),
        "failure_events": failure_events,
        "detail": detail,
        "bootstrap": bootstrap,
        "audit": audit,
    }
    assert hidden not in json.dumps(public_surfaces)
    assert decided.json()["tool_call"]["error"] == PUBLIC_PROCESS_ERROR
    assert detail["task"]["error_json"] == {"message": PUBLIC_PROCESS_ERROR}
    assert detail["messages"][-1]["content"] == f"Tool execution failed: {PUBLIC_PROCESS_ERROR}"
    terminal_audit = next(event for event in audit if event["event_type"] == "tool.failed")
    assert terminal_audit["payload"]["error"] == PUBLIC_PROCESS_ERROR

    with sqlite3.connect(test_app.state.settings.db_path) as db:
        internal = db.execute(
            "SELECT error FROM tool_calls WHERE task_id=?", (task["id"],)
        ).fetchone()
    assert internal == (hidden,)


@pytest.mark.asyncio
async def test_audit_api_projects_legacy_process_errors_without_mutating_raw_rows(
    client: TestClient, paired_headers: dict[str, str], test_app
) -> None:
    task = client.post(
        "/tasks", headers=paired_headers, json={"input": "inspect legacy audit projection"}
    ).json()
    process_call_id = "call_legacy_process_audit"
    workspace_call_id = "call_legacy_workspace_audit"
    process_failed_error = "legacy process failure contains credential-like-secret"
    process_rejected_error = "legacy rejected argv contains --token=hidden-value"
    workspace_failed_error = "workspace file was not found"
    workspace_rejected_error = "workspace path was rejected"
    now = datetime.now(UTC).isoformat()

    async with aiosqlite.connect(test_app.state.settings.db_path) as db:
        await db.execute("BEGIN IMMEDIATE")
        for call_id, tool_name in (
            (process_call_id, "process.run"),
            (workspace_call_id, "workspace.read_text"),
        ):
            await db.execute(
                """
                INSERT INTO tool_calls(
                    id,task_id,tool_name,arguments_json,summary,risk,status,approval_id,
                    result_json,error,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    call_id,
                    task["id"],
                    tool_name,
                    "{}",
                    "legacy summary",
                    "medium",
                    "failed",
                    None,
                    None,
                    None,
                    now,
                    now,
                ),
            )

        inserted_events = []
        for event_type, call_id, error in (
            ("tool.failed", process_call_id, process_failed_error),
            ("tool.execution_rejected", process_call_id, process_rejected_error),
            ("tool.failed", workspace_call_id, workspace_failed_error),
            ("tool.execution_rejected", workspace_call_id, workspace_rejected_error),
        ):
            inserted_events.append(
                await append_real_audit_event(
                    db,
                    event_type,
                    {
                        "tool_call_id": call_id,
                        "error": error,
                        "durable_status": "failed",
                    },
                    task_id=task["id"],
                    trace_id=task["id"],
                    created_at=now,
                )
            )
        await db.commit()

    event_ids = [int(event["id"]) for event in inserted_events]
    async with aiosqlite.connect(test_app.state.settings.db_path) as db:
        raw_before = await (
            await db.execute(
                """
                SELECT id,payload_json,prev_hash,hash FROM audit_events
                WHERE id BETWEEN ? AND ? ORDER BY id
                """,
                (min(event_ids), max(event_ids)),
            )
        ).fetchall()

    response = client.get("/audit?limit=200", headers=paired_headers)

    assert response.status_code == 200
    public_events = {
        int(event["id"]): event for event in response.json() if int(event["id"]) in event_ids
    }
    assert len(public_events) == 4
    assert public_events[event_ids[0]]["payload"]["error"] == PUBLIC_PROCESS_ERROR
    assert public_events[event_ids[1]]["payload"]["error"] == PUBLIC_PROCESS_ERROR
    assert public_events[event_ids[2]]["payload"]["error"] == workspace_failed_error
    assert public_events[event_ids[3]]["payload"]["error"] == workspace_rejected_error
    assert process_failed_error not in json.dumps(public_events)
    assert process_rejected_error not in json.dumps(public_events)

    async with aiosqlite.connect(test_app.state.settings.db_path) as db:
        raw_after = await (
            await db.execute(
                """
                SELECT id,payload_json,prev_hash,hash FROM audit_events
                WHERE id BETWEEN ? AND ? ORDER BY id
                """,
                (min(event_ids), max(event_ids)),
            )
        ).fetchall()
    assert raw_after == raw_before
    assert process_failed_error in str(raw_after[0][1])
    assert process_rejected_error in str(raw_after[1][1])
    assert all(row[3] for row in raw_after)


def test_approval_replay_does_not_repeat_execution(
    client: TestClient, paired_headers: dict[str, str], test_app
) -> None:
    task_id, approval_id = create_write_proposal(client, paired_headers)
    first = client.post(
        f"/approvals/{approval_id}/decision",
        headers=paired_headers,
        json={"decision": "approve"},
    )
    assert first.status_code == 200
    assert first.json()["tool_call"]["status"] == "completed"

    replay = client.post(
        f"/approvals/{approval_id}/decision",
        headers=paired_headers,
        json={"decision": "approve"},
    )
    assert replay.status_code == 409
    workspace: Path = test_app.state.settings.workspace_root
    assert (workspace / "notes" / "result.txt").read_text(encoding="utf-8") == "verified"
    detail = client.get(f"/tasks/{task_id}", headers=paired_headers).json()
    assert len(detail["tool_calls"]) == 1


def test_post_dispatch_terminal_audit_failure_reports_uncertain_and_blocks_replay(
    client: TestClient,
    paired_headers: dict[str, str],
    test_app,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task_id, approval_id = create_write_proposal(
        client, paired_headers, "effect happened exactly once"
    )

    async def reject_terminal_audit(
        db: aiosqlite.Connection,
        event_type: str,
        payload: dict[str, Any],
        **kwargs: Any,
    ) -> dict[str, Any]:
        if event_type == "tool.completed":
            raise RuntimeError("terminal audit persistence rejected")
        return await append_real_audit_event(db, event_type, payload, **kwargs)

    monkeypatch.setattr("app.services.execution_engine.append_audit_event", reject_terminal_audit)
    decision = client.post(
        f"/approvals/{approval_id}/decision",
        headers=paired_headers,
        json={"decision": "approve"},
    )

    assert decision.status_code == 409
    assert decision.json()["detail"] == ("tool outcome is uncertain; the call must not be retried")
    assert (test_app.state.settings.workspace_root / "notes" / "result.txt").read_text(
        encoding="utf-8"
    ) == "effect happened exactly once"
    detail = client.get(f"/tasks/{task_id}", headers=paired_headers).json()
    assert detail["task"]["status"] == "running"
    assert detail["approvals"][0]["status"] == "approved"
    assert detail["tool_calls"][0]["status"] == "running"
    audit = client.get("/audit?limit=100", headers=paired_headers).json()
    assert sum(event["event_type"] == "tool.outcome_uncertain" for event in audit) == 1
    assert sum(event["event_type"] == "tool.completed" for event in audit) == 0

    replay = client.post(
        f"/approvals/{approval_id}/decision",
        headers=paired_headers,
        json={"decision": "approve"},
    )
    assert replay.status_code == 409


def test_uncertainty_response_survives_followup_audit_failure(
    client: TestClient,
    paired_headers: dict[str, str],
    test_app,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task_id, approval_id = create_write_proposal(
        client, paired_headers, "effect happened before reporting failed"
    )

    async def reject_terminal_audit(
        db: aiosqlite.Connection,
        event_type: str,
        payload: dict[str, Any],
        **kwargs: Any,
    ) -> dict[str, Any]:
        if event_type == "tool.completed":
            raise RuntimeError("terminal audit persistence rejected")
        return await append_real_audit_event(db, event_type, payload, **kwargs)

    original_append_audit = test_app.state.state_service.append_audit

    async def reject_uncertainty_audit(
        event_type: str, payload: dict[str, Any], **kwargs: Any
    ) -> dict[str, Any]:
        if event_type == "tool.outcome_uncertain":
            raise RuntimeError("follow-up audit persistence rejected")
        return await original_append_audit(event_type, payload, **kwargs)

    monkeypatch.setattr("app.services.execution_engine.append_audit_event", reject_terminal_audit)
    monkeypatch.setattr(test_app.state.state_service, "append_audit", reject_uncertainty_audit)
    decision = client.post(
        f"/approvals/{approval_id}/decision",
        headers=paired_headers,
        json={"decision": "approve"},
    )

    assert decision.status_code == 409
    assert decision.json()["detail"] == ("tool outcome is uncertain; the call must not be retried")
    assert "approval" not in decision.json()["detail"]
    assert (test_app.state.settings.workspace_root / "notes" / "result.txt").read_text(
        encoding="utf-8"
    ) == "effect happened before reporting failed"
    detail = client.get(f"/tasks/{task_id}", headers=paired_headers).json()
    assert detail["task"]["status"] == "running"
    assert detail["tool_calls"][0]["status"] == "running"
    assert (
        client.post(
            f"/approvals/{approval_id}/decision",
            headers=paired_headers,
            json={"decision": "approve"},
        ).status_code
        == 409
    )


def test_approval_is_bound_to_exact_call_and_exposes_only_safe_action_details(
    client: TestClient, paired_headers: dict[str, str], test_app
) -> None:
    secret_content = "credential-like-value-must-stay-hidden"
    task_id, approval_id = create_write_proposal(client, paired_headers, secret_content)

    detail = client.get(f"/tasks/{task_id}", headers=paired_headers).json()
    approval = next(item for item in detail["approvals"] if item["id"] == approval_id)
    tool_call = next(item for item in detail["tool_calls"] if item["approval_id"] == approval_id)

    assert approval["tool_call_id"] == tool_call["id"]
    with sqlite3.connect(test_app.state.settings.db_path) as db:
        raw_arguments = json.loads(
            db.execute(
                "SELECT arguments_json FROM tool_calls WHERE id=?", (tool_call["id"],)
            ).fetchone()[0]
        )
    assert approval["action_digest"] == canonical_action_digest(
        tool_call_id=tool_call["id"],
        tool_name=tool_call["tool_name"],
        arguments=raw_arguments,
    )
    assert approval["binding_valid"] is True
    assert approval["action_preview"]["target"] == "notes/result.txt"
    assert "UTF-8 bytes" in approval["action_preview"]["details"][0]
    assert secret_content not in str(approval)


def test_process_preview_hides_arbitrary_argument_values() -> None:
    preview = safe_action_preview(
        "process.run",
        {
            "argv": ["pytest", "-k", "credential-like-value-must-stay-hidden"],
            "cwd": "server/tests",
            "timeout_seconds": 20,
        },
    )

    assert preview["target"] == "entire non-protected configured workspace (read-write)"
    assert preview["working_directory"] == "server/tests"
    assert preview["command"] == ["pytest"]
    assert preview["arguments_redacted"] is True
    details = " ".join(preview["details"])
    assert "entire configured workspace is mounted read-write" in details
    assert "cwd selects only the working directory" in details
    summary = safe_affected_data_summary(
        "process.run",
        {"argv": ["pytest"], "cwd": "server/tests"},
    )
    assert "entire configured workspace" in summary
    assert "cwd selects only its working directory" in summary
    assert "does not restrict workspace access" in summary
    assert "credential-like-value-must-stay-hidden" not in str(preview)

    script_preview = safe_action_preview(
        "process.run",
        {
            "argv": ["python3", "credential-like-script.py"],
            "cwd": ".",
        },
    )
    assert script_preview["command"] == ["python3"]
    assert script_preview["arguments_redacted"] is True
    assert "credential-like-script.py" not in str(script_preview)


def test_cancelling_task_invalidates_pending_approval(
    client: TestClient, paired_headers: dict[str, str]
) -> None:
    task_id, approval_id = create_write_proposal(client, paired_headers)
    assert client.post(f"/tasks/{task_id}/cancel", headers=paired_headers).status_code == 200
    decision = client.post(
        f"/approvals/{approval_id}/decision",
        headers=paired_headers,
        json={"decision": "approve"},
    )
    assert decision.status_code == 409
    detail = client.get(f"/tasks/{task_id}", headers=paired_headers).json()
    assert detail["task"]["status"] == "cancelled"
    assert detail["tool_calls"][0]["status"] == "cancelled"


@pytest.mark.asyncio
async def test_cancellation_audit_failure_rolls_back_task_approval_and_tool(
    client: TestClient,
    paired_headers: dict[str, str],
    test_app,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task_id, approval_id = create_write_proposal(client, paired_headers)

    async def reject_cancellation_audit(*_: object, **__: object) -> dict[str, object]:
        raise RuntimeError("cancellation audit persistence rejected")

    monkeypatch.setattr("app.services.state_service.append_audit_event", reject_cancellation_audit)
    with pytest.raises(RuntimeError, match="cancellation audit persistence rejected"):
        await test_app.state.state_service.cancel_task(task_id, actor_id="test-phone")

    async with aiosqlite.connect(test_app.state.settings.db_path) as db:
        task_state = await (
            await db.execute("SELECT status FROM tasks WHERE id=?", (task_id,))
        ).fetchone()
        approval_state = await (
            await db.execute("SELECT status,decided_at FROM approvals WHERE id=?", (approval_id,))
        ).fetchone()
        tool_state = await (
            await db.execute("SELECT status FROM tool_calls WHERE approval_id=?", (approval_id,))
        ).fetchone()
        cancellation_audits = int(
            (
                await (
                    await db.execute(
                        "SELECT COUNT(*) FROM audit_events WHERE event_type='task.cancelled'"
                    )
                ).fetchone()
            )[0]
        )
    assert task_state == ("waiting_permission",)
    assert approval_state == ("pending", None)
    assert tool_state == ("waiting_permission",)
    assert cancellation_audits == 0


def test_cancelled_task_cannot_be_revived_by_new_proposals(
    client: TestClient, paired_headers: dict[str, str]
) -> None:
    task = client.post(
        "/tasks", headers=paired_headers, json={"input": "cancel before proposal"}
    ).json()
    assert client.post(f"/tasks/{task['id']}/cancel", headers=paired_headers).status_code == 200

    proposal = client.post(
        f"/tasks/{task['id']}/tool-calls",
        headers=paired_headers,
        json={
            "tool_name": "workspace.list_dir",
            "arguments": {"path": "."},
            "summary": "must not revive",
        },
    )
    assert proposal.status_code == 409
    assert client.post("/approvals/request", headers=paired_headers, json={}).status_code == 404
    detail = client.get(f"/tasks/{task['id']}", headers=paired_headers).json()
    assert detail["task"]["status"] == "cancelled"
    assert detail["tool_calls"] == []


def test_second_tool_proposal_cannot_create_a_duplicate_execution(
    client: TestClient, paired_headers: dict[str, str]
) -> None:
    task = client.post(
        "/tasks", headers=paired_headers, json={"input": "list the workspace"}
    ).json()
    proposal = {
        "tool_name": "workspace.list_dir",
        "arguments": {"path": "."},
        "summary": "List the workspace once",
    }

    first = client.post(f"/tasks/{task['id']}/tool-calls", headers=paired_headers, json=proposal)
    second = client.post(f"/tasks/{task['id']}/tool-calls", headers=paired_headers, json=proposal)

    assert first.status_code == 200
    assert second.status_code == 409
    detail = client.get(f"/tasks/{task['id']}", headers=paired_headers).json()
    assert len(detail["tool_calls"]) == 1


@pytest.mark.asyncio
async def test_expired_approval_cannot_execute_its_linked_call(
    client: TestClient,
    paired_headers: dict[str, str],
    test_app,
) -> None:
    task_id, approval_id = create_write_proposal(client, paired_headers)
    async with aiosqlite.connect(test_app.state.settings.db_path) as db:
        await db.execute(
            "UPDATE approvals SET expires_at='2000-01-01T00:00:00+00:00' WHERE id=?",
            (approval_id,),
        )
        await db.commit()

    decision = client.post(
        f"/approvals/{approval_id}/decision",
        headers=paired_headers,
        json={"decision": "approve"},
    )

    assert decision.status_code == 409
    detail = client.get(f"/tasks/{task_id}", headers=paired_headers).json()
    assert detail["task"]["status"] == "blocked"
    assert detail["approvals"][0]["status"] == "expired"
    assert detail["tool_calls"][0]["status"] == "cancelled"


@pytest.mark.asyncio
@pytest.mark.parametrize("entry_point", ["get", "list", "decision"])
async def test_approval_expires_at_the_exact_deadline(
    entry_point: str,
    client: TestClient,
    paired_headers: dict[str, str],
    test_app,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task_id, approval_id = create_write_proposal(client, paired_headers)
    deadline = datetime(2030, 1, 2, 3, 4, 5, tzinfo=UTC)
    deadline_iso = deadline.isoformat()
    async with aiosqlite.connect(test_app.state.settings.db_path) as db:
        await db.execute(
            "UPDATE approvals SET expires_at=? WHERE id=?",
            (deadline_iso, approval_id),
        )
        await db.commit()

    class FrozenDateTime:
        @staticmethod
        def now(tz: object) -> datetime:
            assert tz is UTC
            return deadline

    monkeypatch.setattr("app.services.approval_gateway.datetime", FrozenDateTime)
    gateway = test_app.state.approval_gateway

    if entry_point == "get":
        record = await gateway.get(approval_id)
        assert record is not None
        assert record["status"] == "expired"
    elif entry_point == "list":
        pending = await gateway.list_pending()
        assert approval_id not in {record["id"] for record in pending}
        expired = await gateway.list_by_status("expired")
        assert approval_id in {record["id"] for record in expired}
    else:
        with pytest.raises(ApprovalConflict, match="approval expired"):
            await gateway.decide(approval_id, "approve", actor_id="deadline-test")

    async with aiosqlite.connect(test_app.state.settings.db_path) as db:
        approval_state = await (
            await db.execute(
                "SELECT status,decided_at,decision_json FROM approvals WHERE id=?",
                (approval_id,),
            )
        ).fetchone()
        tool_state = await (
            await db.execute("SELECT id,status FROM tool_calls WHERE approval_id=?", (approval_id,))
        ).fetchone()
        task_state = await (
            await db.execute("SELECT status FROM tasks WHERE id=?", (task_id,))
        ).fetchone()
        audit_rows = await (
            await db.execute(
                """
                SELECT event_type,payload_json FROM audit_events
                WHERE event_type IN ('approval.decided','approval.expired')
                ORDER BY id
                """
            )
        ).fetchall()
    assert approval_state == ("expired", deadline_iso, None)
    assert tool_state is not None
    assert tool_state[1] == "cancelled"
    assert task_state == ("blocked",)
    assert [(event_type, json.loads(payload)) for event_type, payload in audit_rows] == [
        (
            "approval.expired",
            {"approval_id": approval_id, "tool_call_id": str(tool_state[0])},
        )
    ]


@pytest.mark.asyncio
async def test_bulk_expiry_is_audited_once_when_materialized_repeatedly(
    client: TestClient,
    paired_headers: dict[str, str],
    test_app,
) -> None:
    _, approval_id = create_write_proposal(client, paired_headers)
    async with aiosqlite.connect(test_app.state.settings.db_path) as db:
        await db.execute(
            "UPDATE approvals SET expires_at='2000-01-01T00:00:00+00:00' WHERE id=?",
            (approval_id,),
        )
        tool_row = await (
            await db.execute("SELECT id FROM tool_calls WHERE approval_id=?", (approval_id,))
        ).fetchone()
        await db.commit()
    assert tool_row is not None

    gateway = test_app.state.approval_gateway
    await gateway.expire_pending()
    await gateway.expire_pending()

    async with aiosqlite.connect(test_app.state.settings.db_path) as db:
        rows = await (
            await db.execute(
                "SELECT payload_json FROM audit_events WHERE event_type='approval.expired'"
            )
        ).fetchall()
    assert [json.loads(row[0]) for row in rows] == [
        {"approval_id": approval_id, "tool_call_id": str(tool_row[0])}
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("entry_point", ["decision", "bulk"])
async def test_expiry_audit_failure_rolls_back_all_expiry_state(
    entry_point: str,
    client: TestClient,
    paired_headers: dict[str, str],
    test_app,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task_id, approval_id = create_write_proposal(client, paired_headers)
    async with aiosqlite.connect(test_app.state.settings.db_path) as db:
        await db.execute(
            "UPDATE approvals SET expires_at='2000-01-01T00:00:00+00:00' WHERE id=?",
            (approval_id,),
        )
        await db.commit()

    async def reject_expiry_audit(*_: object, **__: object) -> dict[str, object]:
        raise RuntimeError("expiry audit persistence rejected")

    monkeypatch.setattr("app.services.approval_gateway.append_audit_event", reject_expiry_audit)
    gateway = test_app.state.approval_gateway
    with pytest.raises(RuntimeError, match="expiry audit persistence rejected"):
        if entry_point == "decision":
            await gateway.decide(approval_id, "approve", actor_id="deadline-test")
        else:
            await gateway.expire_pending()

    async with aiosqlite.connect(test_app.state.settings.db_path) as db:
        approval_state = await (
            await db.execute(
                "SELECT status,decided_at,decision_json FROM approvals WHERE id=?",
                (approval_id,),
            )
        ).fetchone()
        tool_state = await (
            await db.execute("SELECT status FROM tool_calls WHERE approval_id=?", (approval_id,))
        ).fetchone()
        task_state = await (
            await db.execute("SELECT status FROM tasks WHERE id=?", (task_id,))
        ).fetchone()
        expiry_audits = int(
            (
                await (
                    await db.execute(
                        "SELECT COUNT(*) FROM audit_events WHERE event_type='approval.expired'"
                    )
                ).fetchone()
            )[0]
        )
    assert approval_state == ("pending", None, None)
    assert tool_state == ("waiting_permission",)
    assert task_state == ("waiting_permission",)
    assert expiry_audits == 0


@pytest.mark.asyncio
async def test_bootstrap_materializes_expiry_consistently(
    client: TestClient,
    paired_headers: dict[str, str],
    test_app,
) -> None:
    task_id, approval_id = create_write_proposal(client, paired_headers)
    async with aiosqlite.connect(test_app.state.settings.db_path) as db:
        await db.execute(
            "UPDATE approvals SET expires_at='2000-01-01T00:00:00+00:00' WHERE id=?",
            (approval_id,),
        )
        await db.commit()

    bootstrap = client.get("/sync/bootstrap", headers=paired_headers)

    assert bootstrap.status_code == 200
    body = bootstrap.json()
    task = next(item for item in body["tasks"] if item["id"] == task_id)
    approval = next(item for item in body["approvals"] if item["id"] == approval_id)
    tool_call = next(item for item in body["tool_calls"] if item["approval_id"] == approval_id)
    assert task["status"] == "blocked"
    assert approval["status"] == "expired"
    assert approval["decision"] is None
    assert "decision_json" not in approval
    assert tool_call["status"] == "cancelled"
    assert body["counts"]["approvals_pending"] == 0


@pytest.mark.asyncio
async def test_concurrent_gateway_decisions_have_one_winner(
    client: TestClient, paired_headers: dict[str, str], test_app
) -> None:
    _, approval_id = create_write_proposal(client, paired_headers)
    gateway = test_app.state.approval_gateway

    async def decide() -> str:
        try:
            await gateway.decide(approval_id, "approve", actor_id="race-test")
            return "accepted"
        except ApprovalConflict:
            return "conflict"

    assert sorted(await asyncio.gather(decide(), decide())) == ["accepted", "conflict"]


@pytest.mark.asyncio
async def test_approval_decision_audit_failure_rolls_back_all_decision_state(
    client: TestClient,
    paired_headers: dict[str, str],
    test_app,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task_id, approval_id = create_write_proposal(client, paired_headers)

    async def reject_decision_audit(*_: object, **__: object) -> dict[str, object]:
        raise RuntimeError("decision audit persistence rejected")

    monkeypatch.setattr("app.services.approval_gateway.append_audit_event", reject_decision_audit)
    with pytest.raises(RuntimeError, match="decision audit persistence rejected"):
        await test_app.state.approval_gateway.decide(
            approval_id,
            "approve",
            actor_id="test-phone",
        )

    async with aiosqlite.connect(test_app.state.settings.db_path) as db:
        approval_state = await (
            await db.execute(
                "SELECT status,decided_at,decision_json FROM approvals WHERE id=?",
                (approval_id,),
            )
        ).fetchone()
        tool_state = await (
            await db.execute("SELECT status FROM tool_calls WHERE approval_id=?", (approval_id,))
        ).fetchone()
        task_state = await (
            await db.execute("SELECT status FROM tasks WHERE id=?", (task_id,))
        ).fetchone()
        decided_audits = int(
            (
                await (
                    await db.execute(
                        "SELECT COUNT(*) FROM audit_events WHERE event_type='approval.decided'"
                    )
                ).fetchone()
            )[0]
        )
    assert approval_state == ("pending", None, None)
    assert tool_state == ("waiting_permission",)
    assert task_state == ("waiting_permission",)
    assert decided_audits == 0


@pytest.mark.asyncio
async def test_denial_audit_failure_rolls_back_decision_and_authoritative_audit(
    client: TestClient,
    paired_headers: dict[str, str],
    test_app,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task_id, approval_id = create_write_proposal(client, paired_headers)

    async def reject_denied_audit(
        db: aiosqlite.Connection,
        event_type: str,
        payload: dict[str, Any],
        **kwargs: Any,
    ) -> dict[str, Any]:
        if event_type == "tool.denied":
            raise RuntimeError("denial audit persistence rejected")
        return await append_real_audit_event(db, event_type, payload, **kwargs)

    monkeypatch.setattr("app.services.approval_gateway.append_audit_event", reject_denied_audit)
    with pytest.raises(RuntimeError, match="denial audit persistence rejected"):
        await test_app.state.approval_gateway.decide(
            approval_id,
            "deny",
            actor_id="test-phone",
        )

    async with aiosqlite.connect(test_app.state.settings.db_path) as db:
        approval_state = await (
            await db.execute(
                "SELECT status,decided_at,decision_json FROM approvals WHERE id=?",
                (approval_id,),
            )
        ).fetchone()
        tool_state = await (
            await db.execute("SELECT status FROM tool_calls WHERE approval_id=?", (approval_id,))
        ).fetchone()
        task_state = await (
            await db.execute("SELECT status FROM tasks WHERE id=?", (task_id,))
        ).fetchone()
        audit_counts = dict(
            await (
                await db.execute(
                    """
                    SELECT event_type,COUNT(*) FROM audit_events
                    WHERE event_type IN ('approval.decided','tool.denied')
                    GROUP BY event_type
                    """
                )
            ).fetchall()
        )
    assert approval_state == ("pending", None, None)
    assert tool_state == ("waiting_permission",)
    assert task_state == ("waiting_permission",)
    assert audit_counts == {}


@pytest.mark.asyncio
async def test_mutated_arguments_invalidate_consent_before_decision(
    client: TestClient, paired_headers: dict[str, str], test_app
) -> None:
    task_id, approval_id = create_write_proposal(client, paired_headers, "approved content")
    async with aiosqlite.connect(test_app.state.settings.db_path) as db:
        await db.execute(
            "UPDATE tool_calls SET arguments_json=? WHERE approval_id=?",
            ('{"content":"substituted","path":"notes/other.txt"}', approval_id),
        )
        await db.commit()

    decision = client.post(
        f"/approvals/{approval_id}/decision",
        headers=paired_headers,
        json={"decision": "approve"},
    )

    assert decision.status_code == 409
    assert decision.json()["detail"] == "approval action binding is invalid"
    detail = client.get(f"/tasks/{task_id}", headers=paired_headers).json()
    assert detail["approvals"][0]["binding_valid"] is False
    assert detail["approvals"][0]["status"] == "pending"
    assert detail["tool_calls"][0]["status"] == "waiting_permission"
    assert not (test_app.state.settings.workspace_root / "notes" / "other.txt").exists()


@pytest.mark.asyncio
async def test_tampered_request_audit_context_cannot_be_approved(
    client: TestClient, paired_headers: dict[str, str], test_app
) -> None:
    task_id, approval_id = create_write_proposal(client, paired_headers, "approved content")
    approval = client.get(f"/tasks/{task_id}", headers=paired_headers).json()["approvals"][0]
    async with aiosqlite.connect(test_app.state.settings.db_path) as db:
        await db.execute(
            "UPDATE audit_events SET payload_json=? WHERE id=?",
            (
                '{"policy":{"decision":"ask","reason":"forged","rule_id":"forged"}}',
                approval["audit_id"],
            ),
        )
        await db.commit()

    detail = client.get(f"/tasks/{task_id}", headers=paired_headers).json()
    invalid = detail["approvals"][0]
    assert invalid["binding_valid"] is True
    assert invalid["consent_context_valid"] is False
    assert invalid["requester"] is None
    assert invalid["policy"] is None

    decision = client.post(
        f"/approvals/{approval_id}/decision",
        headers=paired_headers,
        json={"decision": "approve"},
    )
    assert decision.status_code == 409
    assert decision.json()["detail"] == "approval consent context is invalid"
    assert not (test_app.state.settings.workspace_root / "notes" / "result.txt").exists()


@pytest.mark.asyncio
async def test_mutated_arguments_cannot_execute_after_approval_claim_gap(
    client: TestClient, paired_headers: dict[str, str], test_app
) -> None:
    _, approval_id = create_write_proposal(client, paired_headers, "approved content")
    gateway = test_app.state.approval_gateway
    engine = test_app.state.execution_engine
    approved = await gateway.decide(approval_id, "approve", actor_id="pytest")
    assert approved is not None
    call = await engine.get_by_approval(approval_id)
    assert call is not None
    async with aiosqlite.connect(test_app.state.settings.db_path) as db:
        await db.execute(
            "UPDATE tool_calls SET arguments_json=? WHERE id=?",
            ('{"content":"substituted","path":"notes/other.txt"}', call["id"]),
        )
        await db.commit()

    with pytest.raises(ExecutionConflict, match="does not match its approved one-shot grant"):
        await engine.execute(call["id"])
    assert not (test_app.state.settings.workspace_root / "notes" / "other.txt").exists()
