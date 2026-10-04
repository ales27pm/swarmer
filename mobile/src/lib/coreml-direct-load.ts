import { COREML_COMPUTE_UNITS, parseCoreMLLoadDiagnostic, type CoreMLComputeUnits, type CoreMLLoadDiagnostic } from "./coreml-load-diagnostics";
import { COREML_PROBE_FIXTURES, type CoreMLProbeFixture } from "./coreml-probe";

// This receipt describes synchronous compile/load only, never prediction or ANE activity.
export type CoreMLDirectLoadReport = {
  schemaVersion: 1;
  fixtureID: CoreMLProbeFixture;
  computeUnits: CoreMLComputeUnits;
  loadingAPI: "MLModel.init";
  configuration: "defaults_except_computeUnits";
  outcome: "loaded" | "failed" | "cancelled";
  stage: "resolve" | "compile" | "load" | "complete";
  compileMilliseconds: number;
  loadMilliseconds: number;
  modelFileSHA256: string | null;
  errors: CoreMLLoadDiagnostic["errors"];
  stateCreated: false;
  predictionsPerformed: 0;
  computePlanRequested: false;
  hardwareExecutionMeasured: false;
};

const record = (v: unknown): v is Record<string, unknown> => v !== null && typeof v === "object" && !Array.isArray(v);
const duration = (v: unknown): v is number => typeof v === "number" && Number.isFinite(v) && v >= 0 && v <= 86_400_000;

export function parseCoreMLDirectLoadReport(raw: unknown, fixtureID: CoreMLProbeFixture, units: CoreMLComputeUnits): CoreMLDirectLoadReport {
  const invalid = () => new Error("Reçu de chargement direct Core ML invalide.");
  if (typeof raw !== "string" || raw.length > 16_000) throw invalid();
  let value: unknown;
  try { value = JSON.parse(raw); } catch { throw invalid(); }
  const keys = ["schemaVersion", "fixtureID", "computeUnits", "loadingAPI", "configuration", "outcome", "stage",
    "compileMilliseconds", "loadMilliseconds", "modelFileSHA256", "errors", "stateCreated", "predictionsPerformed",
    "computePlanRequested", "hardwareExecutionMeasured"].sort().join();
  if (!record(value) || Object.keys(value).sort().join() !== keys || value.schemaVersion !== 1
    || value.fixtureID !== fixtureID || !COREML_PROBE_FIXTURES.includes(fixtureID)
    || value.computeUnits !== units || !COREML_COMPUTE_UNITS.includes(units)
    || value.loadingAPI !== "MLModel.init" || value.configuration !== "defaults_except_computeUnits"
    || !["loaded", "failed", "cancelled"].includes(value.outcome as string)
    || !["resolve", "compile", "load", "complete"].includes(value.stage as string)
    || !duration(value.compileMilliseconds) || !duration(value.loadMilliseconds)
    || !(value.modelFileSHA256 === null || typeof value.modelFileSHA256 === "string" && /^[a-f0-9]{64}$/.test(value.modelFileSHA256))
    || !Array.isArray(value.errors) || value.errors.length > 4
    || value.stateCreated !== false || value.predictionsPerformed !== 0
    || value.computePlanRequested !== false || value.hardwareExecutionMeasured !== false) throw invalid();
  const errors = value.errors.length === 0 ? [] : parseCoreMLLoadDiagnostic({
    computeUnits: units, outcome: "failed", stage: "load", elapsedMilliseconds: 0,
    errors: value.errors, errorsTruncated: false,
  }).errors;
  if (value.outcome === "loaded" && (value.stage !== "complete" || value.modelFileSHA256 === null || errors.length > 0)
    || value.outcome !== "loaded" && errors.length === 0) throw invalid();
  return { ...value, errors } as CoreMLDirectLoadReport;
}
