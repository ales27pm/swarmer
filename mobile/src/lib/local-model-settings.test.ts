import { beforeEach, describe, expect, it, jest } from "@jest/globals";
import * as SecureStore from "expo-secure-store";

import {
  DEFAULT_GENERATION_SETTINGS,
  generationSettingsForModel,
  parseGenerationSettings,
  readLocalModelSettings,
  saveLocalModelSettings,
} from "./local-model-settings";

jest.mock("expo-secure-store", () => ({ getItemAsync: jest.fn(), setItemAsync: jest.fn() }));

describe("local model settings", () => {
  beforeEach(() => { jest.clearAllMocks(); });

  it("keeps the physical-device qualification budget bounded", () => {
    expect(DEFAULT_GENERATION_SETTINGS).toEqual({ maxTokens: 32, temperature: 0.1 });
  });

  it("persists and restores a custom model and iteration settings", async () => {
    const settings = {
      runtime: "mlx" as const, modelId: "community/custom", revision: "a".repeat(40),
      maxTokens: 128, temperature: 0.25,
    };
    jest.mocked(SecureStore.setItemAsync).mockResolvedValue();
    await saveLocalModelSettings(settings);
    const [key, serialized] = jest.mocked(SecureStore.setItemAsync).mock.calls[0];
    jest.mocked(SecureStore.getItemAsync).mockResolvedValue(serialized);
    expect(await readLocalModelSettings()).toEqual(settings);
    expect(SecureStore.getItemAsync).toHaveBeenCalledWith(key);
  });

  it.each(["broken json", '{"version":2}', '{"version":1,"runtime":"remote"}'])(
    "ignores incompatible or corrupt persisted settings: %s", async (stored) => {
      jest.mocked(SecureStore.getItemAsync).mockResolvedValue(stored);
      expect(await readLocalModelSettings()).toBeNull();
    },
  );

  it.each([["0", "0.1"], ["1025", "0.1"], ["1.5", "0.1"], ["256", ""], ["256", "Infinity"], ["256", "2.1"]])(
    "rejects generation settings outside the native contract: %s / %s", (tokens, temperature) => {
      expect(() => parseGenerationSettings(tokens, temperature)).toThrow();
    },
  );

  it("accepts deterministic sampling and a French decimal separator", () => {
    expect(parseGenerationSettings("1024", "0")).toEqual({ maxTokens: 1024, temperature: 0 });
    expect(parseGenerationSettings("128", "0,25")).toEqual({ maxTokens: 128, temperature: 0.25 });
  });

  it.each(["dolphin-monolith-experimental"] as const)(
    "caps only the effective budget for %s", (generationProfile) => {
      const model = { runtime: "coreml" as const, generationProfile };
      expect(generationSettingsForModel("512", "0", model)).toEqual({ maxTokens: 32, temperature: 0 });
      expect(generationSettingsForModel("1024", "0,25", model)).toEqual({ maxTokens: 32, temperature: 0.25 });
      for (const maxTokens of [1, 2, 8, 32]) {
        expect(generationSettingsForModel(String(maxTokens), "0", model).maxTokens).toBe(maxTokens);
      }
      // The persisted/requested value is not silently migrated or clamped.
      expect(parseGenerationSettings("512", "0")).toEqual({ maxTokens: 512, temperature: 0 });
    },
  );

  it("rejects split generation without rewriting its saved preference", async () => {
    const model = { runtime: "coreml" as const, generationProfile: "dolphin-split9-experimental" as const };
    for (const tokens of [1, 8, 32, 1024]) {
      expect(() => generationSettingsForModel(String(tokens), "0", model)).toThrow("monolithique");
    }
    const settings = { runtime: "coreml" as const, modelId: "split-import", revision: "", maxTokens: 512, temperature: 0 };
    await saveLocalModelSettings(settings);
    expect(JSON.parse(jest.mocked(SecureStore.setItemAsync).mock.calls[0][1])).toEqual({ version: 1, ...settings });
  });

  it.each(["coreml", "mlx", "llama.cpp"] as const)("preserves other %s model budgets", (runtime) => {
    expect(generationSettingsForModel("512", "0.1", { runtime })).toEqual({ maxTokens: 512, temperature: 0.1 });
    expect(generationSettingsForModel("1024", "0.1", { runtime }).maxTokens).toBe(1024);
  });

  it("does not guess package identity from an editable display name", () => {
    const model = { runtime: "coreml" as const, displayName: "Dolphin3-ANE" };
    expect(generationSettingsForModel("512", "0", model).maxTokens).toBe(512);
  });

  it.each(["0", "1025", "1.5", "", "NaN"])("still rejects invalid requested budgets before capping: %s", (maxTokens) => {
    expect(() => generationSettingsForModel(maxTokens, "0", {
      runtime: "coreml", generationProfile: "dolphin-split9-experimental",
    })).toThrow();
  });
});
