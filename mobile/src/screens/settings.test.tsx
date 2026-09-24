import { act, render, screen, userEvent, waitFor } from "@testing-library/react-native";
import { beforeEach, describe, expect, it, jest } from "@jest/globals";
import { Linking } from "react-native";

import SettingsScreen from "@/../app/(main)/settings";
import {
  bootstrapSync,
  getServerUrl,
  hasDeviceToken,
  listAudit,
  pairDevice,
  type AuditEvent,
  type Bootstrap,
} from "@/lib/api/client";

const mockPush = jest.fn();

jest.mock("expo-router", () => ({ useRouter: () => ({ push: mockPush }) }));

jest.mock("expo-secure-store", () => ({
  getItemAsync: jest.fn(),
  setItemAsync: jest.fn(),
}));
jest.mock("@/lib/api/client", () => ({
  bootstrapSync: jest.fn(),
  getServerUrl: jest.fn(),
  hasDeviceToken: jest.fn(),
  listAudit: jest.fn(),
  pairDevice: jest.fn(),
}));

const mockBootstrap = jest.mocked(bootstrapSync);
const mockGetServerUrl = jest.mocked(getServerUrl);
const mockHasDeviceToken = jest.mocked(hasDeviceToken);
const mockListAudit = jest.mocked(listAudit);
const mockPairDevice = jest.mocked(pairDevice);

const bootstrap: Bootstrap = {
  server_time: "2026-09-04T12:00:00Z",
  tasks: [],
  approvals: [],
  tool_calls: [],
  conversations: [],
  agents: [],
  pinned_memory: [],
  counts: {
    tasks: 0,
    messages: 0,
    agents: 0,
    approvals_pending: 0,
    memory_items: 0,
    audit_events: 0,
  },
  cursor: "0",
};

const oldAuditEvent: AuditEvent = {
  id: 1,
  trace_id: "old-trace",
  event_type: "old.origin.event",
  actor_type: "device",
  actor_id: "old-device",
  task_id: null,
  payload: {},
  prev_hash: null,
  hash: "a".repeat(64),
  created_at: "2026-09-04T12:00:00Z",
};

describe("SettingsScreen", () => {
  beforeEach(() => {
    jest.clearAllMocks();
    mockBootstrap.mockResolvedValue(bootstrap);
    mockGetServerUrl.mockResolvedValue("https://control.example");
    mockHasDeviceToken.mockResolvedValue(true);
    mockListAudit.mockResolvedValue([]);
  });

  it("does not claim authentication when the stored credential fails bootstrap", async () => {
    mockBootstrap.mockRejectedValue(new Error("Jeton révoqué"));

    await render(<SettingsScreen />);

    const user = userEvent.setup();
    await user.press(await screen.findByRole("button", { name: "Détail de l’erreur" }));
    expect(screen.getByText("Jeton révoqué")).toBeOnTheScreen();
    await user.press(screen.getByRole("button", { name: "État du serveur et journal" }));
    expect(screen.queryByText("Connexion authentifiée")).not.toBeOnTheScreen();
    expect(
      screen.getByText("Connexion non authentifiée ou non vérifiée"),
    ).toBeOnTheScreen();
    expect(
      screen.getByText("Journal indisponible tant que l’accès authentifié n’est pas rétabli."),
    ).toBeOnTheScreen();
    expect(screen.queryByText("Aucun événement authentifié à afficher.")).not.toBeOnTheScreen();
  });

  it("keeps pairing unconfirmed when candidate bootstrap fails before commit", async () => {
    mockHasDeviceToken.mockResolvedValue(false);
    mockPairDevice.mockRejectedValue(new Error("Bootstrap refusé"));
    const user = userEvent.setup();
    await render(<SettingsScreen />);

    await user.type(screen.getByLabelText("Code de jumelage à six chiffres"), "123456");
    await user.press(screen.getByRole("button", { name: "Jumeler cet iPhone" }));

    await waitFor(() => expect(mockPairDevice).toHaveBeenCalledTimes(1));
    await user.press(await screen.findByRole("button", { name: "Détail de l’erreur" }));
    expect(screen.getByText("Bootstrap refusé")).toBeOnTheScreen();
    expect(
      screen.getByText("Le jumelage n’a pas été confirmé par un accès authentifié complet."),
    ).toBeOnTheScreen();
    expect(screen.queryByText(/Jumelage réussi/)).not.toBeOnTheScreen();
    expect(screen.queryByText("Connexion authentifiée")).not.toBeOnTheScreen();
  });

  it("adopts the activated pairing result without rereading connection storage", async () => {
    const pairedBootstrap: Bootstrap = {
      ...bootstrap,
      counts: { ...bootstrap.counts, tasks: 7, audit_events: 1 },
      cursor: "7",
    };
    mockGetServerUrl
      .mockResolvedValueOnce("https://control.example")
      .mockResolvedValueOnce("https://control.example");
    mockBootstrap.mockResolvedValue(bootstrap);
    mockListAudit.mockResolvedValue([oldAuditEvent]);
    mockPairDevice.mockResolvedValue({
      bootstrap: pairedBootstrap,
      serverUrl: "https://candidate.example",
    });
    const user = userEvent.setup();
    await render(<SettingsScreen />);

    expect(
      await screen.findByText("Connexion authentifiée : https://control.example"),
    ).toBeOnTheScreen();
    await user.press(screen.getByRole("button", { name: "État du serveur et journal" }));
    expect(screen.getByText("old.origin.event")).toBeOnTheScreen();
    await user.press(screen.getByRole("button", { name: "Adresse et jumelage" }));
    await user.clear(screen.getByLabelText("Adresse du serveur"));
    await user.type(
      screen.getByLabelText("Adresse du serveur"),
      "https://candidate.example",
    );
    await user.type(screen.getByLabelText("Code de jumelage à six chiffres"), "123456");
    await user.press(screen.getByRole("button", { name: "Jumeler cet iPhone" }));

    expect(await screen.findByText(/Jumelage réussi/)).toBeOnTheScreen();
    expect(
      screen.getByText("Connexion authentifiée : https://candidate.example"),
    ).toBeOnTheScreen();
    expect(screen.getByText("7")).toBeOnTheScreen();
    expect(screen.queryByText("old.origin.event")).not.toBeOnTheScreen();
    expect(
      screen.getByText("Actualise pour charger le journal authentifié de cette connexion."),
    ).toBeOnTheScreen();
    expect(mockBootstrap).toHaveBeenCalledTimes(1);
    expect(mockListAudit).toHaveBeenCalledTimes(1);
    expect(mockGetServerUrl).toHaveBeenCalledTimes(2);
  });

  it("does not let an older initial refresh overwrite a successful pairing", async () => {
    let resolveInitialBootstrap!: (value: Bootstrap) => void;
    const initialBootstrap = new Promise<Bootstrap>((resolve) => {
      resolveInitialBootstrap = resolve;
    });
    const pairedBootstrap: Bootstrap = {
      ...bootstrap,
      counts: { ...bootstrap.counts, tasks: 9 },
      cursor: "9",
    };
    mockBootstrap.mockReturnValue(initialBootstrap);
    mockListAudit.mockResolvedValue([oldAuditEvent]);
    mockPairDevice.mockResolvedValue({
      bootstrap: pairedBootstrap,
      serverUrl: "https://candidate.example",
    });
    const user = userEvent.setup();
    await render(<SettingsScreen />);

    await waitFor(() => expect(mockBootstrap).toHaveBeenCalledTimes(1));
    await user.clear(screen.getByLabelText("Adresse du serveur"));
    await user.type(
      screen.getByLabelText("Adresse du serveur"),
      "https://candidate.example",
    );
    await user.type(screen.getByLabelText("Code de jumelage à six chiffres"), "123456");
    await user.press(screen.getByRole("button", { name: "Jumeler cet iPhone" }));

    expect(await screen.findByText(/Jumelage réussi/)).toBeOnTheScreen();
    expect(
      screen.getByText("Connexion authentifiée : https://candidate.example"),
    ).toBeOnTheScreen();

    await act(() => {
      resolveInitialBootstrap(bootstrap);
    });

    expect(
      screen.getByText("Connexion authentifiée : https://candidate.example"),
    ).toBeOnTheScreen();
    await user.press(screen.getByRole("button", { name: "État du serveur et journal" }));
    expect(screen.getByText("9")).toBeOnTheScreen();
    expect(screen.queryByText("old.origin.event")).not.toBeOnTheScreen();
    expect(mockGetServerUrl).toHaveBeenCalledTimes(1);
  });

  it("invalidates an initial refresh when Settings unmounts", async () => {
    let resolveInitialBootstrap!: (value: Bootstrap) => void;
    const initialBootstrap = new Promise<Bootstrap>((resolve) => {
      resolveInitialBootstrap = resolve;
    });
    mockBootstrap.mockReturnValue(initialBootstrap);
    await render(<SettingsScreen />);

    await waitFor(() => expect(mockBootstrap).toHaveBeenCalledTimes(1));
    await screen.unmount();
    await act(() => {
      resolveInitialBootstrap(bootstrap);
    });

    expect(mockGetServerUrl).toHaveBeenCalledTimes(1);
  });

  it("separates the authenticated origin from an edited pairing candidate", async () => {
    mockBootstrap.mockResolvedValue(bootstrap);
    const user = userEvent.setup();
    await render(<SettingsScreen />);

    expect(
      await screen.findByText("Connexion authentifiée : https://control.example"),
    ).toBeOnTheScreen();
    await user.press(screen.getByRole("button", { name: "Adresse et jumelage" }));
    await user.clear(screen.getByLabelText("Adresse du serveur"));
    await user.type(screen.getByLabelText("Adresse du serveur"), "https://candidate.example");

    expect(
      screen.getByText(
        "Cette adresse est une candidate non vérifiée. La connexion active reste https://control.example jusqu’à un nouveau jumelage réussi.",
      ),
    ).toBeOnTheScreen();
    await user.press(screen.getByRole("button", { name: "État du serveur et journal" }));
    expect(screen.getByText("Aucun événement authentifié à afficher.")).toBeOnTheScreen();
  });

  it("provides a secret-free local pairing checklist", async () => {
    mockHasDeviceToken.mockResolvedValue(false);

    await render(<SettingsScreen />);

    expect(await screen.findByText(/Sur le serveur, génère un code temporaire à six chiffres/)).toBeOnTheScreen();
    expect(screen.queryByLabelText(/secret/i)).not.toBeOnTheScreen();
  });


  it("keeps verified connection and diagnostics compact without discarding pairing input", async () => {
    mockListAudit.mockResolvedValue([oldAuditEvent]);
    const user = userEvent.setup();
    await render(<SettingsScreen />);
    await screen.findByText("Connexion authentifiée : https://control.example");
    expect(screen.getByRole("button", { name: "Adresse et jumelage" })).toBeCollapsed();
    expect(screen.getByRole("button", { name: "État du serveur et journal" })).toBeCollapsed();
    expect(screen.queryByText("old.origin.event")).not.toBeOnTheScreen();
    expect(screen.queryByLabelText("Code de jumelage à six chiffres")).not.toBeOnTheScreen();
    await user.press(screen.getByRole("button", { name: "Adresse et jumelage" }));
    await user.type(screen.getByLabelText("Code de jumelage à six chiffres"), "123");
    await user.press(screen.getByRole("button", { name: "Adresse et jumelage" }));
    await user.press(screen.getByRole("button", { name: "Adresse et jumelage" }));
    expect(screen.getByLabelText("Code de jumelage à six chiffres")).toHaveDisplayValue("123");
    expect(mockPairDevice).not.toHaveBeenCalled();
  });

  it("links memory and pending approvals to their existing routes", async () => {
    const user = userEvent.setup();
    await render(<SettingsScreen />);
    await user.press(screen.getByRole("button", { name: "Consulter la mémoire" }));
    expect(mockPush).toHaveBeenLastCalledWith("/memory");
    await user.press(screen.getByRole("button", { name: "Autorisations en attente" }));
    expect(mockPush).toHaveBeenLastCalledWith("/approvals");
  });

  it("opens system permissions only on request and explains a failed launch", async () => {
    const openSettings = jest.spyOn(Linking, "openSettings")
      .mockResolvedValueOnce(undefined)
      .mockRejectedValueOnce(new Error("Unavailable"));
    const user = userEvent.setup();
    await render(<SettingsScreen />);
    expect(openSettings).not.toHaveBeenCalled();
    await user.press(screen.getByRole("button", { name: "Autorisations de cet appareil" }));
    expect(openSettings).toHaveBeenCalledTimes(1);
    await user.press(screen.getByRole("button", { name: "Autorisations de cet appareil" }));
    expect(await screen.findByText(/Impossible d’ouvrir les réglages du système/)).toBeOnTheScreen();
    openSettings.mockRestore();
  });

  it("opens the isolated local-model workspace", async () => {
    mockHasDeviceToken.mockResolvedValue(false);
    const user = userEvent.setup();
    await render(<SettingsScreen />);

    await user.press(screen.getByRole("button", { name: "Ouvrir les modèles locaux" }));

    expect(mockPush).toHaveBeenCalledWith("/local-model");
  });
});
