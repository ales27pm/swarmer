import { render, screen, userEvent } from "@testing-library/react-native";
import { beforeEach, describe, expect, it, jest } from "@jest/globals";
import { SemanticMemoryPanel } from "./semantic-memory-panel";
import { invokeApplicationCommand } from "@/lib/application-api/registry";

jest.mock("@/lib/application-api/registry", () => ({ invokeApplicationCommand: jest.fn() }));
const invoke = jest.mocked(invokeApplicationCommand);
beforeEach(() => { jest.resetAllMocks(); });

describe("semantic memory settings", () => {
  it("uses the application API and distinguishes server configuration from local execution", async () => {
    const user = userEvent.setup();
    invoke.mockImplementation(async (name) => {
      if (name === "memory.status") return { embedding_configured: false, context_enabled: true, hybrid_enabled: false } as never;
      if (name === "embeddings.load") return { state: "ready" } as never;
      if (name === "embeddings.generate") return { vectors: [[1]], dimensions: 384 } as never;
      return { state: "disabled" } as never;
    });
    await render(<SemanticMemoryPanel />);
    expect(invoke).not.toHaveBeenCalled();
    expect(screen.getByTestId("embedding-generate")).toBeDisabled();
    await user.press(screen.getByTestId("memory-status-refresh"));
    expect(await screen.findByText(/aucun fournisseur d’embeddings configuré/)).toBeOnTheScreen();
    await user.press(screen.getByTestId("embedding-load"));
    expect(invoke).toHaveBeenCalledWith("embeddings.load", expect.objectContaining({ experimental: true, modelId: "intfloat/multilingual-e5-small", revision: expect.stringMatching(/^[a-f0-9]{40}$/) }));
    await user.type(screen.getByTestId("embedding-test-text"), "Retrouver le rendez-vous");
    await user.press(screen.getByTestId("embedding-generate"));
    expect(invoke).toHaveBeenCalledWith("embeddings.generate", { texts: ["Retrouver le rendez-vous"], kind: "query" });
    expect(await screen.findByText(/1 vecteur · 384 dimensions/)).toBeOnTheScreen();
    await user.press(screen.getByTestId("embedding-unload"));
    expect(screen.queryByText(/calcul local terminé/)).toBeNull();
  });

  it("shows a rejected native load without enabling generation", async () => {
    invoke.mockRejectedValue(new Error("Génération déjà chargée"));
    const user = userEvent.setup();
    await render(<SemanticMemoryPanel />);
    await user.press(screen.getByTestId("embedding-load"));
    expect(await screen.findByText("Génération déjà chargée")).toBeOnTheScreen();
    expect(screen.getByTestId("embedding-generate")).toBeDisabled();
  });
});
