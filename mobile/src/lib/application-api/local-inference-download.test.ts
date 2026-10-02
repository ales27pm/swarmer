import { beforeEach, describe, expect, it, jest } from "@jest/globals";
import type { HuggingFaceDownloadPlan } from "@/lib/hugging-face-models";
import * as adapter from "@/lib/local-inference";
import { invokeApplicationCommand } from "./registry";
import { cancelHuggingFaceModelDownload, cancelLocalModelDownload, downloadHuggingFaceModel, getModelDownloadProgress } from "./local-inference";

jest.mock("expo", () => ({ requireOptionalNativeModule: jest.fn(() => null) }));
jest.mock("@/lib/local-inference", () => ({
  ...jest.requireActual<typeof import("@/lib/local-inference")>("@/lib/local-inference"),
  downloadHuggingFaceModel: jest.fn(), getModelDownloadProgress: jest.fn(), cancelHuggingFaceModelDownload: jest.fn(), cancelLocalModelDownload: jest.fn(),
}));
jest.mock("./registry", () => ({ invokeApplicationCommand: jest.fn() }));
const plan: HuggingFaceDownloadPlan = {
  runtime: "mlx", repoId: "test/model", revision: "a".repeat(40), displayName: "Downloaded model",
  files: [{ path: "model.safetensors", sizeBytes: 100, sha256: "b".repeat(64) }],
};

describe("user-selected model download UI bridge", () => {
  beforeEach(() => { jest.clearAllMocks(); });

  it("uses the local adapter for a Hub plan without adding a remote command", async () => {
    const model = { modelId: "model", runtime: "mlx" as const, displayName: plan.displayName,
      source: "Hugging Face", sizeBytes: 100, importedAt: "2026-10-02T00:00:00Z" };
    jest.mocked(adapter.downloadHuggingFaceModel).mockResolvedValue(model);
    expect(await downloadHuggingFaceModel(plan)).toEqual(model);
    expect(adapter.downloadHuggingFaceModel).toHaveBeenCalledWith(plan);
    expect(invokeApplicationCommand).not.toHaveBeenCalled();
  });

  it("reads progress and cancels the local download without requiring an API operation ticket", async () => {
    const progress = { state: "downloading" as const, downloadedBytes: 20, totalBytes: 100, completedFiles: 0, totalFiles: 1 };
    jest.mocked(adapter.getModelDownloadProgress).mockResolvedValue(progress);
    expect(await getModelDownloadProgress()).toEqual(progress);
    await cancelHuggingFaceModelDownload();
    expect(adapter.cancelHuggingFaceModelDownload).toHaveBeenCalledTimes(1);
    expect(adapter.cancelLocalModelDownload).not.toHaveBeenCalled();
    expect(invokeApplicationCommand).not.toHaveBeenCalled();
  });

  it("preserves the existing API-owned preset cancellation path", async () => {
    await cancelLocalModelDownload();
    expect(invokeApplicationCommand).toHaveBeenCalledWith("models.download.cancel", {});
    expect(adapter.cancelLocalModelDownload).not.toHaveBeenCalled();
    expect(adapter.cancelHuggingFaceModelDownload).not.toHaveBeenCalled();
  });
});
