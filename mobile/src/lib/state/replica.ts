import * as SQLite from "expo-sqlite";

import type {
  Agent,
  Approval,
  Bootstrap,
  Conversation,
  GoalDetail,
  GoalRecord,
  GoalResult,
  MemoryItem,
  PlanNode,
  Task,
  TaskStatus,
  ToolCall,
} from "@/lib/api/types";

let dbPromise: Promise<SQLite.SQLiteDatabase> | null = null;
type ReplicaTable =
  | "tasks"
  | "approvals"
  | "tool_calls"
  | "conversations"
  | "messages"
  | "agents"
  | "pinned_memory"
  | "goals"
  | "plan_nodes"
  | "goal_results";
type PayloadRow = { payload: string };
type ValueRow = { value: string };
type ReplicaReader = Pick<SQLite.SQLiteDatabase, "getAllAsync" | "getFirstAsync">;

export type LocalSwarmSnapshot = {
  origin: string;
  goals: GoalRecord[];
  plan_nodes: PlanNode[];
  goal_results: GoalResult[];
  agents: Agent[];
  counts: Bootstrap["counts"] | null;
  cursor: string | null;
};

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
    CREATE TABLE IF NOT EXISTS goals (id TEXT PRIMARY KEY, payload TEXT NOT NULL, updated_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS plan_nodes (id TEXT PRIMARY KEY, payload TEXT NOT NULL, updated_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS goal_results (id TEXT PRIMARY KEY, payload TEXT NOT NULL, updated_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS sync_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
  `);
  return value;
}

async function scopeMatches(database: ReplicaReader, scope: string): Promise<boolean> {
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
  for (const key of ["updated_at", "decided_at", "created_at", "completed_at", "started_at"]) {
    if (typeof payload[key] === "string" && payload[key]) return payload[key];
  }
  return new Date().toISOString();
}

function rowIdentity(table: ReplicaTable, row: Record<string, unknown>): string | null {
  const value = table === "goal_results" ? row.goal_run_id : row.id;
  return typeof value === "string" && value ? value : null;
}

function rowsOrEmpty<T>(rows: T[] | undefined): T[] {
  return rows ?? [];
}

async function replaceRows(database: SQLite.SQLiteDatabase, table: ReplicaTable, rows: Record<string, unknown>[]) {
  await database.runAsync(`DELETE FROM ${table}`);
  for (const row of rows) {
    const id = rowIdentity(table, row);
    if (!id) continue;
    await database.runAsync(`INSERT OR REPLACE INTO ${table}(id,payload,updated_at) VALUES(?,?,?)`, id, JSON.stringify(row), timestamp(row));
  }
}

type ReplicaBootstrap = Pick<Bootstrap, "tasks" | "approvals" | "cursor"> &
  Partial<Pick<Bootstrap, "tool_calls" | "conversations" | "messages" | "agents" | "pinned_memory" | "goals" | "plan_nodes" | "goal_results" | "counts">>;

export async function applyBootstrap(data: ReplicaBootstrap, scope: string) {
  const database = await db();
  await database.withTransactionAsync(async () => {
    await replaceRows(database, "tasks", data.tasks);
    await replaceRows(database, "approvals", data.approvals);
    await replaceRows(database, "tool_calls", rowsOrEmpty(data.tool_calls));
    await replaceRows(database, "conversations", rowsOrEmpty(data.conversations));
    await replaceRows(database, "messages", rowsOrEmpty(data.messages));
    await replaceRows(database, "agents", rowsOrEmpty(data.agents));
    await replaceRows(database, "pinned_memory", rowsOrEmpty(data.pinned_memory));
    await replaceRows(database, "goals", rowsOrEmpty(data.goals));
    await replaceRows(database, "plan_nodes", rowsOrEmpty(data.plan_nodes));
    await replaceRows(database, "goal_results", rowsOrEmpty(data.goal_results));
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
  "goal.updated": "goals", "plan.node.updated": "plan_nodes",
  "goal.result.updated": "goal_results",
};

export async function upsertEvent(scope: string, type: string, payload: Record<string, unknown>) {
  const database = await db();
  if (!(await scopeMatches(database, scope))) return;
  // Content-bearing task/message/approval rows are refreshed from authenticated
  // REST. Never replace a complete cached record with a metadata-only WebSocket
  // invalidation projection.
  if (
    payload.refetch_required === true
    && [
      "task.updated",
      "message.created",
      "approval.requested",
      "approval.decided",
      "goal.updated",
      "plan.node.updated",
      "goal.result.updated",
    ].includes(type)
  ) return;
  const table = EVENT_TABLE[type];
  if (!table) return;
  const id = rowIdentity(table, payload);
  if (!id) return;
  if (type === "memory.updated" && payload.pinned === false) {
    await database.runAsync("DELETE FROM pinned_memory WHERE id=?", id);
    return;
  }
  await database.runAsync(`INSERT OR REPLACE INTO ${table}(id,payload,updated_at) VALUES(?,?,?)`, id, JSON.stringify(payload), timestamp(payload));
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
export const localGoals = (scope: string) => localRows<GoalRecord>(scope, "goals");

export async function localGoalDetail(
  scope: string,
  goalRunId: string,
): Promise<GoalDetail | null> {
  const database = await db();
  let detail: GoalDetail | null = null;
  await database.withExclusiveTransactionAsync(async (transaction) => {
    if (!(await scopeMatches(transaction, scope))) return;
    const goal = parsePayload<GoalRecord>(
      await transaction.getFirstAsync<PayloadRow>(
        "SELECT payload FROM goals WHERE id=?",
        goalRunId,
      ),
    );
    if (!goal) return;
    const nodeRows = await transaction.getAllAsync<PayloadRow>(
      "SELECT payload FROM plan_nodes WHERE json_extract(payload, '$.goal_run_id') = ? ORDER BY updated_at DESC",
      goalRunId,
    );
    const result = parsePayload<GoalResult>(
      await transaction.getFirstAsync<PayloadRow>(
        "SELECT payload FROM goal_results WHERE id=?",
        goalRunId,
      ),
    );
    if (!(await scopeMatches(transaction, scope))) return;
    detail = {
      goal,
      nodes: nodeRows.map((row) => parsePayload<PlanNode>(row) as PlanNode),
      result,
    };
  });
  return detail;
}

export async function localSwarmSnapshot(scope: string): Promise<LocalSwarmSnapshot | null> {
  const database = await db();
  let snapshot: LocalSwarmSnapshot | null = null;
  await database.withExclusiveTransactionAsync(async (transaction) => {
    if (!(await scopeMatches(transaction, scope))) return;
    const goalRows = await transaction.getAllAsync<PayloadRow>(
      "SELECT payload FROM goals ORDER BY updated_at DESC",
    );
    const nodeRows = await transaction.getAllAsync<PayloadRow>(
      "SELECT payload FROM plan_nodes ORDER BY updated_at DESC",
    );
    const resultRows = await transaction.getAllAsync<PayloadRow>(
      "SELECT payload FROM goal_results ORDER BY updated_at DESC",
    );
    const agentRows = await transaction.getAllAsync<PayloadRow>(
      "SELECT payload FROM agents ORDER BY updated_at DESC",
    );
    const cursor = await transaction.getFirstAsync<ValueRow>(
      "SELECT value FROM sync_meta WHERE key='cursor'",
    );
    const counts = await transaction.getFirstAsync<ValueRow>(
      "SELECT value FROM sync_meta WHERE key='counts'",
    );
    if (!(await scopeMatches(transaction, scope))) return;
    snapshot = {
      origin: scope,
      goals: goalRows.map((row) => parsePayload<GoalRecord>(row) as GoalRecord),
      plan_nodes: nodeRows.map((row) => parsePayload<PlanNode>(row) as PlanNode),
      goal_results: resultRows.map((row) => parsePayload<GoalResult>(row) as GoalResult),
      agents: agentRows.map((row) => parsePayload<Agent>(row) as Agent),
      counts: counts ? JSON.parse(counts.value) as Bootstrap["counts"] : null,
      cursor: cursor?.value ?? null,
    };
  });
  return snapshot;
}

export async function localAuditMeta(scope: string): Promise<{ cursor: string | null; counts: Bootstrap["counts"] | null }> {
  const database = await db();
  if (!(await scopeMatches(database, scope))) return { cursor: null, counts: null };
  const cursor = await database.getFirstAsync<ValueRow>("SELECT value FROM sync_meta WHERE key='cursor'");
  const counts = await database.getFirstAsync<ValueRow>("SELECT value FROM sync_meta WHERE key='counts'");
  return { cursor: cursor?.value ?? null, counts: counts ? JSON.parse(counts.value) as Bootstrap["counts"] : null };
}

export function localDataAuthorizesSensitiveAction(): false { return false; }
