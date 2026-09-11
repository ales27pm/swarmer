import asyncio
import hashlib
import hmac
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import aiosqlite
import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.services.auth_service import AuthService
from app.services.state_service import StateService

OPERATOR_TOKEN = "test-operator-token-with-sufficient-entropy"


def remote_request(
    client: TestClient, app: FastAPI, method: str, path: str, **kwargs: Any
) -> httpx.Response:
    """Exercise a remote peer on the existing app loop without a second lifespan."""

    async def request() -> httpx.Response:
        transport = httpx.ASGITransport(app=app, client=("198.51.100.7", 50_001))
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as remote:
            return await remote.request(method, path, **kwargs)

    assert client.portal is not None
    return client.portal.call(request)


class MutableClock:
    def __init__(self, value: datetime) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value


async def release_auth_writer_after_clock_advance(
    blocker: aiosqlite.Connection,
    blocked_operation: asyncio.Task[object],
    clock: MutableClock,
    advanced_to: datetime,
) -> None:
    for _ in range(20):
        if blocked_operation.done():
            break
        await asyncio.sleep(0.005)
    was_blocked = not blocked_operation.done()
    clock.value = advanced_to
    await blocker.commit()
    await blocker.close()
    assert was_blocked


async def create_auth_service(database: Path, clock: MutableClock) -> AuthService:
    await StateService(database).initialize()
    return AuthService(
        database,
        pairing_ttl_seconds=60,
        pairing_candidate_ttl_seconds=30,
        pairing_pepper=OPERATOR_TOKEN,
        clock=clock,
    )


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
    response = remote_request(
        client,
        test_app,
        "POST",
        "/pairing/code",
        headers={"X-Mongars-Operator-Token": OPERATOR_TOKEN},
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


def test_new_websocket_ticket_invalidates_prior_unconsumed_ticket(
    client: TestClient,
    paired_headers: dict[str, str],
) -> None:
    first = client.post("/ws/ticket", headers=paired_headers).json()["ticket"]
    second = client.post("/ws/ticket", headers=paired_headers).json()["ticket"]

    with (
        pytest.raises(WebSocketDisconnect) as rejected,
        client.websocket_connect(f"/ws?ticket={first}") as websocket,
    ):
        websocket.receive_json()
    assert rejected.value.code == 4401

    with client.websocket_connect(f"/ws?ticket={second}") as websocket:
        assert websocket.receive_json()["type"] == "connected"


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
    response = remote_request(client, test_app, "GET", "/tasks", headers=paired_headers)
    assert response.status_code == 426


def test_remote_plain_http_cannot_complete_pairing(test_app: FastAPI, client: TestClient) -> None:
    code = issue_code(client)
    body = {"code": code, "device_id": "remote-phone", "name": "Remote"}
    response = remote_request(client, test_app, "POST", "/pairing/complete", json=body)
    assert response.status_code == 426
    assert client.post("/pairing/complete", json=body).status_code == 200


def test_remote_plain_http_cannot_verify_or_finalize_candidate(
    test_app: FastAPI, client: TestClient
) -> None:
    candidate = stage_pairing(client, device_id="remote-candidate")
    headers = candidate_headers(candidate)
    body = {"pairing_id": candidate["pairing_id"], "device_id": candidate["device_id"]}
    assert (
        remote_request(client, test_app, "GET", "/sync/bootstrap", headers=headers).status_code
        == 426
    )
    assert (
        remote_request(
            client, test_app, "POST", "/pairing/finalize", headers=headers, json=body
        ).status_code
        == 426
    )
    assert client.get("/sync/bootstrap", headers=headers).status_code == 200


@pytest.mark.asyncio
async def test_pairing_code_ttl_starts_after_waiting_for_writer_lock(tmp_path: Path) -> None:
    started = datetime(2026, 9, 8, 16, 0, tzinfo=UTC)
    advanced = started + timedelta(seconds=20)
    database = tmp_path / "state.db"
    clock = MutableClock(started)
    service = await create_auth_service(database, clock)

    blocker = await aiosqlite.connect(database)
    await blocker.execute("BEGIN IMMEDIATE")
    operation = asyncio.create_task(service.create_pairing_code())
    await release_auth_writer_after_clock_advance(blocker, operation, clock, advanced)
    await operation

    async with aiosqlite.connect(database) as db:
        created_at, expires_at = await (
            await db.execute("SELECT created_at,expires_at FROM pairing_codes")
        ).fetchone()
    assert created_at == advanced.isoformat()
    assert expires_at == (advanced + timedelta(seconds=60)).isoformat()


@pytest.mark.asyncio
async def test_pairing_rechecks_code_expiry_after_waiting_for_writer_lock(
    tmp_path: Path,
) -> None:
    started = datetime(2026, 9, 8, 16, 0, tzinfo=UTC)
    database = tmp_path / "state.db"
    clock = MutableClock(started)
    service = await create_auth_service(database, clock)
    code = await service.create_pairing_code()
    clock.value = started + timedelta(seconds=59)

    blocker = await aiosqlite.connect(database)
    await blocker.execute("BEGIN IMMEDIATE")
    operation = asyncio.create_task(service.complete_pairing(code, "late-phone", "Late Phone"))
    await release_auth_writer_after_clock_advance(
        blocker,
        operation,
        clock,
        started + timedelta(seconds=61),
    )

    assert await operation is None


@pytest.mark.asyncio
async def test_pairing_rechecks_candidate_expiry_after_waiting_for_writer_lock(
    tmp_path: Path,
) -> None:
    started = datetime(2026, 9, 8, 16, 0, tzinfo=UTC)
    database = tmp_path / "state.db"
    clock = MutableClock(started)
    service = await create_auth_service(database, clock)
    code = await service.create_pairing_code()
    candidate = await service.complete_pairing(code, "late-phone", "Late Phone")
    assert candidate is not None
    clock.value = started + timedelta(seconds=29)

    blocker = await aiosqlite.connect(database)
    await blocker.execute("BEGIN IMMEDIATE")
    operation = asyncio.create_task(
        service.finalize_pairing(candidate.token, candidate.pairing_id, candidate.device_id)
    )
    await release_auth_writer_after_clock_advance(
        blocker,
        operation,
        clock,
        started + timedelta(seconds=31),
    )

    assert await operation is None


@pytest.mark.asyncio
async def test_websocket_ticket_rechecks_expiry_after_waiting_for_writer_lock(
    tmp_path: Path,
) -> None:
    started = datetime(2026, 9, 8, 16, 0, tzinfo=UTC)
    database = tmp_path / "state.db"
    clock = MutableClock(started)
    service = await create_auth_service(database, clock)
    code = await service.create_pairing_code()
    candidate = await service.complete_pairing(code, "ticket-phone", "Ticket Phone")
    assert candidate is not None
    finalized = await service.finalize_pairing(
        candidate.token,
        candidate.pairing_id,
        candidate.device_id,
    )
    assert finalized is not None
    assert await service.authenticate_token(candidate.token) is not None
    ticket = await service.create_websocket_ticket(candidate.token)
    assert ticket is not None
    clock.value = started + timedelta(seconds=29)

    blocker = await aiosqlite.connect(database)
    await blocker.execute("BEGIN IMMEDIATE")
    operation = asyncio.create_task(service.consume_websocket_ticket(ticket))
    await release_auth_writer_after_clock_advance(
        blocker,
        operation,
        clock,
        started + timedelta(seconds=31),
    )

    assert await operation is None


@pytest.mark.asyncio
async def test_websocket_ticket_cannot_survive_token_replacement_race(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = datetime(2026, 9, 8, 16, 0, tzinfo=UTC)
    database = tmp_path / "state.db"
    clock = MutableClock(started)
    service = await create_auth_service(database, clock)

    old_code = await service.create_pairing_code()
    old = await service.complete_pairing(old_code, "race-phone", "Old Phone")
    assert old is not None
    assert await service.finalize_pairing(old.token, old.pairing_id, old.device_id) is not None
    assert await service.authenticate_token(old.token) is not None

    new_code = await service.create_pairing_code()
    replacement = await service.complete_pairing(new_code, "race-phone", "New Phone")
    assert replacement is not None
    assert (
        await service.finalize_pairing(
            replacement.token,
            replacement.pairing_id,
            replacement.device_id,
        )
        is not None
    )

    validated = asyncio.Event()
    resume_split_transaction = asyncio.Event()
    authenticate_token = service.authenticate_token

    async def pause_after_public_authentication(token: str):
        principal = await authenticate_token(token)
        validated.set()
        await resume_split_transaction.wait()
        return principal

    monkeypatch.setattr(service, "authenticate_token", pause_after_public_authentication)
    replacement_service = AuthService(
        database,
        pairing_ttl_seconds=60,
        pairing_candidate_ttl_seconds=30,
        pairing_pepper=OPERATOR_TOKEN,
        clock=clock,
    )

    ticket_task = asyncio.create_task(service.create_websocket_ticket(old.token))
    validation_task = asyncio.create_task(validated.wait())
    done, _ = await asyncio.wait(
        {ticket_task, validation_task},
        timeout=2,
        return_when=asyncio.FIRST_COMPLETED,
    )
    assert done

    if validation_task in done:
        # This is the vulnerable split-transaction schedule: rotate the token after
        # the first authorization commit and before the ticket-insert transaction.
        assert await replacement_service.authenticate_token(replacement.token) is not None
        resume_split_transaction.set()
    else:
        # Atomic issuance may serialize first; replacement must then invalidate the
        # ticket in the same transaction that activates the new device credential.
        await ticket_task
        assert await replacement_service.authenticate_token(replacement.token) is not None

    resume_split_transaction.set()
    ticket = await ticket_task
    if not validation_task.done():
        validation_task.cancel()
        await asyncio.gather(validation_task, return_exceptions=True)

    assert ticket is not None
    assert await replacement_service.consume_websocket_ticket(ticket) is None
