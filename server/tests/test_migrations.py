from pathlib import Path

import aiosqlite
import pytest

from app.services.approval_binding import PUBLIC_PROCESS_ERROR, canonical_action_digest
from app.services.state_service import SCHEMA_VERSION, StateService

LEGACY_SCHEMA = """
CREATE TABLE tasks (
  id TEXT PRIMARY KEY,input TEXT NOT NULL,mode TEXT NOT NULL,source TEXT NOT NULL,
  conversation_id TEXT,status TEXT NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL
);
CREATE TABLE approvals (
  id TEXT PRIMARY KEY,task_id TEXT NOT NULL,action TEXT NOT NULL,summary TEXT NOT NULL,
  risk TEXT NOT NULL,status TEXT NOT NULL,created_at TEXT NOT NULL,decided_at TEXT
);
CREATE TABLE tool_calls (
  id TEXT PRIMARY KEY,task_id TEXT NOT NULL,tool_name TEXT NOT NULL,arguments_json TEXT NOT NULL,
  summary TEXT NOT NULL,risk TEXT NOT NULL,status TEXT NOT NULL,approval_id TEXT,result_json TEXT,
  error TEXT,created_at TEXT NOT NULL,updated_at TEXT NOT NULL
);
CREATE TABLE pairing_codes (code TEXT PRIMARY KEY, expires_at TEXT NOT NULL);
CREATE TABLE devices (id TEXT PRIMARY KEY,name TEXT NOT NULL,token TEXT UNIQUE NOT NULL,created_at TEXT NOT NULL);
CREATE TABLE audit_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,event_type TEXT NOT NULL,payload_json TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""


@pytest.mark.asyncio
async def test_legacy_database_migrates_idempotently(tmp_path: Path) -> None:
    path = tmp_path / "legacy.db"
    legacy_process_error = "legacy process error contains private argv --token=secret"
    legacy_workspace_error = "legacy workspace read failed"
    legacy_process_payload = (
        f'{{"tool_call_id":"call_legacy_process","error":"{legacy_process_error}"}}'
    )
    legacy_workspace_payload = (
        f'{{"tool_call_id":"call_legacy","error":"{legacy_workspace_error}"}}'
    )
    async with aiosqlite.connect(path) as db:
        await db.executescript(LEGACY_SCHEMA)
        await db.execute(
            "INSERT INTO tasks VALUES(?,?,?,?,?,?,?,?)",
            (
                "tsk_legacy",
                "legacy task",
                "normal",
                "iphone",
                None,
                "waiting_permission",
                "2026-01-01T00:00:00+00:00",
                "2026-01-01T00:00:00+00:00",
            ),
        )
        await db.execute(
            "INSERT INTO pairing_codes VALUES(?,?)",
            ("plaintext-transient-code", "2099-01-01T00:00:00+00:00"),
        )
        await db.execute(
            "INSERT INTO devices VALUES(?,?,?,?)",
            ("legacy-phone", "Legacy", "plaintext-device-token", "2026-01-01"),
        )
        await db.execute(
            "INSERT INTO approvals VALUES(?,?,?,?,?,?,?,?)",
            (
                "apr_legacy",
                "tsk_legacy",
                "workspace.write_text",
                "legacy model summary",
                "medium",
                "pending",
                "2026-01-01T00:00:00+00:00",
                None,
            ),
        )
        await db.execute(
            "INSERT INTO tool_calls VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "call_legacy",
                "tsk_legacy",
                "workspace.write_text",
                '{"content":"legacy","path":"legacy.txt"}',
                "legacy model summary",
                "medium",
                "waiting_permission",
                "apr_legacy",
                None,
                None,
                "2026-01-01T00:00:00+00:00",
                "2026-01-01T00:00:00+00:00",
            ),
        )
        await db.execute(
            "INSERT INTO tool_calls VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "call_legacy_process",
                "tsk_legacy",
                "process.run",
                '{"argv":["legacy-command","--token=secret"]}',
                "legacy process summary",
                "high",
                "failed",
                None,
                None,
                legacy_process_error,
                "2026-01-01T00:00:00+00:00",
                "2026-01-01T00:00:00+00:00",
            ),
        )
        await db.execute(
            "INSERT INTO audit_events(event_type,payload_json,created_at) VALUES(?,?,?)",
            ("tool.failed", legacy_process_payload, "2026-01-01T00:00:01+00:00"),
        )
        await db.execute(
            "INSERT INTO audit_events(event_type,payload_json,created_at) VALUES(?,?,?)",
            ("tool.failed", legacy_workspace_payload, "2026-01-01T00:00:02+00:00"),
        )
        await db.commit()

    state = StateService(path)
    await state.initialize()
    await state.initialize()
    task = await state.get_task("tsk_legacy")
    assert task is not None
    assert task.title == "legacy task"
    public_audit = await state.list_audit(limit=20)
    projected_failures = {
        event["payload"]["tool_call_id"]: event
        for event in public_audit
        if event["event_type"] == "tool.failed"
    }
    assert projected_failures["call_legacy_process"]["payload"]["error"] == (PUBLIC_PROCESS_ERROR)
    assert projected_failures["call_legacy"]["payload"]["error"] == legacy_workspace_error
    assert legacy_process_error not in str(public_audit)
    async with aiosqlite.connect(path) as db:
        assert (
            int((await (await db.execute("PRAGMA user_version")).fetchone())[0]) == SCHEMA_VERSION
        )
        assert (
            int((await (await db.execute("SELECT COUNT(*) FROM pairing_codes")).fetchone())[0]) == 0
        )
        assert (
            int((await (await db.execute("SELECT COUNT(*) FROM pairing_candidates")).fetchone())[0])
            == 0
        )
        device_columns = {
            str(row[1]) for row in await (await db.execute("PRAGMA table_info(devices)")).fetchall()
        }
        assert {"last_seen_at", "last_pairing_id"}.issubset(device_columns)
        stored_token = str(
            (
                await (
                    await db.execute("SELECT token FROM devices WHERE id='legacy-phone'")
                ).fetchone()
            )[0]
        )
        assert stored_token.startswith("sha256:")
        assert "plaintext-device-token" not in stored_token
        approval_binding = await (
            await db.execute(
                """
                SELECT tool_call_id,action_digest,request_audit_id,status
                FROM approvals WHERE id='apr_legacy'
                """
            )
        ).fetchone()
        assert approval_binding == (
            "call_legacy",
            canonical_action_digest(
                tool_call_id="call_legacy",
                tool_name="workspace.write_text",
                arguments={"content": "legacy", "path": "legacy.txt"},
            ),
            None,
            "cancelled",
        )
        assert (
            await (
                await db.execute("SELECT status FROM tool_calls WHERE id='call_legacy'")
            ).fetchone()
        ) == ("cancelled",)
        assert (
            await (await db.execute("SELECT status FROM tasks WHERE id='tsk_legacy'")).fetchone()
        ) == ("blocked",)
        invalidations = await (
            await db.execute(
                """
                SELECT event_type,payload_json FROM audit_events
                WHERE event_type='approval.invalidated.migration'
                """
            )
        ).fetchall()
        assert len(invalidations) == 1
        assert "legacy approval has no authenticated consent context" in str(invalidations[0][1])
        raw_legacy_failures = await (
            await db.execute(
                """
                SELECT payload_json,prev_hash,hash FROM audit_events
                WHERE event_type='tool.failed' ORDER BY id
                """
            )
        ).fetchall()
        assert raw_legacy_failures == [
            (legacy_process_payload, None, None),
            (legacy_workspace_payload, None, None),
        ]
