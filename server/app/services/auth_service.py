import secrets
from datetime import UTC, datetime, timedelta

import aiosqlite


class AuthService:
    def __init__(self, db_path):
        self.db_path = db_path

    async def create_pairing_code(self) -> str:
        code = f"{secrets.randbelow(1_000_000):06d}"
        expires = (datetime.now(UTC) + timedelta(minutes=10)).isoformat()
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("DELETE FROM pairing_codes WHERE expires_at < ?", (datetime.now(UTC).isoformat(),))
            await db.execute("INSERT INTO pairing_codes(code, expires_at) VALUES(?, ?)", (code, expires))
            await db.commit()
        return code

    async def complete_pairing(self, code: str, device_id: str, name: str) -> str | None:
        now = datetime.now(UTC).isoformat()
        async with aiosqlite.connect(self.db_path) as db:
            row = await (await db.execute("SELECT code FROM pairing_codes WHERE code=? AND expires_at>=?", (code, now))).fetchone()
            if not row:
                return None
            token = secrets.token_urlsafe(32)
            await db.execute("INSERT OR REPLACE INTO devices(id, name, token, created_at) VALUES(?, ?, ?, ?)", (device_id, name, token, now))
            await db.execute("DELETE FROM pairing_codes WHERE code=?", (code,))
            await db.commit()
            return token

    async def validate_token(self, token: str) -> bool:
        async with aiosqlite.connect(self.db_path) as db:
            row = await (await db.execute("SELECT 1 FROM devices WHERE token=?", (token,))).fetchone()
            return row is not None
