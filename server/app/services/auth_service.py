from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

import aiosqlite


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
    ) -> None:
        self.db_path = db_path
        self.pairing_ttl_seconds = min(max(pairing_ttl_seconds, 60), 900)
        self.pairing_max_attempts = min(max(pairing_max_attempts, 1), 20)
        self.pairing_candidate_ttl_seconds = min(max(pairing_candidate_ttl_seconds, 30), 300)
        self._pairing_pepper = pairing_pepper.encode("utf-8")

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
        now = datetime.now(UTC)
        expires = (now + timedelta(seconds=self.pairing_ttl_seconds)).isoformat()
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
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
        now_value = datetime.now(UTC)
        now = now_value.isoformat()
        candidate = self._pairing_digest(code)
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
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
        now = datetime.now(UTC).isoformat()
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
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
        now_value = datetime.now(UTC)
        now = now_value.isoformat()
        token_hash = self._stored_token(token)
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
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
        now = datetime.now(UTC).isoformat()
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            await db.execute("DELETE FROM pairing_candidates WHERE expires_at<=?", (now,))
            token_hash = self._stored_token(token)
            row = await (
                await db.execute(
                    """
                    SELECT id,name,created_at,'active' AS credential_state
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
                            last_pairing_id=excluded.last_pairing_id
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
                            SELECT id,name,created_at,'active' AS credential_state
                            FROM devices WHERE id=? AND token=?
                            """,
                            (str(ready["device_id"]), token_hash),
                        )
                    ).fetchone()
            if not row:
                await db.commit()
                return None
            await db.execute("UPDATE devices SET last_seen_at=? WHERE id=?", (now, row["id"]))
            await db.commit()
        return {**dict(row), "last_seen_at": now}

    async def validate_token(self, token: str) -> bool:
        return await self.authenticate_token(token) is not None

    async def create_websocket_ticket(self, token: str) -> str | None:
        principal = await self.authenticate_token(token)
        if not principal:
            return None
        ticket = secrets.token_urlsafe(32)
        expires = (datetime.now(UTC) + timedelta(seconds=30)).isoformat()
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "DELETE FROM websocket_tickets WHERE expires_at<?", (datetime.now(UTC).isoformat(),)
            )
            await db.execute(
                "INSERT INTO websocket_tickets(ticket_hash,device_id,expires_at) VALUES(?,?,?)",
                (self._digest(ticket), principal["id"], expires),
            )
            await db.commit()
        return ticket

    async def consume_websocket_ticket(self, ticket: str) -> dict[str, str] | None:
        now = datetime.now(UTC).isoformat()
        digest = self._digest(ticket)
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            row = await (
                await db.execute(
                    """
                    SELECT ticket_hash,device_id FROM websocket_tickets
                    WHERE ticket_hash=? AND expires_at>=?
                    """,
                    (digest, now),
                )
            ).fetchone()
            await db.execute("DELETE FROM websocket_tickets WHERE ticket_hash=?", (digest,))
            await db.execute("DELETE FROM websocket_tickets WHERE expires_at<?", (now,))
            await db.commit()
        return {"device_id": str(row["device_id"])} if row else None
