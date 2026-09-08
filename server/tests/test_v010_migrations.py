from __future__ import annotations

import json
from pathlib import Path

import aiosqlite
import pytest

from app.services.iphone_capability_binding import (
    canonical_capability_request_fingerprint,
)
from app.services.state_service import SCHEMA_VERSION, StateService

V08_SCHEMA = """
PRAGMA foreign_keys=ON;
CREATE TABLE tasks (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    input TEXT NOT NULL,
    mode TEXT NOT NULL,
    source TEXT NOT NULL,
    conversation_id TEXT,
    status TEXT NOT NULL,
    priority INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    completed_at TEXT,
    error_json TEXT
);
CREATE TABLE approvals (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    tool_call_id TEXT NOT NULL,
    action_digest TEXT NOT NULL,
    request_audit_id INTEGER NOT NULL,
    action TEXT NOT NULL,
    summary TEXT NOT NULL,
    risk TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    decided_at TEXT,
    decision_json TEXT
);
CREATE TABLE tool_calls (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    tool_name TEXT NOT NULL,
    arguments_json TEXT NOT NULL,
    summary TEXT NOT NULL,
    risk TEXT NOT NULL,
    status TEXT NOT NULL,
    approval_id TEXT,
    result_json TEXT,
    error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE pairing_codes (
    code TEXT PRIMARY KEY,
    expires_at TEXT NOT NULL,
    attempts_remaining INTEGER NOT NULL DEFAULT 10,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE devices (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    token TEXT UNIQUE NOT NULL,
    created_at TEXT NOT NULL,
    last_seen_at TEXT,
    last_pairing_id TEXT
);
CREATE TABLE pairing_candidates (
    pairing_id TEXT PRIMARY KEY,
    token_hash TEXT UNIQUE NOT NULL,
    device_id TEXT NOT NULL,
    name TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    finalized_at TEXT
);
CREATE TABLE agents (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    version TEXT NOT NULL,
    endpoint TEXT NOT NULL,
    model_id TEXT,
    status TEXT NOT NULL,
    skills_json TEXT NOT NULL,
    auth_token_hash TEXT,
    last_heartbeat_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE memory_items (
    id TEXT PRIMARY KEY,
    scope TEXT NOT NULL,
    kind TEXT NOT NULL,
    content TEXT NOT NULL,
    summary TEXT,
    sensitivity TEXT NOT NULL DEFAULT 'normal',
    confidence REAL NOT NULL DEFAULT 1.0,
    pinned INTEGER NOT NULL DEFAULT 0,
    metadata_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE feedback_events (
    id TEXT PRIMARY KEY,
    task_id TEXT,
    agent_id TEXT,
    type TEXT NOT NULL,
    label TEXT,
    score REAL,
    notes TEXT,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE message_board_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    topic TEXT NOT NULL,
    event_type TEXT NOT NULL,
    message_id TEXT NOT NULL,
    agent_id TEXT,
    task_id TEXT,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE agent_jobs (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    required_skill TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    status TEXT NOT NULL,
    claimed_by TEXT,
    claim_token TEXT,
    result_json TEXT,
    error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    claimed_at TEXT,
    heartbeat_at TEXT,
    completed_at TEXT,
    FOREIGN KEY(task_id) REFERENCES tasks(id),
    FOREIGN KEY(claimed_by) REFERENCES agents(id)
);
CREATE TABLE memory_embeddings (
    memory_id TEXT NOT NULL,
    provider TEXT NOT NULL,
    dimensions INTEGER NOT NULL,
    vector_json TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(memory_id, provider),
    FOREIGN KEY(memory_id) REFERENCES memory_items(id) ON DELETE CASCADE
);
CREATE TABLE audit_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trace_id TEXT,
    event_type TEXT NOT NULL,
    actor_type TEXT,
    actor_id TEXT,
    task_id TEXT,
    payload_json TEXT NOT NULL,
    prev_hash TEXT,
    hash TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""


V09_ADDITIONS = """
ALTER TABLE agents ADD COLUMN last_seen_at TEXT;
ALTER TABLE agents ADD COLUMN max_concurrency INTEGER NOT NULL DEFAULT 1;
ALTER TABLE agents ADD COLUMN capacity_json TEXT NOT NULL DEFAULT '{}';
ALTER TABLE message_board_events ADD COLUMN dedupe_key TEXT;
ALTER TABLE agent_jobs ADD COLUMN lease_id TEXT;
ALTER TABLE agent_jobs ADD COLUMN lease_token_hash TEXT;
ALTER TABLE agent_jobs ADD COLUMN lease_expires_at TEXT;
ALTER TABLE agent_jobs ADD COLUMN lease_generation INTEGER NOT NULL DEFAULT 0;
ALTER TABLE agent_jobs ADD COLUMN attempt_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE agent_jobs ADD COLUMN max_attempts INTEGER NOT NULL DEFAULT 3;
ALTER TABLE agent_jobs ADD COLUMN last_agent_id TEXT;
ALTER TABLE agent_jobs ADD COLUMN last_failure_reason TEXT;
CREATE TABLE outbox_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    aggregate_type TEXT NOT NULL,
    aggregate_id TEXT NOT NULL,
    topic TEXT NOT NULL,
    event_type TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    message_id TEXT NOT NULL,
    task_id TEXT,
    agent_id TEXT,
    created_at TEXT NOT NULL,
    published_at TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    dedupe_key TEXT NOT NULL UNIQUE
);
CREATE TABLE iphone_capability_requests (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    requesting_agent_id TEXT NOT NULL,
    requesting_job_id TEXT NOT NULL,
    lease_generation INTEGER NOT NULL,
    device_id TEXT NOT NULL,
    capability_name TEXT NOT NULL,
    arguments_json TEXT NOT NULL,
    request_fingerprint TEXT NOT NULL,
    action_digest TEXT NOT NULL,
    status TEXT NOT NULL,
    approval_id TEXT NOT NULL UNIQUE,
    request_audit_id INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    delivered_at TEXT,
    completed_at TEXT,
    FOREIGN KEY(task_id) REFERENCES tasks(id),
    FOREIGN KEY(requesting_agent_id) REFERENCES agents(id),
    FOREIGN KEY(requesting_job_id) REFERENCES agent_jobs(id),
    FOREIGN KEY(device_id) REFERENCES devices(id)
);
CREATE TABLE iphone_capability_grants (
    id TEXT PRIMARY KEY,
    request_id TEXT NOT NULL UNIQUE,
    device_id TEXT NOT NULL,
    capability_name TEXT NOT NULL,
    approval_id TEXT NOT NULL,
    token_hash TEXT NOT NULL UNIQUE,
    action_digest TEXT NOT NULL,
    issued_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    consumed_at TEXT,
    FOREIGN KEY(request_id) REFERENCES iphone_capability_requests(id),
    FOREIGN KEY(device_id) REFERENCES devices(id)
);
CREATE TABLE iphone_capability_results (
    request_id TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    result_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY(request_id) REFERENCES iphone_capability_requests(id)
);
"""


@pytest.mark.asyncio
async def test_future_schema_is_rejected_before_any_authoritative_mutation(
    tmp_path: Path,
) -> None:
    database = tmp_path / "future.db"
    async with aiosqlite.connect(database) as db:
        await db.executescript(
            f"""
            CREATE TABLE future_state(id TEXT PRIMARY KEY, value TEXT NOT NULL);
            INSERT INTO future_state(id,value) VALUES('sentinel','unchanged');
            PRAGMA user_version={SCHEMA_VERSION + 1};
            """
        )
        before_schema = await (
            await db.execute("SELECT type,name,sql FROM sqlite_master ORDER BY type,name")
        ).fetchall()
        before_rows = await (await db.execute("SELECT * FROM future_state")).fetchall()

    with pytest.raises(RuntimeError, match="newer than this control plane"):
        await StateService(database).initialize()

    async with aiosqlite.connect(database) as db:
        after_schema = await (
            await db.execute("SELECT type,name,sql FROM sqlite_master ORDER BY type,name")
        ).fetchall()
        after_rows = await (await db.execute("SELECT * FROM future_state")).fetchall()
        version = await (await db.execute("PRAGMA user_version")).fetchone()

    assert after_schema == before_schema
    assert after_rows == before_rows == [("sentinel", "unchanged")]
    assert version == (SCHEMA_VERSION + 1,)


NOW = "2026-07-01T12:00:00+00:00"
LATER = "2026-07-01T12:10:00+00:00"


async def _create_v08_database(path: Path) -> None:
    async with aiosqlite.connect(path) as db:
        await db.executescript(V08_SCHEMA)
        await db.execute(
            "INSERT INTO audit_events(event_type,task_id,payload_json,created_at) VALUES(?,?,?,?)",
            ("approval.requested", "tsk_historic", '{"approval_id":"apr_historic"}', NOW),
        )
        await db.execute(
            """
            INSERT INTO tasks(
                id,title,input,mode,source,status,priority,created_at,updated_at,completed_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?)
            """,
            (
                "tsk_historic",
                "Historic completed task",
                "List the project root",
                "normal",
                "iphone",
                "completed",
                2,
                NOW,
                LATER,
                LATER,
            ),
        )
        await db.execute(
            """
            INSERT INTO approvals(
                id,task_id,tool_call_id,action_digest,request_audit_id,action,summary,
                risk,status,created_at,expires_at,decided_at,decision_json
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                "apr_historic",
                "tsk_historic",
                "call_historic",
                "sha256:" + "a" * 64,
                1,
                "workspace.list_dir",
                "List the project root",
                "low",
                "approved",
                NOW,
                LATER,
                NOW,
                '{"decision":"approved"}',
            ),
        )
        await db.execute(
            """
            INSERT INTO tool_calls VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                "call_historic",
                "tsk_historic",
                "workspace.list_dir",
                '{"path":"."}',
                "List the project root",
                "low",
                "completed",
                "apr_historic",
                '{"entries":["README.md"]}',
                None,
                NOW,
                LATER,
            ),
        )
        await db.execute(
            "INSERT INTO pairing_codes VALUES(?,?,?,?)",
            ("123456", LATER, 5, NOW),
        )
        await db.execute(
            "INSERT INTO devices VALUES(?,?,?,?,?,?)",
            (
                "dev_historic",
                "Historic iPhone",
                "sha256:" + "b" * 64,
                NOW,
                LATER,
                "pair_historic",
            ),
        )
        await db.execute(
            "INSERT INTO pairing_candidates VALUES(?,?,?,?,?,?,?,?)",
            (
                "pair_historic",
                "sha256:" + "c" * 64,
                "dev_historic",
                "Historic iPhone",
                "finalized",
                NOW,
                LATER,
                NOW,
            ),
        )
        await db.execute(
            "INSERT INTO agents VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                "agt_historic",
                "Historic worker",
                "0.8.0",
                "local-polling",
                None,
                "online",
                '["workspace.list_dir"]',
                "sha256:" + "d" * 64,
                LATER,
                NOW,
                LATER,
            ),
        )
        await db.execute(
            """
            INSERT INTO agent_jobs VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                "job_historic",
                "tsk_historic",
                "workspace.list_dir",
                '{"path":"."}',
                "completed",
                "agt_historic",
                None,
                '{"entries":["README.md"]}',
                None,
                NOW,
                LATER,
                NOW,
                LATER,
                LATER,
            ),
        )
        await db.execute(
            """
            INSERT INTO memory_items VALUES(?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                "mem_historic",
                "project",
                "fact",
                "Ubuntu remains authoritative.",
                "Authority invariant",
                "normal",
                1.0,
                1,
                '{"source":"historic"}',
                NOW,
                LATER,
            ),
        )
        await db.execute(
            "INSERT INTO memory_embeddings VALUES(?,?,?,?,?)",
            ("mem_historic", "fake-v1", 3, "[1.0,0.0,0.0]", LATER),
        )
        await db.execute(
            "INSERT INTO feedback_events VALUES(?,?,?,?,?,?,?,?,?)",
            (
                "fb_historic",
                "tsk_historic",
                "agt_historic",
                "rating",
                "useful",
                0.9,
                "Historic feedback",
                '{"safe":true}',
                LATER,
            ),
        )
        await db.execute(
            "INSERT INTO message_board_events VALUES(NULL,?,?,?,?,?,?,?)",
            (
                "tasks.status",
                "completed",
                "job_historic",
                "agt_historic",
                "tsk_historic",
                '{"job_id":"job_historic","status":"completed"}',
                LATER,
            ),
        )
        await db.execute("PRAGMA user_version=7")
        await db.commit()


async def _upgrade_fixture_to_v09(path: Path) -> None:
    request_fingerprint = canonical_capability_request_fingerprint(
        job_id="job_historic",
        lease_generation=1,
        capability_name="iphone.location.current",
        arguments={},
    )
    async with aiosqlite.connect(path) as db:
        await db.executescript(V09_ADDITIONS)
        await db.execute(
            """
            UPDATE agents SET last_seen_at=?,max_concurrency=2,capacity_json=?
            WHERE id='agt_historic'
            """,
            (LATER, '{"active_jobs":0}'),
        )
        await db.execute(
            "UPDATE message_board_events SET dedupe_key=? WHERE message_id=?",
            ("historic-board-event", "job_historic"),
        )
        await db.execute(
            """
            UPDATE agent_jobs
            SET lease_generation=1,attempt_count=1,max_attempts=3,last_agent_id=?
            WHERE id='job_historic'
            """,
            ("agt_historic",),
        )
        await db.execute(
            """
            INSERT INTO outbox_events(
                aggregate_type,aggregate_id,topic,event_type,payload_json,message_id,
                task_id,agent_id,created_at,published_at,attempts,last_error,dedupe_key
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                "agent_job",
                "job_historic",
                "tasks.status",
                "completed",
                '{"job_id":"job_historic","status":"completed"}',
                "job_historic",
                "tsk_historic",
                "agt_historic",
                LATER,
                LATER,
                1,
                None,
                "historic-outbox-event",
            ),
        )
        await db.execute(
            """
            INSERT INTO iphone_capability_requests VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                "cap_historic",
                "tsk_historic",
                "agt_historic",
                "job_historic",
                1,
                "dev_historic",
                "iphone.location.current",
                "{}",
                request_fingerprint,
                "sha256:" + "e" * 64,
                "completed",
                "apr_cap_historic",
                1,
                NOW,
                LATER,
                NOW,
                LATER,
            ),
        )
        await db.execute(
            "INSERT INTO iphone_capability_grants VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                "grant_historic",
                "cap_historic",
                "dev_historic",
                "iphone.location.current",
                "apr_cap_historic",
                "sha256:" + "f" * 64,
                "sha256:" + "e" * 64,
                NOW,
                LATER,
                NOW,
            ),
        )
        await db.execute(
            "INSERT INTO iphone_capability_results VALUES(?,?,?,?)",
            ("cap_historic", "completed", '{"available":true}', LATER),
        )
        await db.execute("PRAGMA user_version=10")
        await db.commit()


async def _table_names(db: aiosqlite.Connection) -> set[str]:
    return {
        str(row[0])
        for row in await (
            await db.execute("SELECT name FROM sqlite_master WHERE type='table'")
        ).fetchall()
    }


async def _column_names(db: aiosqlite.Connection, table: str) -> set[str]:
    return {
        str(row[1]) for row in await (await db.execute(f"PRAGMA table_info({table})")).fetchall()
    }


async def _authoritative_snapshot(path: Path) -> dict[str, object]:
    async with aiosqlite.connect(path) as db:
        return {
            "task": await (
                await db.execute(
                    "SELECT title,status,priority,completed_at FROM tasks WHERE id='tsk_historic'"
                )
            ).fetchone(),
            "approval": await (
                await db.execute(
                    "SELECT status,action_digest,decision_json FROM approvals "
                    "WHERE id='apr_historic'"
                )
            ).fetchone(),
            "tool": await (
                await db.execute(
                    "SELECT status,result_json FROM tool_calls WHERE id='call_historic'"
                )
            ).fetchone(),
            "job": await (
                await db.execute(
                    "SELECT status,result_json,required_skill FROM agent_jobs "
                    "WHERE id='job_historic'"
                )
            ).fetchone(),
            "device": await (
                await db.execute(
                    "SELECT name,token,last_pairing_id FROM devices WHERE id='dev_historic'"
                )
            ).fetchone(),
            "memory": await (
                await db.execute(
                    "SELECT content,pinned,metadata_json FROM memory_items WHERE id='mem_historic'"
                )
            ).fetchone(),
            "embedding": await (
                await db.execute(
                    "SELECT provider,dimensions,vector_json FROM memory_embeddings "
                    "WHERE memory_id='mem_historic'"
                )
            ).fetchone(),
            "feedback": await (
                await db.execute(
                    "SELECT type,label,score,notes FROM feedback_events WHERE id='fb_historic'"
                )
            ).fetchone(),
            "audit": await (
                await db.execute(
                    "SELECT event_type,task_id,payload_json FROM audit_events WHERE id=1"
                )
            ).fetchone(),
        }


EXPECTED_CORE_SNAPSHOT = {
    "task": ("Historic completed task", "completed", 2, LATER),
    "approval": ("approved", "sha256:" + "a" * 64, '{"decision":"approved"}'),
    "tool": ("completed", '{"entries":["README.md"]}'),
    "job": ("completed", '{"entries":["README.md"]}', "workspace.list_dir"),
    "device": ("Historic iPhone", "sha256:" + "b" * 64, "pair_historic"),
    "memory": ("Ubuntu remains authoritative.", 1, '{"source":"historic"}'),
    "embedding": ("fake-v1", 3, "[1.0,0.0,0.0]"),
    "feedback": ("rating", "useful", 0.9, "Historic feedback"),
    "audit": ("approval.requested", "tsk_historic", '{"approval_id":"apr_historic"}'),
}


@pytest.mark.asyncio
@pytest.mark.parametrize("historic_version", ["0.8", "0.9"])
async def test_v010_upgrade_is_additive_restart_safe_and_preserves_state(
    tmp_path: Path,
    historic_version: str,
) -> None:
    path = tmp_path / f"historic-{historic_version}.db"
    await _create_v08_database(path)
    if historic_version == "0.9":
        await _upgrade_fixture_to_v09(path)

    state = StateService(path)
    await state.initialize()
    first_snapshot = await _authoritative_snapshot(path)
    await state.initialize()
    second_snapshot = await _authoritative_snapshot(path)

    assert first_snapshot == EXPECTED_CORE_SNAPSHOT
    assert second_snapshot == first_snapshot

    async with aiosqlite.connect(path) as db:
        version = await (await db.execute("PRAGMA user_version")).fetchone()
        assert version == (SCHEMA_VERSION,)

        tables = await _table_names(db)
        assert {
            "outbox_events",
            "control_plane_instances",
            "maintenance_leases",
            "message_consumer_deliveries",
            "message_consumer_checkpoints",
            "idempotency_receipts",
            "agent_score_snapshots",
            "scheduler_decisions",
        }.issubset(tables)

        assert {
            "event_id",
            "publishing_owner",
            "publishing_started_at",
            "publishing_lease_expires_at",
            "publish_generation",
        }.issubset(await _column_names(db, "outbox_events"))
        assert {
            "last_seen_at",
            "max_concurrency",
            "capacity_json",
            "runtime",
            "supported_protocol_version",
        }.issubset(await _column_names(db, "agents"))

        agent_card_defaults = await (
            await db.execute(
                """
                SELECT runtime,supported_protocol_version,max_concurrency
                FROM agents WHERE id='agt_historic'
                """
            )
        ).fetchone()
        assert agent_card_defaults is not None
        assert agent_card_defaults[0:2] == ("python", "mongars-worker-v0.9")

        # Pairing codes/candidates are intentionally transient; the durable,
        # token-hashed device identity and pairing lineage above must survive.
        assert await (await db.execute("SELECT COUNT(*) FROM pairing_codes")).fetchone() == (0,)
        assert await (await db.execute("SELECT COUNT(*) FROM pairing_candidates")).fetchone() == (
            0,
        )

        if historic_version == "0.9":
            outbox = await (
                await db.execute(
                    """
                    SELECT aggregate_id,dedupe_key,published_at,event_id,
                           publishing_owner,publishing_lease_expires_at,publish_generation
                    FROM outbox_events WHERE dedupe_key='historic-outbox-event'
                    """
                )
            ).fetchone()
            assert outbox == (
                "job_historic",
                "historic-outbox-event",
                LATER,
                "evt_outbox_1",
                None,
                None,
                0,
            )
            capability = await (
                await db.execute(
                    """
                    SELECT status,device_id,capability_name,arguments_json,completed_at
                    FROM iphone_capability_requests WHERE id='cap_historic'
                    """
                )
            ).fetchone()
            assert capability == (
                "completed",
                "dev_historic",
                "iphone.location.current",
                "{}",
                LATER,
            )
            assert await (
                await db.execute(
                    "SELECT id,consumed_at FROM iphone_capability_grants "
                    "WHERE request_id='cap_historic'"
                )
            ).fetchone() == ("grant_historic", NOW)
            assert await (
                await db.execute(
                    "SELECT status,result_json FROM iphone_capability_results "
                    "WHERE request_id='cap_historic'"
                )
            ).fetchone() == ("completed", '{"available":true}')

        # Empty v0.10 operational tables prove an additive migration rather
        # than a synthetic recreation of historical domain state.
        for table in (
            "control_plane_instances",
            "maintenance_leases",
            "message_consumer_deliveries",
            "message_consumer_checkpoints",
            "idempotency_receipts",
            "agent_score_snapshots",
            "scheduler_decisions",
        ):
            count = await (await db.execute(f"SELECT COUNT(*) FROM {table}")).fetchone()
            assert count == (0,), json.dumps({"unexpected_rows_in": table})


@pytest.mark.asyncio
async def test_v010_upgrade_quarantines_legacy_privileged_agent_card(tmp_path: Path) -> None:
    path = tmp_path / "historic-privileged-agent.db"
    await _create_v08_database(path)
    async with aiosqlite.connect(path) as db:
        await db.execute(
            "UPDATE agents SET skills_json=? WHERE id='agt_historic'",
            ('["process.run","workspace.write_text"]',),
        )
        await db.commit()

    state = StateService(path)
    await state.initialize()

    agent = await state.get_agent("agt_historic")
    assert agent is not None
    assert agent["status"] == "unverified"
    assert agent["skills"] == []
    assert agent["agent_card"]["skills"] == []
    async with aiosqlite.connect(path) as db:
        audit = await (
            await db.execute(
                """SELECT event_type,payload_json FROM audit_events
                WHERE event_type='agent.card.quarantined'"""
            )
        ).fetchone()
    assert audit is not None
    assert json.loads(str(audit[1])) == {
        "agent_id": "agt_historic",
        "reason": "legacy registration outside v0.10 policy",
    }
