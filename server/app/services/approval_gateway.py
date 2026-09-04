from datetime import UTC, datetime
from uuid import uuid4

import aiosqlite


class ApprovalGateway:
    def __init__(self, db_path):
        self.db_path = db_path

    async def request(self, task_id: str, action: str, summary: str, risk: str = "medium") -> dict:
        record = {
            "id": f"apr_{uuid4().hex}",
            "task_id": task_id,
            "action": action,
            "summary": summary,
            "risk": risk,
            "status": "pending",
            "created_at": datetime.now(UTC).isoformat(),
        }
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("INSERT INTO approvals(id,task_id,action,summary,risk,status,created_at) VALUES(?,?,?,?,?,?,?)", tuple(record.values()))
            await db.execute("UPDATE tasks SET status='waiting_permission', updated_at=? WHERE id=?", (record["created_at"], task_id))
            await db.commit()
        return record

    async def list_pending(self) -> list[dict]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            rows = await (await db.execute("SELECT * FROM approvals WHERE status='pending' ORDER BY created_at DESC")).fetchall()
            return [dict(r) for r in rows]

    async def decide(self, approval_id: str, decision: str) -> dict | None:
        status = "approved" if decision == "approve" else "denied"
        now = datetime.now(UTC).isoformat()
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            row = await (await db.execute("SELECT * FROM approvals WHERE id=?", (approval_id,))).fetchone()
            if not row:
                return None
            await db.execute("UPDATE approvals SET status=?, decided_at=? WHERE id=?", (status, now, approval_id))
            next_task_status = "queued" if status == "approved" else "blocked"
            await db.execute("UPDATE tasks SET status=?, updated_at=? WHERE id=?", (next_task_status, now, row["task_id"]))
            await db.commit()
            result = dict(row)
            result.update(status=status, decided_at=now)
            return result
