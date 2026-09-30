import type { MemoryUsageEntry, MemoryUsageItem, MemoryUsagePage } from "@/lib/api/memory-usage";
export function memoryUsageItem(patch: Partial<MemoryUsageItem> = {}): MemoryUsageItem {
  return { id: "mem_1", source_id: "mem_1", source_kind: "general_memory", scope: "general", source_goal_id: null,
    source_revision_id: null, source_at: "2026-09-28T20:00:00+00:00", source_state: "unknown", verification: "user_asserted",
    summary: "Préserver les horaires confirmés.", ...patch };
}
export function memoryUsageEntry(patch: Partial<MemoryUsageEntry> = {}): MemoryUsageEntry {
  return { id: "model_call:gmc_1", evidence_stage: "attached_to_model_call", recorded_at: "2026-09-28T21:00:00+00:00",
    completed_at: null, status: "failed", purpose: "planner", conversation_revision: 1, task_id: "tsk_1", node_id: null,
    model_call_id: "gmc_1", worker_job_id: null, context_id: "ctx_1", model_id: "local-7b",
    retrieval: { mode: "unknown", reason: null, provider_fingerprint: null }, items: [memoryUsageItem()], omitted_item_count: 0, ...patch };
}
export function memoryUsagePage(entries = [memoryUsageEntry()], patch: Partial<MemoryUsagePage> = {}): MemoryUsagePage {
  return { schema_version: "1.0", goal_id: "goal_1", project_id: "project_1", current_conversation_revision: 2,
    observed_at: "2026-09-28T22:00:00+00:00", availability: entries.length ? "available" : "no_records",
    history_coverage: "recorded_receipts_only", entries, next_cursor: null, ...patch };
}
