import type { MemoryItem } from "@/lib/api/types";
import { sha256 } from "@/lib/iphone-capabilities/grant";

/** A display translation must stay bound to the canonical record we received. */
export function memoryPresentation(item: MemoryItem): MemoryItem["presentation"] | null {
  if (!item || typeof item !== "object") throw new Error("La réponse mémoire est invalide.");
  if (item.presentation === undefined) return null;
  const value = item.presentation;
  if (!value || typeof value !== "object"
    || typeof item.content !== "string" || typeof item.updated_at !== "string"
    || !(item.summary === null || typeof item.summary === "string")
    || value.language !== "fr" || value.temporary !== true
    || value.validation_status !== "model_reviewed" || value.grants_authority !== false
    || value.source_revision !== item.updated_at
    || typeof value.content !== "string" || !value.content.trim() || value.content.length > 32000
    || !(value.summary === null || (typeof value.summary === "string" && value.summary.length <= 8000))
    || (value.summary === null) !== (item.summary === null)
    || value.canonical_sha256 !== sha256(item.content)
    || value.summary_sha256 !== (item.summary === null ? null : sha256(item.summary))) {
    throw new Error("La traduction de cette mémoire ne correspond pas à sa version enregistrée.");
  }
  return value;
}

export function validateMemoryPresentations(items: MemoryItem[]): MemoryItem[] {
  if (!Array.isArray(items)) throw new Error("La réponse mémoire est invalide.");
  for (const item of items) memoryPresentation(item);
  return items;
}
