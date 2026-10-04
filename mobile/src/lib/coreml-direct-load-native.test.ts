import { beforeEach, describe, expect, it, jest } from "@jest/globals";
import { requireOptionalNativeModule } from "expo";
import { directLoadCoreMLFixture, isCoreMLDirectLoadAvailable } from "./local-inference";

jest.mock("expo", () => ({ requireOptionalNativeModule: jest.fn(() => ({
  coreMLDiagnosticsAvailable: true, coreMLDirectLoadAvailable: true, directLoadCoreMLFixture: jest.fn(),
})) }));
const native = jest.mocked(requireOptionalNativeModule).mock.results[0].value as {
  coreMLDiagnosticsAvailable: boolean; coreMLDirectLoadAvailable: boolean;
  directLoadCoreMLFixture?: ReturnType<typeof jest.fn<() => Promise<string>>>;
};
const input = { fixtureID: "dolphin-attention-int4-perchannel-cache2-two-blocks-separated-states", computeUnits: "cpuAndNeuralEngine" } as const;
const result = { schemaVersion: 1, ...input, loadingAPI: "MLModel.init", configuration: "defaults_except_computeUnits",
  outcome: "loaded", stage: "complete", compileMilliseconds: 1, loadMilliseconds: 2,
  modelFileSHA256: "a".repeat(64), errors: [], stateCreated: false, predictionsPerformed: 0,
  computePlanRequested: false, hardwareExecutionMeasured: false };

describe("native direct Core ML load boundary", () => {
  beforeEach(() => {
    native.coreMLDiagnosticsAvailable = true; native.coreMLDirectLoadAvailable = true;
    native.directLoadCoreMLFixture = jest.fn<() => Promise<string>>().mockResolvedValue(JSON.stringify(result));
  });
  it("passes only the bounded ID and units then parses the native receipt", async () => {
    expect(await directLoadCoreMLFixture(input)).toEqual(result);
    expect(native.directLoadCoreMLFixture).toHaveBeenCalledWith(input.fixtureID, input.computeUnits);
  });
  it.each(["coreMLDiagnosticsAvailable", "coreMLDirectLoadAvailable"] as const)("requires %s", async flag => {
    native[flag] = false;
    expect(isCoreMLDirectLoadAvailable()).toBe(false);
    await expect(directLoadCoreMLFixture(input)).rejects.toThrow();
    expect(native.directLoadCoreMLFixture).not.toHaveBeenCalled();
  });
  it("refuses older native builds", async () => {
    delete native.directLoadCoreMLFixture;
    expect(isCoreMLDirectLoadAvailable()).toBe(false);
    await expect(directLoadCoreMLFixture(input)).rejects.toThrow();
  });
  it("rejects a forged hardware or prediction claim", async () => {
    native.directLoadCoreMLFixture!.mockResolvedValue(JSON.stringify({ ...result, predictionsPerformed: 1 }));
    await expect(directLoadCoreMLFixture(input)).rejects.toThrow();
  });
});
