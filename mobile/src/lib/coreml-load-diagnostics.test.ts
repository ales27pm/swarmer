import { afterEach, beforeEach, describe, expect, it, jest } from "@jest/globals";
import { requireOptionalNativeModule } from "expo";
import { getLocalInferenceStatus, loadLocalModel } from "./local-inference";
import { COREML_COMPUTE_UNITS, parseCoreMLLoadDiagnostic } from "./coreml-load-diagnostics";

jest.mock("expo", () => ({ requireOptionalNativeModule: jest.fn(() => ({
  coreMLDiagnosticsAvailable: true, status: jest.fn(), loadModel: jest.fn(),
})) }));
const native = jest.mocked(requireOptionalNativeModule).mock.results[0].value as {
  coreMLDiagnosticsAvailable?: boolean;
  status: ReturnType<typeof jest.fn<() => Promise<unknown>>>;
  loadModel: ReturnType<typeof jest.fn<(input: unknown) => Promise<unknown>>>;
};
const ready = { state: "ready", runtime: "coreml", modelId: "imported-model", revision: null };
const diagnostic = { computeUnits: "cpuAndNeuralEngine", stage: "load", outcome: "failed", elapsedMilliseconds: 1234,
  errors: [{ domain: "com.apple.CoreML", code: 0, executionPlanCode: -14 }], errorsTruncated: false };
const developmentGlobal = globalThis as typeof globalThis & { __DEV__: boolean };
const dev = __DEV__;

describe("development Core ML load diagnostics", () => {
  beforeEach(() => {
    jest.clearAllMocks(); developmentGlobal.__DEV__ = true; native.coreMLDiagnosticsAvailable = true;
    native.status.mockResolvedValue(ready); native.loadModel.mockResolvedValue(ready);
  });
  afterEach(() => { developmentGlobal.__DEV__ = dev; });

  it.each(COREML_COMPUTE_UNITS)("passes exactly the explicit %s option once", async (coreMLComputeUnits) => {
    const input = { runtime: "coreml" as const, modelId: "imported-model", coreMLComputeUnits };
    await loadLocalModel(input);
    expect(native.loadModel).toHaveBeenCalledTimes(1);
    expect(native.loadModel).toHaveBeenCalledWith(input);
  });
  it("preserves the default load input in release and older native builds", async () => {
    developmentGlobal.__DEV__ = false; delete native.coreMLDiagnosticsAvailable;
    const input = { runtime: "coreml" as const, modelId: "imported-model" };
    expect(await loadLocalModel(input)).toEqual(ready);
    expect(native.loadModel).toHaveBeenCalledWith(input);
  });
  it("uses native Debug capability with the embedded production JavaScript bundle", async () => {
    developmentGlobal.__DEV__ = false;
    native.coreMLDiagnosticsAvailable = true;
    const input = { runtime: "coreml" as const, modelId: "imported-model", coreMLComputeUnits: "cpuAndNeuralEngine" as const };
    native.loadModel.mockResolvedValue({ ...ready, coreMLLoadDiagnostic: { ...diagnostic,
      stage: "complete", outcome: "succeeded", errors: [] } });
    expect((await loadLocalModel(input)).coreMLLoadDiagnostic).toMatchObject({ outcome: "succeeded" });
    expect(native.loadModel).toHaveBeenCalledWith(input);
    native.status.mockResolvedValue({ ...ready, state: "failed", coreMLLoadDiagnostic: diagnostic });
    expect((await getLocalInferenceStatus()).coreMLLoadDiagnostic).toEqual(diagnostic);
  });
  it.each(["release", "old_native", "other_engine", "unknown_unit"])("rejects %s before any model replacement", async (reason) => {
    if (reason === "release") { developmentGlobal.__DEV__ = false; native.coreMLDiagnosticsAvailable = false; }
    if (reason === "old_native") delete native.coreMLDiagnosticsAvailable;
    const input = { runtime: reason === "other_engine" ? "mlx" : "coreml", modelId: "imported-model",
      coreMLComputeUnits: reason === "unknown_unit" ? "aneOnly" : "cpuAndNeuralEngine" };
    await expect(loadLocalModel(input as Parameters<typeof loadLocalModel>[0])).rejects.toThrow();
    expect(native.loadModel).not.toHaveBeenCalled();
    expect(await getLocalInferenceStatus()).toEqual(ready);
  });
  it("retains safe terminal diagnostics through the read-only status path", async () => {
    native.status.mockResolvedValue({ ...ready, state: "failed", coreMLLoadDiagnostic: diagnostic });
    expect((await getLocalInferenceStatus()).coreMLLoadDiagnostic).toEqual(diagnostic);
    expect(native.loadModel).not.toHaveBeenCalled();
  });
  it("omits diagnostics in release and accepts legacy status without them", async () => {
    developmentGlobal.__DEV__ = false;
    native.coreMLDiagnosticsAvailable = false;
    native.status.mockResolvedValue({ ...ready, coreMLLoadDiagnostic: diagnostic });
    expect(await getLocalInferenceStatus()).toEqual(ready);
  });
  it.each([
    { privatePath: "/private/model" }, { computeUnits: "aneOnly" }, { stage: "private-path" },
    { elapsedMilliseconds: Infinity }, { elapsedMilliseconds: -1 }, { errorsTruncated: "false" },
    { errors: Array(5).fill(diagnostic.errors[0]) }, { errors: [] },
    { errors: [{ domain: "/private/token", code: -14 }] },
    { errors: [{ domain: "other", code: -14, description: "private" }] },
    { errors: [{ domain: "other", code: 1.5 }] },
    { errors: [{ domain: "other", code: -14, executionPlanCode: "-14" }] },
    { outcome: "succeeded" },
    { outcome: ["failed"] },
  ])("rejects unbounded, incoherent or private native fields: %p", (change) => {
    expect(() => parseCoreMLLoadDiagnostic({ ...diagnostic, ...change })).toThrow();
  });
  it("normalizes native null error subcodes without losing unknown-domain numeric codes", () => {
    expect(parseCoreMLLoadDiagnostic({ ...diagnostic, errors: [{ domain: "other", code: -14, executionPlanCode: null }] }).errors)
      .toEqual([{ domain: "other", code: -14 }]);
  });
});
