import { act, fireEvent, render, screen, userEvent, waitFor } from "@testing-library/react-native";
import { beforeEach, describe, expect, it, jest } from "@jest/globals";
import { AppState } from "react-native";

import MemoryScreen from "@/../app/(main)/memory";
import {
  listMemory,
  searchMemory,
  updateMemory,
  type MemoryItem,
} from "@/lib/api/client";
import { notifyConnectionChanged } from "@/lib/connection-events";
import { sha256 } from "@/lib/iphone-capabilities/grant";
import symbolicRows from "@/testing/symbolic-http-ordinary.json";

jest.mock("@/lib/api/client", () => ({
  deleteMemory: jest.fn(),
  listMemory: jest.fn(),
  rememberMemory: jest.fn(),
  searchMemory: jest.fn(),
  updateMemory: jest.fn(),
}));

const memory: MemoryItem = {
  id: "mem_test",
  scope: "swarmer",
  kind: "fact",
  content: "Le control plane local conserve la vérité.",
  summary: "Vérité locale",
  sensitivity: "normal",
  confidence: 1,
  pinned: false,
  metadata: null,
  created_at: "2026-09-04T12:00:00Z",
  updated_at: "2026-09-04T12:00:00Z",
};

const mockListMemory = jest.mocked(listMemory);
const mockSearchMemory = jest.mocked(searchMemory);
const mockUpdateMemory = jest.mocked(updateMemory);
function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((next) => { resolve = next; });
  return { resolve, promise };
}

describe("MemoryScreen", () => {
  beforeEach(() => {
    jest.clearAllMocks();
    AppState.currentState = "active";
    mockListMemory.mockReset(); mockSearchMemory.mockReset();
    mockListMemory.mockResolvedValue([memory]);
    mockSearchMemory.mockResolvedValue([
      { ...memory, score: 1, search_kind: "lexical" },
    ]);
    mockUpdateMemory.mockResolvedValue({ ...memory, pinned: true });
  });

  it("lets the owner select a concept catalog and displays the complete returned evidence", async () => {
    mockSearchMemory.mockResolvedValue(symbolicRows as unknown as MemoryItem[]);
    await render(<MemoryScreen />); await screen.findByText(memory.content);
    const user = userEvent.setup();
    await user.press(screen.getByTestId("memory-symbolic-toggle"));
    await fireEvent.changeText(screen.getByLabelText("Portée"), "project:symbolic-acceptance");
    await fireEvent.changeText(screen.getByLabelText("Espace du catalogue"), "software");
    await fireEvent.changeText(screen.getByLabelText("Identifiant du catalogue"), "engineering");
    await fireEvent.changeText(screen.getByLabelText("Rechercher dans la mémoire"), "horloge silencieuse");
    await user.press(screen.getByRole("button", { name: "Chercher" }));
    expect(mockSearchMemory).toHaveBeenCalledWith("horloge silencieuse", {
      scope: "project:symbolic-acceptance", symbolic: { catalogs: [{ namespace: "software", scheme_id: "engineering" }] },
    });
    expect(await screen.findByText("Proposition non validée")).toBeOnTheScreen();
    const card = symbolicRows[0].symbolic_evidence[0];
    await user.press(screen.getByTestId(`symbolic-details-${card.proposal.proposal_id}`));
    expect(JSON.parse(screen.getByTestId(`symbolic-content-${card.proposal.proposal_id}`).props.children).proposal).toEqual(card.proposal);
    expect(mockUpdateMemory).not.toHaveBeenCalled();
  });

  it("rejects an incomplete catalog before requesting memory", async () => {
    await render(<MemoryScreen />); await screen.findByText(memory.content);
    const user = userEvent.setup();
    await user.press(screen.getByTestId("memory-symbolic-toggle"));
    await fireEvent.changeText(screen.getByLabelText("Rechercher dans la mémoire"), "cache");
    await user.press(screen.getByRole("button", { name: "Chercher" }));
    expect(await screen.findByText("Les filtres de recherche mémoire sont invalides.")).toBeOnTheScreen();
    expect(mockSearchMemory).not.toHaveBeenCalled();
  });

  it("does not accept an earlier result after the catalog changes", async () => {
    const pending = deferred<MemoryItem[]>();
    mockSearchMemory.mockReturnValue(pending.promise);
    await render(<MemoryScreen />); await screen.findByText(memory.content);
    const user = userEvent.setup();
    await fireEvent.changeText(screen.getByLabelText("Rechercher dans la mémoire"), "cache");
    await user.press(screen.getByRole("button", { name: "Chercher" }));
    await user.press(screen.getByTestId("memory-symbolic-toggle"));
    await act(async () => pending.resolve(symbolicRows as unknown as MemoryItem[]));
    expect(screen.queryByText("Proposition non validée")).not.toBeOnTheScreen();
    expect(screen.getByText(memory.content)).toBeOnTheScreen();
  });

  it("shows a temporary French result and exposes its English canonical version without writing", async () => {
    const content = "Do not send automatically.";
    mockSearchMemory.mockResolvedValue([{ ...memory, content, summary: null,
      presentation: { language: "fr", content: "Ne pas envoyer automatiquement.", summary: null,
        canonical_sha256: sha256(content), summary_sha256: null, source_revision: memory.updated_at,
        validation_status: "model_reviewed", temporary: true, grants_authority: false } }]);
    const user = userEvent.setup();
    await render(<MemoryScreen />); await screen.findByText(memory.content);
    await fireEvent.changeText(screen.getByLabelText("Rechercher dans la mémoire"), "envoi automatique");
    await user.press(screen.getByRole("button", { name: "Chercher" }));
    expect(await screen.findByText("Ne pas envoyer automatiquement.")).toBeOnTheScreen();
    expect(screen.getByText(/Traduction française temporaire/)).toBeOnTheScreen();
    expect(screen.queryByText(content)).not.toBeOnTheScreen();
    await user.press(screen.getByRole("button", { name: "Voir la version anglaise" }));
    expect(screen.getByText(content)).toBeOnTheScreen();
    await user.press(screen.getByRole("button", { name: "Revenir au français" }));
    expect(screen.getByText("Ne pas envoyer automatiquement.")).toBeOnTheScreen();
    expect(mockUpdateMemory).not.toHaveBeenCalled();
    expect(mockSearchMemory).toHaveBeenCalledTimes(1);
  });

  it.each<[NonNullable<MemoryItem["search_kind"]>, string]>([["lexical", "lexical"], ["hybrid", "hybride"], ["vector", "vectoriel"], ["symbolic", "symbolique"]])("labels %s search only from returned evidence", async (mode, label) => {
    mockSearchMemory.mockResolvedValue([{ ...memory, search_kind: mode }]);
    const user = userEvent.setup();
    await render(<MemoryScreen />);
    await screen.findByText(memory.content);

    expect(screen.getByText("Ce catalogue ne couvre pas toutes les traces de projets ni les épisodes.")).toBeOnTheScreen();
    await user.type(screen.getByLabelText("Rechercher dans la mémoire"), "control local");
    await user.press(screen.getByRole("button", { name: "Chercher" }));

    await waitFor(() => expect(mockSearchMemory).toHaveBeenCalledWith("control local"));
    expect(await screen.findByText(`Dernière recherche : classement ${label} déclaré par le serveur.`)).toBeOnTheScreen();
  });

  it("labels the exact French original separately from a translated display", async () => {
    const content = "Keep the file `rapport.csv`.";
    const original = "Conserver le fichier `rapport.csv`.";
    const sourceHash = sha256(JSON.stringify({ content: original, summary: null }));
    mockSearchMemory.mockResolvedValue([{ ...memory, content, summary: null,
      metadata: { canonical_language: "en", source_id: "msrc_one", source_sha256: sourceHash,
        canonical_receipt_id: "receipt_one", summary: null,
        content: { source_id: "msrc_one:content", source_language: "fr", source_sha256: sha256(original),
          canonical_sha256: sha256(content), source_revalidated: true, grants_authority: false } },
      presentation: { language: "fr", mode: "original", validation_status: "source_preserved",
        content: original, summary: null, canonical_sha256: sha256(content), summary_sha256: null,
        source_revision: memory.updated_at, temporary: true, grants_authority: false,
        source_id: "msrc_one", source_sha256: sourceHash, canonical_receipt_id: "receipt_one" } }]);
    const user = userEvent.setup();
    await render(<MemoryScreen />); await screen.findByText(memory.content);
    await fireEvent.changeText(screen.getByLabelText("Rechercher dans la mémoire"), "conserver fichier");
    await user.press(screen.getByRole("button", { name: "Chercher" }));
    expect(await screen.findByText(original)).toBeOnTheScreen();
    expect(screen.getByText(/Texte français d’origine/)).toBeOnTheScreen();
    expect(screen.queryByText(/Traduction française temporaire/)).not.toBeOnTheScreen();
    await user.press(screen.getByRole("button", { name: "Voir la version anglaise" }));
    expect(screen.getByText(content)).toBeOnTheScreen();
    expect(mockUpdateMemory).not.toHaveBeenCalled();
  });

  it("ties the pin mutation to the selected memory record", async () => {
    const user = userEvent.setup();
    await render(<MemoryScreen />);

    await user.press(await screen.findByRole("button", { name: "Épingler : Vérité locale" }));

    await waitFor(() =>
      expect(mockUpdateMemory).toHaveBeenCalledWith(memory.id, { pinned: true }),
    );
    expect(screen.getByText("Mémoire « Vérité locale » épinglée.")).toBeOnTheScreen();
    expect(
      screen.getByRole("button", { name: "Supprimer : Vérité locale" }),
    ).toBeOnTheScreen();
  });

  it("preserves a project-scoped catalogue item without mislabeling the catalogue as general-only", async () => {
    mockListMemory.mockResolvedValue([{ ...memory, scope: "project:crm", content: "Préférence du projet CRM" }]);
    await render(<MemoryScreen />);
    expect(await screen.findByText("Préférence du projet CRM")).toBeOnTheScreen();
    expect(screen.getByText("project:crm")).toBeOnTheScreen();
    expect(screen.getByText("Recherche dans les éléments enregistrés")).toBeOnTheScreen();
    expect(screen.getByText("Ce catalogue ne couvre pas toutes les traces de projets ni les épisodes.")).toBeOnTheScreen();
    expect(screen.queryByText(/mémoire générale/)).not.toBeOnTheScreen();
  });

  it("does not claim memory is empty when loading fails", async () => {
    mockListMemory.mockRejectedValue(new Error("Mémoire indisponible"));
    await render(<MemoryScreen />);

    expect(await screen.findByText("Mémoire indisponible")).toBeOnTheScreen();
    expect(screen.queryByText("Aucun élément dans ce catalogue")).not.toBeOnTheScreen();
  });

  it("does not invent a search mode for empty or unlabeled results", async () => {
    const user = userEvent.setup();
    mockSearchMemory.mockResolvedValue([]);
    await render(<MemoryScreen />); await screen.findByText(memory.content);
    await fireEvent.changeText(screen.getByLabelText("Rechercher dans la mémoire"), "absent");
    await user.press(screen.getByRole("button", { name: "Chercher" }));
    expect(await screen.findByText("Aucun résultat dans ce catalogue")).toBeOnTheScreen();
    expect(screen.getByText(/mode non renseigné dans les résultats reçus/)).toBeOnTheScreen();
    expect(screen.queryByText(/classement lexical déclaré/)).not.toBeOnTheScreen();
  });

  it("ignores a list refresh that resolves after a newer search", async () => {
    const pending = deferred<MemoryItem[]>();
    mockListMemory.mockReturnValue(pending.promise);
    mockSearchMemory.mockResolvedValue([{ ...memory, content: "Résultat actuel", search_kind: "hybrid" }]);
    await render(<MemoryScreen />);
    await fireEvent.changeText(screen.getByLabelText("Rechercher dans la mémoire"), "nouveau");
    await userEvent.setup().press(screen.getByRole("button", { name: "Chercher" }));
    expect(await screen.findByText("Résultat actuel")).toBeOnTheScreen();
    await act(async () => pending.resolve([{ ...memory, content: "Ancien relevé retardé" }]));
    expect(screen.queryByText("Ancien relevé retardé")).not.toBeOnTheScreen();
    expect(screen.getByText("Résultat actuel")).toBeOnTheScreen();
  });

  it("ignores an old search after clearing it and preserves a failed refresh as stale", async () => {
    const pending = deferred<MemoryItem[]>();
    mockSearchMemory.mockReturnValue(pending.promise);
    const user = userEvent.setup();
    await render(<MemoryScreen />); await screen.findByText(memory.content);
    await fireEvent.changeText(screen.getByLabelText("Rechercher dans la mémoire"), "ancien");
    await user.press(screen.getByRole("button", { name: "Chercher" }));
    await user.press(screen.getByRole("button", { name: "Effacer la recherche" }));
    await screen.findByText(memory.content);
    await act(async () => pending.resolve([{ ...memory, content: "Ancienne recherche" }]));
    expect(screen.queryByText("Ancienne recherche")).not.toBeOnTheScreen();
    mockListMemory.mockRejectedValue(new Error("Réseau indisponible"));
    await user.press(screen.getByRole("button", { name: "Actualiser les éléments mémorisés" }));
    expect(await screen.findByText("Réseau indisponible")).toBeOnTheScreen();
    expect(screen.getByText(memory.content)).toBeOnTheScreen();
    expect(screen.getByText(/dernier relevé conservé, non actualisé/)).toBeOnTheScreen();
  });

  it("clears old pairing data and ignores its pending refresh until explicitly reloaded", async () => {
    const pending = deferred<MemoryItem[]>();
    mockListMemory.mockResolvedValueOnce([memory]).mockReturnValueOnce(pending.promise).mockResolvedValue([{ ...memory, content: "Nouvelle connexion" }]);
    const user = userEvent.setup();
    await render(<MemoryScreen />); await screen.findByText(memory.content);
    await user.press(screen.getByRole("button", { name: "Actualiser les éléments mémorisés" }));
    await act(async () => notifyConnectionChanged());
    expect(screen.queryByText(memory.content)).not.toBeOnTheScreen();
    await act(async () => pending.resolve([memory]));
    expect(screen.queryByText(memory.content)).not.toBeOnTheScreen();
    expect(screen.getByText(/Le jumelage a changé/)).toBeOnTheScreen();
    expect(mockListMemory).toHaveBeenCalledTimes(2);
    await user.press(screen.getByRole("button", { name: "Actualiser les éléments mémorisés" }));
    expect(await screen.findByText("Nouvelle connexion")).toBeOnTheScreen();
  });

  it("does not read or announce an old mutation after the pairing changes", async () => {
    const pending = deferred<MemoryItem>(); mockUpdateMemory.mockReturnValue(pending.promise);
    const user = userEvent.setup();
    await render(<MemoryScreen />);
    await user.press(await screen.findByRole("button", { name: "Épingler : Vérité locale" }));
    await act(async () => notifyConnectionChanged());
    await act(async () => pending.resolve({ ...memory, pinned: true }));
    expect(mockListMemory).toHaveBeenCalledTimes(1);
    expect(screen.queryByText("Mémoire « Vérité locale » épinglée.")).not.toBeOnTheScreen();
    expect(screen.getByText(/Le jumelage a changé/)).toBeOnTheScreen();
  });

  it("invalidates background responses and reloads on foreground", async () => {
    const pending = deferred<MemoryItem[]>();
    let change!: (state: import("react-native").AppStateStatus) => void;
    const listener = jest.spyOn(AppState, "addEventListener").mockImplementation((_event, callback) => { change = callback; return { remove: jest.fn() }; });
    mockListMemory.mockReturnValueOnce(pending.promise).mockResolvedValueOnce([{ ...memory, content: "Relevé au retour" }]);
    try {
      await render(<MemoryScreen />);
      await act(async () => change("background"));
      await act(async () => pending.resolve([memory]));
      expect(screen.queryByText(memory.content)).not.toBeOnTheScreen();
      await act(async () => change("active"));
      expect(await screen.findByText("Relevé au retour")).toBeOnTheScreen();
    } finally { listener.mockRestore(); }
  });
});
