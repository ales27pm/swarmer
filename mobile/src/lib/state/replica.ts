import * as SQLite from "expo-sqlite";

import type { Agent, Approval, Bootstrap, Conversation, MemoryItem, Message, Task, TaskStatus, ToolCall } from "@/lib/api/types";

let dbPromise: Promise<SQLite.SQLiteDatabase> | null = null;
type ReplicaTable = "tasks" | "approvals" | "tool_calls" | "conversations" | "messages" | "agents" | "pinned_memory";
type PayloadRow = { payload: string };
type ValueRow = { value: string };

async function db() {
  if (!dbPromise) dbPromise = SQLite.openDatabaseAsync("mongars-replica.db");
  const value = await dbPromise;
  await value.execAsync(`
    PRAGMA journal_mode = WAL;
    CREATE TABLE IF NOT EXISTS tasks (id TEXT PRIMARY KEY, payload TEXT NOT NULL, updated_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS approvals (id TEXT PRIMARY KEY, payload TEXT NOT NULL, updated_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS tool_calls (id TEXT PRIMARY KEY, payload TEXT NOT NULL, updated_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS conversations (id TEXT PRIMARY KEY, payload TEXT NOT NULL, updated_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS messages (id TEXT PRIMARY KEY, payload TEXT NOT NULL, updated_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS agents (id TEXT PRIMARY KEY, payload TEXT NOT NULL, updated_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS pinned_memory (id TEXT PRIMARY KEY, payload TEXT NOT NULL, updated_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS sync_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
  `);
  return value;
}

async function scopeMatches(database: SQLite.SQLiteDatabase, scope: string): Promise<boolean> {
  const row = await database.getFirstAsync<ValueRow>("SELECT value FROM sync_meta WHERE key='scope'");
  return row?.value === scope;
}

function parsePayload<T>(row: PayloadRow | null): T | null {
  if (!row) return null;
  const value: unknown = JSON.parse(row.payload);
  if (value === null || typeof value !== "object" || Array.isArray(value)) {
    throw new Error("La réplique locale contient un enregistrement invalide.");
  }
  return value as T;
}

function timestamp(payload: Record<string, unknown>): string {
  for (const key of ["updated_at", "decided_at", "created_at"]) {
    if (typeof payload[key] === "string" && payload[key]) return payload[key];
  }
  return new Date().toISOString();
}

async function replaceRows(database: SQLite.SQLiteDatabase, table: ReplicaTable, rows: Record<string, unknown>[]) {
  await database.runAsync(`DELETE FROM ${table}`);
  for (const row of rows) {
    if (typeof row.id !== "string") continue;
    await database.runAsync(`INSERT OR REPLACE INTO ${table}(id,payload,updated_at) VALUES(?,?,?)`, row.id, JSON.stringify(row), timestamp(row));
  }
}

type ReplicaBootstrap = Pick<Bootstrap, "tasks" | "approvals" | "cursor"> &
  Partial<Pick<Bootstrap, "tool_calls" | "conversations" | "messages" | "agents" | "pinned_memory" | "counts">>;

export async function applyBootstrap(data: ReplicaBootstrap, scope: string) {
  const database = await db();
  await database.withTransactionAsync(async () => {
    await replaceRows(database, "tasks", data.tasks);
    await replaceRows(database, "approvals", data.approvals);
    await replaceRows(database, "tool_calls", data.tool_calls ?? []);
    await replaceRows(database, "conversations", data.conversations ?? []);
    await replaceRows(database, "messages", data.messages ?? []);
    await replaceRows(database, "agents", data.agents ?? []);
    await replaceRows(database, "pinned_memory", data.pinned_memory ?? []);
    await database.runAsync("INSERT OR REPLACE INTO sync_meta(key,value) VALUES('scope',?)", scope);
    await database.runAsync("INSERT OR REPLACE INTO sync_meta(key,value) VALUES('cursor',?)", data.cursor);
    if (data.counts) await database.runAsync("INSERT OR REPLACE INTO sync_meta(key,value) VALUES('counts',?)", JSON.stringify(data.counts));
  });
}

const EVENT_TABLE: Record<string, ReplicaTable | undefined> = {
  "task.updated": "tasks", "tool.proposed": "tool_calls", "tool.updated": "tool_calls",
  "tool.completed": "tool_calls", "tool.failed": "tool_calls", "tool.denied": "tool_calls",
  "approval.requested": "approvals", "approval.decided": "approvals",
  "message.created": "messages", "agent.updated": "agents", "memory.updated": "pinned_memory",
};

export async function upsertEvent(scope: string, type: string, payload: Record<string, unknown>) {
  const database = await db();
  if (!(await scopeMatches(database, scope))) return;
  const table = EVENT_TABLE[type];
  if (!table || typeof payload.id !== "string") return;
  if (type === "memory.updated" && payload.pinned === false) {
    await database.runAsync("DELETE FROM pinned_memory WHERE id=?", payload.id);
    return;
  }
  await database.runAsync(`INSERT OR REPLACE INTO ${table}(id,payload,updated_at) VALUES(?,?,?)`, payload.id, JSON.stringify(payload), timestamp(payload));
}

async function localRows<T>(scope: string, table: ReplicaTable): Promise<T[]> {
  const database = await db();
  if (!(await scopeMatches(database, scope))) return [];
  const rows = await database.getAllAsync<PayloadRow>(`SELECT payload FROM ${table} ORDER BY updated_at DESC`);
  return rows.map((row) => parsePayload<T>(row) as T);
}

export async function localApprovals(scope: string, status: Approval["status"] | "all" = "pending"): Promise<Approval[]> {
  const database = await db();
  if (!(await scopeMatches(database, scope))) return [];
  const rows = status === "all"
    ? await database.getAllAsync<PayloadRow>("SELECT payload FROM approvals ORDER BY updated_at DESC")
    : await database.getAllAsync<PayloadRow>("SELECT payload FROM approvals WHERE json_extract(payload, '$.status') = ? ORDER BY updated_at DESC", status);
  return rows.map((row) => parsePayload<Approval>(row) as Approval);
}

export async function localTasks(scope: string, status?: TaskStatus): Promise<Task[]> {
  const database = await db();
  if (!(await scopeMatches(database, scope))) return [];
  const rows = status
    ? await database.getAllAsync<PayloadRow>("SELECT payload FROM tasks WHERE json_extract(payload, '$.status') = ? ORDER BY updated_at DESC", status)
    : await database.getAllAsync<PayloadRow>("SELECT payload FROM tasks ORDER BY updated_at DESC");
  return rows.map((row) => parsePayload<Task>(row) as Task);
}

export async function localTask(scope: string, id: string): Promise<Task | null> {
  const database = await db();
  if (!(await scopeMatches(database, scope))) return null;
  return parsePayload<Task>(await database.getFirstAsync<PayloadRow>("SELECT payload FROM tasks WHERE id=?", id));
}

export const localToolCalls = (scope: string) => localRows<ToolCall>(scope, "tool_calls");
export const localAgents = (scope: string) => localRows<Agent>(scope, "agents");
export const localMemory = (scope: string) => localRows<MemoryItem>(scope, "pinned_memory");
export const localConversations = (scope: string) => localRows<Conversation>(scope, "conversations");
export const localMessages = (scope: string) => localRows<Message>(scope, "messages");

export async function localAuditMeta(scope: string): Promise<{ cursor: string | null; counts: Bootstrap["counts"] | null }> {
  const database = await db();
  if (!(await scopeMatches(database, scope))) return { cursor: null, counts: null };
  const cursor = await database.getFirstAsync<ValueRow>("SELECT value FROM sync_meta WHERE key='cursor'");
  const counts = await database.getFirstAsync<ValueRow>("SELECT value FROM sync_meta WHERE key='counts'");
  return { cursor: cursor?.value ?? null, counts: counts ? JSON.parse(counts.value) as Bootstrap["counts"] : null };
}

export function localDataAuthorizesSensitiveAction(): false { return false; }
