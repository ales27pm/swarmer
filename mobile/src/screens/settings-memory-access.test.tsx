import { render, screen, userEvent } from "@testing-library/react-native";
import { beforeEach, describe, expect, it, jest } from "@jest/globals";

import SettingsScreen from "@/../app/(main)/settings";
import { invokeApplicationCommand } from "@/lib/application-api/registry";

jest.mock("expo-router", () => ({ useRouter: () => ({ push: jest.fn() }) }));
jest.mock("@/lib/application-api/server", () => ({
  getServerUrl: async () => "https://control.example",
  hasDeviceToken: async () => false,
  bootstrapSync: jest.fn(),
  listAudit: jest.fn(),
  pairConnection: jest.fn(),
}));
jest.mock("@/lib/application-api/registry", () => ({ invokeApplicationCommand: jest.fn() }));
const invoke = jest.mocked(invokeApplicationCommand);

beforeEach(() => { jest.clearAllMocks(); });

describe("Settings memory tools", () => {
  it("keeps model actions explicit and preserves a test draft when folded", async () => {
    invoke.mockImplementation(async (name) => {
      if (name === "embeddings.load") return { state: "ready" } as never;
      if (name === "embeddings.generate") return { vectors: [[1]], dimensions: 384 } as never;
      return { state: "disabled" } as never;
    });
    const user = userEvent.setup();
    await render(<SettingsScreen />);
    expect(invoke).not.toHaveBeenCalled();
    expect(screen.queryByTestId("embedding-load")).not.toBeOnTheScreen();
    await user.press(screen.getByRole("button", { name: "Mémoire et calcul local" }));
    expect(screen.getByRole("button", { name: "Tester les embeddings locaux" })).toBeDisabled();
    await user.press(screen.getByRole("button", { name: "Charger E5 expérimental" }));
    expect(invoke).toHaveBeenCalledWith("embeddings.load", expect.objectContaining({ experimental: true }));
    await user.type(screen.getByLabelText("Texte du test d’embeddings"), "Mon rendez-vous");
    await user.press(screen.getByRole("button", { name: "Mémoire et calcul local" }));
    await user.press(screen.getByRole("button", { name: "Mémoire et calcul local" }));
    expect(screen.getByLabelText("Texte du test d’embeddings")).toHaveDisplayValue("Mon rendez-vous");
    await user.press(screen.getByRole("button", { name: "Tester les embeddings locaux" }));
    expect(invoke).toHaveBeenCalledWith("embeddings.generate", { texts: ["Mon rendez-vous"], kind: "query" });
    expect(await screen.findByText(/1 vecteur · 384 dimensions/)).toBeOnTheScreen();
  });

  it("keeps a server update error separate from local model readiness", async () => {
    invoke.mockRejectedValue(Object.assign(new Error("Method Not Allowed"), { status: 405 }));
    const user = userEvent.setup();
    await render(<SettingsScreen />);
    await user.press(screen.getByRole("button", { name: "Mémoire et calcul local" }));
    await user.press(screen.getByRole("button", { name: "Vérifier la mémoire serveur" }));
    expect(await screen.findByText(/Une mise à jour du serveur est nécessaire/)).toBeOnTheScreen();
    expect(screen.getByRole("button", { name: "Charger E5 expérimental" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "Tester les embeddings locaux" })).toBeDisabled();
  });
});
