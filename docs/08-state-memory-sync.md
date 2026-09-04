# 08 — State, Memory and Sync

## Objectif

Donner à toutes les composantes le même état et une mémoire long terme sans que les agents écrivent n'importe où.

## Architecture data

```text
State Service
  ├─ SQLite WAL MVP / Postgres later
  ├─ append-only event log
  ├─ task/conversation/approval tables
  └─ sync cursors

Memory Service
  ├─ chunker
  ├─ embedding model
  ├─ FAISS/Qdrant vector index
  ├─ metadata store
  └─ retrieval pack builder

iPhone Local Replica
  ├─ SQLite app DB
  ├─ outbox
  ├─ synced cursors
  └─ cached capabilities
```

## Source de vérité

Ubuntu est maître pour:

- task status;
- approvals;
- agent registry;
- long-term memory;
- audit log;
- feedback;
- artifacts.

L'iPhone est maître temporaire pour:

- draft local;
- outbox non synchronisée;
- permissions iOS runtime;
- capteur/donnée native demandée;
- UI state.

## SQLite iPhone tables

```sql
CREATE TABLE messages (
  id TEXT PRIMARY KEY,
  conversation_id TEXT NOT NULL,
  role TEXT NOT NULL,
  content TEXT NOT NULL,
  created_at TEXT NOT NULL,
  sync_status TEXT NOT NULL DEFAULT 'pending'
);

CREATE TABLE sync_outbox (
  id TEXT PRIMARY KEY,
  op_type TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  attempts INTEGER NOT NULL DEFAULT 0,
  last_error TEXT
);

CREATE TABLE approvals (
  id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL,
  status TEXT NOT NULL,
  risk TEXT NOT NULL,
  summary TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);
```

## Ubuntu DB tables MVP

Voir `docs/13-data-model.md` pour le schéma complet.

Tables principales:

- `tasks`
- `messages`
- `agents`
- `approvals`
- `permissions`
- `memory_items`
- `artifacts`
- `feedback_events`
- `audit_events`
- `sync_events`

## Memory layers

### Session context

Court terme, dans la conversation active.

### State context

Données structurées: tâches, agents, permissions, projets.

### Semantic memory

Chunks textuels vectorisés:

- conversations importantes;
- décisions;
- fichiers résumés;
- corrections utilisateur;
- préférences;
- project facts.

### Artifact memory

Fichiers/disques:

- patches;
- rapports;
- exports;
- captures;
- logs.

### Event log

Append-only:

- qui a fait quoi;
- quand;
- permission;
- résultat;
- hash précédent.

## Memory write policy

Les agents ne peuvent pas écrire directement une mémoire permanente. Ils proposent:

```json
{
  "type": "memory_write_candidate",
  "scope": "project:27pm-crm",
  "content": "Le projet préfère les checks locaux plutôt que GitHub Actions.",
  "confidence": 0.92,
  "source_event_id": "evt_...",
  "sensitivity": "normal"
}
```

Memory Service décide:

- auto-store low-risk project facts;
- ask user for personal/sensitive facts;
- reject duplicates;
- merge/update if needed.

## Retrieval policy

Quand l'orchestrateur démarre une tâche:

1. Lire state structuré.
2. Chercher mémoire vectorielle avec query.
3. Filtrer par scope.
4. Construire context pack limité.
5. Citer les memory ids dans le task log.

## Sync strategy

### Bootstrap

```http
GET /sync/bootstrap?device_id=...
```

Retourne:

- server clock;
- user profile minimal;
- last tasks;
- pending approvals;
- agent status;
- sync cursor.

### Pull

```http
GET /sync/pull?cursor=...
```

### Push

```http
POST /sync/push
```

Payload:

```json
{
  "device_id": "iphone-ales",
  "ops": [
    {"id": "op_1", "type": "message.create", "payload": {}}
  ]
}
```

### Conflict resolution

- Server wins: task status, approval status, audit.
- Client wins: local drafts, UI preferences.
- Merge: messages, feedback, memory corrections.
- Ask user: conflicting permission rules.

## Backups

- Daily SQLite backup.
- Weekly vector index snapshot.
- Audit log rotated but immutable.
- Export JSONL for dataset.
