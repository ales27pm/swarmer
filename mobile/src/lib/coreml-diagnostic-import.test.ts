import { beforeEach, describe, expect, it, jest } from "@jest/globals";
import { requireOptionalNativeModule } from "expo";
import { importCoreMLDiagnosticCandidate, isCoreMLDiagnosticImportAvailable } from "./local-inference";

jest.mock("expo", () => ({ requireOptionalNativeModule: jest.fn(() => ({
  coreMLDiagnosticsAvailable: true, coreMLDiagnosticImportAvailable: true,
  importCoreMLDiagnosticCandidate: jest.fn(),
})) }));
const native = jest.mocked(requireOptionalNativeModule).mock.results[0].value as {
  coreMLDiagnosticsAvailable: boolean; coreMLDiagnosticImportAvailable: boolean;
  importCoreMLDiagnosticCandidate?: ReturnType<typeof jest.fn<() => Promise<unknown>>>;
};
const model = { modelId: "new-id", runtime: "coreml", displayName: "Candidate", source: "CoreMLDiagnosticCandidate",
  sizeBytes: 100, importedAt: "2026-10-02T00:00:00Z", purpose: "generation" };

describe("Core ML diagnostic import boundary", () => {
  beforeEach(() => {
    native.coreMLDiagnosticsAvailable = true; native.coreMLDiagnosticImportAvailable = true;
    native.importCoreMLDiagnosticCandidate = jest.fn<() => Promise<unknown>>().mockResolvedValue(model);
  });
  it("preserves the new model record from the native validated importer", async () => {
    expect(await importCoreMLDiagnosticCandidate()).toEqual(model);
    expect(native.importCoreMLDiagnosticCandidate).toHaveBeenCalledWith();
  });
  it.each(["coreMLDiagnosticsAvailable", "coreMLDiagnosticImportAvailable"] as const)("requires %s", async flag => {
    native[flag] = false;
    expect(isCoreMLDiagnosticImportAvailable()).toBe(false);
    await expect(importCoreMLDiagnosticCandidate()).rejects.toThrow("manifeste signé");
    expect(native.importCoreMLDiagnosticCandidate).not.toHaveBeenCalled();
  });
  it("does not expose import on older native builds", async () => {
    delete native.importCoreMLDiagnosticCandidate;
    expect(isCoreMLDiagnosticImportAvailable()).toBe(false);
    await expect(importCoreMLDiagnosticCandidate()).rejects.toThrow("manifeste signé");
  });
  it.each([{ ...model, runtime: "mlx" }, { ...model, purpose: "embeddings" }, { ...model, unexpectedPath: "/private" }])("rejects an invalid native result", async value => {
    native.importCoreMLDiagnosticCandidate!.mockResolvedValue(value);
    await expect(importCoreMLDiagnosticCandidate()).rejects.toThrow();
  });
});
