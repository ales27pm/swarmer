"""Offline protocol receipts for the explicit Swift approval lane; no compiler run."""

from __future__ import annotations

import hashlib
import json

import aiosqlite
import pytest

from app.models import MemoryUpdate
from app.services.memory_symbolic_contracts import SymbolicCatalog
from tests.test_memory_symbolic_store import identity, memory, propose
from tests.test_swift_project_transfer import _approve, _claim, _prepare, _proof, _source_path


@pytest.mark.asyncio
@pytest.mark.parametrize("invalidate", [False, True])
async def test_approved_swift_transport_queue_claim_heartbeat_source_result(
    client, paired_headers, test_app, invalidate
):
    project = await _prepare(test_app, symbolic=True)
    item, source = await memory(test_app.state.state_service, "Hello package source observation.")
    proposal = await propose(
        test_app.state.state_service, source.bindings, subject=identity("Hello")
    )
    test_app.state.agent_dispatcher.symbolic_catalogs = (
        SymbolicCatalog(namespace="software", scheme_id="engineering"),
    )
    approval = _approve(client, paired_headers, project)
    async with aiosqlite.connect(test_app.state.settings.db_path) as db:
        row = await (
            await db.execute(
                "SELECT payload_json FROM agent_jobs WHERE id=?", (approval["job_id"],)
            )
        ).fetchone()
    payload = json.loads(row[0])
    assert payload["symbolic_context"]["evidence"][0]["proposal"] == proposal.model_dump()
    assert payload["symbolic_context_binding"]["node_id"] == approval["validation_id"]
    operation = {k: v for k, v in payload.items() if not k.startswith("symbolic_context")}
    agent = project["agent"]
    if invalidate:
        await test_app.state.state_service.update_memory(
            item["id"], MemoryUpdate(content="Changed."), "test"
        )
        assert (
            await test_app.state.agent_dispatcher.claim(
                agent["id"], context_protocols=("symbolic-v1",)
            )
            is None
        )
        async with aiosqlite.connect(test_app.state.settings.db_path) as db:
            assert await (
                await db.execute(
                    "SELECT status,attempt_count,error FROM agent_jobs WHERE id=?",
                    (approval["job_id"],),
                )
            ).fetchone() == ("cancelled", 0, "symbolic_context_changed")
        return
    job, headers = _claim(client, project, symbolic=True)
    assert job["payload"] == payload
    path = f"/agents/{agent['id']}/jobs/{job['id']}"
    assert client.post(path + "/heartbeat", headers=headers, json=_proof(job)).status_code == 200
    source_reply = client.post(_source_path(project, job), headers=headers, json=_proof(job))
    assert source_reply.status_code == 200, source_reply.text
    assert source_reply.json()["files"] == project["files"]
    receipt = {
        "operation": "test",
        "kind": "swiftpm",
        "status": "passed",
        "exit_code": 0,
        "source_sha256": operation["source_sha256"],
        "request_sha256": hashlib.sha256(
            json.dumps(operation, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
        "project_revision": operation["project_revision"],
        "source_unchanged": True,
        "tests_executed": 1,
        "test_evidence_format": "swiftpm_xunit",
        "test_failures": 0,
        "duration_ms": 1,
        "artifact_directory": ".swarmer-swift-runs/" + "c" * 32,
        "report_error": None,
    }
    response = client.post(
        path + "/result",
        headers=headers,
        json={**_proof(job), "status": "completed", "result": receipt},
    )
    assert response.status_code == 200, response.text
    assert client.get(project["path"], headers=paired_headers).json()["status"] == "passed"
