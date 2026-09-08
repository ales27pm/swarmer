import { render, screen, userEvent, waitFor } from "@testing-library/react-native";
import { beforeEach, describe, expect, it, jest } from "@jest/globals";

import LocalModelScreen from "@/../app/local-model";
import { sendChat, submitToolProposal, type Task } from "@/lib/api/client";
import {
  cancelLocalGeneration,
  generateLocalProposal,
  getLocalInferenceCapabilities,
  importLocalModel,
  isLocalInferenceAvailable,
  listLocalModels,
  loadLocalModel,
  pickAndImportLocalModelDirectory,
  unloadLocalModel,
} from "@/lib/local-inference";

const mockPush = jest.fn();

jest.mock("expo-router", () => ({ useRouter: () => ({ push: mockPush }) }));
jest.mock("expo-document-picker", () => ({ getDocumentAsync: jest.fn() }));
jest.mock("@/lib/api/client", () => ({
  sendChat: jest.fn(),
  submitToolProposal: jest.fn(),
}));
jest.mock("@/lib/local-inference", () => {
  const actual = jest.requireActual<typeof import("@/lib/local-inference")>(
    "@/lib/local-inference",
  );
  return {
    ...actual,
    cancelLocalGeneration: jest.fn(),
    generateLocalProposal: jest.fn(),
    getLocalInferenceCapabilities: jest.fn(),
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
  await user.type(screen.getByLabelText("Dépôt Hugging Face"), "mlx-community/test-model");
  await user.type(
    screen.getByLabelText("Révision Hugging Face immuable"),
    "a".repeat(40),
  );
  await user.press(screen.getByRole("button", { name: "Charger le modèle" }));
  expect(await screen.findByText(/Modèle chargé localement/)).toBeOnTheScreen();
}

describe("LocalModelScreen", () => {
  beforeEach(() => {
    jest.clearAllMocks();
    mockIsAvailable.mockReturnValue(true);
    mockCapabilities.mockResolvedValue({
      coreml: true,
      mlx: true,
      llamaCpp: true,
      platform: "ios",
    });
    mockListModels.mockResolvedValue([]);
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

  it("imports a Core ML folder so tokenizer sidecars remain in the same payload", async () => {
    const user = userEvent.setup();
    await render(<LocalModelScreen />);

    await screen.findByText(/Choisis un modèle local/);
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
    await user.type(screen.getByLabelText("Dépôt Hugging Face"), "mlx-community/test-model");
    await user.type(screen.getByLabelText("Révision Hugging Face immuable"), "main");
    expect(screen.getByRole("button", { name: "Charger le modèle" })).toBeDisabled();
    expect(screen.getByText(/Une branche ou une étiquette mobile est refusée/)).toBeOnTheScreen();
    await user.clear(screen.getByLabelText("Révision Hugging Face immuable"));
    await user.type(
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
    expect(mockSendChat).toHaveBeenCalledWith("Inspecter le projet");
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
    await user.type(screen.getByLabelText("Dépôt Hugging Face"), "mlx-community/test-model");
    await user.type(
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

  it("unloads a model whose load resolves after the screen unmounts", async () => {
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
    await user.type(screen.getByLabelText("Dépôt Hugging Face"), "mlx-community/test-model");
    await user.type(
      screen.getByLabelText("Révision Hugging Face immuable"),
      "a".repeat(40),
    );
    await user.press(screen.getByRole("button", { name: "Charger le modèle" }));
    await waitFor(() => expect(mockLoad).toHaveBeenCalledTimes(1));
    await screen.unmount();

    resolveLoad({
      state: "ready",
      runtime: "mlx",
      modelId: "mlx-community/test-model",
      revision: "a".repeat(40),
    });

    await waitFor(() => expect(mockUnload).toHaveBeenCalledTimes(2));
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
