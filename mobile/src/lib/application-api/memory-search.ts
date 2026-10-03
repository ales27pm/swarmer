import { memorySymbolicEvidence } from "@/lib/api/memory-symbolic";
import type { MemoryItem } from "@/lib/api/types";
import { ApplicationApiError, serializableResult } from "./schema";

/** Redaction may hide credentials elsewhere, but must never change a claim's meaning. */
export function memorySearchResult(value: unknown): unknown {
  if (!Array.isArray(value)) throw new ApplicationApiError("invalid_response", "Le résultat mémoire est invalide.");
  for (const item of value as MemoryItem[]) {
    const evidence = memorySymbolicEvidence(item);
    if (JSON.stringify(serializableResult(evidence)) !== JSON.stringify(evidence)) {
      throw new ApplicationApiError("invalid_response", "Une proposition mémoire ne peut pas être transmise intégralement.");
    }
  }
  return value;
}
