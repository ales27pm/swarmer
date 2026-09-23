import { act, fireEvent, render, screen, userEvent, waitFor } from "@testing-library/react-native";
import { beforeEach, describe, expect, it, jest } from "@jest/globals";

import LocalModelScreen from "@/../app/local-model";
import { sendChat, submitToolProposal, type Task } from "@/lib/api/client";
import {
  cancelLocalGeneration,
  cancelLocalModelDownload,
  downloadLocalGgufModel,
  generateLocalProposal,
  getLocalInferenceCapabilities,
  getLocalInferenceStatus,
  importLocalModel,
  isLocalInferenceAvailable,
  listLocalModels,
  loadLocalModel,
  pickAndImportLocalModelDirectory,
  unloadLocalModel,
} from "@/lib/local-inference";

import { LOCAL_MODEL_PRESETS } from "@/lib/local-model-presets";
import { readLocalModelSettings, saveLocalModelSettings } from "@/lib/local-model-settings";
import { applicationApi } from "@/lib/application-api/registry";

jest.mock("@/lib/local-model-settings", () => ({
  ...jest.requireActual<typeof import("@/lib/local-model-settings")>("@/lib/local-model-settings"),
  readLocalModelSettings: jest.fn(),
  saveLocalModelSettings: jest.fn(),
}));

const mockPush = jest.fn();

jest.mock("expo-router", () => ({ useRouter: () => ({ push: mockPush }), useLocalSearchParams: () => ({}), useFocusEffect: (effect: () => void) => jest.requireActual<typeof import("react")>("react").useEffect(effect, [effect]) }));
jest.mock("expo-document-picker", () => ({ getDocumentAsync: jest.fn() }));
jest.mock("@/lib/api/client", () => {
  const sendChat = jest.fn<(...args: unknown[]) => Promise<{ task: Task | null }>>();
  const submitToolProposal = jest.fn();
  return {
    sendChat, submitToolProposal,
    createLocalToolSubmissionSession: async () => ({
      createTask: async (intent: string, onTaskCreated?: (task: Task) => void) => {
        const chat = await sendChat(intent, undefined, "normal", true);
        if (chat.task) onTaskCreated?.(chat.task);
        return chat;
      },
      submit: (id: string, proposal: unknown) => submitToolProposal(id, proposal),
    }),
  };
});
jest.mock("@/lib/local-inference", () => {
  const actual = jest.requireActual<typeof import("@/lib/local-inference")>(
    "@/lib/local-inference",
  );
  return {
    ...actual,
    cancelLocalGeneration: jest.fn(),
    cancelLocalModelDownload: jest.fn(),
    downloadLocalGgufModel: jest.fn(),
    generateLocalProposal: jest.fn(),
    getLocalInferenceCapabilities: jest.fn(),
    getLocalInferenceStatus: jest.fn(),
    importLocalModel: jest.fn(),
    isLocalInferenceAvailable: jest.fn(),
    listLocalModels: jest.fn(),
    loadLocalModel: jest.fn(),
    pickAndImportLocalModelDirectory: jest.fn(),
    unloadLocalModel: jest.fn(),
  };
});

const task: Task = {
  id: "tsk_local",
  title: "Inspecter le projet",
  input: "Inspecter le projet",
  mode: "normal",
  source: "iphone-test",
  conversation_id: "conv_local",
  status: "created",
  priority: 0,
  created_at: "2026-09-07T12:00:00Z",
  updated_at: "2026-09-07T12:00:00Z",
  completed_at: null,
  error_json: null,
};

const mockGenerate = jest.mocked(generateLocalProposal);
const mockCancel = jest.mocked(cancelLocalGeneration);
const mockCapabilities = jest.mocked(getLocalInferenceCapabilities);
const mockIsAvailable = jest.mocked(isLocalInferenceAvailable);
const mockListModels = jest.mocked(listLocalModels);
const mockImport = jest.mocked(importLocalModel);
const mockLoad = jest.mocked(loadLocalModel);
const mockPickAndImportDirectory = jest.mocked(pickAndImportLocalModelDirectory);
const mockUnload = jest.mocked(unloadLocalModel);
const mockSendChat = jest.mocked(sendChat);
const mockSubmit = jest.mocked(submitToolProposal);

async function prepareMlxModel(user: ReturnType<typeof userEvent.setup>) {
  await screen.findByText(/Choisis un modèle local/);
  await user.press(screen.getByRole("button", { name: "Runtime MLX" }));
  await fireEvent.changeText(screen.getByLabelText("Dépôt Hugging Face"), "mlx-community/test-model");
  await fireEvent.changeText(screen.getByLabelText("Révision Hugging Face immuable"), "");
  await fireEvent.changeText(
    screen.getByLabelText("Révision Hugging Face immuable"),
    "a".repeat(40),
  );
  await user.press(screen.getByRole("button", { name: "Charger le modèle" }));
  expect(await screen.findByText(/Modèle chargé localement/)).toBeOnTheScreen();
}

describe("LocalModelScreen", () => {
  beforeEach(() => {
    jest.clearAllMocks();
    jest.mocked(readLocalModelSettings).mockResolvedValue(null);
    jest.mocked(saveLocalModelSettings).mockResolvedValue();
    jest.mocked(cancelLocalModelDownload).mockResolvedValue();
    mockIsAvailable.mockReturnValue(true);
    mockCapabilities.mockResolvedValue({
      coreml: true,
      mlx: true,
      llamaCpp: true,
      platform: "ios",
    });
    mockListModels.mockResolvedValue([]);
    jest.mocked(getLocalInferenceStatus).mockImplementation(async () => {
      const latest = mockLoad.mock.results.at(-1);
      return latest?.type === "return" ? await latest.value : { state: "idle", runtime: null, modelId: null, revision: null };
    });
    mockImport.mockResolvedValue({
      modelId: "local_import",
      runtime: "coreml",
      displayName: "CoreMLBundle",
      source: "CoreMLBundle",
      sizeBytes: 1_024,
      importedAt: "2026-09-08T00:00:00Z",
    });
    mockPickAndImportDirectory.mockResolvedValue({
      modelId: "local_import",
      runtime: "coreml",
      displayName: "CoreMLBundle",
      source: "CoreMLBundle",
      sizeBytes: 1_024,
      importedAt: "2026-09-08T00:00:00Z",
    });
    mockCancel.mockResolvedValue();
    mockLoad.mockResolvedValue({
      state: "ready",
      runtime: "mlx",
      modelId: "mlx-community/test-model",
      revision: "a".repeat(40),
    });
    mockSendChat.mockResolvedValue({ conversation_id: "conv_local", task });
    mockSubmit.mockResolvedValue({ id: "call_local" } as never);
    mockUnload.mockResolvedValue();
  });

  it("prefills pinned Dolphin MLX without loading or downloading automatically", async () => {
    await render(<LocalModelScreen />);
    await screen.findByText(/Choisis un modèle local/);
    expect(screen.getByLabelText("Dépôt Hugging Face")).toHaveDisplayValue(LOCAL_MODEL_PRESETS.mlx.repoId);
    expect(screen.getByLabelText("Révision Hugging Face immuable")).toHaveDisplayValue(LOCAL_MODEL_PRESETS.mlx.revision);
    expect(screen.getByRole("button", { name: "Charger le modèle" })).toBeEnabled();
    expect(mockLoad).not.toHaveBeenCalled();
    expect(downloadLocalGgufModel).not.toHaveBeenCalled();
  });

  it("keeps embeddings out of generation choices and clears a saved embedding selection", async () => {
    mockListModels.mockResolvedValue([{
      modelId: "durable_e5", runtime: "mlx", displayName: "E5 local", purpose: "embeddings",
      source: "Documents/Models · intfloat/multilingual-e5-small@" + "a".repeat(40),
      sizeBytes: 400_000_000, importedAt: "2026-09-22T00:00:00Z",
    }]);
    jest.mocked(readLocalModelSettings).mockResolvedValue({
      runtime: "mlx", modelId: "durable_e5", revision: "", maxTokens: 128, temperature: 0.2,
    });
    await render(<LocalModelScreen />);
    await screen.findByText(/Choisis un modèle local/);
    expect(screen.queryByRole("button", { name: "Choisir E5 local" })).toBeNull();
    expect(screen.getByLabelText("Dépôt Hugging Face")).toHaveDisplayValue(LOCAL_MODEL_PRESETS.mlx.repoId);
    expect(mockLoad).not.toHaveBeenCalled();
  });

  it("does not cancel or unload an API generation when an unused screen unmounts", async () => {
    let finish!: (value: Awaited<ReturnType<typeof generateLocalProposal>>) => void;
    mockGenerate.mockImplementation(() => new Promise((resolve) => { finish = resolve; }));
    const pending = applicationApi.execute("inference.generate", { prompt: "API" });
    await render(<LocalModelScreen />);
    await screen.findByText(/Choisis un modèle local/);
    await screen.unmount();
    expect(mockCancel).not.toHaveBeenCalled();
    expect(mockUnload).not.toHaveBeenCalled();
    finish({ text: "API_OK", tokenCount: 2, finishReason: "stop" });
    await pending;
  });

  it("restores the actual API-loaded model instead of saved selections", async () => {
    jest.mocked(getLocalInferenceStatus).mockResolvedValue({ state: "ready", runtime: "mlx", modelId: "actual/dolphin", revision: "b".repeat(40) });
    await render(<LocalModelScreen />);
    await screen.findByText(/Le modèle de l’app est déjà chargé/);
    expect(screen.getByLabelText("Dépôt Hugging Face")).toHaveDisplayValue("actual/dolphin");
    expect(screen.getByLabelText("Révision Hugging Face immuable")).toHaveDisplayValue("b".repeat(40));
    expect(screen.getByRole("button", { name: "Charger le modèle" })).toBeDisabled();
    await screen.unmount();
    expect(mockLoad).not.toHaveBeenCalled();
    expect(mockUnload).not.toHaveBeenCalled();
  });

  it("refreshes global model state while the screen is visible after an API unload", async () => {
    jest.mocked(getLocalInferenceStatus).mockResolvedValue({ state: "ready", runtime: "mlx", modelId: "actual/dolphin", revision: "b".repeat(40) });
    await render(<LocalModelScreen />);
    await screen.findByText(/Le modèle de l’app est déjà chargé/);
    expect(screen.getByRole("button", { name: "Charger le modèle" })).toBeDisabled();
    jest.mocked(getLocalInferenceStatus).mockResolvedValue({ state: "idle", runtime: null, modelId: null, revision: null });
    await waitFor(() => expect(screen.getByRole("button", { name: "Charger le modèle" })).toBeEnabled(), { timeout: 3_000 });
    expect(mockUnload).not.toHaveBeenCalled();
  });

  it.each([
    { supported: false, osSupported: true, gpuSupported: false, entitlementGranted: null, active: false, operationId: null,
      executionDevice: null, outputBytes: 0, state: "idle", reason: "gpu_unsupported", expected: "Cette version du module ne propose pas de continuation sur cet appareil." },
    { supported: true, osSupported: true, gpuSupported: true, entitlementGranted: null, active: false, operationId: null,
      executionDevice: null, outputBytes: 0, state: "idle", reason: "permission_unverified", expected: "l’autorisation n’a pas encore été confirmée" },
    { supported: true, osSupported: true, gpuSupported: true, entitlementGranted: true, active: true, operationId: "operation-1",
      executionDevice: "gpu", outputBytes: 128, state: "active", reason: null, expected: "Tâche GPU admise par iOS." },
    { supported: true, osSupported: true, gpuSupported: true, entitlementGranted: true, active: false, operationId: "operation-1",
      executionDevice: "gpu", outputBytes: 128, state: "expiring", reason: "user_or_system_cancelled", expected: "Arrêt demandé par le système ou l’utilisateur." },
    { supported: true, osSupported: true, gpuSupported: false, entitlementGranted: null, active: false, operationId: null,
      executionDevice: null, outputBytes: 0, state: "idle", reason: "cpu_fallback", expected: "Le repli CPU est disponible." },
    { supported: true, osSupported: true, gpuSupported: false, entitlementGranted: false, active: false, operationId: null,
      executionDevice: "cpu", outputBytes: 0, state: "idle", reason: "cpu_fallback", expected: "Le repli CPU est disponible." },
    { supported: true, osSupported: true, gpuSupported: false, entitlementGranted: null, active: true, operationId: "cpu-1",
      executionDevice: "cpu", outputBytes: 64, state: "active", reason: "cpu_fallback", expected: "Tâche CPU admise par iOS." },
  ] as const)("shows truthful MLX background state: $reason / $state", async ({ expected, ...backgroundExecution }) => {
    jest.mocked(getLocalInferenceStatus).mockResolvedValue({ state: "ready", runtime: "mlx", modelId: "actual/dolphin", revision: "b".repeat(40), backgroundExecution });
    await render(<LocalModelScreen />);
    await screen.findByText(/Le modèle de l’app est déjà chargé/);
    expect(screen.getByTestId("local-model-background-status")).toBeOnTheScreen();
    expect(screen.getByText(expected, { exact: false })).toBeOnTheScreen();
    expect(screen.getByText(`Activité : ${backgroundExecution.active ? "tâche admise en cours" : "aucune tâche admise active"}`)).toBeOnTheScreen();
    if (backgroundExecution.supported && !backgroundExecution.gpuSupported) {
      expect(screen.getByText(/Calcul local sur CPU : il peut être plus lent que sur GPU/)).toBeOnTheScreen();
      expect(screen.queryByText(/Une admission GPU a déjà été confirmée/)).not.toBeOnTheScreen();
    }
    expect(screen.getByText(/uniquement de suivre ou d’annuler le calcul déjà admis/)).toBeOnTheScreen();
    if (backgroundExecution.entitlementGranted === true || backgroundExecution.active || backgroundExecution.outputBytes > 0) {
      expect(screen.getByText(`Texte produit par la tâche d’arrière-plan : ${backgroundExecution.outputBytes} octets UTF-8`, { exact: false })).toBeOnTheScreen();
    } else {
      expect(screen.queryByText(/Texte produit par la tâche d’arrière-plan/)).not.toBeOnTheScreen();
    }
    expect(mockGenerate).not.toHaveBeenCalled();
    expect(mockLoad).not.toHaveBeenCalled();
  });

  it("cancels only its own generation on unmount and retains the loaded model", async () => {
    let finish!: (value: Awaited<ReturnType<typeof generateLocalProposal>>) => void;
    mockGenerate.mockImplementation(() => new Promise((resolve) => { finish = resolve; }));
    const user = userEvent.setup();
    await render(<LocalModelScreen />);
    await prepareMlxModel(user);
    await user.type(screen.getByLabelText("Intention pour le modèle local"), "Test");
    await user.press(screen.getByRole("button", { name: "Générer une proposition locale" }));
    await waitFor(() => expect(mockGenerate).toHaveBeenCalledTimes(1));
    await screen.unmount();
    expect(mockCancel).toHaveBeenCalledTimes(1);
    expect(mockUnload).not.toHaveBeenCalled();
    await act(async () => finish({ text: "", tokenCount: 0, finishReason: "cancelled" }));
  });

  it("loads the pinned default only after an explicit load", async () => {
    const preset = LOCAL_MODEL_PRESETS.mlx;
    mockLoad.mockResolvedValue({ state: "ready", runtime: "mlx", modelId: preset.repoId, revision: preset.revision });
    const user = userEvent.setup();
    await render(<LocalModelScreen />);
    await screen.findByText(/Choisis un modèle local/);
    await user.press(screen.getByRole("button", { name: "Charger le modèle" }));
    expect(await screen.findByText(/Modèle chargé localement/)).toBeOnTheScreen();
    expect(mockLoad).toHaveBeenCalledWith({ runtime: "mlx", modelId: preset.repoId, revision: preset.revision });
  });

  it("shows the durable Files copy created by a successful MLX load", async () => {
    const preset = LOCAL_MODEL_PRESETS.mlx;
    mockLoad.mockResolvedValue({ state: "ready", runtime: "mlx", modelId: preset.repoId, revision: preset.revision });
    mockListModels.mockResolvedValueOnce([]).mockResolvedValueOnce([{
      modelId: "durable_mlx", runtime: "mlx", displayName: "Dolphin · Modèles",
      source: `Documents/Models · ${preset.repoId}@${preset.revision}`,
      sizeBytes: 1_824_808_562, importedAt: "2026-09-13T22:00:00Z",
    }]);
    const user = userEvent.setup();
    await render(<LocalModelScreen />);
    await screen.findByText(/Choisis un modèle local/);
    await user.press(screen.getByRole("button", { name: "Charger le modèle" }));
    expect(await screen.findByText(/Conservé dans Fichiers/)).toBeOnTheScreen();
    expect(screen.getByRole("button", { name: "Décharger le modèle" })).toBeEnabled();
    expect(screen.getByLabelText("Dépôt Hugging Face")).toHaveDisplayValue(preset.repoId);
  });

  it("keeps a confirmed model ready if refreshing its Files listing fails", async () => {
    const preset = LOCAL_MODEL_PRESETS.mlx;
    mockLoad.mockResolvedValue({ state: "ready", runtime: "mlx", modelId: preset.repoId, revision: preset.revision });
    mockListModels.mockResolvedValueOnce([]).mockRejectedValueOnce(new Error("Listing unavailable"));
    const user = userEvent.setup();
    await render(<LocalModelScreen />);
    await screen.findByText(/Choisis un modèle local/);
    await user.press(screen.getByRole("button", { name: "Charger le modèle" }));
    expect(await screen.findByText(/Modèle chargé localement/)).toBeOnTheScreen();
    expect(screen.getByRole("button", { name: "Décharger le modèle" })).toBeEnabled();
    expect(screen.queryByText(/Conservé dans Fichiers/)).not.toBeOnTheScreen();
  });

  it("keeps the native runtimes usable if saved settings cannot be read", async () => {
    jest.mocked(readLocalModelSettings).mockRejectedValue(new Error("Storage unavailable"));
    await render(<LocalModelScreen />);
    await screen.findByText(/Choisis un modèle local/);
    expect(screen.getByText(/Les réglages enregistrés n’ont pas pu être lus/)).toBeOnTheScreen();
    expect(screen.getByRole("button", { name: "Charger le modèle" })).toBeEnabled();
    expect(mockLoad).not.toHaveBeenCalled();
  });

  it("keeps a custom model when switching away and back to its runtime", async () => {
    jest.mocked(readLocalModelSettings).mockResolvedValue({
      runtime: "mlx", modelId: "owner/custom", revision: "b".repeat(40), maxTokens: 128, temperature: 0.2,
    });
    const user = userEvent.setup();
    await render(<LocalModelScreen />);
    await screen.findByText(/Choisis un modèle local/);
    await user.press(screen.getByRole("button", { name: "Runtime Core ML" }));
    await user.press(screen.getByRole("button", { name: "Runtime MLX" }));
    expect(screen.getByLabelText("Dépôt Hugging Face")).toHaveDisplayValue("owner/custom");
    expect(screen.getByLabelText("Révision Hugging Face immuable")).toHaveDisplayValue("b".repeat(40));
    expect(mockLoad).not.toHaveBeenCalled();
  });

  it("restores custom settings and passes edited sampling limits into generation", async () => {
    jest.mocked(readLocalModelSettings).mockResolvedValue({
      runtime: "mlx", modelId: "mlx-community/test-model", revision: "a".repeat(40), maxTokens: 128, temperature: 0,
    });
    mockGenerate.mockResolvedValue({ text: '{"tool_name":"none","arguments":{},"summary":"Prêt"}', finishReason: "stop", tokenCount: 20 });
    const user = userEvent.setup();
    await render(<LocalModelScreen />);
    await screen.findByText(/Choisis un modèle local/);
    expect(screen.getByLabelText("Dépôt Hugging Face")).toHaveDisplayValue("mlx-community/test-model");
    await user.press(screen.getByRole("button", { name: "Charger le modèle" }));
    await screen.findByText(/Modèle chargé localement/);
    await user.type(screen.getByLabelText("Intention pour le modèle local"), "Bonjour");
    await user.press(screen.getByRole("button", { name: "Générer une proposition locale" }));
    await screen.findByText("Prêt");
    expect(mockGenerate).toHaveBeenCalledWith(expect.objectContaining({ maxTokens: 128, temperature: 0 }));
    await user.press(screen.getByRole("button", { name: "Enregistrer les réglages" }));
    await screen.findByText(/Réglages enregistrés/);
    expect(saveLocalModelSettings).toHaveBeenCalledWith(expect.objectContaining({ modelId: "mlx-community/test-model", maxTokens: 128, temperature: 0 }));
  });

  it("downloads and selects GGUF with its exact pin and checksum before loading", async () => {
    jest.mocked(downloadLocalGgufModel).mockResolvedValue({
      modelId: "local_dolphin", runtime: "llama.cpp", displayName: "Dolphin GGUF", source: "Dolphin.gguf", sizeBytes: 2_019_382_400, importedAt: "2026-09-12T12:00:00Z",
    });
    const user = userEvent.setup();
    await render(<LocalModelScreen />);
    await screen.findByText(/Choisis un modèle local/);
    await user.press(screen.getByRole("button", { name: "Runtime llama.cpp" }));
    expect(screen.getByRole("button", { name: "Charger le modèle" })).toBeDisabled();
    await user.press(screen.getByRole("button", { name: "Télécharger Dolphin GGUF · 2,02 Go" }));
    await screen.findByText(/Dolphin GGUF est téléchargé et vérifié/);
    expect(downloadLocalGgufModel).toHaveBeenCalledWith(LOCAL_MODEL_PRESETS["llama.cpp"].download);
    expect(screen.getByRole("button", { name: "Charger le modèle" })).toBeEnabled();
    expect(mockLoad).not.toHaveBeenCalled();
    expect(mockSendChat).not.toHaveBeenCalled();
  });

  it("ignores a late GGUF result after download cancellation", async () => {
    let completeDownload!: (value: Awaited<ReturnType<typeof downloadLocalGgufModel>>) => void;
    jest.mocked(downloadLocalGgufModel).mockImplementation(() => new Promise((resolve) => { completeDownload = resolve; }));
    const user = userEvent.setup();
    await render(<LocalModelScreen />);
    await screen.findByText(/Choisis un modèle local/);
    await user.press(screen.getByRole("button", { name: "Runtime llama.cpp" }));
    await user.press(screen.getByRole("button", { name: "Télécharger Dolphin GGUF · 2,02 Go" }));
    await user.press(screen.getByRole("button", { name: "Annuler le téléchargement" }));
    await screen.findByText(/Annulation du téléchargement demandée/);
    await act(async () => completeDownload({
      modelId: "late_dolphin", runtime: "llama.cpp", displayName: "Late Dolphin", source: "Dolphin.gguf", sizeBytes: 2_019_382_400, importedAt: "2026-09-12T12:00:00Z",
    }));
    expect(cancelLocalModelDownload).toHaveBeenCalledTimes(1);
    expect(screen.getByRole("button", { name: "Charger le modèle" })).toBeDisabled();
    expect(screen.queryByText("Late Dolphin")).not.toBeOnTheScreen();
    expect(mockLoad).not.toHaveBeenCalled();
    expect(mockSendChat).not.toHaveBeenCalled();
  });

  it("imports a Core ML folder so tokenizer sidecars remain in the same payload", async () => {
    const user = userEvent.setup();
    await render(<LocalModelScreen />);

    await screen.findByText(/Choisis un modèle local/);
    await user.press(screen.getByRole("button", { name: "Runtime Core ML" }));
    await user.press(screen.getByRole("button", { name: "Importer un dossier Core ML" }));

    await waitFor(() =>
      expect(mockPickAndImportDirectory).toHaveBeenCalledWith("coreml"),
    );
    expect(await screen.findByText(/copié dans le stockage privé/)).toBeOnTheScreen();
  });

  it("exposes local MLX folder import alongside immutable Hub loading", async () => {
    mockPickAndImportDirectory.mockResolvedValue({
      modelId: "local_mlx",
      runtime: "mlx",
      displayName: "LocalMLX",
      source: "LocalMLX",
      sizeBytes: 2_048,
      importedAt: "2026-09-08T00:00:00Z",
    });
    const user = userEvent.setup();
    await render(<LocalModelScreen />);

    await screen.findByText(/Choisis un modèle local/);
    await user.press(screen.getByRole("button", { name: "Runtime MLX" }));
    await user.press(screen.getByRole("button", { name: "Importer un dossier MLX local" }));

    await waitFor(() =>
      expect(mockPickAndImportDirectory).toHaveBeenCalledWith("mlx"),
    );
  });

  it("fails closed when the native module is absent", async () => {
    mockIsAvailable.mockReturnValue(false);
    await render(<LocalModelScreen />);

    expect(
      screen.getByText("Le module natif n’est pas présent dans cette version de l’app."),
    ).toBeOnTheScreen();
    expect(screen.getByRole("button", { name: "Runtime Core ML" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Charger le modèle" })).toBeDisabled();
    expect(mockCapabilities).not.toHaveBeenCalled();
    expect(mockListModels).not.toHaveBeenCalled();
  });

  it("requires an immutable MLX revision and explicitly submits an actionable proposal", async () => {
    const proposalText = JSON.stringify({
      tool_name: "workspace.read_text",
      arguments: { path: "README.md" },
      summary: "Lire le fichier de présentation",
    });
    mockGenerate.mockResolvedValue({
      text: proposalText,
      finishReason: "stop",
      tokenCount: 28,
    });
    const user = userEvent.setup();
    await render(<LocalModelScreen />);

    await screen.findByText(/Choisis un modèle local/);
    await user.press(screen.getByRole("button", { name: "Runtime MLX" }));
    await fireEvent.changeText(screen.getByLabelText("Dépôt Hugging Face"), "mlx-community/test-model");
    await fireEvent.changeText(screen.getByLabelText("Révision Hugging Face immuable"), "");
    await user.type(screen.getByLabelText("Révision Hugging Face immuable"), "main");
    expect(screen.getByRole("button", { name: "Charger le modèle" })).toBeDisabled();
    expect(screen.getByText(/Une branche ou une étiquette mobile est refusée/)).toBeOnTheScreen();
    await user.clear(screen.getByLabelText("Révision Hugging Face immuable"));
    await fireEvent.changeText(
      screen.getByLabelText("Révision Hugging Face immuable"),
      "a".repeat(40),
    );
    await user.press(screen.getByRole("button", { name: "Charger le modèle" }));
    expect(await screen.findByText(/Modèle chargé localement/)).toBeOnTheScreen();

    await user.type(
      screen.getByLabelText("Intention pour le modèle local"),
      "Inspecter le projet",
    );
    await user.press(
      screen.getByRole("button", { name: "Générer une proposition locale" }),
    );

    expect(await screen.findByText("Lire le fichier de présentation")).toBeOnTheScreen();
    expect(
      screen.getByText("Proposition locale — non vérifiée et non exécutée"),
    ).toBeOnTheScreen();
    await user.press(screen.getByRole("button", { name: "Soumettre au control plane" }));

    await waitFor(() => expect(mockSubmit).toHaveBeenCalledTimes(1));
    expect(mockSendChat).toHaveBeenCalledWith("Inspecter le projet", undefined, "normal", true);
    expect(mockSubmit).toHaveBeenCalledWith(task.id, {
      tool_name: "workspace.read_text",
      arguments: { path: "README.md" },
      summary: "Lire le fichier de présentation",
    });
    expect(mockPush).toHaveBeenCalledWith({
      pathname: "/task/[id]",
      params: { id: task.id },
    });
  });

  it("never offers submission for a none proposal", async () => {
    mockGenerate.mockResolvedValue({
      text: '{"tool_name":"none","arguments":{},"summary":"Aucun outil sûr"}',
      finishReason: "stop",
      tokenCount: 12,
    });
    const user = userEvent.setup();
    await render(<LocalModelScreen />);
    await prepareMlxModel(user);

    await user.type(screen.getByLabelText("Intention pour le modèle local"), "Réponds localement");
    await user.press(
      screen.getByRole("button", { name: "Générer une proposition locale" }),
    );

    expect(await screen.findByText("Aucun outil sûr")).toBeOnTheScreen();
    expect(
      screen.queryByRole("button", { name: "Soumettre au control plane" }),
    ).not.toBeOnTheScreen();
    expect(mockSendChat).not.toHaveBeenCalled();
    expect(mockSubmit).not.toHaveBeenCalled();
  });

  it("does not enable generation for a mismatched native MLX revision", async () => {
    mockLoad.mockResolvedValue({
      state: "ready",
      runtime: "mlx",
      modelId: "mlx-community/test-model",
      revision: "b".repeat(40),
    });
    const user = userEvent.setup();
    await render(<LocalModelScreen />);

    await screen.findByText(/Choisis un modèle local/);
    await user.press(screen.getByRole("button", { name: "Runtime MLX" }));
    await fireEvent.changeText(screen.getByLabelText("Dépôt Hugging Face"), "mlx-community/test-model");
    await fireEvent.changeText(screen.getByLabelText("Révision Hugging Face immuable"), "");
    await fireEvent.changeText(
      screen.getByLabelText("Révision Hugging Face immuable"),
      "a".repeat(40),
    );
    await user.press(screen.getByRole("button", { name: "Charger le modèle" }));

    expect(
      await screen.findByText("Le runtime natif n’a pas confirmé le modèle exact demandé."),
    ).toBeOnTheScreen();
    expect(
      screen.getByRole("button", { name: "Générer une proposition locale" }),
    ).toBeDisabled();
  });

  it("keeps a model whose load resolves after the screen unmounts", async () => {
    let resolveLoad!: (value: Awaited<ReturnType<typeof loadLocalModel>>) => void;
    mockLoad.mockImplementation(
      () =>
        new Promise((resolve) => {
          resolveLoad = resolve;
        }),
    );
    const user = userEvent.setup();
    await render(<LocalModelScreen />);

    await screen.findByText(/Choisis un modèle local/);
    await user.press(screen.getByRole("button", { name: "Runtime MLX" }));
    await fireEvent.changeText(screen.getByLabelText("Dépôt Hugging Face"), "mlx-community/test-model");
    await fireEvent.changeText(screen.getByLabelText("Révision Hugging Face immuable"), "");
    await fireEvent.changeText(
      screen.getByLabelText("Révision Hugging Face immuable"),
      "a".repeat(40),
    );
    await user.press(screen.getByRole("button", { name: "Charger le modèle" }));
    await waitFor(() => expect(mockLoad).toHaveBeenCalledTimes(1));
    await screen.unmount();

    await act(async () => resolveLoad({
      state: "ready",
      runtime: "mlx",
      modelId: "mlx-community/test-model",
      revision: "a".repeat(40),
    }));

    expect(mockUnload).not.toHaveBeenCalled();
    expect(mockCancel).not.toHaveBeenCalled();
  });

  it("never submits a token-limit-truncated JSON object", async () => {
    mockGenerate.mockResolvedValue({
      text: '{"tool_name":"workspace.list_dir","arguments":{"path":"."},"summary":"List"}',
      finishReason: "length",
      tokenCount: 512,
    });
    const user = userEvent.setup();
    await render(<LocalModelScreen />);
    await prepareMlxModel(user);

    await user.type(screen.getByLabelText("Intention pour le modèle local"), "Inspecter");
    await user.press(
      screen.getByRole("button", { name: "Générer une proposition locale" }),
    );

    expect(await screen.findByText(/sortie incomplète ne peut pas être soumise/)).toBeOnTheScreen();
    expect(
      screen.queryByRole("button", { name: "Soumettre au control plane" }),
    ).not.toBeOnTheScreen();
    expect(mockSendChat).not.toHaveBeenCalled();
    expect(mockSubmit).not.toHaveBeenCalled();
  });

  it("rejects simulated completion claims before creating a task", async () => {
    mockGenerate.mockResolvedValue({
      text: '{"tool_name":"workspace.list_dir","arguments":{"path":"."},"summary":"Done","completed":true}',
      finishReason: "stop",
      tokenCount: 15,
    });
    const user = userEvent.setup();
    await render(<LocalModelScreen />);
    await prepareMlxModel(user);

    await user.type(screen.getByLabelText("Intention pour le modèle local"), "Inspecter");
    await user.press(
      screen.getByRole("button", { name: "Générer une proposition locale" }),
    );

    expect(
      await screen.findByText("La proposition locale ne respecte pas l’enveloppe attendue."),
    ).toBeOnTheScreen();
    expect(screen.getByText(/aucune tâche ni exécution n’a été créée/i)).toBeOnTheScreen();
    expect(mockSendChat).not.toHaveBeenCalled();
    expect(mockSubmit).not.toHaveBeenCalled();
  });

  it("routes to an already-created task when proposal acceptance is uncertain", async () => {
    mockGenerate.mockResolvedValue({
      text: '{"tool_name":"workspace.read_text","arguments":{"path":"README.md"},"summary":"Lire"}',
      finishReason: "stop",
      tokenCount: 14,
    });
    mockSubmit.mockRejectedValue(new Error("Réponse réseau perdue"));
    const user = userEvent.setup();
    await render(<LocalModelScreen />);
    await prepareMlxModel(user);

    await user.type(screen.getByLabelText("Intention pour le modèle local"), "Inspecter");
    await user.press(
      screen.getByRole("button", { name: "Générer une proposition locale" }),
    );
    await screen.findByText("Lire");
    await user.press(screen.getByRole("button", { name: "Soumettre au control plane" }));

    expect(await screen.findByText(/l’état de la proposition doit être vérifié/)).toBeOnTheScreen();
    expect(mockPush).toHaveBeenCalledWith({
      pathname: "/task/[id]",
      params: { id: task.id },
    });
    expect(
      screen.queryByRole("button", { name: "Soumettre au control plane" }),
    ).not.toBeOnTheScreen();
  });

  it("invalidates a proposal when authenticated task creation is uncertain", async () => {
    mockGenerate.mockResolvedValue({
      text: '{"tool_name":"workspace.read_text","arguments":{"path":"README.md"},"summary":"Lire"}',
      finishReason: "stop",
      tokenCount: 14,
    });
    mockSendChat.mockRejectedValue(new Error("Réponse réseau perdue"));
    const user = userEvent.setup();
    await render(<LocalModelScreen />);
    await prepareMlxModel(user);

    await user.type(screen.getByLabelText("Intention pour le modèle local"), "Inspecter");
    await user.press(
      screen.getByRole("button", { name: "Générer une proposition locale" }),
    );
    await screen.findByText("Lire");
    await user.press(screen.getByRole("button", { name: "Soumettre au control plane" }));

    expect(await screen.findByText(/Vérifie la liste des tâches/)).toBeOnTheScreen();
    expect(mockSubmit).not.toHaveBeenCalled();
    expect(mockPush).not.toHaveBeenCalled();
    expect(
      screen.queryByRole("button", { name: "Soumettre au control plane" }),
    ).not.toBeOnTheScreen();
  });
});
