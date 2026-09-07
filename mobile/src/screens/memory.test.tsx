import { render, screen, userEvent, waitFor } from "@testing-library/react-native";
import { beforeEach, describe, expect, it, jest } from "@jest/globals";

import MemoryScreen from "@/../app/(main)/memory";
import {
  listMemory,
  searchMemory,
  updateMemory,
  type MemoryItem,
} from "@/lib/api/client";

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

describe("MemoryScreen", () => {
  beforeEach(() => {
    jest.clearAllMocks();
    mockListMemory.mockResolvedValue([memory]);
    mockSearchMemory.mockResolvedValue([
      { ...memory, score: 1, search_kind: "lexical" },
    ]);
    mockUpdateMemory.mockResolvedValue({ ...memory, pinned: true });
  });

  it("labels and executes lexical search without a semantic claim", async () => {
    const user = userEvent.setup();
    await render(<MemoryScreen />);
    await screen.findByText(memory.content);

    expect(
      screen.getByText(
        "Classement lexical uniquement — aucun score vectoriel ou sémantique n’est revendiqué.",
      ),
    ).toBeOnTheScreen();
    await user.type(screen.getByLabelText("Rechercher dans la mémoire"), "control local");
    await user.press(screen.getByRole("button", { name: "Chercher" }));

    await waitFor(() => expect(mockSearchMemory).toHaveBeenCalledWith("control local"));
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

  it("does not claim memory is empty when loading fails", async () => {
    mockListMemory.mockRejectedValue(new Error("Mémoire indisponible"));
    await render(<MemoryScreen />);

    expect(await screen.findByText("Mémoire indisponible")).toBeOnTheScreen();
    expect(screen.queryByText("Mémoire vide")).not.toBeOnTheScreen();
  });
});
