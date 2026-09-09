from __future__ import annotations

import hashlib
import hmac
import secrets
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

import aiosqlite

from app.services.device_session_coordinator import DeviceSessionCoordinator


class PairingRateLimited(RuntimeError):
    pass


class PairingConflict(RuntimeError):
    pass


@dataclass(frozen=True)
class PairingCandidate:
    pairing_id: str
    token: str
    device_id: str
    expires_in_seconds: int


@dataclass(frozen=True)
class PairingFinalization:
    pairing_id: str
    device_id: str
    status: Literal["ready", "active"]
    already_finalized: bool


class AuthService:
    def __init__(
        self,
        db_path: Path,
        *,
        pairing_ttl_seconds: int = 600,
        pairing_max_attempts: int = 10,
        pairing_candidate_ttl_seconds: int = 120,
        pairing_pepper: str,
        clock: Callable[[], datetime] | None = None,
        session_coordinator: DeviceSessionCoordinator | None = None,
    ) -> None:
        self.db_path = db_path
        self.pairing_ttl_seconds = min(max(pairing_ttl_seconds, 60), 900)
        self.pairing_max_attempts = min(max(pairing_max_attempts, 1), 20)
        self.pairing_candidate_ttl_seconds = min(max(pairing_candidate_ttl_seconds, 30), 300)
        self._pairing_pepper = pairing_pepper.encode("utf-8")
        self.clock = clock or (lambda: datetime.now(UTC))
        self._session_coordinator = session_coordinator or DeviceSessionCoordinator(db_path)

    @staticmethod
    def _digest(value: str) -> str:
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    def _pairing_digest(self, value: str) -> str:
        return hmac.new(self._pairing_pepper, value.encode("utf-8"), hashlib.sha256).hexdigest()

    @classmethod
    def _stored_token(cls, value: str) -> str:
        return f"sha256:{cls._digest(value)}"

    async def create_pairing_code(self) -> str:
        code = f"{secrets.randbelow(1_000_000):06d}"
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            now = self._now()
            expires = (now + timedelta(seconds=self.pairing_ttl_seconds)).isoformat()
            await db.execute("DELETE FROM pairing_codes")
            await db.execute(
                """
                INSERT INTO pairing_codes(code,expires_at,attempts_remaining,created_at)
                VALUES(?,?,?,?)
                """,
                (self._pairing_digest(code), expires, self.pairing_max_attempts, now.isoformat()),
            )
            await db.commit()
        return code

    async def complete_pairing(
        self, code: str, device_id: str, name: str
    ) -> PairingCandidate | None:
        candidate = self._pairing_digest(code)
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            now_value = self._now()
            now = now_value.isoformat()
            row = await (
                await db.execute(
                    """
                    SELECT code,expires_at,attempts_remaining
                    FROM pairing_codes ORDER BY created_at DESC LIMIT 1
                    """
                )
            ).fetchone()
            if not row or row["expires_at"] < now:
                await db.execute("DELETE FROM pairing_codes")
                await db.commit()
                return None

            if not secrets.compare_digest(str(row["code"]), candidate):
                attempts = int(row["attempts_remaining"]) - 1
                if attempts <= 0:
                    await db.execute("DELETE FROM pairing_codes")
                    await db.commit()
                    raise PairingRateLimited("pairing attempt budget exhausted")
                await db.execute(
                    "UPDATE pairing_codes SET attempts_remaining=? WHERE code=?",
                    (attempts, row["code"]),
                )
                await db.commit()
                return None

            await db.execute("DELETE FROM pairing_candidates WHERE expires_at<=?", (now,))
            await db.execute("DELETE FROM pairing_candidates WHERE device_id=?", (device_id,))
            credential: tuple[str, str] | None = None
            for _ in range(5):
                generated_token = secrets.token_urlsafe(32)
                generated_hash = self._stored_token(generated_token)
                collision = await (
                    await db.execute(
                        """
                        SELECT 1 FROM devices WHERE token=?
                        UNION ALL
                        SELECT 1 FROM pairing_candidates WHERE token_hash=?
                        LIMIT 1
                        """,
                        (generated_hash, generated_hash),
                    )
                ).fetchone()
                if collision is None:
                    credential = (generated_token, generated_hash)
                    break
            if credential is None:
                await db.rollback()
                raise RuntimeError("could not allocate a unique pairing credential")
            token, token_hash = credential

            pairing_id = f"pair_{secrets.token_hex(16)}"
            expires = (
                now_value + timedelta(seconds=self.pairing_candidate_ttl_seconds)
            ).isoformat()
            await db.execute(
                """
                INSERT INTO pairing_candidates(
                    pairing_id,token_hash,device_id,name,status,created_at,expires_at
                ) VALUES(?,?,?,?,?,?,?)
                """,
                (pairing_id, token_hash, device_id, name, "staged", now, expires),
            )
            await db.execute("DELETE FROM pairing_codes")
            await db.commit()
            return PairingCandidate(
                pairing_id=pairing_id,
                token=token,
                device_id=device_id,
                expires_in_seconds=self.pairing_candidate_ttl_seconds,
            )

    async def authenticate_pairing_candidate(self, token: str) -> dict[str, Any] | None:
        if not token:
            return None
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            now = self._now().isoformat()
            await db.execute("DELETE FROM pairing_candidates WHERE expires_at<=?", (now,))
            row = await (
                await db.execute(
                    """
                    SELECT pairing_id,device_id,name,created_at,expires_at,status
                    FROM pairing_candidates
                    WHERE token_hash=? AND expires_at>? AND status='staged'
                    """,
                    (self._stored_token(token), now),
                )
            ).fetchone()
            await db.commit()
        if not row:
            return None
        return {**dict(row), "credential_state": "pairing_candidate"}

    async def finalize_pairing(
        self, token: str, pairing_id: str, device_id: str
    ) -> PairingFinalization | None:
        if not token:
            return None
        token_hash = self._stored_token(token)
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            now_value = self._now()
            now = now_value.isoformat()
            await db.execute("DELETE FROM pairing_candidates WHERE expires_at<=?", (now,))

            active = await (
                await db.execute(
                    """
                    SELECT id,last_pairing_id FROM devices WHERE token=?
                    """,
                    (token_hash,),
                )
            ).fetchone()
            if active is not None:
                if str(active["id"]) != device_id or str(active["last_pairing_id"]) != pairing_id:
                    await db.rollback()
                    raise PairingConflict("pairing finalization does not match this credential")
                await db.execute("UPDATE devices SET last_seen_at=? WHERE id=?", (now, device_id))
                await db.commit()
                return PairingFinalization(
                    pairing_id=pairing_id,
                    device_id=device_id,
                    status="active",
                    already_finalized=True,
                )

            staged = await (
                await db.execute(
                    """
                    SELECT pairing_id,device_id,status
                    FROM pairing_candidates WHERE token_hash=? AND expires_at>?
                    """,
                    (token_hash, now),
                )
            ).fetchone()
            if staged is None:
                await db.commit()
                return None
            if str(staged["pairing_id"]) != pairing_id or str(staged["device_id"]) != device_id:
                await db.rollback()
                raise PairingConflict("pairing finalization does not match this candidate")
            was_finalized = str(staged["status"]) == "finalized"
            if not was_finalized:
                activation_expires = (
                    now_value + timedelta(seconds=self.pairing_candidate_ttl_seconds)
                ).isoformat()
                await db.execute(
                    """
                    UPDATE pairing_candidates
                    SET status='finalized',finalized_at=?,expires_at=?
                    WHERE pairing_id=? AND token_hash=? AND status='staged'
                    """,
                    (now, activation_expires, pairing_id, token_hash),
                )
            await db.commit()
            return PairingFinalization(
                pairing_id=pairing_id,
                device_id=device_id,
                status="ready",
                already_finalized=was_finalized,
            )

    async def authenticate_token(self, token: str) -> dict[str, Any] | None:
        if not token:
            return None
        token_hash = self._stored_token(token)
        candidate_device_id = await self._candidate_device_id(token_hash)
        async with (
            self._serialize_candidate_promotion(candidate_device_id),
            aiosqlite.connect(self.db_path) as db,
        ):
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            now = self._now().isoformat()
            row = await self._authenticate_token_locked(db, token_hash, now)
            if not row:
                await db.commit()
                return None
            await db.commit()
        return {**dict(row), "last_seen_at": now}

    async def _authenticate_token_locked(
        self,
        db: aiosqlite.Connection,
        token_hash: str,
        now: str,
    ) -> aiosqlite.Row | None:
        """Authenticate or atomically promote a device credential under a writer lock."""

        await db.execute("DELETE FROM pairing_candidates WHERE expires_at<=?", (now,))
        row = await (
            await db.execute(
                """
                SELECT id,name,created_at,last_pairing_id AS session_id,
                       'active' AS credential_state
                FROM devices WHERE token=?
                """,
                (token_hash,),
            )
        ).fetchone()
        if row is None:
            ready = await (
                await db.execute(
                    """
                    SELECT pairing_id,device_id,name,created_at
                    FROM pairing_candidates
                    WHERE token_hash=? AND status='finalized' AND expires_at>?
                    """,
                    (token_hash, now),
                )
            ).fetchone()
            if ready is not None:
                await db.execute(
                    """
                    INSERT INTO devices(id,name,token,created_at,last_seen_at,last_pairing_id)
                    VALUES(?,?,?,?,?,?)
                    ON CONFLICT(id) DO UPDATE SET
                        name=excluded.name,
                        token=excluded.token,
                        last_seen_at=excluded.last_seen_at,
                        last_pairing_id=excluded.last_pairing_id,
                        websocket_connection_id=NULL
                    """,
                    (
                        str(ready["device_id"]),
                        str(ready["name"]),
                        token_hash,
                        str(ready["created_at"]),
                        now,
                        str(ready["pairing_id"]),
                    ),
                )
                await db.execute(
                    "DELETE FROM pairing_candidates WHERE device_id=?",
                    (str(ready["device_id"]),),
                )
                await db.execute(
                    "DELETE FROM websocket_tickets WHERE device_id=?",
                    (str(ready["device_id"]),),
                )
                row = await (
                    await db.execute(
                        """
                        SELECT id,name,created_at,last_pairing_id AS session_id,
                               'active' AS credential_state
                        FROM devices WHERE id=? AND token=?
                        """,
                        (str(ready["device_id"]), token_hash),
                    )
                ).fetchone()
        if row is not None:
            await db.execute("UPDATE devices SET last_seen_at=? WHERE id=?", (now, row["id"]))
        return row

    async def validate_token(self, token: str) -> bool:
        return await self.authenticate_token(token) is not None

    async def create_websocket_ticket(self, token: str) -> str | None:
        if not token:
            return None
        token_hash = self._stored_token(token)
        candidate_device_id = await self._candidate_device_id(token_hash)
        async with (
            self._serialize_candidate_promotion(candidate_device_id),
            aiosqlite.connect(self.db_path) as db,
        ):
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            now = self._now()
            principal = await self._authenticate_token_locked(db, token_hash, now.isoformat())
            if principal is None:
                await db.commit()
                return None
            ticket = secrets.token_urlsafe(32)
            expires = (now + timedelta(seconds=30)).isoformat()
            await db.execute("DELETE FROM websocket_tickets WHERE expires_at<?", (now.isoformat(),))
            await db.execute(
                "DELETE FROM websocket_tickets WHERE device_id=?",
                (principal["id"],),
            )
            await db.execute(
                "INSERT INTO websocket_tickets(ticket_hash,device_id,expires_at) VALUES(?,?,?)",
                (self._digest(ticket), principal["id"], expires),
            )
            await db.commit()
        return ticket

    async def _candidate_device_id(self, token_hash: str) -> str | None:
        """Resolve a possible cutover target; the writer transaction revalidates it."""

        async with aiosqlite.connect(self.db_path) as db:
            now = self._now().isoformat()
            row = await (
                await db.execute(
                    """
                    SELECT device_id FROM pairing_candidates
                    WHERE token_hash=? AND expires_at>?
                    """,
                    (token_hash, now),
                )
            ).fetchone()
        return str(row[0]) if row is not None else None

    @asynccontextmanager
    async def _serialize_candidate_promotion(
        self, candidate_device_id: str | None
    ) -> AsyncIterator[None]:
        if candidate_device_id is None:
            yield
            return
        async with self.serialize_device_session(candidate_device_id):
            yield

    @asynccontextmanager
    async def serialize_device_session(self, device_id: str) -> AsyncIterator[None]:
        """Linearize one device's credential cutover and WebSocket delivery."""

        async with self._session_coordinator.hold(device_id):
            yield

    async def consume_websocket_ticket(self, ticket: str) -> dict[str, str] | None:
        digest = self._digest(ticket)
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            now = self._now().isoformat()
            row = await (
                await db.execute(
                    """
                    SELECT ticket.ticket_hash,ticket.device_id,device.last_pairing_id
                    FROM websocket_tickets AS ticket
                    JOIN devices AS device ON device.id=ticket.device_id
                    WHERE ticket.ticket_hash=? AND ticket.expires_at>=?
                      AND device.last_pairing_id IS NOT NULL
                    """,
                    (digest, now),
                )
            ).fetchone()
            await db.execute("DELETE FROM websocket_tickets WHERE ticket_hash=?", (digest,))
            await db.execute("DELETE FROM websocket_tickets WHERE expires_at<?", (now,))
            await db.commit()
        return (
            {
                "device_id": str(row["device_id"]),
                "session_id": str(row["last_pairing_id"]),
            }
            if row
            else None
        )

    async def is_device_session_current(self, device_id: str, session_id: str) -> bool:
        """Check that a live transport still belongs to the active pairing generation."""

        if not device_id or not session_id:
            return False
        async with aiosqlite.connect(self.db_path) as db:
            row = await (
                await db.execute(
                    """
                    SELECT 1 FROM devices
                    WHERE id=? AND last_pairing_id=?
                    """,
                    (device_id, session_id),
                )
            ).fetchone()
        return row is not None

    async def activate_websocket_connection(
        self,
        device_id: str,
        session_id: str,
        connection_id: str,
    ) -> bool:
        """Install the sole authoritative WebSocket delivery owner for a device."""

        if not device_id or not session_id or not connection_id:
            return False
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            now = self._now().isoformat()
            updated = await db.execute(
                """
                UPDATE devices SET websocket_connection_id=?
                WHERE id=? AND last_pairing_id=?
                  AND NOT EXISTS (
                    SELECT 1 FROM pairing_candidates AS candidate
                    WHERE candidate.device_id=devices.id
                      AND candidate.status='finalized'
                      AND candidate.expires_at>?
                  )
                """,
                (connection_id, device_id, session_id, now),
            )
            await db.commit()
        return updated.rowcount == 1

    async def is_websocket_connection_current(
        self,
        device_id: str,
        session_id: str,
        connection_id: str,
    ) -> bool:
        if not device_id or not session_id or not connection_id:
            return False
        async with aiosqlite.connect(self.db_path) as db:
            now = self._now().isoformat()
            row = await (
                await db.execute(
                    """
                    SELECT 1 FROM devices AS device
                    WHERE device.id=? AND device.last_pairing_id=?
                      AND device.websocket_connection_id=?
                      AND NOT EXISTS (
                        SELECT 1 FROM pairing_candidates AS candidate
                        WHERE candidate.device_id=device.id
                          AND candidate.status='finalized'
                          AND candidate.expires_at>?
                      )
                    """,
                    (device_id, session_id, connection_id, now),
                )
            ).fetchone()
        return row is not None

    async def clear_websocket_connection(
        self,
        device_id: str,
        session_id: str,
        connection_id: str,
    ) -> None:
        """Clear a connection only while it still owns the durable device slot."""

        if not device_id or not session_id or not connection_id:
            return
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            await db.execute(
                """
                UPDATE devices SET websocket_connection_id=NULL
                WHERE id=? AND last_pairing_id=? AND websocket_connection_id=?
                """,
                (device_id, session_id, connection_id),
            )
            await db.commit()

    def _now(self) -> datetime:
        current = self.clock()
        if current.tzinfo is None:
            raise RuntimeError("authentication clock must be timezone-aware")
        return current.astimezone(UTC)
