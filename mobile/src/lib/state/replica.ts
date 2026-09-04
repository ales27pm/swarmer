import * as SQLite from "expo-sqlite";

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

export async function applyBootstrap(data: { tasks: any[]; approvals: any[]; cursor: string }) {
  const database = await db();
  await database.withTransactionAsync(async () => {
    for (const task of data.tasks) {
      await database.runAsync("INSERT OR REPLACE INTO tasks(id,payload,updated_at) VALUES(?,?,?)", task.id, JSON.stringify(task), task.updated_at ?? task.created_at ?? new Date().toISOString());
    }
    for (const approval of data.approvals) {
      await database.runAsync("INSERT OR REPLACE INTO approvals(id,payload,updated_at) VALUES(?,?,?)", approval.id, JSON.stringify(approval), approval.decided_at ?? approval.created_at ?? new Date().toISOString());
    }
    await database.runAsync("INSERT OR REPLACE INTO sync_meta(key,value) VALUES('cursor',?)", data.cursor);
  });
}

export async function upsertEvent(type: string, payload: any) {
  const database = await db();
  if (type.startsWith("task.")) {
    await database.runAsync("INSERT OR REPLACE INTO tasks(id,payload,updated_at) VALUES(?,?,?)", payload.id, JSON.stringify(payload), payload.updated_at ?? new Date().toISOString());
  }
  if (type.startsWith("approval.")) {
    await database.runAsync("INSERT OR REPLACE INTO approvals(id,payload,updated_at) VALUES(?,?,?)", payload.id, JSON.stringify(payload), payload.decided_at ?? payload.created_at ?? new Date().toISOString());
  }
}

export async function localApprovals() {
  const database = await db();
  const rows = await database.getAllAsync<{ payload: string }>("SELECT payload FROM approvals ORDER BY updated_at DESC");
  return rows.map((row) => JSON.parse(row.payload));
}
