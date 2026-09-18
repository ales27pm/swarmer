import { Database } from "bun:sqlite";
import { mkdirSync } from "node:fs";
import path from "node:path";

// State Service — SQLite (WAL) is the source of truth (MASTER_SPEC §6).
const dataDir = path.join(import.meta.dir, "..", "data");
mkdirSync(dataDir, { recursive: true });

export const db = new Database(path.join(dataDir, "swarmer.db"));
db.exec("PRAGMA journal_mode = WAL;");
db.exec("PRAGMA foreign_keys = ON;");

db.exec(`
CREATE TABLE IF NOT EXISTS devices (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  token TEXT NOT NULL UNIQUE,
  created_at TEXT NOT NULL,
  last_seen_at TEXT
);

CREATE TABLE IF NOT EXISTS conversations (
  id TEXT PRIMARY KEY,
  title TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tasks (
  id TEXT PRIMARY KEY,
  conversation_id TEXT,
  user_id TEXT NOT NULL DEFAULT 'local',
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
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);

CREATE TABLE IF NOT EXISTS messages (
  id TEXT PRIMARY KEY,
  conversation_id TEXT NOT NULL,
  task_id TEXT,
  role TEXT NOT NULL,
  agent_id TEXT,
  content TEXT NOT NULL,
  metadata_json TEXT,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_conversation ON messages(conversation_id, created_at);

CREATE TABLE IF NOT EXISTS agents (
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

CREATE TABLE IF NOT EXISTS approvals (
  id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL,
  agent_id TEXT,
  action_type TEXT NOT NULL,
  target TEXT,
  reason TEXT,
  risk TEXT NOT NULL,
  status TEXT NOT NULL,
  request_json TEXT NOT NULL,
  decision_json TEXT,
  created_at TEXT NOT NULL,
  decided_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_approvals_status ON approvals(status, created_at);

CREATE TABLE IF NOT EXISTS memory_items (
  id TEXT PRIMARY KEY,
  scope TEXT NOT NULL,
  kind TEXT NOT NULL,
  content TEXT NOT NULL,
  summary TEXT,
  source_event_id TEXT,
  sensitivity TEXT NOT NULL DEFAULT 'normal',
  confidence REAL NOT NULL DEFAULT 1.0,
  pinned INTEGER NOT NULL DEFAULT 0,
  embedding_id TEXT,
  metadata_json TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_memory_scope ON memory_items(scope, kind);

CREATE TABLE IF NOT EXISTS feedback_events (
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
CREATE INDEX IF NOT EXISTS idx_feedback_task ON feedback_events(task_id);

CREATE TABLE IF NOT EXISTS audit_events (
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
CREATE INDEX IF NOT EXISTS idx_audit_trace ON audit_events(trace_id, created_at);

CREATE TABLE IF NOT EXISTS sync_events (
  id TEXT PRIMARY KEY,
  device_id TEXT NOT NULL,
  op_type TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  status TEXT NOT NULL,
  server_seq INTEGER NOT NULL,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sync_device_seq ON sync_events(device_id, server_seq);
`);

export const now = () => new Date().toISOString();
export const id = (prefix: string) => `${prefix}_${crypto.randomUUID().replaceAll("-", "").slice(0, 16)}`;

// Seed the local control-plane agents on first boot.
const agentCount = db.query("SELECT COUNT(*) as n FROM agents").get() as { n: number };
if (agentCount.n === 0) {
  const ts = now();
  const seed = db.prepare(
    `INSERT INTO agents (id, name, version, endpoint, model_id, status, skills_json, last_heartbeat_at, created_at, updated_at)
     VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`
  );
  seed.run(
    "orchestrator-01", "Orchestrateur", "0.1.0", "local://orchestrator",
    "Hermes-3-Llama-3.2-3B-abliterated-Q4_K_M", "online",
    JSON.stringify(["planification", "routing", "tool-call-json"]), ts, ts, ts
  );
  seed.run(
    "code-worker-01", "Code Worker", "0.1.0", "local://workers/code",
    "G9v3-3B-Heretic-Abliterated-Q4_K_M", "online",
    JSON.stringify(["read_file", "write_file", "diff", "run_tests"]), ts, ts, ts
  );
  seed.run(
    "research-worker-01", "Research Worker", "0.1.0", "local://workers/research",
    "Dolphin3.0-Llama3.2-3B-Q4_K_M", "offline",
    JSON.stringify(["web_search", "summarize"]), null, ts, ts
  );
  seed.run(
    "phone-broker-01", "Phone Broker", "0.1.0", "local://brokers/iphone",
    null, "online",
    JSON.stringify(["location.current", "contacts.search", "calendar.events.read", "photos.pick"]), ts, ts, ts
  );
}

// Seed a couple of long-term memories on first boot.
const memCount = db.query("SELECT COUNT(*) as n FROM memory_items").get() as { n: number };
if (memCount.n === 0) {
  const ts = now();
  const ins = db.prepare(
    `INSERT INTO memory_items (id, scope, kind, content, summary, pinned, created_at, updated_at)
     VALUES (?, ?, ?, ?, ?, ?, ?, ?)`
  );
  ins.run(id("mem"), "projet", "fait", "Le projet 27PM CRM utilise un backend Hono et une app Expo.", "27PM CRM: stack Hono + Expo", 1, ts, ts);
  ins.run(id("mem"), "profil", "préférence", "Aucun sermon inutile: les agents proposent un chemin utile au lieu de refuser.", "Préférence: pas de moralisme", 1, ts, ts);
  ins.run(id("mem"), "système", "règle", "Toute action sensible passe par la Permission Gateway avant exécution.", "Règle: gateway stricte", 0, ts, ts);
}
