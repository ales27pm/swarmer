export type DurableProjectContext = {
  project_id: string; goal_id: string; version: number; fingerprint: string;
  conversation_revision: number; base_revision_id: string | null;
  requirements: { text: string; source_id: string; source_ids: string[] }[];
};

export function parseProjectContext(value: unknown, goalId: string): DurableProjectContext | null {
  if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error("Contexte projet invalide.");
  const row = value as Record<string, unknown>;
  if (row.enabled === false) return null;
  if (row.goal_id !== goalId || typeof row.project_id !== "string"
      || !Number.isSafeInteger(row.version) || Number(row.version) < 1
      || !Number.isSafeInteger(row.conversation_revision)
      || typeof row.fingerprint !== "string" || !/^[a-f0-9]{64}$/.test(row.fingerprint)
      || (row.base_revision_id !== null && typeof row.base_revision_id !== "string")
      || !Array.isArray(row.requirements) || row.requirements.length > 10_000) throw new Error("Contexte projet invalide.");
  for (const item of row.requirements) {
    if (!item || typeof item !== "object" || typeof item.text !== "string" || item.text.length > 100_000
        || typeof item.source_id !== "string" || !Array.isArray(item.source_ids)
        || !item.source_ids.includes(item.source_id) || item.source_ids.some((id: unknown) => typeof id !== "string")) {
      throw new Error("Source du contexte projet invalide.");
    }
  }
  return row as DurableProjectContext;
}
