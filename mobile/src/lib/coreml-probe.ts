import { COREML_COMPUTE_UNITS, parseCoreMLLoadDiagnostic, type CoreMLComputeUnits, type CoreMLLoadDiagnostic } from "./coreml-load-diagnostics";

// Only fixtures installed with the diagnostic build are addressable. The native
// implementation independently checks the signed bundle manifest and file hashes.
export const COREML_PROBE_FIXTURES = [
  "attention-stateful-fused", "attention-stateful-decomposed",
  "attention-stateless-fused", "attention-stateless-decomposed",
  "dolphin-attention-int4-block32", "dolphin-attention-int4-perchannel",
  "dolphin-attention-int4-perchannel-cache28",
  "dolphin-attention-int4-perchannel-cache28-two-blocks",
  "dolphin-attention-int4-perchannel-cache28-slot1",
  "dolphin-attention-int4-perchannel-cache28-two-blocks-independent",
  "dolphin-attention-int4-perchannel-cache28-two-blocks-separated-states",
] as const;
export type CoreMLProbeFixture = typeof COREML_PROBE_FIXTURES[number];
const STAGES = ["resolve", "compile", "load", "plan", "predict", "compare", "complete"] as const;
export type CoreMLProbeReport = {
  schemaVersion: 1;
  fixtureID: CoreMLProbeFixture;
  computeUnits: CoreMLComputeUnits;
  outcome: "passed" | "failed" | "cancelled";
  stage: typeof STAGES[number];
  loadMilliseconds: number;
  predictionMilliseconds: number;
  preferredDeviceCounts: { cpu: number; gpu: number; neuralEngine: number; unknown: number };
  supportedDeviceCounts: { cpu: number; gpu: number; neuralEngine: number; unknown: number };
  maxAbsoluteError: number | null;
  elementsCompared: number;
  errors: CoreMLLoadDiagnostic["errors"];
  // MLComputePlan describes assignment; it is not hardware activity telemetry.
  hardwareExecutionMeasured: false;
};

const record = (v: unknown): v is Record<string, unknown> => v !== null && typeof v === "object" && !Array.isArray(v);
const bounded = (v: unknown, max: number): v is number => typeof v === "number" && Number.isFinite(v) && v >= 0 && v <= max;
const integer = (v: unknown, max: number): v is number => bounded(v, max) && Number.isInteger(v);

export function parseCoreMLProbeReport(raw: unknown, fixtureID: CoreMLProbeFixture, units: CoreMLComputeUnits): CoreMLProbeReport {
  if (typeof raw !== "string" || raw.length > 16_000) throw new Error("Reçu de diagnostic Core ML invalide.");
  let value: unknown;
  try { value = JSON.parse(raw); } catch { throw new Error("Reçu de diagnostic Core ML invalide."); }
  const keys = ["schemaVersion", "fixtureID", "computeUnits", "outcome", "stage", "loadMilliseconds", "predictionMilliseconds",
    "preferredDeviceCounts", "supportedDeviceCounts", "maxAbsoluteError", "elementsCompared", "errors", "hardwareExecutionMeasured"].sort().join();
  if (!record(value) || Object.keys(value).sort().join() !== keys || value.schemaVersion !== 1
    || value.fixtureID !== fixtureID || !COREML_PROBE_FIXTURES.includes(fixtureID)
    || value.computeUnits !== units || !COREML_COMPUTE_UNITS.includes(units)
    || typeof value.outcome !== "string" || !["passed", "failed", "cancelled"].includes(value.outcome)
    || !STAGES.includes(value.stage as CoreMLProbeReport["stage"])
    || !bounded(value.loadMilliseconds, 86_400_000) || !bounded(value.predictionMilliseconds, 86_400_000)
    || !(value.maxAbsoluteError === null || bounded(value.maxAbsoluteError, Number.MAX_VALUE))
    || !integer(value.elementsCompared, 10_000_000) || value.hardwareExecutionMeasured !== false
    || !record(value.preferredDeviceCounts) || Object.keys(value.preferredDeviceCounts).sort().join() !== "cpu,gpu,neuralEngine,unknown"
    || !Object.values(value.preferredDeviceCounts).every(v => integer(v, 1_000_000))
    || !record(value.supportedDeviceCounts) || Object.keys(value.supportedDeviceCounts).sort().join() !== "cpu,gpu,neuralEngine,unknown"
    || !Object.values(value.supportedDeviceCounts).every(v => integer(v, 1_000_000))
    || !Array.isArray(value.errors) || value.errors.length > 4) throw new Error("Reçu de diagnostic Core ML invalide.");
  // Reuse the strict domain/code allowlist; never pass native paths or userInfo.
  const errors = value.errors.length === 0 ? [] : parseCoreMLLoadDiagnostic({
    computeUnits: units, outcome: "failed", stage: "load", elapsedMilliseconds: 0,
    errors: value.errors, errorsTruncated: false,
  }).errors;
  if (value.outcome === "passed" && (value.stage !== "complete" || errors.length > 0
    || value.elementsCompared === 0 || value.maxAbsoluteError === null)) throw new Error("Diagnostic Core ML incomplet.");
  return { ...value, errors } as CoreMLProbeReport;
}
