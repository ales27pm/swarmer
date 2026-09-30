from pathlib import Path

import aiosqlite
import pytest

from app.models import MemoryCreate, MemorySearch
from app.services.state_service import StateService


@pytest.mark.asyncio
async def test_other_scopes_cannot_evict_requested_scope_from_search_window(tmp_path: Path) -> None:
    state = StateService(tmp_path / "state.db")
    await state.initialize()
    target = await state.create_memory(
        MemoryCreate(content="SQLite requirements", scope="crm"), "phone"
    )
    async with aiosqlite.connect(state.db_path) as db:
        await db.executemany(
            """INSERT INTO memory_items(id,kind,scope,content,summary,pinned,created_at,updated_at)
            VALUES(?,'fact','other','SQLite other project',NULL,1,'now','now')""",
            [(f"other_{i}",) for i in range(501)],
        )
        await db.commit()
    result = await state.search_memory(MemorySearch(query="SQLite", scope="crm"))
    assert [item["id"] for item in result] == [target["id"]]
