import { act, fireEvent, render, screen, waitFor } from "@testing-library/react-native";
import { beforeEach, describe, expect, it, jest } from "@jest/globals";
import { HuggingFaceModelDownload } from "./hugging-face-model-download";
import { resolveHuggingFaceModels, type HuggingFaceModelChoice } from "@/lib/hugging-face-models";
import { cancelHuggingFaceModelDownload, downloadHuggingFaceModel, getModelDownloadProgress, type LocalModel } from "@/lib/application-api/local-inference";

jest.mock("@/lib/hugging-face-models", () => ({ resolveHuggingFaceModels: jest.fn() }));
jest.mock("@/lib/application-api/local-inference", () => ({
  cancelHuggingFaceModelDownload: jest.fn(), downloadHuggingFaceModel: jest.fn(), getModelDownloadProgress: jest.fn(),
}));

const choice: HuggingFaceModelChoice = {
  id: "model.gguf", label: "Modèle Q4", sizeBytes: 1024,
  plan: { runtime: "llama.cpp", repoId: "example/model", revision: "a".repeat(40), displayName: "Modèle Q4", files: [{ path: "model.gguf", sizeBytes: 1024, sha256: "b".repeat(64) }] },
};
const imported: LocalModel = { modelId: "local-test", runtime: "llama.cpp", displayName: "Modèle Q4", source: "model.gguf", sizeBytes: 1024, importedAt: "2026-10-02T00:00:00Z" };
const busy = jest.fn<(value: boolean) => void>();
const onImported = jest.fn<(value: LocalModel) => void>();

async function openChoices() {
  const view = await render(<HuggingFaceModelDownload runtime="llama.cpp" disabled={false} onBusyChange={busy} onImported={onImported} />);
  await fireEvent.changeText(screen.getByLabelText("Adresse Hugging Face du modèle"), "https://huggingface.co/example/model");
  await fireEvent.press(screen.getByText("Rechercher les modèles"));
  await screen.findByText("Modèle Q4");
  return view;
}

describe("Hugging Face model download", () => {
  beforeEach(() => {
    jest.clearAllMocks();
    jest.mocked(resolveHuggingFaceModels).mockResolvedValue([choice]);
    jest.mocked(downloadHuggingFaceModel).mockResolvedValue(imported);
    jest.mocked(cancelHuggingFaceModelDownload).mockResolvedValue();
    jest.mocked(getModelDownloadProgress).mockResolvedValue({ state: "downloading", downloadedBytes: 512, totalBytes: 1024, completedFiles: 0, totalFiles: 1 });
  });

  it("resolves an address, requires choosing a variant, then selects the imported model", async () => {
    await openChoices();
    expect(resolveHuggingFaceModels).toHaveBeenCalledWith("https://huggingface.co/example/model", "llama.cpp", expect.any(AbortSignal));
    expect(downloadHuggingFaceModel).not.toHaveBeenCalled();
    await fireEvent.press(screen.getByLabelText("Télécharger Modèle Q4"));
    await screen.findByText(/est téléchargé et vérifié/);
    expect(downloadHuggingFaceModel).toHaveBeenCalledWith(choice.plan);
    expect(onImported).toHaveBeenCalledWith(imported);
    expect(busy).toHaveBeenLastCalledWith(false);
  });

  it("shows byte progress and cancels only through the local download path", async () => {
    let rejectDownload!: (reason: Error) => void;
    jest.mocked(downloadHuggingFaceModel).mockReturnValue(new Promise((_, reject) => { rejectDownload = reject; }));
    await openChoices();
    await fireEvent.press(screen.getByLabelText("Télécharger Modèle Q4"));
    await screen.findByText("Téléchargement : 50 %");
    await fireEvent.press(screen.getByText("Annuler le téléchargement Hugging Face"));
    expect(cancelHuggingFaceModelDownload).toHaveBeenCalledTimes(1);
    await act(async () => rejectDownload(new Error("cancelled")));
    await screen.findByText("Téléchargement annulé.");
    expect(onImported).not.toHaveBeenCalled();
    expect(busy).toHaveBeenLastCalledWith(false);
  });

  it("never imports on metadata or checksum failure", async () => {
    jest.mocked(downloadHuggingFaceModel).mockRejectedValue(new Error("Empreinte incorrecte"));
    await openChoices();
    await fireEvent.press(screen.getByLabelText("Télécharger Modèle Q4"));
    await screen.findByText("Empreinte incorrecte");
    expect(onImported).not.toHaveBeenCalled();
    expect(busy).toHaveBeenLastCalledWith(false);
  });

  it("discards choices when the address changes and respects another active operation", async () => {
    await openChoices();
    await fireEvent.changeText(screen.getByLabelText("Adresse Hugging Face du modèle"), "example/other");
    expect(screen.queryByText("Modèle Q4")).toBeNull();
    await screen.rerender(<HuggingFaceModelDownload runtime="llama.cpp" disabled onBusyChange={busy} onImported={onImported} />);
    expect(screen.getByText("Rechercher les modèles")).toBeDisabled();
  });

  it("aborts metadata lookup on navigation without starting a download", async () => {
    let received: AbortSignal | undefined;
    jest.mocked(resolveHuggingFaceModels).mockImplementation((_address, _runtime, signal) => {
      received = signal;
      return new Promise((_, reject) => signal?.addEventListener("abort", () => reject(new Error("aborted"))));
    });
    const view = await render(<HuggingFaceModelDownload runtime="mlx" disabled={false} onBusyChange={busy} onImported={onImported} />);
    await fireEvent.changeText(screen.getByLabelText("Adresse Hugging Face du modèle"), "example/model");
    await fireEvent.press(screen.getByText("Rechercher les modèles"));
    await waitFor(() => expect(received).toBeDefined());
    await view.unmount();
    expect(received?.aborted).toBe(true);
    expect(busy).toHaveBeenLastCalledWith(false);
    expect(downloadHuggingFaceModel).not.toHaveBeenCalled();
    expect(cancelHuggingFaceModelDownload).not.toHaveBeenCalled();
  });

  it("reattaches after navigation without another download and delivers the result only to the current screen", async () => {
    let complete!: (model: LocalModel) => void;
    jest.mocked(downloadHuggingFaceModel).mockReturnValue(new Promise((resolve) => { complete = resolve; }));
    const first = await openChoices();
    await fireEvent.press(screen.getByLabelText("Télécharger Modèle Q4"));
    await first.unmount();
    const nextImported = jest.fn<(value: LocalModel) => void>();
    await render(<HuggingFaceModelDownload runtime="llama.cpp" disabled={false} onBusyChange={busy} onImported={nextImported} />);
    await screen.findByText("Téléchargement : 50 %");
    expect(screen.getByText("Annuler le téléchargement Hugging Face")).toBeEnabled();
    expect(busy).toHaveBeenLastCalledWith(true);
    expect(downloadHuggingFaceModel).toHaveBeenCalledTimes(1);
    await act(async () => complete(imported));
    await waitFor(() => expect(nextImported).toHaveBeenCalledTimes(1));
    expect(onImported).not.toHaveBeenCalled();
    expect(busy).toHaveBeenLastCalledWith(false);
  });

  it("retains a completion while unmounted and reports it exactly once on return", async () => {
    let complete!: (model: LocalModel) => void;
    jest.mocked(downloadHuggingFaceModel).mockReturnValue(new Promise((resolve) => { complete = resolve; }));
    const first = await openChoices();
    await fireEvent.press(screen.getByLabelText("Télécharger Modèle Q4"));
    await first.unmount();
    await act(async () => complete(imported));
    expect(onImported).not.toHaveBeenCalled();
    const second = await render(<HuggingFaceModelDownload runtime="mlx" disabled={false} onBusyChange={busy} onImported={onImported} />);
    await waitFor(() => expect(onImported).toHaveBeenCalledTimes(1));
    expect(onImported).toHaveBeenCalledWith(imported);
    await second.unmount();
    await render(<HuggingFaceModelDownload runtime="mlx" disabled={false} onBusyChange={busy} onImported={onImported} />);
    expect(onImported).toHaveBeenCalledTimes(1);
    expect(downloadHuggingFaceModel).toHaveBeenCalledTimes(1);
  });

  it("cancels the same operation after remount and waits for its original promise", async () => {
    let reject!: (error: Error) => void;
    jest.mocked(downloadHuggingFaceModel).mockReturnValue(new Promise((_, fail) => { reject = fail; }));
    const first = await openChoices();
    await fireEvent.press(screen.getByLabelText("Télécharger Modèle Q4"));
    await first.unmount();
    await render(<HuggingFaceModelDownload runtime="mlx" disabled onBusyChange={busy} onImported={onImported} />);
    await fireEvent.press(screen.getByText("Annuler le téléchargement Hugging Face"));
    await screen.findByText("Annulation demandée…");
    expect(busy).toHaveBeenLastCalledWith(true);
    expect(screen.queryByText("Rechercher les modèles")).toBeNull();
    expect(cancelHuggingFaceModelDownload).toHaveBeenCalledTimes(1);
    await act(async () => reject(new Error("cancelled")));
    await screen.findByText("Téléchargement annulé.");
    expect(busy).toHaveBeenLastCalledWith(false);
    expect(onImported).not.toHaveBeenCalled();
    expect(downloadHuggingFaceModel).toHaveBeenCalledTimes(1);
  });

  it("does not admit a successor or report import while an old cancellation call is unresolved", async () => {
    let complete!: (model: LocalModel) => void;
    let cancelled!: () => void;
    jest.mocked(downloadHuggingFaceModel).mockReturnValue(new Promise((resolve) => { complete = resolve; }));
    jest.mocked(cancelHuggingFaceModelDownload).mockReturnValue(new Promise((resolve) => { cancelled = resolve; }));
    await openChoices();
    await fireEvent.press(screen.getByLabelText("Télécharger Modèle Q4"));
    await fireEvent.press(screen.getByText("Annuler le téléchargement Hugging Face"));
    await act(async () => complete(imported));
    expect(busy).toHaveBeenLastCalledWith(true);
    expect(onImported).not.toHaveBeenCalled();
    expect(screen.getByText("Annuler le téléchargement Hugging Face")).toBeDisabled();
    expect(screen.queryByText("Rechercher les modèles")).toBeNull();
    await act(async () => cancelled());
    await waitFor(() => expect(onImported).toHaveBeenCalledTimes(1));
    expect(busy).toHaveBeenLastCalledWith(false);
    expect(downloadHuggingFaceModel).toHaveBeenCalledTimes(1);
  });

  it("keeps cancellation available during progress and cancellation errors without duplicating a download", async () => {
    let reject!: (error: Error) => void;
    jest.mocked(downloadHuggingFaceModel).mockReturnValue(new Promise((_, fail) => { reject = fail; }));
    jest.mocked(getModelDownloadProgress).mockRejectedValue(new Error("bridge disconnected"));
    jest.mocked(cancelHuggingFaceModelDownload).mockRejectedValueOnce(new Error("Annulation indisponible"));
    await openChoices();
    await fireEvent.press(screen.getByLabelText("Télécharger Modèle Q4"));
    await screen.findByText(/suivi du téléchargement est temporairement indisponible/);
    await fireEvent.press(screen.getByText("Annuler le téléchargement Hugging Face"));
    await screen.findByText("Annulation indisponible");
    expect(busy).toHaveBeenLastCalledWith(true);
    expect(screen.getByText("Annuler le téléchargement Hugging Face")).toBeEnabled();
    await fireEvent.press(screen.getByText("Annuler le téléchargement Hugging Face"));
    await act(async () => reject(new Error("cancelled")));
    expect(cancelHuggingFaceModelDownload).toHaveBeenCalledTimes(2);
    expect(downloadHuggingFaceModel).toHaveBeenCalledTimes(1);
    expect(onImported).not.toHaveBeenCalled();
  });

  it.each([false, true])("successful import supersedes a cancellation error (error after success: %s)", async (afterSuccess) => {
    let complete!: (model: LocalModel) => void;
    let failCancel!: (error: Error) => void;
    jest.mocked(downloadHuggingFaceModel).mockReturnValue(new Promise((resolve) => { complete = resolve; }));
    jest.mocked(cancelHuggingFaceModelDownload).mockReturnValue(new Promise((_, reject) => { failCancel = reject; }));
    await openChoices();
    await fireEvent.press(screen.getByLabelText("Télécharger Modèle Q4"));
    await fireEvent.press(screen.getByText("Annuler le téléchargement Hugging Face"));
    if (afterSuccess) await act(async () => complete(imported));
    await act(async () => failCancel(new Error("Annulation indisponible")));
    if (!afterSuccess) {
      await screen.findByText("Annulation indisponible");
      await act(async () => complete(imported));
    }
    await waitFor(() => expect(onImported).toHaveBeenCalledTimes(1));
    expect(screen.queryByText("Annulation indisponible")).toBeNull();
    expect(busy).toHaveBeenLastCalledWith(false);
  });

  it("does not let a late progress read resurrect an operation that has completed", async () => {
    let complete!: (model: LocalModel) => void;
    let lateProgress!: (progress: Awaited<ReturnType<typeof getModelDownloadProgress>>) => void;
    jest.mocked(downloadHuggingFaceModel).mockReturnValue(new Promise((resolve) => { complete = resolve; }));
    jest.mocked(getModelDownloadProgress).mockReturnValue(new Promise((resolve) => { lateProgress = resolve; }));
    await openChoices();
    await fireEvent.press(screen.getByLabelText("Télécharger Modèle Q4"));
    await waitFor(() => expect(getModelDownloadProgress).toHaveBeenCalled());
    await act(async () => complete(imported));
    await waitFor(() => expect(onImported).toHaveBeenCalledTimes(1));
    await act(async () => lateProgress({ state: "downloading", downloadedBytes: 512, totalBytes: 1024, completedFiles: 0, totalFiles: 1 }));
    expect(screen.queryByText("Annuler le téléchargement Hugging Face")).toBeNull();
    expect(busy).toHaveBeenLastCalledWith(false);
    expect(onImported).toHaveBeenCalledTimes(1);
  });

  it("waits for the original import promise even when native progress reports completed", async () => {
    let complete!: (model: LocalModel) => void;
    jest.mocked(downloadHuggingFaceModel).mockReturnValue(new Promise((resolve) => { complete = resolve; }));
    jest.mocked(getModelDownloadProgress).mockResolvedValue({ state: "completed", downloadedBytes: 1024, totalBytes: 1024, completedFiles: 1, totalFiles: 1 });
    await openChoices();
    await fireEvent.press(screen.getByLabelText("Télécharger Modèle Q4"));
    await screen.findByText("Téléchargement : 100 %");
    expect(onImported).not.toHaveBeenCalled();
    expect(busy).toHaveBeenLastCalledWith(true);
    await act(async () => complete(imported));
    await waitFor(() => expect(onImported).toHaveBeenCalledTimes(1));
  });

  it("does not claim another native operation from identity-free progress when disabled by a preset", async () => {
    await render(<HuggingFaceModelDownload runtime="llama.cpp" disabled onBusyChange={busy} onImported={onImported} />);
    expect(getModelDownloadProgress).not.toHaveBeenCalled();
    expect(screen.queryByText("Annuler le téléchargement Hugging Face")).toBeNull();
    expect(screen.getByText("Rechercher les modèles")).toBeDisabled();
    expect(downloadHuggingFaceModel).not.toHaveBeenCalled();
    expect(cancelHuggingFaceModelDownload).not.toHaveBeenCalled();
    expect(onImported).not.toHaveBeenCalled();
  });
});
