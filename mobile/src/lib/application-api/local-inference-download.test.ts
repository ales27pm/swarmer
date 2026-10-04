import { beforeEach, describe, expect, it, jest } from "@jest/globals";
import type { HuggingFaceDownloadPlan } from "@/lib/hugging-face-models";
import * as adapter from "@/lib/local-inference";
import { invokeApplicationCommand } from "./registry";
import { LOCAL_MODEL_PRESETS } from "@/lib/local-model-presets";
import { cancelHuggingFaceModelDownload, cancelLocalModelDownload, downloadHuggingFaceModel, downloadLocalGgufModel, getModelDownloadProgress } from "./local-inference";

jest.mock("expo", () => ({ requireOptionalNativeModule: jest.fn(() => null) }));
jest.mock("expo/fetch", () => ({ fetch: jest.fn() }));
jest.mock("expo-constants", () => ({ __esModule: true, default: {} }));
jest.mock("@/lib/state/mutation-outbox", () => ({ mutationOutbox: { pendingCount: jest.fn() } }));
jest.mock("@/lib/state/replica", () => ({ applyBootstrap: jest.fn(), upsertEvent: jest.fn(), localSwarmSnapshot: jest.fn() }));
jest.mock("@/lib/local-inference", () => ({
  ...jest.requireActual<typeof import("@/lib/local-inference")>("@/lib/local-inference"),
  downloadHuggingFaceModel: jest.fn(), getModelDownloadProgress: jest.fn(), cancelHuggingFaceModelDownload: jest.fn(), cancelLocalModelDownload: jest.fn(),
  downloadLocalGgufModel: jest.fn(),
}));
const plan: HuggingFaceDownloadPlan = {
  runtime: "mlx", repoId: "test/model", revision: "a".repeat(40), displayName: "Downloaded model",
  files: [{ path: "model.safetensors", sizeBytes: 100, sha256: "b".repeat(64) }],
};
const model = { modelId: "model", runtime: "mlx" as const, displayName: plan.displayName,
  source: "Hugging Face", sizeBytes: 100, importedAt: "2026-10-02T00:00:00Z" };

describe("user-selected model download UI bridge", () => {
  beforeEach(() => {
    jest.clearAllMocks();
    jest.mocked(adapter.cancelHuggingFaceModelDownload).mockResolvedValue(undefined);
    jest.mocked(adapter.cancelLocalModelDownload).mockResolvedValue(undefined);
  });

  it("passes the UI plan through the shared ticket before calling the local adapter", async () => {
    let finish!: (value: adapter.LocalModel) => void;
    jest.mocked(adapter.downloadHuggingFaceModel).mockReturnValue(new Promise(resolve => { finish = resolve; }));
    const pending = downloadHuggingFaceModel(plan);
    try {
      await expect(invokeApplicationCommand("models.load", { runtime: "mlx", modelId: "other" })).rejects.toMatchObject({ code: "busy" });
    } finally { finish(model); }
    expect(await pending).toEqual(model);
    expect(adapter.downloadHuggingFaceModel).toHaveBeenCalledWith(plan);
  });

  it("reads progress and cancels only an active UI-owned Hub download", async () => {
    let finish!: (value: adapter.LocalModel) => void;
    jest.mocked(adapter.downloadHuggingFaceModel).mockReturnValue(new Promise(resolve => { finish = resolve; }));
    const pending = downloadHuggingFaceModel(plan);
    const cancelled = expect(pending).rejects.toMatchObject({ code: "cancelled" });
    const progress = { state: "downloading" as const, downloadedBytes: 20, totalBytes: 100, completedFiles: 0, totalFiles: 1 };
    jest.mocked(adapter.getModelDownloadProgress).mockResolvedValue(progress);
    expect(await getModelDownloadProgress()).toEqual(progress);
    try { await cancelHuggingFaceModelDownload(); }
    finally { finish(model); await cancelled; }
    expect(adapter.cancelHuggingFaceModelDownload).toHaveBeenCalledTimes(1);
    expect(adapter.cancelLocalModelDownload).not.toHaveBeenCalled();
    await expect(cancelHuggingFaceModelDownload()).rejects.toMatchObject({ code: "not_running" });
    expect(adapter.cancelHuggingFaceModelDownload).toHaveBeenCalledTimes(1);
  });

  it("preserves the existing API-owned preset cancellation path", async () => {
    let finish!: (value: adapter.LocalModel) => void;
    jest.mocked(adapter.downloadLocalGgufModel).mockReturnValue(new Promise(resolve => { finish = resolve; }));
    const pending = downloadLocalGgufModel(LOCAL_MODEL_PRESETS["llama.cpp"].download!);
    try { await cancelLocalModelDownload(); }
    finally { finish({ ...model, runtime: "llama.cpp" }); await pending; }
    expect(adapter.cancelLocalModelDownload).toHaveBeenCalledTimes(1);
    expect(adapter.cancelHuggingFaceModelDownload).not.toHaveBeenCalled();
  });
});
