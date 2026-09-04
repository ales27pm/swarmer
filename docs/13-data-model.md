# 13 — Data Model

## SQLite/Postgres schema draft

### tasks

```sql
CREATE TABLE tasks (
  id TEXT PRIMARY KEY,
  conversation_id TEXT,
  user_id TEXT NOT NULL,
  title TEXT,
  input TEXT NOT NULL,
  mode TEXT NOT NULL,
  status TEXT NOT NULL,
  priority INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  completed_at TEXT,
  error_json TEXT
);
```

### messages

```sql
CREATE TABLE messages (
  id TEXT PRIMARY KEY,
  conversation_id TEXT NOT NULL,
  task_id TEXT,
  role TEXT NOT NULL,
  agent_id TEXT,
  content TEXT NOT NULL,
  metadata_json TEXT,
  created_at TEXT NOT NULL
);
```

### agents

```sql
CREATE TABLE agents (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  version TEXT NOT NULL,
  endpoint TEXT NOT NULL,
  model_id TEXT,
  status TEXT NOT NULL,
  skills_json TEXT NOT NULL,
  permissions_json TEXT,
  last_heartbeat_at TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
```

### approvals

```sql
CREATE TABLE approvals (
  id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL,
  agent_id TEXT,
  action_type TEXT NOT NULL,
  target TEXT,
  risk TEXT NOT NULL,
  status TEXT NOT NULL,
  request_json TEXT NOT NULL,
  decision_json TEXT,
  created_at TEXT NOT NULL,
  decided_at TEXT
);
```

### memory_items

```sql
CREATE TABLE memory_items (
  id TEXT PRIMARY KEY,
  scope TEXT NOT NULL,
  kind TEXT NOT NULL,
  content TEXT NOT NULL,
  summary TEXT,
  source_event_id TEXT,
  sensitivity TEXT NOT NULL DEFAULT 'normal',
  confidence REAL NOT NULL DEFAULT 1.0,
  embedding_id TEXT,
  metadata_json TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
```

### artifacts

```sql
CREATE TABLE artifacts (
  id TEXT PRIMARY KEY,
  task_id TEXT,
  path TEXT NOT NULL,
  mime_type TEXT,
  sha256 TEXT,
  size_bytes INTEGER,
  metadata_json TEXT,
  created_at TEXT NOT NULL
);
```

### feedback_events

```sql
CREATE TABLE feedback_events (
  id TEXT PRIMARY KEY,
  task_id TEXT,
  agent_id TEXT,
  type TEXT NOT NULL,
  label TEXT,
  score REAL,
  notes TEXT,
  payload_json TEXT,
  created_at TEXT NOT NULL
);
```

### audit_events

```sql
CREATE TABLE audit_events (
  id TEXT PRIMARY KEY,
  trace_id TEXT NOT NULL,
  event_type TEXT NOT NULL,
  actor_type TEXT NOT NULL,
  actor_id TEXT NOT NULL,
  task_id TEXT,
  payload_json TEXT NOT NULL,
  prev_hash TEXT,
  hash TEXT,
  created_at TEXT NOT NULL
);
```

### sync_events

```sql
CREATE TABLE sync_events (
  id TEXT PRIMARY KEY,
  device_id TEXT NOT NULL,
  op_type TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  status TEXT NOT NULL,
  server_seq INTEGER NOT NULL,
  created_at TEXT NOT NULL
);
```

## Indexes

```sql
CREATE INDEX idx_tasks_status ON tasks(status);
CREATE INDEX idx_messages_conversation ON messages(conversation_id, created_at);
CREATE INDEX idx_approvals_status ON approvals(status, created_at);
CREATE INDEX idx_memory_scope ON memory_items(scope, kind);
CREATE INDEX idx_feedback_task ON feedback_events(task_id);
CREATE INDEX idx_audit_trace ON audit_events(trace_id, created_at);
CREATE INDEX idx_sync_device_seq ON sync_events(device_id, server_seq);
```

## Data retention

- audit: garder long terme;
- task messages: configurable;
- memory: user editable;
- feedback: conserver pour eval/dataset après nettoyage;
- iPhone native data: TTL court sauf si explicitement mémorisé.
