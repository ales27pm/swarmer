import { act, fireEvent, render, screen, waitFor } from "@testing-library/react-native";
import { beforeEach, describe, expect, it, jest } from "@jest/globals";

import TasksScreen from "@/../app/(main)/tasks";
import { getServerUrl, listTasks, type Task } from "@/lib/api/client";
import { localTasks } from "@/lib/state/replica";

const mockPush = jest.fn();
let refreshFromLiveEvent: (() => void | Promise<unknown>) | undefined;

jest.mock("expo-router", () => ({ useRouter: () => ({ push: mockPush }) }));
jest.mock("@/lib/api/client", () => ({ getServerUrl: jest.fn(), listTasks: jest.fn() }));
jest.mock("@/lib/state/replica", () => ({ localTasks: jest.fn() }));
jest.mock("@/lib/sync/live-sync-context", () => ({
  useLiveRefresh: (refresh: () => void | Promise<unknown>) => { refreshFromLiveEvent = refresh; },
}));

const mockListTasks = jest.mocked(listTasks);
const mockLocalTasks = jest.mocked(localTasks);
const mockGetServerUrl = jest.mocked(getServerUrl);

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((settle) => { resolve = settle; });
  return { promise, resolve };
}

function task(id: string, status: Task["status"] = "running"): Task {
  return {
    id, title: id, input: id, mode: "normal", source: "iphone", conversation_id: null,
    status, priority: 0, created_at: "2030-01-01T00:00:00Z",
    updated_at: "2030-01-01T00:00:00Z", completed_at: null, error_json: null,
  };
}

describe("TasksScreen", () => {
  beforeEach(() => {
    jest.clearAllMocks();
    refreshFromLiveEvent = undefined;
    mockGetServerUrl.mockResolvedValue("https://control.example");
    mockLocalTasks.mockRejectedValue(new Error("Cache indisponible"));
  });

  it("shows four primary filters and preserves every other status in a disclosure", async () => {
    mockListTasks.mockResolvedValue([]);
    await render(<TasksScreen />);

    expect(await screen.findByRole("button", { name: "Toutes" })).toBeOnTheScreen();
    for (const key of ["all", "running", "waiting_permission", "completed"]) {
      expect(screen.getByTestId(`filter-${key}`)).toBeOnTheScreen();
    }
    expect(screen.queryByTestId("filter-cancelled")).not.toBeOnTheScreen();
    await fireEvent.press(screen.getByTestId("tasks-more-filters"));
    expect(screen.getByTestId("tasks-more-filters").props.accessibilityState).toMatchObject({ expanded: true });
    for (const key of ["created", "planned", "queued", "blocked", "failed", "cancelled"]) {
      expect(screen.getByTestId(`filter-${key}`)).toBeOnTheScreen();
    }
    await fireEvent.press(screen.getByTestId("filter-cancelled"));
    await waitFor(() => expect(mockListTasks).toHaveBeenLastCalledWith("cancelled"));
    await fireEvent.press(screen.getByTestId("tasks-more-filters"));
    expect(screen.getByText("Autres filtres · Annulées")).toBeOnTheScreen();
  });

  it("links directly to approvals and opens the selected task", async () => {
    mockListTasks.mockResolvedValue([task("tsk_open")]);
    await render(<TasksScreen />);
    await fireEvent.press(await screen.findByTestId("task-row-tsk_open"));
    expect(mockPush).toHaveBeenCalledWith({ pathname: "/task/[id]", params: { id: "tsk_open" } });
    await fireEvent.press(screen.getByTestId("tasks-approvals-button"));
    expect(mockPush).toHaveBeenLastCalledWith("/approvals");
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

  it("ignores a slower response for the previous filter", async () => {
    const old = deferred<Task[]>();
    mockListTasks.mockImplementationOnce(() => old.promise).mockResolvedValueOnce([task("Running")]);
    await render(<TasksScreen />);
    await waitFor(() => expect(mockListTasks).toHaveBeenCalledTimes(1));
    await fireEvent.press(screen.getByTestId("filter-running"));
    expect(await screen.findByText("Running")).toBeOnTheScreen();
    await act(async () => old.resolve([task("Old", "completed")]));
    expect(screen.queryByText("Old")).not.toBeOnTheScreen();
    expect(screen.getByText("Running")).toBeOnTheScreen();
    expect(screen.getByTestId("filter-running").props.accessibilityState).toMatchObject({ selected: true });
  });

  it("ignores an older cached result after an authoritative refresh", async () => {
    const cache = deferred<Task[]>();
    mockListTasks.mockRejectedValueOnce(new Error("Offline")).mockResolvedValueOnce([task("Fresh")]);
    mockLocalTasks.mockImplementationOnce(() => cache.promise);
    await render(<TasksScreen />);
    await waitFor(() => expect(mockLocalTasks).toHaveBeenCalledTimes(1));
    await act(async () => { await refreshFromLiveEvent?.(); });
    expect(await screen.findByText("Fresh")).toBeOnTheScreen();
    await act(async () => cache.resolve([task("Stale")]));
    expect(screen.queryByText("Stale")).not.toBeOnTheScreen();
    expect(screen.queryByText(/Les statuts affichés peuvent être périmés/)).not.toBeOnTheScreen();
  });

  it("does not display cache from an origin changed during the lookup", async () => {
    const cache = deferred<Task[]>();
    mockListTasks.mockRejectedValueOnce(new Error("Offline"));
    mockLocalTasks.mockImplementationOnce(() => cache.promise);
    await render(<TasksScreen />);
    await waitFor(() => expect(mockLocalTasks).toHaveBeenCalledWith("https://control.example", undefined));
    mockGetServerUrl.mockResolvedValue("https://other.example");
    await act(async () => cache.resolve([task("Wrong server")]));
    expect(await screen.findByText("Le serveur a changé. Actualise l’activité.")).toBeOnTheScreen();
    expect(screen.queryByText("Wrong server")).not.toBeOnTheScreen();
    expect(screen.queryByText("Aucune tâche")).not.toBeOnTheScreen();
  });

  it("does not display an authoritative response from the previous server", async () => {
    const old = deferred<Task[]>();
    mockListTasks.mockImplementationOnce(() => old.promise);
    await render(<TasksScreen />);
    await waitFor(() => expect(mockListTasks).toHaveBeenCalledTimes(1));
    mockGetServerUrl.mockResolvedValue("https://other.example");
    await act(async () => old.resolve([task("Wrong server")]));
    expect(await screen.findByText("Le serveur a changé. Actualise l’activité.")).toBeOnTheScreen();
    expect(screen.queryByText("Wrong server")).not.toBeOnTheScreen();
    expect(mockLocalTasks).not.toHaveBeenCalled();
  });
});
