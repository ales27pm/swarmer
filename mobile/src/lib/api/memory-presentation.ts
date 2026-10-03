import type { MemoryItem } from "@/lib/api/types";
import { sha256 } from "@/lib/iphone-capabilities/grant";

function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

/** A source view is a copy of the current journal, never a fresh model assertion. */
function originalIsBound(item: MemoryItem): boolean {
  const view = item.presentation;
  const metadata = item.metadata;
  if (!view || view.mode !== "original" || view.validation_status !== "source_preserved"
    || !isRecord(metadata) || metadata.canonical_language !== "en"
    || typeof view.source_id !== "string" || !view.source_id
    || typeof view.canonical_receipt_id !== "string" || !view.canonical_receipt_id
    || view.source_id !== metadata.source_id
    || view.canonical_receipt_id !== metadata.canonical_receipt_id
    || view.source_sha256 !== metadata.source_sha256
    || view.source_sha256 !== sha256(JSON.stringify({ content: view.content, summary: view.summary }))) return false;
  for (const field of ["content", "summary"] as const) {
    const text = view[field];
    const unit = metadata[field];
    if (text === null) {
      if (unit !== null) return false;
    } else if (!isRecord(unit) || unit.source_language !== "fr"
      || unit.source_id !== `${view.source_id}:${field}`
      || unit.source_sha256 !== sha256(text)
      || unit.canonical_sha256 !== sha256(item[field]!)
      || unit.source_revalidated !== true || unit.grants_authority !== false) return false;
  }
  return true;
}

/** Both original and translated views must match the canonical revision received. */
export function memoryPresentation(item: MemoryItem): MemoryItem["presentation"] | null {
  if (!item || typeof item !== "object") throw new Error("La réponse mémoire est invalide.");
  if (item.presentation === undefined) return null;
  const value = item.presentation;
  if (!value || typeof value !== "object"
    || typeof item.content !== "string" || typeof item.updated_at !== "string"
    || !(item.summary === null || typeof item.summary === "string")
    || value.language !== "fr" || value.temporary !== true
    || value.grants_authority !== false
    || value.source_revision !== item.updated_at
    || typeof value.content !== "string" || !value.content.trim() || value.content.length > 32000
    || !(value.summary === null || (typeof value.summary === "string" && value.summary.length <= 8000))
    || (value.summary === null) !== (item.summary === null)
    || value.canonical_sha256 !== sha256(item.content)
    || value.summary_sha256 !== (item.summary === null ? null : sha256(item.summary))) {
    throw new Error("La traduction de cette mémoire ne correspond pas à sa version enregistrée.");
  }
  if (!(value.mode === undefined && value.validation_status === "model_reviewed")
    && !originalIsBound(item)) {
    throw new Error("La présentation de cette mémoire ne correspond pas à sa source enregistrée.");
  }
  return value;
}

export function validateMemoryPresentations(items: MemoryItem[]): MemoryItem[] {
  if (!Array.isArray(items)) throw new Error("La réponse mémoire est invalide.");
  for (const item of items) memoryPresentation(item);
  return items;
}
