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
});
