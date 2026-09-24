import { act, fireEvent, render, screen, waitFor } from "@testing-library/react-native";
import { beforeEach, describe, expect, it, jest } from "@jest/globals";

import TasksScreen from "@/../app/(main)/tasks";
import { ApiError, ConnectionChangedError, getServerUrl, listTasks, type Task } from "@/lib/api/client";
import { notifyConnectionChanged } from "@/lib/connection-events";
import { localTasks } from "@/lib/state/replica";

const mockPush = jest.fn();
let refreshFromLiveEvent: (() => void | Promise<unknown>) | undefined;

jest.mock("expo-router", () => ({ useRouter: () => ({ push: mockPush }) }));
jest.mock("@/lib/api/client", () => ({
  getServerUrl: jest.fn(), listTasks: jest.fn(),
  ApiError: class extends Error {
    readonly status: number;
    constructor(status: number, message: string) { super(message); this.status = status; }
  },
  ConnectionChangedError: class extends Error {
    constructor() { super("La connexion jumelée a changé pendant la requête."); }
  },
}));
jest.mock("@/lib/state/replica", () => ({ localTasks: jest.fn() }));
jest.mock("@/lib/sync/live-sync-context", () => ({
  useLiveRefresh: (refresh: () => void | Promise<unknown>) => { refreshFromLiveEvent = refresh; },
}));

const mockListTasks = jest.mocked(listTasks);
const mockLocalTasks = jest.mocked(localTasks);
const mockGetServerUrl = jest.mocked(getServerUrl);

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: Error) => void;
  const promise = new Promise<T>((settle, fail) => { resolve = settle; reject = fail; });
  return { promise, resolve, reject };
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
    expect(screen.getByTestId("filter-cancelled").props.accessibilityState).toMatchObject({ selected: true });
    expect(screen.queryByTestId("filter-queued")).not.toBeOnTheScreen();
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
    expect(screen.queryByText("Les statuts affichés peuvent être périmés.")).not.toBeOnTheScreen();
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

  it("shows a loading state before the first response instead of an empty list", async () => {
    const pending = deferred<Task[]>();
    mockListTasks.mockReturnValueOnce(pending.promise);
    await render(<TasksScreen />);
    expect(screen.getByText("Chargement des tâches…")).toBeOnTheScreen();
    expect(screen.queryByText("Aucune tâche")).not.toBeOnTheScreen();
    await act(async () => pending.resolve([]));
    expect(await screen.findByText("Aucune tâche")).toBeOnTheScreen();
    expect(screen.queryByTestId("tasks-loading")).not.toBeOnTheScreen();
  });

  it("keeps same-scope rows visible during live refresh and ignores an older response", async () => {
    const older = deferred<Task[]>();
    const newer = deferred<Task[]>();
    mockListTasks.mockResolvedValueOnce([task("Existing")])
      .mockReturnValueOnce(older.promise).mockReturnValueOnce(newer.promise);
    await render(<TasksScreen />);
    expect(await screen.findByText("Existing")).toBeOnTheScreen();
    await act(async () => { void refreshFromLiveEvent?.(); });
    await waitFor(() => expect(mockListTasks).toHaveBeenCalledTimes(2));
    expect(screen.getByText("Existing")).toBeOnTheScreen();
    expect(screen.getByText(/Actualisation… Les statuts affichés proviennent/)).toBeOnTheScreen();
    await act(async () => { void refreshFromLiveEvent?.(); });
    await waitFor(() => expect(mockListTasks).toHaveBeenCalledTimes(3));
    await act(async () => newer.resolve([task("Updated")]));
    await act(async () => older.resolve([task("Obsolete")]));
    expect(screen.getByText("Updated")).toBeOnTheScreen();
    expect(screen.queryByText("Obsolete")).not.toBeOnTheScreen();
    expect(screen.queryByTestId("tasks-loading")).not.toBeOnTheScreen();
  });

  it("retains the last successful rows with stale status and a usable retry after a refresh fails", async () => {
    mockListTasks.mockResolvedValueOnce([task("Existing")])
      .mockRejectedValueOnce(new Error("Réseau indisponible"))
      .mockResolvedValueOnce([task("Updated")]);
    await render(<TasksScreen />);
    expect(await screen.findByText("Existing")).toBeOnTheScreen();
    await act(async () => { await refreshFromLiveEvent?.(); });
    expect(screen.getByText("Existing")).toBeOnTheScreen();
    expect(screen.getByText("Les statuts affichés peuvent être périmés.")).toBeOnTheScreen();
    expect(mockLocalTasks).not.toHaveBeenCalled();
    await fireEvent.press(screen.getByTestId("tasks-connection"));
    expect(mockPush).toHaveBeenLastCalledWith("/settings");
    await fireEvent.press(screen.getByTestId("tasks-retry"));
    expect(await screen.findByText("Updated")).toBeOnTheScreen();
    expect(screen.queryByText("Existing")).not.toBeOnTheScreen();
    expect(screen.queryByText("Les statuts affichés peuvent être périmés.")).not.toBeOnTheScreen();
  });

  it("clears previous-filter rows immediately while the new filter is loading", async () => {
    const pending = deferred<Task[]>();
    mockListTasks.mockResolvedValueOnce([task("Previous", "completed")]).mockReturnValueOnce(pending.promise);
    await render(<TasksScreen />);
    expect(await screen.findByText("Previous")).toBeOnTheScreen();
    await fireEvent.press(screen.getByTestId("filter-running"));
    expect(screen.queryByText("Previous")).not.toBeOnTheScreen();
    expect(screen.getByText("Chargement des tâches…")).toBeOnTheScreen();
    await act(async () => pending.resolve([task("Running")]));
    expect(screen.getByText("Running")).toBeOnTheScreen();
  });

  it("clears rows on a changed origin before the new request completes", async () => {
    const pending = deferred<Task[]>();
    mockListTasks.mockResolvedValueOnce([task("Old server")]).mockReturnValueOnce(pending.promise);
    await render(<TasksScreen />);
    expect(await screen.findByText("Old server")).toBeOnTheScreen();
    mockGetServerUrl.mockResolvedValue("https://other.example");
    await act(async () => { void refreshFromLiveEvent?.(); });
    await waitFor(() => expect(mockListTasks).toHaveBeenCalledTimes(2));
    expect(screen.queryByText("Old server")).not.toBeOnTheScreen();
    await act(async () => pending.resolve([task("New server")]));
    expect(screen.getByText("New server")).toBeOnTheScreen();
  });

  it("clears rows for a new token on the same origin and discards its older in-flight response", async () => {
    const pending = deferred<Task[]>();
    mockListTasks.mockResolvedValueOnce([task("Old pairing")]).mockReturnValueOnce(pending.promise)
      .mockResolvedValueOnce([task("New pairing")]);
    await render(<TasksScreen />);
    expect(await screen.findByText("Old pairing")).toBeOnTheScreen();
    await act(async () => { void refreshFromLiveEvent?.(); });
    await waitFor(() => expect(mockListTasks).toHaveBeenCalledTimes(2));
    await act(async () => notifyConnectionChanged());
    expect(screen.queryByText("Old pairing")).not.toBeOnTheScreen();
    expect(screen.getByText(/Le jumelage a changé/)).toBeOnTheScreen();
    await act(async () => pending.resolve([task("Late private result")]));
    expect(screen.queryByText("Late private result")).not.toBeOnTheScreen();
    expect(mockLocalTasks).not.toHaveBeenCalled();
    await fireEvent.press(screen.getByTestId("tasks-retry"));
    expect(await screen.findByText("New pairing")).toBeOnTheScreen();
  });

  it("never falls back to private cached rows after the API rejects a changed connection", async () => {
    mockListTasks.mockResolvedValueOnce([task("Old pairing")]).mockRejectedValueOnce(new ConnectionChangedError());
    mockLocalTasks.mockResolvedValue([task("Private cache")]);
    await render(<TasksScreen />);
    expect(await screen.findByText("Old pairing")).toBeOnTheScreen();
    await act(async () => { await refreshFromLiveEvent?.(); });
    expect(screen.queryByText("Old pairing")).not.toBeOnTheScreen();
    expect(screen.queryByText("Private cache")).not.toBeOnTheScreen();
    expect(mockLocalTasks).not.toHaveBeenCalled();
    expect(screen.getByText(/La connexion jumelée a changé/)).toBeOnTheScreen();
  });

  it.each([401, 403])("does not retain or read cache after an authorization failure (%s)", async (status) => {
    mockListTasks.mockResolvedValueOnce([task("Previously authorized")])
      .mockRejectedValueOnce(new ApiError(status, "Autorisation refusée"));
    await render(<TasksScreen />);
    expect(await screen.findByText("Previously authorized")).toBeOnTheScreen();
    await act(async () => { await refreshFromLiveEvent?.(); });
    expect(screen.queryByText("Previously authorized")).not.toBeOnTheScreen();
    expect(mockLocalTasks).not.toHaveBeenCalled();
    expect(screen.getByTestId("tasks-connection")).toBeOnTheScreen();
  });

  it("rejects an in-flight cache result when pairing changes on the same origin", async () => {
    const cached = deferred<Task[]>();
    mockListTasks.mockRejectedValueOnce(new Error("Offline"));
    mockLocalTasks.mockReturnValueOnce(cached.promise);
    await render(<TasksScreen />);
    await waitFor(() => expect(mockLocalTasks).toHaveBeenCalledTimes(1));
    await act(async () => notifyConnectionChanged());
    await act(async () => cached.resolve([task("Wrong pairing cache")]));
    expect(screen.queryByText("Wrong pairing cache")).not.toBeOnTheScreen();
    expect(screen.getByText(/Le jumelage a changé/)).toBeOnTheScreen();
  });

  it("provides a useful action for empty results and resets a restrictive filter", async () => {
    mockListTasks.mockResolvedValue([]);
    await render(<TasksScreen />);
    await fireEvent.press(await screen.findByTestId("tasks-empty-action"));
    expect(mockPush).toHaveBeenLastCalledWith("/");
    await fireEvent.press(screen.getByTestId("filter-running"));
    expect(await screen.findByText("Aucune tâche dans ce filtre")).toBeOnTheScreen();
    await fireEvent.press(screen.getByTestId("tasks-empty-action"));
    await waitFor(() => expect(mockListTasks).toHaveBeenLastCalledWith(undefined));
    expect(screen.getByTestId("filter-all").props.accessibilityState).toMatchObject({ selected: true });
  });
});
