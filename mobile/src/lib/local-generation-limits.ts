import type { LocalModel } from "./local-inference";

export const MAX_LOCAL_GENERATION_TOKENS = 1_024;
export const DEFAULT_GOAL_PLAN_MAX_TOKENS = 1_024;
export const DOLPHIN_COREML_QUALIFICATION_MAX_TOKENS = 32;

/** Native package metadata, never a user-editable model name, selects this policy. */
export function localModelGenerationTokenLimit(model?: Pick<LocalModel, "runtime" | "generationProfile">): number {
  return model?.runtime === "coreml" && (
    model.generationProfile === "dolphin-split9-experimental"
    || model.generationProfile === "dolphin-monolith-experimental"
  ) ? DOLPHIN_COREML_QUALIFICATION_MAX_TOKENS : MAX_LOCAL_GENERATION_TOKENS;
}
