"""Real paired preparation and local-result acceptance, without any model or network."""

import socket
import sqlite3

import pytest

from app.services import goal_manager as goal_module
from app.services import memory_local_context as local_module
from app.services.evaluator_provider import NoopEvaluatorProvider
from tests.test_goal_api import _create_goal, _plan
from tests.test_symbolic_direct_execution import seeded as seeded_fixture


@pytest.fixture(name="seeded")
def symbolic_seed(client, paired_headers, test_app):
    return seeded_fixture.__wrapped__(client, paired_headers, test_app)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("local receipt tests must never use network")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)


INTENT = "Inspect the silent clock"
TOOL = {
    "tool_name": "workspace.write_text",
    "arguments": {"path": "draft.txt", "content": "A bounded draft"},
    "summary": "Write the draft",
    "planner_source": "iphone_local",
}


def prepare(client, headers, **changes):
    body = {
        "purpose": "tool_proposal",
        "intent": INTENT,
        "mode": "normal",
        "source": "iphone_local",
        **changes,
    }
    response = client.post("/memory/local-context", headers=headers, json=body)
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    return response.json()


def receipt(context):
    return {key: context["receipt"][key] for key in ("id", "context_sha256")}


def task(client, headers, intent=INTENT):
    response = client.post(
        "/chat", headers=headers, json={"content": intent, "mode": "normal", "start_task": True}
    )
    assert response.status_code == 201, response.text
    return response.json()["task"]["id"]


def records(app, table):
    with sqlite3.connect(app.state.settings.db_path) as db:
        return db.execute("SELECT * FROM " + table).fetchall()


@pytest.mark.parametrize("query", ["horloge silencieuse", "silent clock"])
def test_prepare_is_label_based_complete_scoped_and_creates_no_task(
    client, paired_headers, test_app, seeded, query
):
    before = {
        t: records(test_app, t)
        for t in ("tasks", "goal_model_calls", "tool_calls", "approvals", "audit_events")
    }
    context = prepare(client, paired_headers, intent=query)
    assert context["enabled"] and context["project_id"] is None
    [proof] = context["symbolic_context"]["evidence"]
    assert proof["proposal"] == seeded[1]
    assert proof["grants_authority"] is False and proof["validation_status"] == "unvalidated"
    assert (
        proof["proposal"]["claim"]["effective_conditions"][0]["argument"]["lexical_value"]
        == "Cache/State.py"
    )
    assert {t: records(test_app, t) for t in before} == before
    assert len(records(test_app, "memory_local_context_receipts")) == 1
    assert {r[1] for r in records(test_app, "memory_local_context_sources")} == {seeded[0]}


@pytest.mark.parametrize("budget", [0, 1, 100, 500])
def test_budget_omits_whole_evidence_and_digest_is_nonce_independent(
    client, paired_headers, test_app, seeded, budget
):
    first = prepare(client, paired_headers, max_context_bytes=budget)
    second = prepare(client, paired_headers, max_context_bytes=budget)
    assert first["symbolic_context"] == {
        "schema_version": "symbolic-context-v1",
        "evidence": [],
        "status": "omitted_budget",
        "grants_authority": False,
    }
    assert first["receipt"]["id"] != second["receipt"]["id"]
    assert first["receipt"]["context_sha256"] == second["receipt"]["context_sha256"]


@pytest.mark.parametrize(
    "change",
    [
        "missing",
        "digest",
        "other_device",
        "input",
        "source",
        "sensitivity",
        "catalogs",
        "expired",
        "corrupt",
    ],
)
def test_tool_receipt_rejects_stale_or_forged_without_any_effect(
    client, paired_headers, test_app, seeded, change
):
    context = prepare(client, paired_headers)
    tid = task(client, paired_headers, intent="Different input" if change == "input" else INTENT)
    ref = receipt(context)
    with sqlite3.connect(test_app.state.settings.db_path) as db:
        if change == "other_device":
            db.execute("UPDATE memory_local_context_receipts SET device_id='other'")
        if change == "source":
            db.execute("UPDATE memory_items SET content='Changed' WHERE id=?", (seeded[0],))
        if change == "sensitivity":
            db.execute("UPDATE memory_items SET sensitivity='sensitive' WHERE id=?", (seeded[0],))
        if change == "expired":
            db.execute(
                "UPDATE memory_local_context_receipts SET expires_at='2000-01-01T00:00:00+00:00'"
            )
        if change == "corrupt":
            db.execute("UPDATE memory_local_context_receipts SET context_json='{}'")
    if change == "catalogs":
        test_app.state.strategy_retrieval.symbolic_catalogs = ()
    if change == "digest":
        ref["context_sha256"] = "0" * 64
    body = {**TOOL, **({} if change == "missing" else {"local_context_receipt": ref})}
    response = client.post(f"/tasks/{tid}/tool-calls", headers=paired_headers, json=body)
    assert response.status_code == 409, response.text
    assert records(test_app, "tool_calls") == [] and records(test_app, "approvals") == []
    with sqlite3.connect(test_app.state.settings.db_path) as db:
        assert db.execute("SELECT status FROM tasks WHERE id=?", (tid,)).fetchone() == ("created",)
        assert db.execute("SELECT accepted_at FROM memory_local_context_receipts").fetchone() == (
            None,
        )


def test_tool_acceptance_is_one_effect_replay_same_target_and_cannot_cross_target(
    client, paired_headers, test_app, seeded
):
    context = prepare(client, paired_headers)
    tid = task(client, paired_headers)
    body = {**TOOL, "local_context_receipt": receipt(context)}
    first = client.post(f"/tasks/{tid}/tool-calls", headers=paired_headers, json=body)
    assert first.status_code == 200, first.text
    before = {t: records(test_app, t) for t in ("tool_calls", "approvals", "audit_events")}
    replay = client.post(f"/tasks/{tid}/tool-calls", headers=paired_headers, json=body)
    assert replay.status_code == 200 and replay.json() == first.json(), replay.text
    assert {t: records(test_app, t) for t in before} == before
    other = task(client, paired_headers)
    assert (
        client.post(f"/tasks/{other}/tool-calls", headers=paired_headers, json=body).status_code
        == 409
    )
    changed = {**body, "arguments": {"path": "other.txt", "content": "Different"}}
    assert (
        client.post(f"/tasks/{tid}/tool-calls", headers=paired_headers, json=changed).status_code
        == 409
    )
    assert len(records(test_app, "tool_calls")) == 1 and len(records(test_app, "approvals")) == 1


def test_catalog_change_during_tool_audit_rolls_back_consumption_and_approval(
    client, paired_headers, test_app, seeded, monkeypatch
):
    context = prepare(client, paired_headers)
    tid = task(client, paired_headers)
    original = local_module.append_audit_event

    async def mutate(*args, **kwargs):
        result = await original(*args, **kwargs)
        test_app.state.strategy_retrieval.symbolic_catalogs = ()
        return result

    monkeypatch.setattr(local_module, "append_audit_event", mutate)
    response = client.post(
        f"/tasks/{tid}/tool-calls",
        headers=paired_headers,
        json={**TOOL, "local_context_receipt": receipt(context)},
    )
    assert response.status_code == 409, response.text
    assert not records(test_app, "tool_calls") and not records(test_app, "approvals")
    with sqlite3.connect(test_app.state.settings.db_path) as db:
        assert db.execute("SELECT accepted_at FROM memory_local_context_receipts").fetchone() == (
            None,
        )
        assert (
            db.execute(
                "SELECT id FROM audit_events WHERE event_type='memory.local_context.accepted'"
            ).fetchall()
            == []
        )


@pytest.mark.parametrize("accepted", [False, True])
def test_forgetting_erases_copied_pending_or_accepted_context_without_fk(
    client, paired_headers, test_app, seeded, accepted
):
    context = prepare(client, paired_headers)
    if accepted:
        tid = task(client, paired_headers)
        assert (
            client.post(
                f"/tasks/{tid}/tool-calls",
                headers=paired_headers,
                json={**TOOL, "local_context_receipt": receipt(context)},
            ).status_code
            == 200
        )
    response = client.delete("/memory/" + seeded[0], headers=paired_headers)
    assert response.status_code == 204, response.text
    assert records(test_app, "memory_local_context_receipts") == []
    assert records(test_app, "memory_local_context_sources") == []


def test_disabled_is_no_receipt_and_legacy_tool_still_uses_existing_policy(
    client, paired_headers, test_app
):
    context = prepare(client, paired_headers)
    assert (
        not context["enabled"]
        and context["symbolic_context"] is None
        and context["receipt"] is None
    )
    assert records(test_app, "memory_local_context_receipts") == []
    tid = task(client, paired_headers)
    response = client.post(f"/tasks/{tid}/tool-calls", headers=paired_headers, json=TOOL)
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "waiting_permission"


def goal_setup(client, headers):
    agent = client.post(
        "/agents/register",
        headers=headers,
        json={
            "name": "reader",
            "endpoint": "https://worker.invalid",
            "skills": ["workspace.list_dir"],
        },
    ).json()
    assert (
        client.post(
            f"/agents/{agent['id']}/heartbeat",
            headers={"Authorization": "Bearer " + agent["credential"]},
            json={"status": "online"},
        ).status_code
        == 200
    )
    goal = _create_goal(client, headers, objective=INTENT, autonomy_profile="manual")["goal"]
    memory = client.post(
        f"/goals/{goal['id']}/memory-context",
        headers=headers,
        json={"purpose": "planner", "expected_goal_updated_at": goal["updated_at"]},
    )
    assert memory.status_code == 200, memory.text
    fresh = client.get(f"/goals/{goal['id']}", headers=headers).json()["goal"]
    context = client.post(
        "/memory/local-context",
        headers=headers,
        json={
            "purpose": "goal_plan",
            "goal_id": goal["id"],
            "expected_goal_updated_at": fresh["updated_at"],
        },
    )
    assert context.status_code == 200, context.text
    body = {
        "planner_source": "iphone_local",
        "plan_proposal": _plan(INTENT, temporary_id="inspect"),
        "memory_context_fingerprint": memory.json()["context_fingerprint"],
        "local_context_receipt": receipt(context.json()),
    }
    return goal, context.json(), body


@pytest.mark.parametrize(
    "change", [None, "source", "conversation", "project", "catalogs", "missing", "during_audit"]
)
def test_goal_initial_plan_consumes_local_receipt_atomically(
    client, paired_headers, test_app, seeded, monkeypatch, change
):
    test_app.state.goal_manager.evaluator = NoopEvaluatorProvider()
    goal, context, body = goal_setup(client, paired_headers)
    assert context["goal_id"] == goal["id"] and context["project_id"]
    assert context["symbolic_context"]["evidence"][0]["proposal"] == seeded[1]
    with sqlite3.connect(test_app.state.settings.db_path) as db:
        if change == "source":
            db.execute("UPDATE memory_items SET content='Changed' WHERE id=?", (seeded[0],))
        if change == "conversation":
            db.execute(
                "UPDATE goal_runs SET conversation_revision=conversation_revision+1 WHERE id=?",
                (goal["id"],),
            )
        if change == "project":
            db.execute("DELETE FROM goal_project_links WHERE goal_run_id=?", (goal["id"],))
    if change == "catalogs":
        test_app.state.strategy_retrieval.symbolic_catalogs = ()
    if change == "missing":
        body.pop("local_context_receipt")
    if change == "during_audit":
        original = goal_module.append_audit_event

        async def mutate(db, event, *args, **kwargs):
            result = await original(db, event, *args, **kwargs)
            if event == "goal.plan.accepted":
                test_app.state.strategy_retrieval.symbolic_catalogs = ()
            return result

        monkeypatch.setattr(goal_module, "append_audit_event", mutate)
    response = client.post(f"/goals/{goal['id']}/start", headers=paired_headers, json=body)
    if change:
        assert response.status_code == 409, response.text
        assert records(test_app, "plan_nodes") == [] and records(test_app, "agent_jobs") == []
        with sqlite3.connect(test_app.state.settings.db_path) as db:
            assert db.execute(
                "SELECT accepted_at FROM memory_local_context_receipts"
            ).fetchone() == (None,)
    else:
        assert response.status_code == 200, response.text
        before = {t: records(test_app, t) for t in ("plan_nodes", "agent_jobs", "audit_events")}
        replay = client.post(f"/goals/{goal['id']}/start", headers=paired_headers, json=body)
        assert replay.status_code == 200, replay.text
        assert {t: records(test_app, t) for t in before} == before


def test_pending_receipt_cannot_advance_another_started_goal_without_legacy_fingerprint(
    client, paired_headers, test_app, seeded
):
    test_app.state.goal_manager.evaluator = NoopEvaluatorProvider()
    _, _, first = goal_setup(client, paired_headers)
    other, _, other_body = goal_setup(client, paired_headers)
    assert (
        client.post(
            f"/goals/{other['id']}/start", headers=paired_headers, json=other_body
        ).status_code
        == 200
    )
    first.pop("memory_context_fingerprint")
    before = {
        t: records(test_app, t)
        for t in (
            "goal_runs",
            "goal_model_calls",
            "plan_nodes",
            "agent_jobs",
            "audit_events",
            "memory_local_context_receipts",
        )
    }
    response = client.post(f"/goals/{other['id']}/start", headers=paired_headers, json=first)
    assert response.status_code == 409, response.text
    assert {t: records(test_app, t) for t in before} == before


@pytest.mark.parametrize("change", ["cross_goal", "plan"])
def test_accepted_goal_receipt_cannot_be_reused_for_different_target_or_plan(
    client, paired_headers, test_app, seeded, change
):
    test_app.state.goal_manager.evaluator = NoopEvaluatorProvider()
    goal, _, body = goal_setup(client, paired_headers)
    assert (
        client.post(f"/goals/{goal['id']}/start", headers=paired_headers, json=body).status_code
        == 200
    )
    if change == "cross_goal":
        goal = _create_goal(client, paired_headers, objective=INTENT, autonomy_profile="manual")[
            "goal"
        ]
    else:
        body["plan_proposal"]["rationale_summary"] = "Different reviewed plan"
    before = {
        t: records(test_app, t) for t in ("goal_runs", "plan_nodes", "agent_jobs", "audit_events")
    }
    response = client.post(f"/goals/{goal['id']}/start", headers=paired_headers, json=body)
    assert response.status_code == 409, response.text
    assert {t: records(test_app, t) for t in before} == before


@pytest.mark.parametrize("stamp", ["garbage", "9999-01-01T00:00:00", "9999-01-01T00:00:00+00:00"])
def test_malformed_expiry_fails_closed(client, paired_headers, test_app, seeded, stamp):
    context = prepare(client, paired_headers)
    tid = task(client, paired_headers)
    with sqlite3.connect(test_app.state.settings.db_path) as db:
        db.execute("UPDATE memory_local_context_receipts SET expires_at=?", (stamp,))
    response = client.post(
        f"/tasks/{tid}/tool-calls",
        headers=paired_headers,
        json={**TOOL, "local_context_receipt": receipt(context)},
    )
    assert response.status_code == 409, response.text
    assert records(test_app, "tool_calls") == records(test_app, "approvals") == []


def test_catalog_change_in_later_approval_audit_rolls_back_entire_acceptance(
    client, paired_headers, test_app, seeded, monkeypatch
):
    from app.services import execution_engine as engine_module

    context = prepare(client, paired_headers)
    tid = task(client, paired_headers)
    original = engine_module.append_audit_event

    async def changed(db, event, *args, **kwargs):
        result = await original(db, event, *args, **kwargs)
        if event == "approval.requested":
            test_app.state.strategy_retrieval.symbolic_catalogs = ()
        return result

    monkeypatch.setattr(engine_module, "append_audit_event", changed)
    before = {
        t: records(test_app, t)
        for t in (
            "tasks",
            "tool_calls",
            "approvals",
            "audit_events",
            "memory_local_context_receipts",
        )
    }
    response = client.post(
        f"/tasks/{tid}/tool-calls",
        headers=paired_headers,
        json={**TOOL, "local_context_receipt": receipt(context)},
    )
    assert response.status_code == 409, response.text
    assert {t: records(test_app, t) for t in before} == before


@pytest.mark.parametrize(
    "changes",
    [
        {"max_context_bytes": -1},
        {"max_context_bytes": 16385},
        {"max_context_bytes": True},
        {"scope": "general"},
        {"catalogs": []},
        {"source": "server"},
        {"intent": ""},
    ],
)
def test_prepare_rejects_caller_scope_catalog_and_invalid_transport(
    client, paired_headers, test_app, changes
):
    response = client.post(
        "/memory/local-context",
        headers=paired_headers,
        json={
            "purpose": "tool_proposal",
            "intent": INTENT,
            "mode": "normal",
            "source": "iphone_local",
            **changes,
        },
    )
    assert response.status_code == 422, response.text
    assert records(test_app, "memory_local_context_receipts") == []


def test_forgetting_related_source_erases_whole_context_and_only_dependent_receipt(
    client, paired_headers, test_app, seeded
):
    from tests.test_symbolic_retrieval_acceptance import _claim, _concept, _proposal, _source

    other_id, other_source = _source(
        client, paired_headers, "Related source with unrelated text", scope="general"
    )
    concept = _concept(client, paired_headers, scope="general")
    claim = _claim(concept)
    claim["object"]["identity"] = "Related.py"
    other = _proposal(client, paired_headers, claim, other_source)
    relation = client.post(
        f"/memory/proposals/{seeded[1]['proposal_id']}/relations",
        headers=paired_headers,
        json={"relationship": "contradicts", "target_proposal_id": other["proposal_id"]},
    )
    assert relation.status_code == 201, relation.text
    selected = prepare(client, paired_headers)
    empty = prepare(client, paired_headers, intent="no lexical or identity matches")
    assert selected["symbolic_context"]["evidence"][0]["relations"]
    assert {r[1] for r in records(test_app, "memory_local_context_sources")} == {
        seeded[0],
        other_id,
    }
    assert client.delete("/memory/" + other_id, headers=paired_headers).status_code == 204
    assert [r[0] for r in records(test_app, "memory_local_context_receipts")] == [
        empty["receipt"]["id"]
    ]
    assert records(test_app, "memory_local_context_sources") == []


def test_delete_failure_rolls_back_copied_context_and_sources(
    client, paired_headers, test_app, seeded, monkeypatch
):
    from app.services import state_service as state_module

    prepare(client, paired_headers)
    before = {
        t: records(test_app, t)
        for t in (
            "memory_items",
            "memory_symbolic_proposals",
            "memory_local_context_receipts",
            "memory_local_context_sources",
            "audit_events",
        )
    }

    class Crash(BaseException):
        pass

    async def crash(*args, **kwargs):
        raise Crash()

    monkeypatch.setattr(state_module, "forget_symbolic_memory_locked", crash)
    with pytest.raises(Crash):
        __import__("asyncio").run(test_app.state.state_service.delete_memory(seeded[0], "test"))
    assert {t: records(test_app, t) for t in before} == before


def test_expired_unaccepted_receipts_are_purged_but_accepted_provenance_persists(
    client, paired_headers, test_app, seeded
):
    from datetime import UTC, datetime, timedelta

    pending = prepare(client, paired_headers)
    accepted = prepare(client, paired_headers)
    tid = task(client, paired_headers)
    assert (
        client.post(
            f"/tasks/{tid}/tool-calls",
            headers=paired_headers,
            json={**TOOL, "local_context_receipt": receipt(accepted)},
        ).status_code
        == 200
    )
    test_app.state.local_contexts.clock = lambda: datetime.now(UTC) + timedelta(minutes=31)
    fresh = prepare(client, paired_headers)
    assert {r[0] for r in records(test_app, "memory_local_context_receipts")} == {
        accepted["receipt"]["id"],
        fresh["receipt"]["id"],
    }
    assert pending["receipt"]["id"] not in {
        r[0] for r in records(test_app, "memory_local_context_sources")
    }


@pytest.mark.parametrize("change", ["message", "planned", "old_task", "previous_call", "input"])
def test_tool_first_acceptance_requires_fresh_original_task(
    client, paired_headers, test_app, seeded, change
):
    import asyncio

    old_task = task(client, paired_headers) if change == "old_task" else None
    context = prepare(client, paired_headers)
    tid = old_task or task(client, paired_headers)
    if change == "message":
        asyncio.run(
            test_app.state.state_service.append_task_message(
                tid, "user", "Latest correction: do not write"
            )
        )
    elif change == "previous_call":
        legacy = client.post(
            f"/tasks/{tid}/tool-calls",
            headers=paired_headers,
            json={**TOOL, "planner_source": "manual"},
        )
        assert legacy.status_code == 200, legacy.text
        with sqlite3.connect(test_app.state.settings.db_path) as db:
            db.execute("UPDATE tasks SET status='created',updated_at=created_at WHERE id=?", (tid,))
    elif change in {"planned", "input"}:
        with sqlite3.connect(test_app.state.settings.db_path) as db:
            if change == "planned":
                db.execute("UPDATE tasks SET status='planned' WHERE id=?", (tid,))
            else:
                db.execute("UPDATE tasks SET input='Later correction' WHERE id=?", (tid,))
    before = {
        t: records(test_app, t)
        for t in (
            "tasks",
            "tool_calls",
            "approvals",
            "audit_events",
            "memory_local_context_receipts",
        )
    }
    response = client.post(
        f"/tasks/{tid}/tool-calls",
        headers=paired_headers,
        json={**TOOL, "local_context_receipt": receipt(context)},
    )
    assert response.status_code == 409, response.text
    assert {t: records(test_app, t) for t in before} == before
