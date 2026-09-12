import { beforeEach, describe, expect, it, jest } from "@jest/globals";
import * as SecureStore from "expo-secure-store";

import { parseGenerationSettings, readLocalModelSettings, saveLocalModelSettings } from "./local-model-settings";

jest.mock("expo-secure-store", () => ({ getItemAsync: jest.fn(), setItemAsync: jest.fn() }));

describe("local model settings", () => {
  beforeEach(() => { jest.clearAllMocks(); });

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

  it.each([["0", "0.1"], ["513", "0.1"], ["1.5", "0.1"], ["256", ""], ["256", "Infinity"], ["256", "2.1"]])(
    "rejects generation settings outside the native contract: %s / %s", (tokens, temperature) => {
      expect(() => parseGenerationSettings(tokens, temperature)).toThrow();
    },
  );

  it("accepts deterministic sampling and a French decimal separator", () => {
    expect(parseGenerationSettings("512", "0")).toEqual({ maxTokens: 512, temperature: 0 });
    expect(parseGenerationSettings("128", "0,25")).toEqual({ maxTokens: 128, temperature: 0.25 });
  });
});
