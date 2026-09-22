# SQLite workspace worker

Executable skills: `database.sqlite.inspect`, `.query`, `.create`, `.backup`, `.migrate`.
Register these through the existing authenticated worker administration and skill
policy, then launch the worker with `SWARMER_WORKER_TOKEN` in its environment:

```sh
python3 workers/sqlite-worker/sqlite_worker.py --base-url https://control.example \
  --agent-id APPROVED_AGENT_ID --workspace /srv/project-databases/PROJECT_ID \
  --deny-path /srv/control-plane/data
```

The workspace is fixed by the operator, never by a job. Run one project-scoped
worker account with exclusive directory ownership. Neither this module nor an SQL
authorizer is an OS sandbox. Do not grant the account permission to change the
control-plane data or other projects, and do not share the directory with arbitrary
shell/code writers. The CLI requires at least one protected application-data path.
Existing worker policies, authorization, approval and lease generation still apply;
this worker must not be advertised as available before registration and health checks.

| Operation | Payload |
| --- | --- |
| inspect | `{"path":"crm.sqlite"}` |
| query | `{"path":"crm.sqlite","sql":"SELECT name FROM clients WHERE id=?","parameters":[1]}` |
| create | `{"path":"crm.sqlite","migration_id":"initial-v1","statements":[{"sql":"CREATE TABLE clients(id INTEGER PRIMARY KEY, name TEXT)"}]}` |
| migrate | Same shape as create, with a new stable migration ID |
| backup | `{"path":"crm.sqlite","destination":"crm-copy.sqlite"}` |

Queries are read-only. Writes require statements individually executed inside
`BEGIN IMMEDIATE`; transaction control, attachment, pragmas and virtual tables are
blocked in submitted SQL. Each migration creates a coherent SQLite backup before
editing, validates integrity and foreign keys before commit, and stores an atomic
receipt. Identical migration retries return that receipt; reusing its ID with
different statements fails. Failed migrations roll back. Existing files are never
overwritten by create or backup. Existing backups are intentionally retained.

Limits: 32 statements / 32 KB migration input, 200 rows / 64 KB query result,
1 MB SQLite value, 10-second operation deadline, 1-second busy wait. Results beyond
a limit fail explicitly. No model is needed. A lost result delivery can follow a
successful commit; retry using the **same migration ID**, never invent a new one.
A backup is a filesystem operation: use a unique destination and inspect it after
an uncertain result; the worker will not overwrite it on retry.

Integration is additive: route authorized project jobs to these skill IDs and pass
only a relative database path. Preserve receipt `job_id`, `lease_generation`,
`migration_id`, statement hash, backup hash and integrity fields as tool evidence.
The worker release must include the sibling file-worker protocol and
`server/app/services/sqlite_workspace.py`. No API/catalog registration or production
deployment is performed by this module.

Validation:

```sh
cd server
.venv/bin/pytest tests/test_sqlite_workspace.py ../workers/sqlite-worker/test_sqlite_worker.py -q
```
