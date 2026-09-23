"""The isolated release adds native grants without importing durable context."""

import sqlite3
from typing import Any

from fastapi import FastAPI

from tests import test_swift_project_transfer as transfer

native_project = transfer.native_project


async def test_schema24_to26_preserves_history_and_adds_only_native_grants(
    test_app: FastAPI, native_project: dict[str, Any]
) -> None:
    assert native_project["files"]  # Real, persisted handwritten Hello revision.
    path = test_app.state.settings.db_path
    with sqlite3.connect(path) as db:
        # This release's base schema is otherwise byte-identical to schema24.
        db.execute("DROP TABLE swift_project_validations")
        db.execute("PRAGMA user_version=24")
        before_schema = db.execute(
            "SELECT type,name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()
        tables = [row[1] for row in before_schema if row[0] == "table"]
        before_rows = {
            table: db.execute('SELECT * FROM "' + table + '"').fetchall() for table in tables
        }

    await test_app.state.state_service.initialize()
    await test_app.state.state_service.initialize()

    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA user_version").fetchone() == (26,)
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
        after_schema = db.execute(
            "SELECT type,name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()
        assert [row for row in after_schema if row[1] in {r[1] for r in before_schema}] == (
            before_schema
        )
        assert {(row[0], row[1]) for row in after_schema if row not in before_schema} == {
            ("table", "swift_project_validations"),
            ("index", "idx_swift_project_revision"),
        }
        assert {
            table: db.execute('SELECT * FROM "' + table + '"').fetchall() for table in tables
        } == before_rows
        assert db.execute("SELECT COUNT(*) FROM swift_project_validations").fetchone() == (0,)
