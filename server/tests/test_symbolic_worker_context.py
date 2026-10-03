from __future__ import annotations

import copy
import hashlib
import json

import pytest

from app.services.agent_capsule import validate_symbolic_context
from app.services.memory_symbolic_contracts import SymbolicEvidence
from app.services.remote_job_policy import validate_remote_job


def symbolic_context() -> dict:
    claim = {
        "scope": "general",
        "namespace": "software",
        "scheme_id": "engineering",
        "kind": "constraint",
        "subject": {"type": "identity", "namespace": "python", "identity": "Cache.py"},
        "predicate": {"type": "identity", "namespace": "software", "identity": "size"},
        "object": {
            "type": "literal",
            "datatype": "quantity",
            "lexical_value": "5.0",
            "unit": "MiB",
            "language": None,
        },
        "polarity": "negated",
        "modality": "required",
        "version": "v2.0",
        "applicability": {"os": ["Linux"], "enabled": False, "retry": 0},
        "effective_conditions": [
            {
                "relation": "only_after",
                "argument": {
                    "type": "identity",
                    "namespace": "software",
                    "identity": "ApprovalReceived",
                },
            }
        ],
    }
    digest = hashlib.sha256(
        json.dumps(
            {"policy": "symbolic-claim-v1", "claim": claim},
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    evidence = SymbolicEvidence.model_validate(
        {
            "catalog": {"namespace": "software", "scheme_id": "engineering"},
            "proposal": {
                "proposal_id": "proposal_1",
                "claim": claim,
                "claim_sha256": digest,
                "sources": [
                    {
                        "binding": {
                            "memory_id": "mem_1",
                            "revision": 1,
                            "view_id": "view_original",
                            "field": "content",
                            "field_sha256": "a" * 64,
                            "document_sha256": "b" * 64,
                        },
                        "scope": "general",
                        "origin": "user_statement",
                    }
                ],
                "concept_ids": [],
                "lifecycle": "active",
                "created_at": "2026-10-03T00:00:00Z",
            },
            "matches": [{"channel": "exact_identity", "value": "Cache.py", "field": "subject"}],
            "concepts": [],
            "relations": [],
            "read_token": "c" * 64,
        }
    ).model_dump()
    return {
        "schema_version": "symbolic-context-v1",
        "evidence": [evidence],
        "status": "available",
        "grants_authority": False,
    }


def symbolic_binding() -> dict:
    return {
        "goal_id": "goal_1",
        "node_id": "node_1",
        "conversation_revision": 0,
        "project_id": None,
        "catalogs": [{"namespace": "software", "scheme_id": "engineering"}],
    }


def test_typed_symbolic_context_preserves_values_and_is_independent_copy():
    original = symbolic_context()
    accepted = validate_symbolic_context(original)
    assert accepted == original
    accepted["evidence"][0]["proposal"]["claim"]["applicability"]["os"].append("macOS")
    assert original["evidence"][0]["proposal"]["claim"]["applicability"]["os"] == ["Linux"]


@pytest.mark.parametrize(
    "path,value",
    [
        (("grants_authority",), True),
        (("status",), "verified"),
        (("status",), "omitted_budget"),
        (("evidence", 0, "validation_status"), "validated"),
        (("evidence", 0, "proposal", "grants_authority"), True),
        (("evidence", 0, "proposal", "claim", "object", "lexical_value"), "6.0"),
        (("evidence", 0, "proposal", "sources", 0, "binding", "revision"), True),
        (("evidence", 0, "read_token"), "invalid"),
        (("evidence", 0, "proposal", "claim", "surprise"), "authority"),
    ],
)
def test_transport_rejects_invalid_or_tampered_evidence(path, value):
    context = symbolic_context()
    target = context
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    with pytest.raises(ValueError, match="invalid symbolic context"):
        validate_symbolic_context(context)


@pytest.mark.parametrize(
    "skill,payload",
    [
        ("workspace.list_dir", {"path": "."}),
        ("research.query", {"query": "Cache", "max_results": 5}),
        ("code_review.git_status", {}),
        (
            "writing.draft",
            {"schema_version": "1.0", "objective": "Explain caching.", "conversation": []},
        ),
        ("code.generate_python", {"objective": "Explain caching."}),
    ],
)
def test_remote_policy_preserves_context_separately_from_operation_arguments(skill, payload):
    before = copy.deepcopy(payload)
    operation = validate_remote_job(skill, payload)
    context = symbolic_context()
    actual = validate_remote_job(
        skill,
        {**payload, "symbolic_context": context, "symbolic_context_binding": symbolic_binding()},
    )
    assert actual.pop("symbolic_context") == context
    assert actual.pop("symbolic_context_binding") == symbolic_binding()
    assert actual == operation
    assert payload == before


async def prepared_job(tmp_path):
    from app.models import AgentCreate
    from app.services.episode_memory import EpisodeMemoryService
    from app.services.memory_symbolic_contracts import SymbolicCatalog
    from app.services.strategy_retrieval import StrategyRetrieval
    from tests.test_goal_runtime_recovery import _create_and_start, _manager, _worker_plan
    from tests.test_memory_symbolic_store import memory, propose

    plan = _worker_plan(objective="Inspect cache")
    plan.nodes[0].objective = "Inspect cache"
    manager = await _manager(tmp_path / "state.db", plan)
    item, source = await memory(manager.state_service)
    proposal = await propose(manager.state_service, source.bindings)
    catalogs = (SymbolicCatalog(namespace="software", scheme_id="engineering"),)
    episodes = EpisodeMemoryService(manager.db_path)
    await episodes.initialize()
    manager.strategy_retrieval = StrategyRetrieval(
        manager.db_path,
        episodes,
        max_success_hints=0,
        max_failure_hints=0,
        max_memory_hints=0,
        symbolic_catalogs=catalogs,
    )
    manager.agent_dispatcher.symbolic_catalogs = catalogs
    goal = await _create_and_start(manager, objective="Inspect cache")
    agent = await manager.state_service.register_agent(
        AgentCreate(
            name="test reader", endpoint="http://127.0.0.1:9001", skills=["workspace.list_dir"]
        ),
        "test",
    )
    await manager.state_service.heartbeat_agent(agent["id"], "online", agent["credential"])
    return manager, agent, item, proposal, goal


@pytest.mark.asyncio
async def test_real_goal_queue_claim_preserves_typed_context_and_accepts_operation_result(tmp_path):
    import aiosqlite

    manager, agent, item, proposal, _ = await prepared_job(tmp_path)
    claimed = await manager.agent_dispatcher.claim(agent["id"])
    assert claimed is not None
    [evidence] = claimed["payload"]["symbolic_context"]["evidence"]
    assert evidence["proposal"] == proposal.model_dump()
    assert evidence["proposal"]["sources"][0]["binding"]["memory_id"] == item["id"]
    assert set(claimed["payload"]) == {"path", "symbolic_context", "symbolic_context_binding"}
    result = {"path": ".", "entries": [], "truncated": False, "content_trust": "untrusted"}
    completed = await manager.agent_dispatcher.submit_result(
        agent["id"],
        claimed["id"],
        claimed["claim_token"],
        status="completed",
        result=result,
        error=None,
        lease_id=claimed["lease_id"],
        lease_generation=claimed["lease_generation"],
    )
    assert completed[0]["status"] == "completed" and completed[1] is True
    async with aiosqlite.connect(manager.db_path) as db:
        calls = await (await db.execute("SELECT role FROM goal_model_calls")).fetchall()
    assert calls == [("planner",)]


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["source", "scope", "sensitivity", "catalog", "binding"])
async def test_changed_context_is_cancelled_before_worker_lease_without_private_error(
    tmp_path, change
):
    import aiosqlite

    from app.models import MemoryUpdate

    manager, agent, item, proposal, _ = await prepared_job(tmp_path)
    if change == "source":
        await manager.state_service.update_memory(
            item["id"], MemoryUpdate(content="Changed."), "test"
        )
    elif change in {"scope", "sensitivity"}:
        value = "project:other" if change == "scope" else "sensitive"
        async with aiosqlite.connect(manager.db_path) as db:
            await db.execute(f"UPDATE memory_items SET {change}=? WHERE id=?", (value, item["id"]))
            await db.commit()
    elif change == "catalog":
        manager.agent_dispatcher.symbolic_catalogs = ()
    else:
        async with aiosqlite.connect(manager.db_path) as db:
            row = await (await db.execute("SELECT id,payload_json FROM agent_jobs")).fetchone()
            payload = json.loads(row[1])
            payload["symbolic_context_binding"]["project_id"] = "other"
            await db.execute(
                "UPDATE agent_jobs SET payload_json=? WHERE id=?", (json.dumps(payload), row[0])
            )
            await db.commit()
    assert await manager.agent_dispatcher.claim(agent["id"]) is None
    async with aiosqlite.connect(manager.db_path) as db:
        row = await (
            await db.execute(
                "SELECT status,attempt_count,error,last_failure_reason,lease_id FROM agent_jobs"
            )
        ).fetchone()
        calls = await (await db.execute("SELECT role FROM goal_model_calls")).fetchall()
    assert row == ("cancelled", 0, "symbolic_context_changed", "symbolic_context_changed", None)
    assert item["id"] not in repr(row) and proposal.proposal_id not in repr(row)
    assert calls == [("planner",)]


def test_swift_receipt_digest_excludes_validated_advisory_transport():
    from app.services.swift_contracts import valid_swift_receipt, validate_swift_project_payload

    operation = {"kind": "swiftpm", "source_sha256": "a" * 64}
    payload = {
        **operation,
        "symbolic_context": symbolic_context(),
        "symbolic_context_binding": symbolic_binding(),
    }
    digest = hashlib.sha256(
        json.dumps(operation, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    receipt = {
        "operation": "build",
        "kind": "swiftpm",
        "status": "passed",
        "exit_code": 0,
        "source_sha256": "a" * 64,
        "request_sha256": digest,
        "source_unchanged": True,
        "tests_executed": 0,
        "test_evidence_format": "swiftpm_xunit",
        "test_failures": 0,
        "duration_ms": 1,
        "artifact_directory": ".swarmer-swift-runs/" + "b" * 32,
        "report_error": None,
    }
    assert validate_swift_project_payload(payload) == operation
    assert valid_swift_receipt("code.swift.build", receipt, operation)
    assert valid_swift_receipt("code.swift.build", receipt, payload)
    payload["symbolic_context"]["grants_authority"] = True
    assert not valid_swift_receipt("code.swift.build", receipt, payload)


@pytest.mark.asyncio
async def test_source_deleted_after_claim_cannot_be_accepted_as_current_result(tmp_path):
    import aiosqlite

    from app.services.agent_dispatcher import AgentDispatchConflict

    manager, agent, item, _, _ = await prepared_job(tmp_path)
    claimed = await manager.agent_dispatcher.claim(agent["id"])
    assert claimed
    await manager.state_service.delete_memory(item["id"], "test")
    with pytest.raises(AgentDispatchConflict, match="^symbolic_context_changed$"):
        await manager.agent_dispatcher.submit_result(
            agent["id"],
            claimed["id"],
            claimed["claim_token"],
            status="completed",
            result={"entries": []},
            error=None,
            lease_id=claimed["lease_id"],
            lease_generation=claimed["lease_generation"],
        )
    async with aiosqlite.connect(manager.db_path) as db:
        row = await (
            await db.execute("SELECT status,result_json,error,payload_json FROM agent_jobs")
        ).fetchone()
        task = await (
            await db.execute("SELECT status FROM tasks WHERE id=?", (claimed["task_id"],))
        ).fetchone()
    assert row[:3] == ("cancelled", None, "symbolic_context_changed")
    assert task == ("cancelled",)
    assert (
        json.loads(row[3]) == claimed["payload"]
    )  # Historical selection survives without promotion.


@pytest.mark.asyncio
async def test_source_changed_between_attachment_and_queue_fails_closed(tmp_path, monkeypatch):
    from app.models import MemoryUpdate
    from app.services import goal_manager as module
    from app.services.state_service import StateService

    real = module.attach_symbolic_worker_context
    attached = []

    async def change_after_attachment(db_path, payload, **kwargs):
        result = await real(db_path, payload, **kwargs)
        [evidence] = result["symbolic_context"]["evidence"]
        memory_id = evidence["proposal"]["sources"][0]["binding"]["memory_id"]
        await StateService(db_path).update_memory(
            memory_id, MemoryUpdate(content="Changed after selection."), "test"
        )
        attached.append(memory_id)
        return result

    monkeypatch.setattr(module, "attach_symbolic_worker_context", change_after_attachment)
    await prepared_job(tmp_path)
    assert len(attached) == 1
    import aiosqlite

    async with aiosqlite.connect(tmp_path / "state.db") as db:
        assert await (await db.execute("SELECT COUNT(*) FROM agent_jobs")).fetchone() == (0,)


@pytest.mark.asyncio
async def test_valid_match_overflow_is_explicitly_omitted_at_worker_boundary(tmp_path):
    import aiosqlite

    from app.services.memory_concepts import ConceptLabel, LanguageAnnotation
    from app.services.worker_context import attach_symbolic_worker_context
    from tests.test_memory_symbolic_store import concept, identity, propose

    manager, _, _, proposal, goal = await prepared_job(tmp_path)
    labels = [
        ConceptLabel(
            text=f"label{i:03}",
            language=LanguageAnnotation(tag="en", origin="declared"),
            role="pref" if i == 0 else "alt",
        )
        for i in range(64)
    ]
    definition = await concept(manager.state_service, labels=labels)
    await propose(
        manager.state_service,
        [s.binding for s in proposal.sources],
        subject=identity(definition.concept_id),
        object=identity("Archive.py"),
    )
    query = " ".join([label.text for label in labels] + ["Archive.py"])
    async with aiosqlite.connect(manager.db_path) as db:
        node = await (
            await db.execute(
                "SELECT id,conversation_revision FROM plan_nodes WHERE goal_run_id=?", (goal["id"],)
            )
        ).fetchone()
        await db.execute("UPDATE plan_nodes SET objective=? WHERE id=?", (query, node[0]))
        await db.commit()
    result = await attach_symbolic_worker_context(
        manager.db_path,
        {"path": "."},
        goal_id=goal["id"],
        node_id=node[0],
        conversation_revision=node[1],
        catalogs=manager.agent_dispatcher.symbolic_catalogs,
    )
    assert result["symbolic_context"] == {
        "schema_version": "symbolic-context-v1",
        "evidence": [],
        "status": "omitted_budget",
        "grants_authority": False,
    }
    assert result["path"] == "."
    assert result["symbolic_context_binding"]["goal_id"] == goal["id"]
    assert result["symbolic_context_binding"]["catalogs"] == [
        {"namespace": "software", "scheme_id": "engineering"}
    ]


@pytest.mark.asyncio
async def test_other_symbolic_store_errors_are_not_disguised_as_budget_omission(
    tmp_path, monkeypatch
):
    from app.services import memory_symbolic_search as reader
    from app.services.memory_symbolic_store import SymbolicStoreError
    from app.services.worker_context import attach_symbolic_worker_context

    manager, _, _, _, goal = await prepared_job(tmp_path)
    [node] = await manager.graph.list_nodes(goal["id"])

    async def unavailable(*args, **kwargs):
        raise SymbolicStoreError("symbolic_unavailable", 503)

    monkeypatch.setattr(reader, "search_symbolic_evidence", unavailable)
    with pytest.raises(ValueError, match="^symbolic worker context unavailable$"):
        await attach_symbolic_worker_context(
            manager.db_path,
            {"path": "."},
            goal_id=goal["id"],
            node_id=node["id"],
            conversation_revision=node["conversation_revision"],
            catalogs=manager.agent_dispatcher.symbolic_catalogs,
        )
