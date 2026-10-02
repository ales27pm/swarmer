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
  await render(<HuggingFaceModelDownload runtime="llama.cpp" disabled={false} onBusyChange={busy} onImported={onImported} />);
  await fireEvent.changeText(screen.getByLabelText("Adresse Hugging Face du modèle"), "https://huggingface.co/example/model");
  await fireEvent.press(screen.getByText("Rechercher les modèles"));
  await screen.findByText("Modèle Q4");
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
});
