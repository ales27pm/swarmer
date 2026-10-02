export const COREML_COMPUTE_UNITS = ["all", "cpuOnly", "cpuAndGPU", "cpuAndNeuralEngine"] as const;
export type CoreMLComputeUnits = typeof COREML_COMPUTE_UNITS[number];
const STAGES = ["resolve", "validateArtifact", "compile", "load", "validateContract", "tokenizer", "complete"] as const;
const DOMAINS = ["other", "com.apple.CoreML", "com.apple.appleneuralengine", "com.apple.espresso", "com.apple.Espresso",
  "com.apple.MPS", "MPSGraphErrorDomain", "NSCocoaErrorDomain", "NSPOSIXErrorDomain", "NSOSStatusErrorDomain", "NSURLErrorDomain"] as const;
export type CoreMLLoadDiagnostic = {
  computeUnits: CoreMLComputeUnits;
  stage: typeof STAGES[number];
  outcome: "succeeded" | "failed" | "cancelled";
  elapsedMilliseconds: number;
  errors: { domain: typeof DOMAINS[number]; code: number; executionPlanCode?: number }[];
  errorsTruncated: boolean;
};

export function assertCoreMLDiagnosticRequest(input: { runtime: string; coreMLComputeUnits?: unknown }, nativeAvailable: boolean): void {
  if (input.coreMLComputeUnits === undefined) return;
  if (!nativeAvailable || input.runtime !== "coreml" || !COREML_COMPUTE_UNITS.includes(input.coreMLComputeUnits as CoreMLComputeUnits)) {
    throw new Error("Le diagnostic Core ML nécessite une version de développement et une unité valide.");
  }
}

function record(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}
const integer = (value: unknown): value is number => Number.isInteger(value) && Number(value) >= -2_147_483_648 && Number(value) <= 2_147_483_647;

export function parseCoreMLLoadDiagnostic(value: unknown): CoreMLLoadDiagnostic {
  if (!record(value) || Object.keys(value).sort().join() !== "computeUnits,elapsedMilliseconds,errors,errorsTruncated,outcome,stage"
    || !COREML_COMPUTE_UNITS.includes(value.computeUnits as CoreMLComputeUnits)
    || !STAGES.includes(value.stage as CoreMLLoadDiagnostic["stage"])
    || typeof value.outcome !== "string" || !["succeeded", "failed", "cancelled"].includes(value.outcome)
    || typeof value.elapsedMilliseconds !== "number" || !Number.isFinite(value.elapsedMilliseconds)
    || value.elapsedMilliseconds < 0 || value.elapsedMilliseconds > 86_400_000
    || typeof value.errorsTruncated !== "boolean" || !Array.isArray(value.errors) || value.errors.length > 4) {
    throw new Error("Diagnostic Core ML invalide.");
  }
  const errors = value.errors.map((item): CoreMLLoadDiagnostic["errors"][number] => {
    if (!record(item) || Object.keys(item).some((key) => !["domain", "code", "executionPlanCode"].includes(key))
      || !DOMAINS.includes(item.domain as typeof DOMAINS[number]) || !integer(item.code)
      || !(item.executionPlanCode === undefined || item.executionPlanCode === null || integer(item.executionPlanCode))) {
      throw new Error("Diagnostic Core ML invalide.");
    }
    return { domain: item.domain as typeof DOMAINS[number], code: item.code,
      ...(typeof item.executionPlanCode === "number" ? { executionPlanCode: item.executionPlanCode } : {}) };
  });
  if ((value.outcome === "succeeded" && (value.stage !== "complete" || errors.length > 0 || value.errorsTruncated))
    || (value.outcome !== "succeeded" && errors.length === 0)) throw new Error("Diagnostic Core ML incohérent.");
  return { computeUnits: value.computeUnits as CoreMLComputeUnits, stage: value.stage as CoreMLLoadDiagnostic["stage"],
    outcome: value.outcome as CoreMLLoadDiagnostic["outcome"], elapsedMilliseconds: value.elapsedMilliseconds,
    errors, errorsTruncated: value.errorsTruncated };
}
