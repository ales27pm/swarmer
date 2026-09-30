"""Durable, device-owned website records and exact revision compare-and-swap."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import closing, contextmanager
from pathlib import Path
from typing import Any

ACTIVE_STATUSES = ("capturing", "branding", "building", "publishing")


class WebsiteConflict(ValueError):
    pass


class WebsiteStore:
    def __init__(self, root: Path):
        self.root = root
        self.db = root / "projects.sqlite3"

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with closing(sqlite3.connect(self.db, timeout=10)) as connection, connection:
            connection.row_factory = sqlite3.Row
            connection.execute("BEGIN IMMEDIATE")
            yield connection

    def initialize(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self.transaction() as connection:
            connection.execute("""CREATE TABLE IF NOT EXISTS website_projects (
                id TEXT PRIMARY KEY, owner TEXT NOT NULL, request_id TEXT NOT NULL,
                request_digest TEXT NOT NULL, version INTEGER NOT NULL, data TEXT NOT NULL,
                UNIQUE(owner, request_id))""")
            connection.execute("""CREATE TABLE IF NOT EXISTS website_requests (
                project_id TEXT NOT NULL, request_id TEXT NOT NULL, digest TEXT NOT NULL,
                PRIMARY KEY(project_id, request_id))""")
            connection.execute("""CREATE TABLE IF NOT EXISTS website_preview_tokens (
                token_hash TEXT PRIMARY KEY, project_id TEXT NOT NULL, build_digest TEXT NOT NULL,
                expires REAL NOT NULL)""")

    def pending_operations(self) -> list[dict[str, Any]]:
        with self.transaction() as connection:
            return [
                json.loads(row[0])
                for row in connection.execute(
                    "SELECT data FROM website_projects WHERE json_extract(data,'$.status') "
                    "IN (?,?,?,?)",
                    ACTIVE_STATUSES,
                )
            ]

    def command_result(
        self, project_id: str, owner: str, request_id: str, digest: str
    ) -> dict[str, Any] | None:
        """Read an exact accepted command receipt, even while its owner is running."""
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT r.digest,p.data FROM website_requests r JOIN website_projects p "
                "ON p.id=r.project_id WHERE r.project_id=? AND r.request_id=? AND p.owner=?",
                (project_id, request_id, owner),
            ).fetchone()
            if row is None:
                return None
            if row["digest"] != digest:
                raise WebsiteConflict("Identifiant de demande déjà utilisé avec un autre contenu.")
            return json.loads(row["data"])  # type: ignore[no-any-return]

    def create(
        self, owner: str, request_id: str, digest: str, data: dict[str, Any]
    ) -> dict[str, Any]:
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT request_digest,data FROM website_projects WHERE owner=? AND request_id=?",
                (owner, request_id),
            ).fetchone()
            if row:
                if row["request_digest"] != digest:
                    raise WebsiteConflict(
                        "Identifiant de demande déjà utilisé avec un autre contenu."
                    )
                return json.loads(row["data"])  # type: ignore[no-any-return]
            connection.execute(
                "INSERT INTO website_projects VALUES(?,?,?,?,?,?)",
                (data["id"], owner, request_id, digest, data["version"], json.dumps(data)),
            )
        return data

    def get(self, project_id: str, owner: str | None = None) -> dict[str, Any]:
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT owner,data FROM website_projects WHERE id=?", (project_id,)
            ).fetchone()
        if not row or (owner is not None and row["owner"] != owner):
            raise KeyError(project_id)
        return json.loads(row["data"])  # type: ignore[no-any-return]

    def list(self, owner: str) -> list[dict[str, Any]]:
        with self.transaction() as connection:
            return [
                json.loads(row[0])
                for row in connection.execute(
                    "SELECT data FROM website_projects WHERE owner=? ORDER BY rowid DESC LIMIT 100",
                    (owner,),
                )
            ]

    def save(
        self,
        data: dict[str, Any],
        expected: int,
        *,
        request_id: str | None = None,
        request_digest: str | None = None,
        admit_operation: bool = False,
    ) -> dict[str, Any]:
        with self.transaction() as connection:
            if request_id:
                prior = connection.execute(
                    "SELECT digest FROM website_requests WHERE project_id=? AND request_id=?",
                    (data["id"], request_id),
                ).fetchone()
                if prior:
                    raise WebsiteConflict("Cette demande a déjà été traitée ; actualise le projet.")
            if admit_operation:
                active = connection.execute(
                    "SELECT COUNT(*) FROM website_projects WHERE json_extract(data,'$.status') "
                    "IN (?,?,?,?)",
                    ACTIVE_STATUSES,
                ).fetchone()[0]
                if active >= 8:
                    raise WebsiteConflict(
                        "La file de travail est pleine. Réessaie après les opérations en cours."
                    )
            data = {**data, "version": expected + 1}
            cursor = connection.execute(
                "UPDATE website_projects SET version=?,data=? WHERE id=? AND version=?",
                (data["version"], json.dumps(data), data["id"], expected),
            )
            if cursor.rowcount != 1:
                raise WebsiteConflict("Le projet a changé. Actualise avant de continuer.")
            if request_id:
                connection.execute(
                    "INSERT INTO website_requests VALUES(?,?,?)",
                    (data["id"], request_id, request_digest),
                )
        return data
