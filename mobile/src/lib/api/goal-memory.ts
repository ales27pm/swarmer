import type { GoalMemoryContext } from "@/lib/api/types";

export const MEMORY_FINGERPRINT = /^[0-9a-f]{64}$/;

function invalid(): never { throw new Error("Le contexte mémoire reçu est invalide ou ne correspond pas à ce but."); }
function record(value: unknown): Record<string, unknown> {
  if (!value || typeof value !== "object" || Array.isArray(value)) invalid();
  return value as Record<string, unknown>;
}
function text(value: unknown, maximum: number): string {
  if (typeof value !== "string" || !value.trim() || Array.from(value).length > maximum) invalid();
  for (const character of value) {
    const point = character.codePointAt(0)!;
    if (point >= 0xd800 && point <= 0xdfff) invalid();
  }
  return value;
}
function nullableText(value: unknown, maximum: number): string | null {
  return value === null ? null : text(value, maximum);
}
function count(value: unknown): number {
  if (typeof value !== "number" || !Number.isSafeInteger(value) || value < 0) invalid();
  return value;
}

/** Project-scoped, bounded receipt. Never forward bootstrap memory or arbitrary metadata. */
export function parseGoalMemoryContext(value: unknown, goalId: string): GoalMemoryContext {
  const raw = record(value);
  const embedding = record(raw.embedding);
  if (raw.schema_version !== "1.0" || raw.goal_id !== goalId
      || typeof raw.provider_fingerprint !== "string" || !MEMORY_FINGERPRINT.test(raw.provider_fingerprint)
      || typeof raw.context_fingerprint !== "string" || !MEMORY_FINGERPRINT.test(raw.context_fingerprint)
      || (raw.mode !== "semantic" && raw.mode !== "lexical" && raw.mode !== "hybrid")
      || typeof raw.local_planning_eligible !== "boolean"
      || !Array.isArray(raw.items) || raw.items.length > 4
      || !Array.isArray(raw.recent_conversation) || raw.recent_conversation.length > 40
      || typeof embedding.configured !== "boolean" || embedding.storage !== "ubuntu_sqlite") invalid();
  const items = raw.items.map((value) => {
    const item = record(value);
    if (typeof item.score !== "number" || !Number.isFinite(item.score) || item.score < -1 || item.score > 1) invalid();
    return { id: text(item.id, 500), source_id: text(item.source_id, 500), summary: text(item.summary, 1_200), score: item.score };
  });
  if (new Set(items.map((item) => item.id)).size !== items.length) invalid();
  return {
    schema_version: "1.0", goal_id: text(raw.goal_id, 128),
    project_id: nullableText(raw.project_id, 500), conversation_revision: count(raw.conversation_revision),
    base_revision_id: nullableText(raw.base_revision_id, 500),
    provider_fingerprint: raw.provider_fingerprint, context_fingerprint: raw.context_fingerprint,
    mode: raw.mode, reason: text(raw.reason, 100), items,
    embedding: { configured: embedding.configured, model: nullableText(embedding.model, 500),
      model_revision: nullableText(embedding.model_revision, 500), storage: "ubuntu_sqlite" },
    local_planning_eligible: raw.local_planning_eligible,
    planning_embedding_call_count: count(raw.planning_embedding_call_count),
    recent_conversation: raw.recent_conversation.map((value) => {
      const message = record(value);
      if (message.role !== "user" && message.role !== "assistant") invalid();
      return { role: message.role, content: text(message.content, 4_000) };
    }),
  };
}
