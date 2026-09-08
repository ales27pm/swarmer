import { render, screen } from "@testing-library/react-native";
import { beforeEach, describe, expect, it, jest } from "@jest/globals";

import TasksScreen from "@/../app/(main)/tasks";
import { getServerUrl, listTasks } from "@/lib/api/client";
import { localTasks } from "@/lib/state/replica";

const mockPush = jest.fn();

jest.mock("expo-router", () => ({ useRouter: () => ({ push: mockPush }) }));
jest.mock("@/lib/api/client", () => ({ getServerUrl: jest.fn(), listTasks: jest.fn() }));
jest.mock("@/lib/state/replica", () => ({ localTasks: jest.fn() }));

const mockListTasks = jest.mocked(listTasks);
const mockLocalTasks = jest.mocked(localTasks);
const mockGetServerUrl = jest.mocked(getServerUrl);

describe("TasksScreen", () => {
  beforeEach(() => {
    jest.clearAllMocks();
    mockGetServerUrl.mockResolvedValue("https://control.example");
    mockLocalTasks.mockRejectedValue(new Error("Cache indisponible"));
  });

  it("keeps every lifecycle filter visible without a hidden horizontal tail", async () => {
    mockListTasks.mockResolvedValue([]);
    await render(<TasksScreen />);

    expect(await screen.findByRole("button", { name: "Toutes" })).toBeOnTheScreen();
    expect(screen.getByRole("button", { name: "Annulées" })).toBeOnTheScreen();
  });

  it("does not claim the authoritative task list is empty when loading fails", async () => {
    mockListTasks.mockRejectedValue(new Error("Tâches indisponibles"));
    await render(<TasksScreen />);

    expect(await screen.findByText("Tâches indisponibles")).toBeOnTheScreen();
    expect(screen.queryByText("Aucune tâche")).not.toBeOnTheScreen();
  });

  it("renders cached tasks with an explicit stale warning when the server is offline", async () => {
    mockListTasks.mockRejectedValue(new Error("Tâches indisponibles"));
    mockLocalTasks.mockResolvedValue([
      {
        id: "tsk_cached",
        title: "Tâche en cache",
        input: "Inspecter le dépôt",
        mode: "normal",
        source: "iphone",
        conversation_id: null,
        status: "completed",
        priority: 0,
        created_at: "2026-09-08T00:00:00Z",
        updated_at: "2026-09-08T00:01:00Z",
        completed_at: "2026-09-08T00:01:00Z",
        error_json: null,
      },
    ]);

    await render(<TasksScreen />);

    expect(await screen.findByText("Tâche en cache")).toBeOnTheScreen();
    expect(screen.getByText("Les statuts affichés peuvent être périmés.")).toBeOnTheScreen();
    expect(screen.getByText(/Hors ligne — affichage du cache local/)).toBeOnTheScreen();
  });
});
