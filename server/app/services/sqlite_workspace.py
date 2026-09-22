"""SQLite tools for dedicated, operator-owned project workspaces.

The caller authorizes the project and operation; payloads cannot choose a root.
Never point this service at the control-plane data directory. Workspace directory
ownership must exclude other processes that can rename files during an operation.
SQL authorizers constrain SQL capabilities; they are not an OS sandbox.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
import threading
import time
import uuid
from collections.abc import Callable, Sequence
from contextlib import closing
from pathlib import Path
from typing import Any

MAX_RESULT_BYTES = 64_000
MAX_SQL_BYTES = 32_000
MAX_ROWS = 200
_PRIVATE = "_swarmer_migrations"
_LOCK = threading.Lock()


class SQLiteWorkspaceError(ValueError):
    """A workspace operation could not be safely completed."""


class SQLiteWorkspace:
    def __init__(self, root: Path, *, denied_paths: Sequence[Path] = ()) -> None:
        self.root = root.resolve(strict=True)
        if not self.root.is_dir():
            raise SQLiteWorkspaceError("workspace root must be a directory")
        self.denied = {path.resolve() for path in denied_paths}
        if any(
            path == self.root or self.root in path.parents or path in self.root.parents
            for path in self.denied
        ):
            raise SQLiteWorkspaceError("workspace contains protected application data")

    def _path(self, relative: object, *, existing: bool = True) -> Path:
        if not isinstance(relative, str) or not relative or "\x00" in relative:
            raise SQLiteWorkspaceError("database path is required")
        value = Path(relative)
        if value.is_absolute() or any(p in {"..", "."} or p.startswith(".") for p in value.parts):
            raise SQLiteWorkspaceError("database must be inside its project workspace")
        candidate = self.root
        for part in value.parts:
            candidate /= part
            if candidate.is_symlink():
                raise SQLiteWorkspaceError("symlinks are not permitted")
        if candidate.suffix.lower() not in {".db", ".sqlite", ".sqlite3"}:
            raise SQLiteWorkspaceError("database must have a SQLite file extension")
        if candidate.resolve() in self.denied or not candidate.parent.is_dir():
            raise SQLiteWorkspaceError("database path is not permitted")
        for suffix in ("", "-wal", "-shm", "-journal"):
            path = Path(str(candidate) + suffix)
            if path.exists() or path.is_symlink():
                info = path.lstat()
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise SQLiteWorkspaceError(
                        "database and sidecars must be regular unlinked files"
                    )
        if existing and not candidate.is_file():
            raise SQLiteWorkspaceError("database does not exist")
        return candidate

    @staticmethod
    def _connect(path: Path, *, readonly: bool) -> sqlite3.Connection:
        connection = sqlite3.connect(
            path.as_uri() + ("?mode=ro" if readonly else "?mode=rw"),
            uri=True,
            timeout=1,
            isolation_level=None,
        )
        connection.execute("PRAGMA trusted_schema=OFF")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, 1_000_000)
        connection.setlimit(sqlite3.SQLITE_LIMIT_SQL_LENGTH, MAX_SQL_BYTES)
        connection.setlimit(sqlite3.SQLITE_LIMIT_COLUMN, 128)
        connection.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 256)
        return connection

    @staticmethod
    def _authorizer(write: bool) -> Callable[..., int]:
        readonly = {
            sqlite3.SQLITE_SELECT,
            sqlite3.SQLITE_READ,
            sqlite3.SQLITE_FUNCTION,
            sqlite3.SQLITE_RECURSIVE,
        }
        blocked = {
            sqlite3.SQLITE_ATTACH,
            sqlite3.SQLITE_DETACH,
            sqlite3.SQLITE_PRAGMA,
            sqlite3.SQLITE_TRANSACTION,
            sqlite3.SQLITE_SAVEPOINT,
            sqlite3.SQLITE_CREATE_VTABLE,
            sqlite3.SQLITE_DROP_VTABLE,
        }

        def authorize(
            action: int,
            arg1: str | None,
            arg2: str | None,
            database: str | None,
            trigger: str | None,
        ) -> int:
            if action in blocked or (not write and action not in readonly):
                return sqlite3.SQLITE_DENY
            if database not in {None, "main"}:
                return sqlite3.SQLITE_DENY
            if action == sqlite3.SQLITE_FUNCTION and (arg2 or "").lower() in {
                "load_extension",
                "writefile",
                "readfile",
                "fts3_tokenizer",
            }:
                return sqlite3.SQLITE_DENY
            if (
                arg1 == _PRIVATE or (action == sqlite3.SQLITE_ALTER_TABLE and arg2 == _PRIVATE)
            ) and action != sqlite3.SQLITE_READ:
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        return authorize

    @staticmethod
    def _rows(cursor: sqlite3.Cursor, limit: int = MAX_ROWS) -> dict[str, Any]:
        rows = cursor.fetchmany(limit + 1)
        if len(rows) > limit:
            raise SQLiteWorkspaceError("result exceeds row limit; narrow the query")

        def value(item: Any) -> Any:
            if isinstance(item, bytes):
                return {"encoding": "hex", "value": item.hex()}
            return item

        result = {
            "columns": [item[0] for item in cursor.description or []],
            "rows": [[value(item) for item in row] for row in rows],
        }
        if len(json.dumps(result, allow_nan=False).encode()) > MAX_RESULT_BYTES:
            raise SQLiteWorkspaceError("result exceeds byte limit; narrow the query")
        return result

    @staticmethod
    def _integrity(connection: sqlite3.Connection) -> dict[str, Any]:
        integrity = connection.execute("PRAGMA integrity_check").fetchmany(2)
        foreign = connection.execute("PRAGMA foreign_key_check").fetchmany(1)
        if integrity != [("ok",)] or foreign:
            raise SQLiteWorkspaceError("database integrity or foreign-key validation failed")
        return {"integrity_check": "ok", "foreign_key_check": "ok"}

    def _backup(self, source: Path, target: Path, check: Callable[[], None]) -> dict[str, Any]:
        fd = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
        try:
            with (
                closing(self._connect(source, readonly=True)) as src,
                closing(self._connect(target, readonly=False)) as dst,
            ):

                def progress() -> int:
                    check()
                    return 0

                dst.set_progress_handler(progress, 1000)
                src.backup(dst, pages=128, progress=lambda *_: check(), sleep=0.01)
                proof = self._integrity(dst)
            check()
            digest = hashlib.sha256()
            with target.open("rb") as stream:
                for block in iter(lambda: stream.read(65536), b""):
                    check()
                    digest.update(block)
            return {
                "path": str(target.relative_to(self.root)),
                "sha256": digest.hexdigest(),
                **proof,
            }
        except BaseException:
            target.unlink(missing_ok=True)
            raise

    def execute(
        self,
        operation: str,
        payload: dict[str, Any],
        *,
        ensure_active: Callable[[], None] = lambda: None,
    ) -> dict[str, Any]:
        """Execute an authorized operation. Mutations require stable migration_id.

        migrate/create statements are one SQL statement each, parameters optional.
        Retrying the same migration_id and identical statements returns a receipt;
        reusing an ID with different SQL is rejected. query is always read-only.
        """
        if operation not in {"inspect", "query", "create", "backup", "migrate"}:
            raise SQLiteWorkspaceError("unsupported SQLite operation")
        if not isinstance(payload, dict):
            raise SQLiteWorkspaceError("payload must be an object")
        deadline = time.monotonic() + 10

        def check() -> None:
            ensure_active()
            if time.monotonic() > deadline:
                raise SQLiteWorkspaceError("SQLite operation exceeded wall-time limit")

        if not _LOCK.acquire(blocking=False):
            raise SQLiteWorkspaceError("another SQLite operation is active")
        connection = None
        created = False
        committed = False
        try:
            check()
            path = self._path(payload.get("path"), existing=operation != "create")
            if operation == "backup":
                target = self._path(payload.get("destination"), existing=False)
                return {"operation": operation, "backup": self._backup(path, target, check)}
            write = operation in {"create", "migrate"}
            if operation == "create" and not path.exists():
                fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                os.close(fd)
                created = True
            connection = self._connect(path, readonly=not write)

            def progress() -> int:
                check()
                return 0

            connection.set_progress_handler(progress, 1000)
            if operation == "inspect":
                result = self._rows(
                    connection.execute(
                        "SELECT type,name,tbl_name,sql FROM sqlite_schema "
                        "WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name"
                    )
                )
                return {"operation": operation, "schema": result, **self._integrity(connection)}
            if operation == "query":
                sql = payload.get("sql")
                if not isinstance(sql, str) or len(sql.encode()) > MAX_SQL_BYTES:
                    raise SQLiteWorkspaceError("bounded SQL text is required")
                connection.set_authorizer(self._authorizer(False))
                result = self._rows(connection.execute(sql, payload.get("parameters", [])))
                check()
                return {"operation": operation, **result}
            migration = payload.get("migration_id")
            statements = payload.get("statements")
            if not isinstance(migration, str) or not 1 <= len(migration) <= 128:
                raise SQLiteWorkspaceError("stable migration_id is required")
            if not isinstance(statements, list) or not 1 <= len(statements) <= 32:
                raise SQLiteWorkspaceError("one to 32 parameterized statements are required")
            encoded = json.dumps(statements, sort_keys=True, allow_nan=False).encode()
            if len(encoded) > MAX_SQL_BYTES:
                raise SQLiteWorkspaceError("migration exceeds SQL byte limit")
            digest = hashlib.sha256(encoded).hexdigest()
            connection.execute("BEGIN IMMEDIATE")
            exists = connection.execute(
                "SELECT 1 FROM sqlite_schema WHERE name=?", (_PRIVATE,)
            ).fetchone()
            if exists:
                previous = connection.execute(
                    "SELECT digest,receipt FROM _swarmer_migrations WHERE id=?", (migration,)
                ).fetchone()
                if previous:
                    if previous[0] != digest:
                        raise SQLiteWorkspaceError(
                            "migration_id already used with different statements"
                        )
                    return {**json.loads(previous[1]), "replayed": True}
            if operation == "create" and not created:
                raise SQLiteWorkspaceError("database already exists; use migrate")
            backup = None
            if not created:
                backup_path = path.with_name(f"{path.stem}-backup-{uuid.uuid4().hex}.sqlite")
                backup = self._backup(path, backup_path, check)
            connection.execute(
                f"CREATE TABLE IF NOT EXISTS {_PRIVATE} (id TEXT PRIMARY KEY, digest TEXT NOT NULL, receipt TEXT NOT NULL)"
            )
            before = connection.total_changes
            connection.set_authorizer(self._authorizer(True))
            for statement in statements:
                check()
                if not isinstance(statement, dict) or not isinstance(statement.get("sql"), str):
                    raise SQLiteWorkspaceError("each statement requires SQL text")
                connection.execute(statement["sql"], statement.get("parameters", []))
            connection.set_authorizer(None)
            result = {
                "operation": operation,
                "path": str(path.relative_to(self.root)),
                "migration_id": migration,
                "statement_sha256": digest,
                "changes": connection.total_changes - before,
                "backup": backup,
                "replayed": False,
                **self._integrity(connection),
            }
            connection.execute(
                "INSERT INTO _swarmer_migrations VALUES (?,?,?)",
                (migration, digest, json.dumps(result)),
            )
            check()
            connection.commit()
            committed = True
            return result
        except sqlite3.Error as exc:
            raise SQLiteWorkspaceError(
                "SQLite rejected the operation; no transaction was committed"
            ) from exc
        finally:
            if connection is not None:
                connection.close()
            if created and not committed:
                path.unlink(missing_ok=True)
            _LOCK.release()
