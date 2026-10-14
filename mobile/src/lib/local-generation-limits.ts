import type { LocalModel } from "./local-inference";

export const MAX_LOCAL_GENERATION_TOKENS = 1_024;
export const DEFAULT_GOAL_PLAN_MAX_TOKENS = 1_024;
export const DOLPHIN_COREML_QUALIFICATION_MAX_TOKENS = 32;
export const DOLPHIN_SPLIT_DIAGNOSTIC_ONLY_MESSAGE = "Dolphin3-ANE scindé est réservé aux diagnostics. Pour générer, importe et charge le package Core ML monolithique recommandé.";

/** Split imports stay inspectable, but only the monolith supports ordinary Dolphin generation. */
export function localModelSupportsGeneration(model?: Pick<LocalModel, "runtime" | "generationProfile">): boolean {
  return model?.runtime !== "coreml" || model.generationProfile !== "dolphin-split9-experimental";
}

/** Native package metadata, never a user-editable model name, selects this policy. */
export function localModelGenerationTokenLimit(model?: Pick<LocalModel, "runtime" | "generationProfile">): number {
  return model?.runtime === "coreml" && (
    model.generationProfile === "dolphin-split9-experimental"
    || model.generationProfile === "dolphin-monolith-experimental"
  ) ? DOLPHIN_COREML_QUALIFICATION_MAX_TOKENS : MAX_LOCAL_GENERATION_TOKENS;
}
