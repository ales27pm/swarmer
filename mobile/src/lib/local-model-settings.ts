import * as SecureStore from "expo-secure-store";

import type { LocalInferenceRuntime } from "@/lib/local-inference";

const SETTINGS_KEY = "swarmer.local-model-settings.v1";

export type LocalModelSettings = {
  runtime: LocalInferenceRuntime;
  modelId: string;
  revision: string;
  maxTokens: number;
  temperature: number;
};

export const DEFAULT_GENERATION_SETTINGS = { maxTokens: 256, temperature: 0.1 } as const;

export function parseGenerationSettings(maxTokens: string, temperature: string) {
  const tokens = Number(maxTokens.trim());
  const heat = Number(temperature.trim().replace(",", "."));
  if (!maxTokens.trim() || !Number.isInteger(tokens) || tokens < 1 || tokens > 512) {
    throw new Error("La limite de sortie doit être un entier entre 1 et 512 jetons.");
  }
  if (!temperature.trim() || !Number.isFinite(heat) || heat < 0 || heat > 2) {
    throw new Error("La température doit être comprise entre 0 et 2.");
  }
  return { maxTokens: tokens, temperature: heat };
}

function parseSettings(value: unknown): LocalModelSettings | null {
  if (value === null || typeof value !== "object") return null;
  const record = value as Record<string, unknown>;
  if (
    record.version !== 1 ||
    !["mlx", "llama.cpp", "coreml"].includes(String(record.runtime)) ||
    typeof record.modelId !== "string" || record.modelId.length > 200 ||
    typeof record.revision !== "string" || record.revision.length > 40 ||
    typeof record.maxTokens !== "number" || typeof record.temperature !== "number"
  ) return null;
  try {
    const generation = parseGenerationSettings(String(record.maxTokens), String(record.temperature));
    return {
      runtime: record.runtime as LocalInferenceRuntime,
      modelId: record.modelId,
      revision: record.revision,
      ...generation,
    };
  } catch {
    return null;
  }
}

export async function readLocalModelSettings(): Promise<LocalModelSettings | null> {
  const stored = await SecureStore.getItemAsync(SETTINGS_KEY);
  if (!stored) return null;
  try {
    return parseSettings(JSON.parse(stored));
  } catch {
    return null;
  }
}

export async function saveLocalModelSettings(settings: LocalModelSettings): Promise<void> {
  const validated = parseSettings({ version: 1, ...settings });
  if (!validated) throw new Error("Les réglages du modèle local sont invalides.");
  await SecureStore.setItemAsync(SETTINGS_KEY, JSON.stringify({ version: 1, ...validated }));
}
