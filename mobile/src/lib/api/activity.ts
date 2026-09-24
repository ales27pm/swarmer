/** Public persisted activity metadata. Raw job payloads, logs and model output are never accepted. */
export type ActivityKind = "model_call" | "worker_job" | "tool_call" | "project_revision" | "project_check";
export type ActivityStatus = "queued" | "running" | "waiting" | "completed" | "failed" | "cancelled" | "skipped" | "recorded";
export type ActivityScopeType = "task" | "goal";
export type ActivityDetail = {
  revision_id: string | null;
  check_index: number | null;
  exit_code: number | null;
  file_count: number | null;
  command: string[] | null;
};
export type ActivityItem = {
  id: string;
  kind: ActivityKind;
  status: ActivityStatus;
  title: string;
  recorded_at: string;
  started_at: string | null;
  completed_at: string | null;
  duration_ms: number | null;
  goal_run_id: string | null;
  task_id: string | null;
  node_id: string | null;
  agent_id: string | null;
  model_id: string | null;
  role: "planner" | "evaluator" | "summarizer" | "synthesizer" | null;
  tool_name: string | null;
  detail: ActivityDetail;
};
export type ActivityPage = {
  schema_version: "1.0";
  scope: { type: ActivityScopeType; id: string; goal_run_id: string | null; root_task_id: string | null };
  items: ActivityItem[];
  next_cursor: string | null;
  has_more: boolean;
  coverage: { mode: "persisted_records"; live_operations: false; notice: string };
};

const KINDS: ActivityKind[] = ["model_call", "worker_job", "tool_call", "project_revision", "project_check"];
const STATUSES: ActivityStatus[] = ["queued", "running", "waiting", "completed", "failed", "cancelled", "skipped", "recorded"];
const ROLES: NonNullable<ActivityItem["role"]>[] = ["planner", "evaluator", "summarizer", "synthesizer"];
function invalid(): never { throw new Error("Les preuves d’activité reçues sont invalides. Actualisez pour réessayer."); }
function record(value: unknown, keys: string[]): Record<string, unknown> {
  if (!value || typeof value !== "object" || Array.isArray(value)) return invalid();
  const result = value as Record<string, unknown>;
  if (Object.keys(result).length !== keys.length || keys.some((key) => !Object.hasOwn(result, key))) return invalid();
  return result;
}
function string(value: unknown, min = 0, max = Number.MAX_SAFE_INTEGER): string {
  if (typeof value !== "string" || value.length > max * 2) return invalid();
  let count = 0;
  for (const char of value) {
    const code = char.codePointAt(0)!;
    if (!code || (code >= 0xd800 && code <= 0xdfff) || ++count > max) return invalid();
  }
  if (count < min) return invalid();
  return value;
}
export function activityIdentifier(value: unknown, max = 200): string {
  const result = string(value, 1, max);
  if (!/^[A-Za-z0-9][A-Za-z0-9._:-]*$/.test(result)) return invalid();
  return result;
}
export function activityCursor(value: unknown): string {
  const result = string(value, 1, 4096);
  if (!/^[A-Za-z0-9_-]+$/.test(result)) return invalid();
  return result;
}
function nullable<T>(value: unknown, parse: (item: unknown) => T): T | null {
  return value === null ? null : parse(value);
}
function timestamp(value: unknown): string {
  const result = string(value, 1, 64);
  if (!Number.isFinite(Date.parse(result))) return invalid();
  return result;
}
function integer(value: unknown, min: number, max = Number.MAX_SAFE_INTEGER): number {
  if (typeof value !== "number" || !Number.isSafeInteger(value) || value < min || value > max) return invalid();
  return value;
}
function choice<T extends string>(value: unknown, choices: T[]): T {
  if (typeof value !== "string" || !choices.includes(value as T)) return invalid();
  return value as T;
}
function parseDetail(value: unknown): ActivityDetail {
  const item = record(value, ["revision_id", "check_index", "exit_code", "file_count", "command"]);
  const command = nullable(item.command, (value) => {
    if (!Array.isArray(value) || value.length > 8) return invalid();
    return value.map((part) => string(part, 0, 256));
  });
  return {
    revision_id: nullable(item.revision_id, activityIdentifier),
    check_index: nullable(item.check_index, (value) => integer(value, 0, 11)),
    exit_code: nullable(item.exit_code, (value) => integer(value, -255, 255)),
    file_count: nullable(item.file_count, (value) => integer(value, 0, 80)),
    command,
  };
}
function parseItem(value: unknown): ActivityItem {
  const item = record(value, ["id", "kind", "status", "title", "recorded_at", "started_at", "completed_at", "duration_ms", "goal_run_id", "task_id", "node_id", "agent_id", "model_id", "role", "tool_name", "detail"]);
  return {
    id: activityIdentifier(item.id, 260), kind: choice(item.kind, KINDS), status: choice(item.status, STATUSES),
    title: string(item.title, 1, 160), recorded_at: timestamp(item.recorded_at),
    started_at: nullable(item.started_at, timestamp), completed_at: nullable(item.completed_at, timestamp),
    duration_ms: nullable(item.duration_ms, (value) => integer(value, 0)),
    goal_run_id: nullable(item.goal_run_id, activityIdentifier), task_id: nullable(item.task_id, activityIdentifier),
    node_id: nullable(item.node_id, activityIdentifier), agent_id: nullable(item.agent_id, activityIdentifier),
    model_id: nullable(item.model_id, (value) => string(value, 0, 200)),
    tool_name: nullable(item.tool_name, (value) => string(value, 0, 200)),
    role: nullable(item.role, (value) => choice(value, ROLES)), detail: parseDetail(item.detail),
  };
}

export function parseActivityPage(value: unknown, scopeType: ActivityScopeType, scopeId: string): ActivityPage {
  const page = record(value, ["schema_version", "scope", "items", "next_cursor", "has_more", "coverage"]);
  const scope = record(page.scope, ["type", "id", "goal_run_id", "root_task_id"]);
  const coverage = record(page.coverage, ["mode", "live_operations", "notice"]);
  if (page.schema_version !== "1.0" || scope.type !== scopeType || scope.id !== activityIdentifier(scopeId)
      || !["task", "goal"].includes(scopeType) || typeof page.has_more !== "boolean"
      || !Array.isArray(page.items) || page.items.length > 100
      || coverage.mode !== "persisted_records" || coverage.live_operations !== false) return invalid();
  const items = page.items.map(parseItem);
  if (new Set(items.map((item) => item.id)).size !== items.length) return invalid();
  const next = nullable(page.next_cursor, activityCursor);
  if (page.has_more !== (next !== null) || (page.has_more && items.length === 0)) return invalid();
  return {
    schema_version: "1.0",
    scope: { type: scopeType, id: scopeId, goal_run_id: nullable(scope.goal_run_id, activityIdentifier), root_task_id: nullable(scope.root_task_id, activityIdentifier) },
    items, next_cursor: next, has_more: page.has_more,
    coverage: { mode: "persisted_records", live_operations: false, notice: string(coverage.notice, 0, 2048) },
  };
}
