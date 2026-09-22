import os
import sqlite3

import pytest

from app.services.sqlite_workspace import SQLiteWorkspace, SQLiteWorkspaceError


@pytest.fixture
def workspace(tmp_path):
    return SQLiteWorkspace(tmp_path)


def create(workspace):
    return workspace.execute(
        "create",
        {
            "path": "crm.sqlite",
            "migration_id": "initial",
            "statements": [
                {"sql": "CREATE TABLE clients(id INTEGER PRIMARY KEY, name TEXT NOT NULL)"},
                {"sql": "INSERT INTO clients(name) VALUES (?)", "parameters": ["Alice"]},
            ],
        },
    )


def test_create_query_inspect_backup(workspace):
    receipt = create(workspace)
    assert receipt["changes"] == 1
    assert receipt["integrity_check"] == "ok"
    assert workspace.execute("query", {"path": "crm.sqlite", "sql": "SELECT name FROM clients"})[
        "rows"
    ] == [["Alice"]]
    schema = workspace.execute("inspect", {"path": "crm.sqlite"})
    assert any(row[1] == "clients" for row in schema["schema"]["rows"])
    backup = workspace.execute("backup", {"path": "crm.sqlite", "destination": "copy.sqlite"})[
        "backup"
    ]
    assert len(backup["sha256"]) == 64
    with sqlite3.connect(workspace.root / "copy.sqlite") as db:
        assert db.execute("SELECT name FROM clients").fetchall() == [("Alice",)]


def test_migration_backup_and_idempotency(workspace):
    create(workspace)
    payload = {
        "path": "crm.sqlite",
        "migration_id": "add-contact",
        "statements": [
            {"sql": "ALTER TABLE clients ADD COLUMN email TEXT"},
            {"sql": "UPDATE clients SET email=?", "parameters": ["alice@example.test"]},
        ],
    }
    receipt = workspace.execute("migrate", payload)
    assert receipt["backup"]["integrity_check"] == "ok"
    with sqlite3.connect(workspace.root / receipt["backup"]["path"]) as db:
        assert len(db.execute("PRAGMA table_info(clients)").fetchall()) == 2
    assert workspace.execute("migrate", payload)["replayed"] is True
    payload["statements"] = [{"sql": "DELETE FROM clients"}]
    with pytest.raises(SQLiteWorkspaceError, match="already used"):
        workspace.execute("migrate", payload)


def test_create_retry_does_not_replace_database(workspace):
    create(workspace)
    assert create(workspace)["replayed"] is True


@pytest.mark.parametrize(
    "sql",
    [
        "DELETE FROM clients",
        "PRAGMA user_version=3",
        "ATTACH DATABASE ':memory:' AS stolen",
        "SELECT load_extension('x')",
        "CREATE TEMP TABLE x(a)",
        "VACUUM INTO 'leak.sqlite'",
    ],
)
def test_read_only_query_denies_mutations(workspace, sql):
    create(workspace)
    with pytest.raises(SQLiteWorkspaceError):
        workspace.execute("query", {"path": "crm.sqlite", "sql": sql})
    assert workspace.execute(
        "query", {"path": "crm.sqlite", "sql": "SELECT count(*) FROM clients"}
    )["rows"] == [[1]]


@pytest.mark.parametrize(
    "sql",
    [
        "COMMIT",
        "ATTACH DATABASE ':memory:' AS other",
        "PRAGMA foreign_keys=OFF",
        "DELETE FROM _swarmer_migrations",
        "ALTER TABLE _swarmer_migrations RENAME TO hidden",
        "CREATE VIRTUAL TABLE x USING fts5(body)",
    ],
)
def test_migrations_cannot_escape_transaction(workspace, sql):
    create(workspace)
    with pytest.raises(SQLiteWorkspaceError):
        workspace.execute(
            "migrate",
            {
                "path": "crm.sqlite",
                "migration_id": "bad",
                "statements": [{"sql": "INSERT INTO clients(name) VALUES ('Bob')"}, {"sql": sql}],
            },
        )
    assert workspace.execute(
        "query", {"path": "crm.sqlite", "sql": "SELECT count(*) FROM clients"}
    )["rows"] == [[1]]


def test_failed_create_removed(workspace):
    with pytest.raises(SQLiteWorkspaceError):
        workspace.execute(
            "create",
            {"path": "bad.sqlite", "migration_id": "bad", "statements": [{"sql": "BOGUS"}]},
        )
    assert not (workspace.root / "bad.sqlite").exists()


def test_foreign_key_failure_rolls_back(workspace):
    create(workspace)
    with pytest.raises(SQLiteWorkspaceError):
        workspace.execute(
            "migrate",
            {
                "path": "crm.sqlite",
                "migration_id": "foreign",
                "statements": [
                    {
                        "sql": "CREATE TABLE projects(client INTEGER REFERENCES clients(id) DEFERRABLE INITIALLY DEFERRED)"
                    },
                    {"sql": "INSERT INTO projects VALUES (200)"},
                ],
            },
        )
    with sqlite3.connect(workspace.root / "crm.sqlite") as db:
        assert not db.execute("SELECT 1 FROM sqlite_schema WHERE name='projects'").fetchone()


def test_cancel_before_commit_rolls_back(workspace):
    create(workspace)
    calls = 0

    def cancel():
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("lease lost")

    with pytest.raises(RuntimeError, match="lease lost"):
        workspace.execute(
            "create",
            {
                "path": "cancel.sqlite",
                "migration_id": "cancel",
                "statements": [{"sql": "CREATE TABLE x(a)"}],
            },
            ensure_active=cancel,
        )
    assert not (workspace.root / "cancel.sqlite").exists()


@pytest.mark.parametrize(
    "path",
    ["../escape.sqlite", "/tmp/escape.sqlite", ".internal/db.sqlite", "file:other.db", "other.txt"],
)
def test_invalid_paths(workspace, path):
    with pytest.raises(SQLiteWorkspaceError):
        workspace.execute("inspect", {"path": path})


def test_protected_files_links_and_sidecars(tmp_path):
    with pytest.raises(SQLiteWorkspaceError):
        SQLiteWorkspace(tmp_path, denied_paths=[tmp_path / "server.db"])
    root = tmp_path / "projects"
    root.mkdir()
    secret = tmp_path / "server.db"
    secret.write_bytes(b"secret")
    service = SQLiteWorkspace(root, denied_paths=[secret])
    (root / "link.sqlite").symlink_to(secret)
    os.link(secret, root / "hard.sqlite")
    for name in ["link.sqlite", "hard.sqlite"]:
        with pytest.raises(SQLiteWorkspaceError):
            service.execute("inspect", {"path": name})
    create(service)
    (root / "crm.sqlite-wal").symlink_to(secret)
    with pytest.raises(SQLiteWorkspaceError):
        service.execute("inspect", {"path": "crm.sqlite"})


def test_bounded_result(workspace):
    create(workspace)
    with pytest.raises(SQLiteWorkspaceError, match="row limit"):
        workspace.execute(
            "query",
            {
                "path": "crm.sqlite",
                "sql": "WITH RECURSIVE n(x) AS (VALUES(1) UNION ALL SELECT x+1 FROM n WHERE x<201) SELECT x FROM n",
            },
        )
    with pytest.raises(SQLiteWorkspaceError, match="byte limit"):
        workspace.execute("query", {"path": "crm.sqlite", "sql": "SELECT hex(zeroblob(40000))"})


def test_backup_existing_target_never_overwritten(workspace):
    create(workspace)
    target = workspace.root / "target.sqlite"
    target.write_text("original")
    with pytest.raises(FileExistsError):
        workspace.execute("backup", {"path": "crm.sqlite", "destination": "target.sqlite"})
    assert target.read_text() == "original"
