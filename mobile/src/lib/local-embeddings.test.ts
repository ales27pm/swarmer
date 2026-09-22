import { beforeEach, expect, it, jest } from "@jest/globals";
const mockNative = {
  embeddingStatus: jest.fn<() => Promise<unknown>>(), loadEmbedder: jest.fn<() => Promise<unknown>>(),
  embed: jest.fn<() => Promise<unknown>>(), unloadEmbedder: jest.fn<() => Promise<void>>(),
};
jest.mock("expo", () => ({ requireOptionalNativeModule: () => mockNative }));
// Load after the native fixture is initialized; the wrapper captures its module at import time.
// eslint-disable-next-line @typescript-eslint/no-require-imports
const api = require("./local-embeddings") as typeof import("./local-embeddings");
const identity = { modelId: "intfloat/multilingual-e5-small" as const, revision: "a".repeat(40), dimensions: 384 as const, pipeline: "e5-prefixes-mean-l2-specialtokens-v1" as const };
const vector = [1, ...Array(383).fill(0)];
beforeEach(() => { jest.resetAllMocks(); });
it("requires an explicit experiment flag and immutable revision before any native load", async () => {
  await expect(api.loadLocalEmbedder({ modelId: identity.modelId, revision: identity.revision, experimental: false })).rejects.toThrow("explicitement");
  await expect(api.loadLocalEmbedder({ modelId: identity.modelId, revision: "main", experimental: true })).rejects.toThrow("immuable");
  expect(mockNative.loadEmbedder).not.toHaveBeenCalled();
});
it("requires the requested revision to be ready", async () => {
  mockNative.loadEmbedder.mockResolvedValue({ ...identity, state: "ready", revision: "b".repeat(40) });
  await expect(api.loadLocalEmbedder({ modelId: identity.modelId, revision: identity.revision, experimental: true })).rejects.toThrow("révision");
});
it("returns real vector receipts with revision, pipeline, and tokenizer counts", async () => {
  const receipt = { ...identity, kind: "query", vectors: [vector], tokenCounts: [8] };
  mockNative.embed.mockResolvedValue(receipt);
  await expect(api.embedLocalTexts({ texts: ["Mon prochain rendez-vous"], kind: "query" })).resolves.toEqual(receipt);
  expect(api.localEmbeddingIndexIdentity(identity)).not.toEqual(api.localEmbeddingIndexIdentity({ ...identity, revision: "b".repeat(40) }));
});
it.each([
  { vectors: [[1, 2]] }, { vectors: [Array(384).fill(0)] }, { vectors: [[NaN, ...Array(383).fill(0)]] },
  { tokenCounts: [513] }, { pipeline: "other" }, { kind: "document" },
])("rejects unverified or incompatible native vectors %j", async (bad) => {
  mockNative.embed.mockResolvedValue({ ...identity, kind: "query", vectors: [vector], tokenCounts: [8], ...bad });
  await expect(api.embedLocalTexts({ texts: ["hello"], kind: "query" })).rejects.toThrow("invalides");
});
it("rejects oversized batches before execution", async () => {
  await expect(api.embedLocalTexts({ texts: Array(9).fill("hello"), kind: "document" })).rejects.toThrow();
  expect(mockNative.embed).not.toHaveBeenCalled();
});
it("unloads through the distinct embedder path", async () => {
  mockNative.unloadEmbedder.mockResolvedValue();
  await api.unloadLocalEmbedder();
  expect(mockNative.unloadEmbedder).toHaveBeenCalledTimes(1);
});
