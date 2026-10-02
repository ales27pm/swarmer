import { beforeEach, describe, expect, it, jest } from "@jest/globals";
import { requireOptionalNativeModule } from "expo";
import type { HuggingFaceDownloadPlan } from "./hugging-face-models";
import { cancelHuggingFaceModelDownload, cancelLocalModelDownload, downloadHuggingFaceModel, getModelDownloadProgress } from "./local-inference";

jest.mock("expo", () => ({ requireOptionalNativeModule: jest.fn(() => ({})) }));
const native = jest.mocked(requireOptionalNativeModule).mock.results[0].value as {
  downloadHuggingFaceModel?: ReturnType<typeof jest.fn<(input: HuggingFaceDownloadPlan) => Promise<unknown>>>;
  getModelDownloadProgress?: ReturnType<typeof jest.fn<() => Promise<unknown>>>;
  cancelHuggingFaceModelDownload?: ReturnType<typeof jest.fn<() => Promise<unknown>>>;
  cancelModelDownload?: ReturnType<typeof jest.fn<() => Promise<unknown>>>;
};
const plan: HuggingFaceDownloadPlan = {
  runtime: "mlx", repoId: "test/model", revision: "a".repeat(40), displayName: "Downloaded model",
  files: [{ path: "model.safetensors", sizeBytes: 100, sha256: "b".repeat(64) }],
};
const model = { modelId: "local-model", runtime: "mlx", displayName: plan.displayName,
  source: "Hugging Face test/model", sizeBytes: 100, importedAt: "2026-10-02T00:00:00Z" };
const progress = { state: "downloading", downloadedBytes: 40, totalBytes: 100, completedFiles: 1, totalFiles: 2 };

describe("local Hugging Face native adapter", () => {
  beforeEach(() => {
    native.downloadHuggingFaceModel = jest.fn<(input: HuggingFaceDownloadPlan) => Promise<unknown>>().mockResolvedValue(model);
    native.getModelDownloadProgress = jest.fn<() => Promise<unknown>>().mockResolvedValue(progress);
    native.cancelHuggingFaceModelDownload = jest.fn<() => Promise<unknown>>().mockResolvedValue(null);
    native.cancelModelDownload = jest.fn<() => Promise<unknown>>().mockResolvedValue(null);
  });

  it.each(["mlx", "coreml", "llama.cpp"] as const)("passes the pinned %s plan and validates the imported model", async (runtime) => {
    native.downloadHuggingFaceModel!.mockResolvedValue({ ...model, runtime });
    const requested = { ...plan, runtime };
    expect(await downloadHuggingFaceModel(requested)).toEqual({ ...model, runtime, purpose: "generation" });
    expect(native.downloadHuggingFaceModel).toHaveBeenCalledWith(requested);
    expect(native.downloadHuggingFaceModel).toHaveBeenCalledTimes(1);
  });

  it("preserves an explicit embedding purpose", async () => {
    native.downloadHuggingFaceModel!.mockResolvedValue({ ...model, purpose: "embeddings" });
    expect(await downloadHuggingFaceModel(plan)).toMatchObject({ purpose: "embeddings" });
  });

  it.each([{ runtime: "coreml" }, { sizeBytes: -1 }, { sizeBytes: Infinity }, { purpose: "unknown" }, { secret: "unexpected" }])(
    "rejects an inconsistent native model %j", async (change) => {
      native.downloadHuggingFaceModel!.mockResolvedValue({ ...model, ...change });
      await expect(downloadHuggingFaceModel(plan)).rejects.toThrow("modèle");
    },
  );

  it("reports an update requirement for an older module instead of pretending to download", async () => {
    delete native.downloadHuggingFaceModel;
    await expect(downloadHuggingFaceModel(plan)).rejects.toThrow("Mets à jour");
  });

  it("does not hide native download errors", async () => {
    const error = new Error("download_cancelled");
    native.downloadHuggingFaceModel!.mockRejectedValue(error);
    await expect(downloadHuggingFaceModel(plan)).rejects.toBe(error);
  });

  it("returns validated progress without inventing completion", async () => {
    expect(await getModelDownloadProgress()).toEqual(progress);
  });

  it.each(["verifying", "importing", "cancelled", "failed"] as const)("preserves the %s phase", async (state) => {
    native.getModelDownloadProgress!.mockResolvedValue({ ...progress, state });
    expect(await getModelDownloadProgress()).toEqual({ ...progress, state });
  });

  it("accepts the initial idle state and a fully completed import", async () => {
    const idle = { state: "idle", downloadedBytes: 0, totalBytes: 0, completedFiles: 0, totalFiles: 0 };
    native.getModelDownloadProgress!.mockResolvedValueOnce(idle)
      .mockResolvedValueOnce({ ...progress, state: "completed", downloadedBytes: 100, completedFiles: 2 });
    expect(await getModelDownloadProgress()).toEqual(idle);
    expect(await getModelDownloadProgress()).toMatchObject({ state: "completed", downloadedBytes: 100, completedFiles: 2 });
  });

  it.each([
    { state: "ready" }, { downloadedBytes: -1 }, { downloadedBytes: 101 }, { downloadedBytes: 0.5 },
    { totalBytes: Infinity }, { totalBytes: Number.MAX_SAFE_INTEGER + 1 }, { totalBytes: "100" },
    { completedFiles: 3 }, { completedFiles: -1 }, { totalFiles: 1.5 }, { state: "completed" },
    { state: "idle" }, { state: "completed", downloadedBytes: 0, totalBytes: 0, completedFiles: 0, totalFiles: 0 },
    { privatePath: "/private/download" },
  ])("rejects malformed or contradictory progress %j", async (change) => {
    native.getModelDownloadProgress!.mockResolvedValue({ ...progress, ...change });
    await expect(getModelDownloadProgress()).rejects.toThrow("progression");
  });

  it("does not fabricate progress when an older module lacks reporting", async () => {
    delete native.getModelDownloadProgress;
    await expect(getModelDownloadProgress()).rejects.toThrow("Mets à jour");
  });

  it("propagates a progress read failure", async () => {
    const error = new Error("native_unavailable");
    native.getModelDownloadProgress!.mockRejectedValue(error);
    await expect(getModelDownloadProgress()).rejects.toBe(error);
  });

  it("cancels through the existing native operation", async () => {
    await cancelLocalModelDownload();
    expect(native.cancelModelDownload).toHaveBeenCalledTimes(1);
    expect(native.cancelHuggingFaceModelDownload).not.toHaveBeenCalled();
  });

  it("uses only the native Hugging Face cancellation owner", async () => {
    await cancelHuggingFaceModelDownload();
    expect(native.cancelHuggingFaceModelDownload).toHaveBeenCalledTimes(1);
    expect(native.cancelModelDownload).not.toHaveBeenCalled();
  });

  it("never falls back to cancelling another download on an older native module", async () => {
    delete native.cancelHuggingFaceModelDownload;
    await expect(cancelHuggingFaceModelDownload()).rejects.toThrow("Mets à jour");
    expect(native.cancelModelDownload).not.toHaveBeenCalled();
  });

  it("does not hide a Hugging Face cancellation failure", async () => {
    const error = new Error("cancellation_failed");
    native.cancelHuggingFaceModelDownload!.mockRejectedValue(error);
    await expect(cancelHuggingFaceModelDownload()).rejects.toBe(error);
    expect(native.cancelModelDownload).not.toHaveBeenCalled();
  });

  it("reports an old native cancellation bridge explicitly", async () => {
    delete native.cancelModelDownload;
    await expect(cancelLocalModelDownload()).rejects.toThrow("Mets à jour");
  });
});
