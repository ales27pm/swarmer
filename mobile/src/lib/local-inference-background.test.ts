import { beforeEach, describe, expect, it, jest } from "@jest/globals";
import { requireOptionalNativeModule } from "expo";
import { getLocalInferenceStatus, type BackgroundExecutionStatus } from "./local-inference";

jest.mock("expo", () => ({ requireOptionalNativeModule: jest.fn(() => ({ status: jest.fn(), getBackgroundExecutionStatus: jest.fn() })) }));
const native = jest.mocked(requireOptionalNativeModule).mock.results[0].value as {
  status: ReturnType<typeof jest.fn<() => Promise<unknown>>>;
  getBackgroundExecutionStatus?: ReturnType<typeof jest.fn<() => Promise<unknown>>>;
};
const ready = { state: "ready", runtime: "mlx", modelId: "test/dolphin", revision: "a".repeat(40) };
const unverified: BackgroundExecutionStatus = {
  supported: true, reason: "permission_unverified", osSupported: true, gpuSupported: true,
  entitlementGranted: null, active: false, operationId: null, outputBytes: 0, state: "idle",
  executionDevice: null,
};

describe("native background execution status", () => {
  beforeEach(() => {
    native.status.mockResolvedValue(ready);
    native.getBackgroundExecutionStatus = jest.fn<() => Promise<unknown>>().mockResolvedValue(unverified);
  });

  it("preserves model status and distinguishes GPU support from an unverified permission", async () => {
    expect(await getLocalInferenceStatus()).toEqual({ ...ready, backgroundExecution: unverified });
  });

  it("reports an admitted operation with real output bytes without changing model state", async () => {
    const active = { ...unverified, state: "active", reason: null, active: true, executionDevice: "gpu", entitlementGranted: true, operationId: "operation-1", outputBytes: 54 };
    native.getBackgroundExecutionStatus!.mockResolvedValue(active);
    expect(await getLocalInferenceStatus()).toEqual({ ...ready, backgroundExecution: active });
  });

  it("accepts an admitted CPU fallback without GPU support or a GPU entitlement", async () => {
    const active = { ...unverified, gpuSupported: false, executionDevice: "cpu", reason: "cpu_fallback",
      active: true, operationId: "cpu-1", state: "active", outputBytes: 28 };
    native.getBackgroundExecutionStatus!.mockResolvedValue(active);
    expect(await getLocalInferenceStatus()).toEqual({ ...ready, backgroundExecution: active });
  });

  it("does not turn CPU availability into an admitted task", async () => {
    const available = { ...unverified, gpuSupported: false, reason: "cpu_fallback" };
    native.getBackgroundExecutionStatus!.mockResolvedValue(available);
    expect((await getLocalInferenceStatus()).backgroundExecution).toEqual(available);
  });

  it.each([false, true])("keeps historical GPU permission separate from CPU admission (%s)", async (entitlementGranted) => {
    const active = { ...unverified, gpuSupported: false, executionDevice: "cpu", reason: "cpu_fallback",
      entitlementGranted, active: true, operationId: "cpu-1", state: "active" };
    native.getBackgroundExecutionStatus!.mockResolvedValue(active);
    expect((await getLocalInferenceStatus()).backgroundExecution).toEqual(active);
  });

  it.each([false, true])("normalizes an older GPU-only module without inventing CPU support (active=%s)", async (active) => {
    const { executionDevice: _executionDevice, ...legacy } = unverified;
    const value = { ...legacy, active, supported: active, gpuSupported: active,
      entitlementGranted: active ? true : null, state: active ? "active" : "idle",
      reason: active ? null : "gpu_unsupported", operationId: active ? "old-gpu" : null };
    native.getBackgroundExecutionStatus!.mockResolvedValue(value);
    expect((await getLocalInferenceStatus()).backgroundExecution).toEqual({ ...value, executionDevice: active ? "gpu" : null });
  });

  it("supports older native modules without claiming background permission", async () => {
    delete native.getBackgroundExecutionStatus;
    expect(await getLocalInferenceStatus()).toEqual(ready);
  });

  it("does not turn a reporting failure into an inference failure or false permission", async () => {
    native.getBackgroundExecutionStatus!.mockRejectedValue(new Error("unavailable"));
    expect(await getLocalInferenceStatus()).toEqual(ready);
  });

  it.each([
    { outputBytes: -1 }, { outputBytes: 1.5 }, { outputBytes: Number.MAX_SAFE_INTEGER + 1 },
    { state: "invented" }, { reason: "invented" }, { entitlementGranted: "true" },
    { active: true }, { supported: false }, { operationId: "invalid\nidentifier" },
    { state: "active", active: false }, { privateData: "not part of the DTO" },
    { executionDevice: "npu" }, { executionDevice: undefined }, { state: "requesting", operationId: null },
    { state: "idle", operationId: "stale-operation" },
    { active: true, state: "active", operationId: "cpu-1", executionDevice: null },
    { active: true, state: "active", operationId: "gpu-1", executionDevice: "gpu", entitlementGranted: true, gpuSupported: false },
    { active: true, state: "active", operationId: "gpu-1", executionDevice: "gpu", entitlementGranted: null },
  ])("rejects malformed or contradictory background data: %j", async (change) => {
    native.getBackgroundExecutionStatus!.mockResolvedValue({ ...unverified, ...change });
    expect(await getLocalInferenceStatus()).toEqual(ready);
  });

  it("keeps explicit system rejection distinct from unverified permission", async () => {
    const rejected = { ...unverified, state: "foreground_only", entitlementGranted: false, reason: "not_permitted" };
    native.getBackgroundExecutionStatus!.mockResolvedValue(rejected);
    expect((await getLocalInferenceStatus()).backgroundExecution).toEqual(rejected);
  });
});
