import { describe, expect, it } from "@jest/globals";

import { memoryPresentation, validateMemoryPresentations } from "./memory-presentation";
import type { MemoryItem } from "./types";
import { sha256 } from "@/lib/iphone-capabilities/grant";

const source: MemoryItem = {
  id: "mem_one", scope: "project:crm", kind: "constraint",
  content: "Do not send automatically.", summary: "Explicit confirmation required.",
  sensitivity: "normal", confidence: 1, pinned: false, metadata: null,
  created_at: "2026-09-29T00:00:00Z", updated_at: "2026-09-29T00:00:00Z",
};
const translated: MemoryItem = {
  ...source,
  presentation: {
    language: "fr", content: "Ne pas envoyer automatiquement.", summary: "Confirmation explicite requise.",
    canonical_sha256: sha256(source.content), summary_sha256: sha256(source.summary!),
    source_revision: source.updated_at, validation_status: "model_reviewed",
    temporary: true, grants_authority: false,
  },
};

const originalText = "Conserver exactement 30 ms et le fichier `rapport.csv`.";
const originalEnglish = "Keep exactly 30 ms and the file `rapport.csv`.";
const journalHash = sha256(JSON.stringify({ content: originalText, summary: null }));
const preservedOriginal: MemoryItem = {
  ...source, content: originalEnglish, summary: null,
  metadata: { canonical_language: "en", source_id: "msrc_original", source_sha256: journalHash,
    canonical_receipt_id: "mreceipt_original", summary: null,
    content: { source_id: "msrc_original:content", source_language: "fr", source_sha256: sha256(originalText),
      canonical_sha256: sha256(originalEnglish), source_revalidated: true, grants_authority: false } },
  presentation: { language: "fr", mode: "original", validation_status: "source_preserved",
    content: originalText, summary: null, canonical_sha256: sha256(originalEnglish), summary_sha256: null,
    source_revision: source.updated_at, source_id: "msrc_original", source_sha256: journalHash,
    canonical_receipt_id: "mreceipt_original", temporary: true, grants_authority: false },
};

describe("memory display translations", () => {
  it("keeps English canonical data and accepts the version-bound French rendering", () => {
    expect(validateMemoryPresentations([translated])[0].content).toBe(source.content);
    expect(memoryPresentation(translated)?.content).toBe("Ne pas envoyer automatiquement.");
    expect(memoryPresentation(source)).toBeNull();
  });

  it.each([
    { canonical_sha256: "a".repeat(64) }, { summary_sha256: "a".repeat(64) },
    { source_revision: "older" }, { grants_authority: true }, { temporary: false },
    { language: "en" }, { validation_status: "unreviewed" }, { content: " " },
    { summary: null },
  ])("rejects a damaged or misleading presentation %j", (change) => {
    const item = { ...translated, presentation: { ...translated.presentation, ...change } } as MemoryItem;
    expect(() => memoryPresentation(item)).toThrow(/ne correspond pas/);
  });

  it("invalidates translations after canonical edits even when the timestamp was reused", () => {
    expect(() => memoryPresentation({ ...translated, content: "Send automatically." })).toThrow();
    expect(() => memoryPresentation({ ...translated, summary: "No confirmation required." })).toThrow();
  });

  it("does not silently discard an invalid translation or manufacture a summary", () => {
    expect(() => validateMemoryPresentations([{ ...source, presentation: null } as unknown as MemoryItem])).toThrow();
    const noSummary = { ...translated, summary: null, presentation: { ...translated.presentation!, summary: null, summary_sha256: null } };
    expect(memoryPresentation(noSummary)?.summary).toBeNull();
  });

  it("returns the exact French source bound to its journal and current receipt", () => {
    expect(memoryPresentation(preservedOriginal)?.content).toBe(originalText);
    expect(memoryPresentation(preservedOriginal)?.mode).toBe("original");
    expect(preservedOriginal.content).toBe(originalEnglish);
  });

  it.each([
    { source_id: "other" }, { canonical_receipt_id: "other" }, { source_sha256: "a".repeat(64) },
    { content: "Archiver exactement 30 ms et le fichier `rapport.csv`." },
    { mode: undefined }, { mode: "translated" }, { validation_status: "model_reviewed" },
  ])("rejects an unbound or relabeled original %j", (change) => {
    expect(() => memoryPresentation({ ...preservedOriginal,
      presentation: { ...preservedOriginal.presentation, ...change } } as MemoryItem)).toThrow();
  });

  it.each([
    { source_language: "en" }, { source_id: "another:content" }, { source_sha256: "a".repeat(64) },
    { canonical_sha256: "a".repeat(64) }, { source_revalidated: false }, { grants_authority: true },
  ])("checks each original field against the source receipt %j", (change) => {
    expect(() => memoryPresentation({ ...preservedOriginal, metadata: { ...preservedOriginal.metadata,
      content: { ...(preservedOriginal.metadata!.content as object), ...change } } })).toThrow();
  });

  it("rejects originals with a missing journal or a summary that was never present", () => {
    expect(() => memoryPresentation({ ...preservedOriginal, metadata: null })).toThrow();
    expect(() => memoryPresentation({ ...preservedOriginal,
      metadata: { ...preservedOriginal.metadata, summary: {} } })).toThrow();
    expect(() => memoryPresentation({ ...preservedOriginal, content: "Changed canonical record." })).toThrow();
  });
});
