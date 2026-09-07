import hashlib
import hmac
from datetime import UTC, datetime, timedelta

import aiosqlite
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

OPERATOR_TOKEN = "test-operator-token-with-sufficient-entropy"


def issue_code(client: TestClient) -> str:
    response = client.post("/pairing/code", headers={"X-Mongars-Operator-Token": OPERATOR_TOKEN})
    assert response.status_code == 200
    return response.json()["code"]


def stage_pairing(
    client: TestClient, *, device_id: str = "phone-one", name: str = "Phone One"
) -> dict[str, object]:
    response = client.post(
        "/pairing/complete",
        json={"code": issue_code(client), "device_id": device_id, "name": name},
    )
    assert response.status_code == 200
    return response.json()


def candidate_headers(candidate: dict[str, object]) -> dict[str, str]:
    return {"Authorization": f"Bearer {candidate['candidate_token']}"}


def finalize_pairing(client: TestClient, candidate: dict[str, object]):
    return client.post(
        "/pairing/finalize",
        headers=candidate_headers(candidate),
        json={"pairing_id": candidate["pairing_id"], "device_id": candidate["device_id"]},
    )


def test_pairing_code_requires_local_authenticated_operator(
    test_app: FastAPI, client: TestClient
) -> None:
    assert client.post("/pairing/code").status_code == 403
    assert (
        client.post("/pairing/code", headers={"X-Mongars-Operator-Token": "wrong"}).status_code
        == 403
    )
    with TestClient(test_app, client=("198.51.100.7", 50_001)) as remote:
        response = remote.post(
            "/pairing/code", headers={"X-Mongars-Operator-Token": OPERATOR_TOKEN}
        )
    assert response.status_code == 403


def test_pairing_code_is_single_use(client: TestClient) -> None:
    code = issue_code(client)
    body = {"code": code, "device_id": "phone-one", "name": "Phone One"}
    paired = client.post("/pairing/complete", json=body)
    assert paired.status_code == 200
    assert client.post("/pairing/complete", json=body).status_code == 400
    candidate = paired.json()
    headers = candidate_headers(candidate)
    assert client.get("/sync/bootstrap", headers=headers).status_code == 200
    assert client.get("/tasks", headers=headers).status_code == 401
    assert finalize_pairing(client, candidate).status_code == 200
    assert client.get("/tasks", headers=headers).status_code == 200


@pytest.mark.asyncio
async def test_pairing_code_is_not_stored_in_replayable_form(
    client: TestClient, test_app: FastAPI
) -> None:
    code = issue_code(client)
    async with aiosqlite.connect(test_app.state.settings.db_path) as db:
        stored = str((await (await db.execute("SELECT code FROM pairing_codes")).fetchone())[0])

    assert stored != code
    assert stored != hashlib.sha256(code.encode("utf-8")).hexdigest()
    assert (
        stored
        == hmac.new(
            OPERATOR_TOKEN.encode("utf-8"), code.encode("utf-8"), hashlib.sha256
        ).hexdigest()
    )
    assert len(stored) == 64
    assert all(character in "0123456789abcdef" for character in stored)


def test_pairing_attempt_budget_is_enforced(client: TestClient) -> None:
    valid_code = issue_code(client)
    for suffix in ("1", "2"):
        wrong = f"{valid_code[:5]}{suffix}"
        if wrong == valid_code:
            wrong = f"{valid_code[:5]}9"
        assert (
            client.post(
                "/pairing/complete",
                json={"code": wrong, "device_id": "attacker", "name": "Attacker"},
            ).status_code
            == 400
        )
    final_wrong = "000000" if valid_code != "000000" else "999999"
    assert (
        client.post(
            "/pairing/complete",
            json={"code": final_wrong, "device_id": "attacker", "name": "Attacker"},
        ).status_code
        == 429
    )
    assert (
        client.post(
            "/pairing/complete",
            json={"code": valid_code, "device_id": "phone", "name": "Phone"},
        ).status_code
        == 400
    )


def test_websocket_ticket_is_one_use(client: TestClient, paired_headers: dict[str, str]) -> None:
    ticket_response = client.post("/ws/ticket", headers=paired_headers)
    assert ticket_response.status_code == 200
    ticket = ticket_response.json()["ticket"]
    with client.websocket_connect(f"/ws?ticket={ticket}") as websocket:
        assert websocket.receive_json()["type"] == "connected"

    accepted_twice = False
    try:
        with client.websocket_connect(f"/ws?ticket={ticket}") as websocket:
            websocket.receive_json()
    except WebSocketDisconnect as exc:
        assert exc.code == 4401
    else:
        accepted_twice = True
    assert accepted_twice is False


@pytest.mark.asyncio
async def test_device_token_is_stored_as_a_digest(client: TestClient, test_app: FastAPI) -> None:
    candidate = stage_pairing(client, device_id="hashed-phone", name="Hashed")
    token = str(candidate["candidate_token"])
    expected = f"sha256:{hashlib.sha256(token.encode('utf-8')).hexdigest()}"
    async with aiosqlite.connect(test_app.state.settings.db_path) as db:
        staged = str(
            (
                await (
                    await db.execute(
                        "SELECT token_hash FROM pairing_candidates WHERE device_id='hashed-phone'"
                    )
                ).fetchone()
            )[0]
        )
        assert staged == expected
        assert (
            await (await db.execute("SELECT token FROM devices WHERE id='hashed-phone'")).fetchone()
        ) is None
    assert finalize_pairing(client, candidate).status_code == 200
    assert client.get("/tasks", headers=candidate_headers(candidate)).status_code == 200
    async with aiosqlite.connect(test_app.state.settings.db_path) as db:
        stored = str(
            (
                await (
                    await db.execute("SELECT token FROM devices WHERE id='hashed-phone'")
                ).fetchone()
            )[0]
        )
    assert stored == expected
    assert stored != token


def test_same_device_candidate_failure_preserves_active_token(client: TestClient) -> None:
    active = stage_pairing(client, device_id="stable-phone", name="Stable")
    active_headers = candidate_headers(active)
    assert finalize_pairing(client, active).status_code == 200
    assert client.get("/tasks", headers=active_headers).status_code == 200

    replacement = stage_pairing(client, device_id="stable-phone", name="Replacement")
    replacement_headers = candidate_headers(replacement)
    assert client.get("/sync/bootstrap", headers=replacement_headers).status_code == 200
    assert client.get("/tasks", headers=replacement_headers).status_code == 401
    assert finalize_pairing(client, replacement).json()["status"] == "ready"

    # Abandoning the replacement before local credential cutover (for example after
    # bootstrap, finalize-response loss, or SecureStore failure) does not alter the
    # server-side active credential.
    assert client.get("/tasks", headers=active_headers).status_code == 200


def test_candidate_is_bootstrap_and_finalize_only_until_promoted(client: TestClient) -> None:
    candidate = stage_pairing(client, device_id="restricted-candidate")
    headers = candidate_headers(candidate)

    assert client.get("/sync/bootstrap", headers=headers).status_code == 200
    assert client.get("/tasks", headers=headers).status_code == 401
    assert (
        client.post("/tasks", headers=headers, json={"input": "must not be created"}).status_code
        == 401
    )
    assert client.post("/ws/ticket", headers=headers).status_code == 401

    finalized = finalize_pairing(client, candidate)
    assert finalized.status_code == 200
    assert finalized.json() == {
        "status": "ready",
        "device_id": "restricted-candidate",
        "pairing_id": candidate["pairing_id"],
        "already_finalized": False,
    }
    assert client.get("/tasks", headers=headers).status_code == 200


def test_finalize_is_exactly_bound_and_lost_response_retry_is_idempotent(
    client: TestClient,
) -> None:
    candidate = stage_pairing(client, device_id="bound-phone")
    headers = candidate_headers(candidate)

    wrong_device = client.post(
        "/pairing/finalize",
        headers=headers,
        json={"pairing_id": candidate["pairing_id"], "device_id": "other-phone"},
    )
    assert wrong_device.status_code == 409
    wrong_pairing = client.post(
        "/pairing/finalize",
        headers=headers,
        json={"pairing_id": "pair_" + ("0" * 32), "device_id": candidate["device_id"]},
    )
    assert wrong_pairing.status_code == 409
    assert client.get("/tasks", headers=headers).status_code == 401

    first = finalize_pairing(client, candidate)
    second = finalize_pairing(client, candidate)
    assert first.status_code == 200
    assert first.json()["status"] == "ready"
    assert first.json()["already_finalized"] is False
    assert second.status_code == 200
    assert second.json()["status"] == "ready"
    assert second.json()["already_finalized"] is True
    assert client.get("/tasks", headers=headers).status_code == 200
    after_cutover = finalize_pairing(client, candidate)
    assert after_cutover.status_code == 200
    assert after_cutover.json()["status"] == "active"
    assert after_cutover.json()["already_finalized"] is True


def test_first_post_finalize_request_atomically_cuts_over_same_device(client: TestClient) -> None:
    active = stage_pairing(client, device_id="cutover-phone", name="Old")
    old_headers = candidate_headers(active)
    assert finalize_pairing(client, active).status_code == 200
    assert client.get("/tasks", headers=old_headers).status_code == 200

    replacement = stage_pairing(client, device_id="cutover-phone", name="New")
    new_headers = candidate_headers(replacement)
    assert finalize_pairing(client, replacement).json()["status"] == "ready"
    assert client.get("/tasks", headers=old_headers).status_code == 200

    # This represents the first request after the mobile client durably stores the
    # replacement credential. The transaction activates it and removes the old token.
    assert client.get("/tasks", headers=new_headers).status_code == 200
    assert client.get("/tasks", headers=old_headers).status_code == 401
    assert client.get("/tasks", headers=new_headers).status_code == 200


def test_finalize_requires_candidate_authentication(client: TestClient) -> None:
    candidate = stage_pairing(client, device_id="auth-phone")
    body = {"pairing_id": candidate["pairing_id"], "device_id": candidate["device_id"]}
    assert client.post("/pairing/finalize", json=body).status_code == 401
    assert (
        client.post(
            "/pairing/finalize",
            headers={"Authorization": "Bearer invalid"},
            json=body,
        ).status_code
        == 401
    )


@pytest.mark.asyncio
async def test_expired_candidate_cannot_bootstrap_or_finalize_and_keeps_old_token(
    client: TestClient, test_app: FastAPI
) -> None:
    active = stage_pairing(client, device_id="expiry-phone", name="Active")
    active_headers = candidate_headers(active)
    assert finalize_pairing(client, active).status_code == 200
    assert client.get("/tasks", headers=active_headers).status_code == 200

    ready = stage_pairing(client, device_id="expiry-phone", name="Ready but expired")
    assert finalize_pairing(client, ready).json()["status"] == "ready"
    async with aiosqlite.connect(test_app.state.settings.db_path) as db:
        await db.execute(
            "UPDATE pairing_candidates SET expires_at=? WHERE pairing_id=?",
            ((datetime.now(UTC) - timedelta(seconds=1)).isoformat(), ready["pairing_id"]),
        )
        await db.commit()
    assert client.get("/tasks", headers=candidate_headers(ready)).status_code == 401
    assert client.get("/tasks", headers=active_headers).status_code == 200

    candidate = stage_pairing(client, device_id="expiry-phone", name="Expired")
    async with aiosqlite.connect(test_app.state.settings.db_path) as db:
        await db.execute(
            "UPDATE pairing_candidates SET expires_at=? WHERE pairing_id=?",
            ((datetime.now(UTC) - timedelta(seconds=1)).isoformat(), candidate["pairing_id"]),
        )
        await db.commit()

    headers = candidate_headers(candidate)
    assert client.get("/sync/bootstrap", headers=headers).status_code == 401
    assert finalize_pairing(client, candidate).status_code == 401
    assert client.get("/tasks", headers=active_headers).status_code == 200


def test_remote_plain_http_rejects_device_credentials(
    test_app: FastAPI, client: TestClient, paired_headers: dict[str, str]
) -> None:
    del client
    with TestClient(test_app, client=("198.51.100.7", 50_001)) as remote:
        response = remote.get("/tasks", headers=paired_headers)
    assert response.status_code == 426


def test_remote_plain_http_cannot_complete_pairing(test_app: FastAPI, client: TestClient) -> None:
    code = issue_code(client)
    body = {"code": code, "device_id": "remote-phone", "name": "Remote"}
    with TestClient(test_app, client=("198.51.100.7", 50_001)) as remote:
        response = remote.post("/pairing/complete", json=body)
    assert response.status_code == 426
    assert client.post("/pairing/complete", json=body).status_code == 200


def test_remote_plain_http_cannot_verify_or_finalize_candidate(
    test_app: FastAPI, client: TestClient
) -> None:
    candidate = stage_pairing(client, device_id="remote-candidate")
    headers = candidate_headers(candidate)
    body = {"pairing_id": candidate["pairing_id"], "device_id": candidate["device_id"]}
    with TestClient(test_app, client=("198.51.100.7", 50_001)) as remote:
        assert remote.get("/sync/bootstrap", headers=headers).status_code == 426
        assert remote.post("/pairing/finalize", headers=headers, json=body).status_code == 426
    assert client.get("/sync/bootstrap", headers=headers).status_code == 200
