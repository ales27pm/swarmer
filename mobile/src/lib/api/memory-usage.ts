/** Passive receipts only. Presence in a request is not proof a model read or used it. */
export type MemoryUsageItem = {
  id: string; source_id: string | null;
  source_kind: "general_memory" | "message" | "project_plan" | "episode" | "unknown";
  scope: "general" | "project"; source_goal_id: string | null; source_revision_id: string | null;
  source_at: string | null; source_state: "available" | "changed" | "missing" | "redacted" | "unknown";
  verification: "user_asserted" | "assistant_claim" | "recorded_outcome" | "unknown"; summary: string | null;
};
export type MemoryUsageEntry = {
  id: string; evidence_stage: "retrieved" | "attached_to_model_call" | "included_in_worker_job";
  recorded_at: string; completed_at: string | null; status: string; purpose: string | null;
  conversation_revision: number | null; task_id: string | null; node_id: string | null;
  model_call_id: string | null; worker_job_id: string | null; context_id: string | null; model_id: string | null;
  retrieval: {mode: "lexical" | "semantic" | "hybrid" | "unknown"; reason: string | null; provider_fingerprint: string | null};
  items: MemoryUsageItem[]; omitted_item_count: number;
};
export type MemoryUsagePage = {
  schema_version: "1.0"; goal_id: string; project_id: string | null; current_conversation_revision: number;
  observed_at: string; availability: "available" | "no_records"; history_coverage: "recorded_receipts_only";
  entries: MemoryUsageEntry[]; next_cursor: string | null;
};
function invalid(): never { throw new Error("Les reçus mémoire sont invalides ou ne correspondent pas à ce projet."); }
function object(value: unknown, keys: string[]): Record<string, unknown> {
  if (!value || typeof value !== "object" || Array.isArray(value)) invalid();
  const result = value as Record<string, unknown>;
  if (Object.keys(result).length !== keys.length || keys.some(key => !Object.hasOwn(result, key))) invalid();
  return result;
}
function text(value: unknown, max: number): string {
  if (typeof value !== "string" || !value.length || value.length > max * 2) invalid();
  let size = 0;
  for (const char of value) { const point = char.codePointAt(0)!; if (!point || point >= 0xd800 && point <= 0xdfff || ++size > max) invalid(); }
  return value;
}
export function memoryUsageIdentifier(value: unknown): string {
  const id = text(value, 200); if (!/^[A-Za-z0-9][A-Za-z0-9._:-]*$/.test(id)) invalid(); return id;
}
export function memoryUsageCursor(value: unknown): string {
  const cursor = text(value, 512); if (!/^[A-Za-z0-9_-]+$/.test(cursor)) invalid(); return cursor;
}
function nullable<T>(value: unknown, parse: (raw: unknown) => T): T | null { return value === null ? null : parse(value); }
function timestamp(value: unknown): string {
  const result = text(value, 64); if (!/(?:Z|[+-]\d{2}:\d{2})$/.test(result) || !Number.isFinite(Date.parse(result))) invalid(); return result;
}
function count(value: unknown): number { if (typeof value !== "number" || !Number.isSafeInteger(value) || value < 0) invalid(); return value; }
function choice<T extends string>(value: unknown, choices: readonly T[]): T { if (typeof value !== "string" || !choices.includes(value as T)) invalid(); return value as T; }
function list<T>(value: unknown, limit: number, parse: (raw: unknown) => T): T[] {
  if (!Array.isArray(value) || value.length > limit) invalid(); return value.map(parse);
}
function parseItem(value: unknown): MemoryUsageItem {
  const item = object(value, ["id", "source_id", "source_kind", "scope", "source_goal_id", "source_revision_id", "source_at", "source_state", "verification", "summary"]);
  const state = choice(item.source_state, ["available", "changed", "missing", "redacted", "unknown"] as const);
  const summary = nullable(item.summary, raw => text(raw, 800));
  if ((state === "missing" || state === "redacted") && summary !== null) invalid();
  return { id: memoryUsageIdentifier(item.id), source_id: nullable(item.source_id, memoryUsageIdentifier),
    source_kind: choice(item.source_kind, ["general_memory", "message", "project_plan", "episode", "unknown"] as const),
    scope: choice(item.scope, ["general", "project"] as const), source_goal_id: nullable(item.source_goal_id, memoryUsageIdentifier),
    source_revision_id: nullable(item.source_revision_id, memoryUsageIdentifier), source_at: nullable(item.source_at, timestamp),
    source_state: state, verification: choice(item.verification, ["user_asserted", "assistant_claim", "recorded_outcome", "unknown"] as const), summary };
}
function parseEntry(value: unknown): MemoryUsageEntry {
  const entry = object(value, ["id", "evidence_stage", "recorded_at", "completed_at", "status", "purpose", "conversation_revision", "task_id", "node_id", "model_call_id", "worker_job_id", "context_id", "model_id", "retrieval", "items", "omitted_item_count"]);
  const retrieval = object(entry.retrieval, ["mode", "reason", "provider_fingerprint"]);
  const fingerprint = nullable(retrieval.provider_fingerprint, raw => text(raw, 64));
  if (fingerprint !== null && !/^[a-f0-9]{64}$/.test(fingerprint)) invalid();
  const items = list(entry.items, 100, parseItem);
  if (new Set(items.map(item => item.id)).size !== items.length) invalid();
  const parsed: MemoryUsageEntry = {
    id: memoryUsageIdentifier(entry.id), evidence_stage: choice(entry.evidence_stage, ["retrieved", "attached_to_model_call", "included_in_worker_job"] as const),
    recorded_at: timestamp(entry.recorded_at), completed_at: nullable(entry.completed_at, timestamp), status: text(entry.status, 50), purpose: nullable(entry.purpose, raw => text(raw, 100)),
    conversation_revision: nullable(entry.conversation_revision, count), task_id: nullable(entry.task_id, memoryUsageIdentifier), node_id: nullable(entry.node_id, memoryUsageIdentifier),
    model_call_id: nullable(entry.model_call_id, memoryUsageIdentifier), worker_job_id: nullable(entry.worker_job_id, memoryUsageIdentifier), context_id: nullable(entry.context_id, memoryUsageIdentifier), model_id: nullable(entry.model_id, raw => text(raw, 200)),
    retrieval: { mode: choice(retrieval.mode, ["lexical", "semantic", "hybrid", "unknown"] as const), reason: nullable(retrieval.reason, raw => text(raw, 100)), provider_fingerprint: fingerprint },
    items, omitted_item_count: count(entry.omitted_item_count),
  };
  if (parsed.evidence_stage === "attached_to_model_call" && (!parsed.context_id || !parsed.model_call_id || parsed.id !== `model_call:${parsed.model_call_id}` || parsed.worker_job_id !== null)
    || parsed.evidence_stage === "included_in_worker_job" && (!parsed.worker_job_id || !parsed.task_id || !parsed.node_id || parsed.id !== `worker_job:${parsed.worker_job_id}` || parsed.model_call_id !== null || parsed.context_id !== null)
    || parsed.evidence_stage === "retrieved" && (!parsed.id.startsWith("retrieval:") || parsed.model_call_id !== null || parsed.worker_job_id !== null || parsed.context_id !== null)) invalid();
  return parsed;
}
export function parseMemoryUsagePage(value: unknown, goalId: string): MemoryUsagePage {
  let bytes = 0;
  const encoded = JSON.stringify(value); if (!encoded) invalid();
  for (const char of encoded) { const point = char.codePointAt(0)!; bytes += point < 0x80 ? 1 : point < 0x800 ? 2 : point < 0x10000 ? 3 : 4; if (bytes > 128 * 1024) invalid(); }
  const page = object(value, ["schema_version", "goal_id", "project_id", "current_conversation_revision", "observed_at", "availability", "history_coverage", "entries", "next_cursor"]);
  if (page.schema_version !== "1.0" || page.history_coverage !== "recorded_receipts_only" || page.goal_id !== memoryUsageIdentifier(goalId)) invalid();
  const entries = list(page.entries, 50, parseEntry); const next = nullable(page.next_cursor, memoryUsageCursor);
  const availability = choice(page.availability, ["available", "no_records"] as const);
  if (new Set(entries.map(entry => entry.id)).size !== entries.length || (availability === "available") !== (entries.length > 0) || next !== null && !entries.length) invalid();
  return { schema_version: "1.0", goal_id: goalId, project_id: nullable(page.project_id, memoryUsageIdentifier),
    current_conversation_revision: count(page.current_conversation_revision), observed_at: timestamp(page.observed_at), availability,
    history_coverage: "recorded_receipts_only", entries, next_cursor: next };
}
