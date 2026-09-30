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
});
