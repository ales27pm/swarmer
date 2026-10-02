import { describe, expect, it } from "@jest/globals";
import { COREML_PROBE_FIXTURES, parseCoreMLProbeReport } from "./coreml-probe";

const fixtureID = "attention-stateful-fused";
const computeUnits = "cpuAndNeuralEngine";
const result = {
  schemaVersion: 1, fixtureID, computeUnits, outcome: "passed", stage: "complete",
  loadMilliseconds: 15, predictionMilliseconds: 2,
  preferredDeviceCounts: { cpu: 1, gpu: 0, neuralEngine: 3, unknown: 0 },
  supportedDeviceCounts: { cpu: 4, gpu: 0, neuralEngine: 3, unknown: 0 },
  maxAbsoluteError: 0.0001, elementsCompared: 6144, errors: [], hardwareExecutionMeasured: false,
};
const parse = (v: unknown) => parseCoreMLProbeReport(JSON.stringify(v), fixtureID, computeUnits);

describe("bounded Core ML fixture receipts", () => {
  it("preserves the six existing fixtures and adds only the cache28 attention ablation", () => {
    expect(COREML_PROBE_FIXTURES).toEqual([
      "attention-stateful-fused", "attention-stateful-decomposed",
      "attention-stateless-fused", "attention-stateless-decomposed",
      "dolphin-attention-int4-block32", "dolphin-attention-int4-perchannel",
      "dolphin-attention-int4-perchannel-cache28",
    ]);
  });
  it.each(["cpuOnly", "cpuAndNeuralEngine"] as const)("accepts cache28 receipts for %s without asserting hardware execution", (units) => {
    const cacheFixture = "dolphin-attention-int4-perchannel-cache28";
    const receipt = { ...result, fixtureID: cacheFixture, computeUnits: units };
    expect(parseCoreMLProbeReport(JSON.stringify(receipt), cacheFixture, units)).toEqual(receipt);
    expect(() => parseCoreMLProbeReport(JSON.stringify({ ...receipt, fixtureID: "dolphin-attention-int4-perchannel" }), cacheFixture, units)).toThrow();
    expect(() => parseCoreMLProbeReport(JSON.stringify({ ...receipt, hardwareExecutionMeasured: true }), cacheFixture, units)).toThrow();
  });
  it("keeps model plan assignment distinct from actual hardware telemetry", () => {
    expect(parse(result)).toEqual(result);
    expect(() => parse({ ...result, hardwareExecutionMeasured: true })).toThrow();
  });
  it.each([
    { fixtureID: "attention-stateless-fused" }, { computeUnits: "cpuAndGPU" }, { fixtureID: "../../private" },
    { schemaVersion: 2 }, { outcome: ["passed"] }, { stage: "load" }, { elementsCompared: 0 },
    { maxAbsoluteError: null }, { predictionMilliseconds: -1 }, { elementsCompared: 1.2 },
    { extra: "private native details" }, { preferredDeviceCounts: { cpu: 0 } },
    { supportedDeviceCounts: { cpu: 0, gpu: 0, neuralEngine: 1_000_001, unknown: 0 } },
    { errors: [{ domain: "com.apple.CoreML", code: 0, executionPlanCode: -14 }] },
  ])("rejects inconsistent or unbounded success %j", (change) => {
    expect(() => parse({ ...result, ...change })).toThrow();
  });
  it("preserves a native load failure without leaking paths or userInfo", () => {
    const failure = { ...result, outcome: "failed", stage: "load", maxAbsoluteError: null, elementsCompared: 0,
      errors: [{ domain: "com.apple.CoreML", code: 0, executionPlanCode: -14 }] };
    expect(parse(failure)).toEqual(failure);
    expect(() => parse({ ...failure, errors: [{ ...failure.errors[0], path: "/private/model.mil" }] })).toThrow();
    expect(() => parseCoreMLProbeReport("/private/model.mil", fixtureID, computeUnits)).toThrow("Reçu de diagnostic Core ML invalide.");
  });
  it("allows a measured numerical mismatch without an invented native error", () => {
    expect(parse({ ...result, outcome: "failed", stage: "compare", maxAbsoluteError: 10 })).toMatchObject({ outcome: "failed", errors: [] });
  });
});
