import * as SQLite from "expo-sqlite";

import type { Approval, Bootstrap, Task, TaskStatus } from "@/lib/api/types";

let dbPromise: Promise<SQLite.SQLiteDatabase> | null = null;

async function db() {
  if (!dbPromise) dbPromise = SQLite.openDatabaseAsync("mongars-replica.db");
  const value = await dbPromise;
  await value.execAsync(`
    PRAGMA journal_mode = WAL;
    CREATE TABLE IF NOT EXISTS tasks (id TEXT PRIMARY KEY, payload TEXT NOT NULL, updated_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS approvals (id TEXT PRIMARY KEY, payload TEXT NOT NULL, updated_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS sync_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
  `);
  return value;
}

type PayloadRow = { payload: string };
type ScopeRow = { value: string };

async function scopeMatches(
  database: SQLite.SQLiteDatabase,
  scope: string,
): Promise<boolean> {
  const row = await database.getFirstAsync<ScopeRow>(
    "SELECT value FROM sync_meta WHERE key='scope'",
  );
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

function payloadTimestamp(payload: Record<string, unknown>, keys: string[]): string {
  for (const key of keys) {
    const value = payload[key];
    if (typeof value === "string" && value) return value;
  }
  return new Date().toISOString();
}

export async function applyBootstrap(
  data: Pick<Bootstrap, "tasks" | "approvals" | "cursor">,
  scope: string,
) {
  const database = await db();
  await database.withTransactionAsync(async () => {
    await database.runAsync("DELETE FROM tasks");
    await database.runAsync("DELETE FROM approvals");
    await database.runAsync(
      "INSERT OR REPLACE INTO sync_meta(key,value) VALUES('scope',?)",
      scope,
    );
    for (const task of data.tasks) {
      await database.runAsync("INSERT OR REPLACE INTO tasks(id,payload,updated_at) VALUES(?,?,?)", task.id, JSON.stringify(task), task.updated_at ?? task.created_at ?? new Date().toISOString());
    }
    for (const approval of data.approvals) {
      await database.runAsync("INSERT OR REPLACE INTO approvals(id,payload,updated_at) VALUES(?,?,?)", approval.id, JSON.stringify(approval), approval.decided_at ?? approval.created_at ?? new Date().toISOString());
    }
    await database.runAsync("INSERT OR REPLACE INTO sync_meta(key,value) VALUES('cursor',?)", data.cursor);
  });
}

export async function upsertEvent(
  scope: string,
  type: string,
  payload: Record<string, unknown>,
) {
  const database = await db();
  if (!(await scopeMatches(database, scope))) return;
  if (type === "task.updated" && typeof payload.id === "string") {
    await database.runAsync(
      "INSERT OR REPLACE INTO tasks(id,payload,updated_at) VALUES(?,?,?)",
      payload.id,
      JSON.stringify(payload),
      payloadTimestamp(payload, ["updated_at", "created_at"]),
    );
  }
  if (
    ["approval.requested", "approval.decided"].includes(type) &&
    typeof payload.id === "string"
  ) {
    await database.runAsync(
      "INSERT OR REPLACE INTO approvals(id,payload,updated_at) VALUES(?,?,?)",
      payload.id,
      JSON.stringify(payload),
      payloadTimestamp(payload, ["decided_at", "created_at"]),
    );
  }
}

export async function localApprovals(
  scope: string,
  status: Approval["status"] | "all" = "pending",
): Promise<Approval[]> {
  const database = await db();
  if (!(await scopeMatches(database, scope))) return [];
  const rows = status === "all"
    ? await database.getAllAsync<PayloadRow>(
      "SELECT payload FROM approvals ORDER BY updated_at DESC",
    )
    : await database.getAllAsync<PayloadRow>(
      "SELECT payload FROM approvals WHERE json_extract(payload, '$.status') = ? ORDER BY updated_at DESC",
      status,
    );
  return rows.map((row) => parsePayload<Approval>(row) as Approval);
}

export async function localTasks(scope: string, status?: TaskStatus): Promise<Task[]> {
  const database = await db();
  if (!(await scopeMatches(database, scope))) return [];
  const rows = status
    ? await database.getAllAsync<PayloadRow>(
      "SELECT payload FROM tasks WHERE json_extract(payload, '$.status') = ? ORDER BY updated_at DESC",
      status,
    )
    : await database.getAllAsync<PayloadRow>(
      "SELECT payload FROM tasks ORDER BY updated_at DESC",
    );
  return rows.map((row) => parsePayload<Task>(row) as Task);
}

export async function localTask(scope: string, id: string): Promise<Task | null> {
  const database = await db();
  if (!(await scopeMatches(database, scope))) return null;
  const row = await database.getFirstAsync<PayloadRow>(
    "SELECT payload FROM tasks WHERE id=?",
    id,
  );
  return parsePayload<Task>(row);
}
