import { describe, expect, it, jest } from "@jest/globals";
import { requireOptionalNativeModule } from "expo";
import { listLocalModels } from "./local-inference";

jest.mock("expo", () => ({ requireOptionalNativeModule: jest.fn(() => ({ listModels: jest.fn() })) }));
const native = jest.mocked(requireOptionalNativeModule).mock.results[0].value as {
  listModels: ReturnType<typeof jest.fn<() => Promise<unknown>>>;
};
const model = { modelId: "local-model", runtime: "mlx", displayName: "Local model", source: "Imported model",
  sizeBytes: 100, importedAt: "2026-09-22T00:00:00Z" };

describe("native model purpose", () => {
  it.each(["generation", "embeddings"] as const)("preserves explicit %s purpose", async (purpose) => {
    native.listModels.mockResolvedValue([{ ...model, purpose }]);
    expect(await listLocalModels()).toEqual([{ ...model, purpose }]);
  });

  it("accepts legacy native records without misclassifying existing generators", async () => {
    native.listModels.mockResolvedValue([model]);
    expect(await listLocalModels()).toEqual([{ ...model, purpose: "generation" }]);
  });

  it.each([null, "unknown", 42, undefined])("rejects invalid explicit purpose %s", async (purpose) => {
    native.listModels.mockResolvedValue([{ ...model, purpose }]);
    await expect(listLocalModels()).rejects.toThrow("modèle natif");
  });

  it("still rejects unrelated unexpected metadata", async () => {
    native.listModels.mockResolvedValue([{ ...model, purpose: "embeddings", loaded: true }]);
    await expect(listLocalModels()).rejects.toThrow("modèle natif");
  });

  it.each(["dolphin-split9-experimental", "dolphin-monolith-experimental"] as const)(
    "preserves the native Core ML classification %s", async (generationProfile) => {
      const candidate = { ...model, runtime: "coreml", generationProfile };
      native.listModels.mockResolvedValue([candidate]);
      expect(await listLocalModels()).toEqual([{ ...candidate, purpose: "generation" }]);
    },
  );

  it("accepts a nullable native profile without classifying a generic model as Dolphin", async () => {
    native.listModels.mockResolvedValue([{ ...model, generationProfile: null }]);
    expect(await listLocalModels()).toEqual([{ ...model, purpose: "generation" }]);
  });

  it.each(["unknown", 42, undefined])("rejects an invalid explicit generation profile %s", async (generationProfile) => {
    native.listModels.mockResolvedValue([{ ...model, runtime: "coreml", generationProfile }]);
    await expect(listLocalModels()).rejects.toThrow("modèle natif");
  });

  it.each(["mlx", "llama.cpp"])("rejects a Dolphin Core ML profile on %s", async (runtime) => {
    native.listModels.mockResolvedValue([{ ...model, runtime, generationProfile: "dolphin-split9-experimental" }]);
    await expect(listLocalModels()).rejects.toThrow("modèle natif");
  });

  it("rejects a Dolphin generation profile on an embedding model", async () => {
    native.listModels.mockResolvedValue([{ ...model, runtime: "coreml", purpose: "embeddings", generationProfile: "dolphin-split9-experimental" }]);
    await expect(listLocalModels()).rejects.toThrow("modèle natif");
  });
});
