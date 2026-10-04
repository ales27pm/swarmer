import { describe, expect, it } from "@jest/globals";
import { parseCoreMLDirectLoadReport } from "./coreml-direct-load";
import { COREML_PROBE_FIXTURES } from "./coreml-probe";

const fixtureID = "dolphin-attention-int4-perchannel-cache2-two-blocks-separated-states";
const computeUnits = "cpuAndNeuralEngine";
const result = {
  schemaVersion: 1, fixtureID, computeUnits, loadingAPI: "MLModel.init", configuration: "defaults_except_computeUnits",
  outcome: "loaded", stage: "complete", compileMilliseconds: 1, loadMilliseconds: 2,
  modelFileSHA256: "a".repeat(64), errors: [], stateCreated: false, predictionsPerformed: 0,
  computePlanRequested: false, hardwareExecutionMeasured: false,
};
const parse = (value: unknown) => parseCoreMLDirectLoadReport(JSON.stringify(value), fixtureID, computeUnits);

describe("direct Core ML load receipt", () => {
  it.each(COREML_PROBE_FIXTURES)("accepts loaded %s without prediction or hardware claims", (id) => {
    const value = { ...result, fixtureID: id };
    expect(parseCoreMLDirectLoadReport(JSON.stringify(value), id, computeUnits)).toEqual(value);
  });
  it.each([
    { stateCreated: true }, { predictionsPerformed: 1 }, { computePlanRequested: true }, { hardwareExecutionMeasured: true },
    { fixtureID: "../../private" }, { computeUnits: "all" }, { loadingAPI: "MLModel.load" }, { configuration: "other" },
    { schemaVersion: 2 }, { modelFileSHA256: null }, { modelFileSHA256: "A".repeat(64) }, { stage: "predict" },
    { outcome: "passed" }, { outcome: ["loaded"] }, { outcome: "failed" }, { outcome: "cancelled" }, { loadMilliseconds: -1 }, { compileMilliseconds: null },
    { extra: "/private/model" }, { errors: [{ domain: "com.apple.CoreML", code: 0 }] },
  ])("rejects false or malformed load evidence %j", (change) => { expect(() => parse({ ...result, ...change })).toThrow(); });
  it.each(["resolve", "compile", "load"])("preserves %s failures with sanitized error fields", (stage) => {
    const value = { ...result, stage, outcome: "failed", modelFileSHA256: stage === "resolve" ? null : result.modelFileSHA256,
      errors: [{ domain: "com.apple.CoreML", code: 0, executionPlanCode: -14 }] };
    expect(parse(value)).toEqual(value);
    expect(() => parse({ ...value, errors: [{ ...value.errors[0], path: "/private/model" }] })).toThrow();
  });
  it("preserves cancellation without publishing loaded", () => {
    expect(parse({ ...result, outcome: "cancelled", errors: [{ domain: "NSCocoaErrorDomain", code: 3072 }] })).toMatchObject({ outcome: "cancelled" });
  });
  it("rejects unbounded or non-JSON native values", () => {
    for (const raw of [null, {}, "/private/model", " ".repeat(16_001)]) {
      expect(() => parseCoreMLDirectLoadReport(raw, fixtureID, computeUnits)).toThrow();
    }
  });
});
