import { act, render, screen, userEvent } from "@testing-library/react-native";
import { beforeEach, describe, expect, it, jest } from "@jest/globals";
import { AppState } from "react-native";
import { LiveSyncContextProvider } from "@/lib/sync/live-sync-context";
import { ActivityTimeline } from "@/components/activity-timeline";
import { ApiError, getActivity, type ActivityPage } from "@/lib/application-api/server";
import { notifyConnectionChanged } from "@/lib/connection-events";
import { activityItem, activityPage } from "@/testing/activity-fixtures";

jest.mock("@/lib/application-api/server", () => ({
  getActivity: jest.fn(),
  ApiError: class extends Error { status: number; constructor(status: number, message: string) { super(message); this.status = status; } },
}));
const load = jest.mocked(getActivity);
const props = { scope: "goal" as const, id: "goal_1", enabled: true, refreshKey: 0 };
function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((next) => { resolve = next; });
  return { resolve, promise };
}
async function open() {
  const user = userEvent.setup();
  await render(<ActivityTimeline {...props} />);
  await user.press(screen.getByRole("button", { name: "Opérations détaillées" }));
  return user;
}

describe("persisted activity timeline", () => {
  beforeEach(() => { jest.restoreAllMocks(); jest.clearAllMocks(); load.mockReset(); AppState.currentState = "active"; load.mockResolvedValue(activityPage()); });
  it("loads on disclosure and shows evidence, with unknown timing left unknown", async () => {
    const model = activityItem("model_call:model_1", { kind: "model_call", role: "planner", title: "Appel modèle", model_id: "local-model-7b", duration_ms: null });
    const check = activityItem("project_check:rev_1:00", { kind: "project_check", title: "Vérification du projet", duration_ms: 0,
      detail: { revision_id: "revision_1", check_index: 0, file_count: 4, exit_code: 0, command: ["python", "-m", "pytest"] } });
    load.mockResolvedValue(activityPage([model, check]));
    const user = userEvent.setup(); await render(<ActivityTimeline {...props} />);
    expect(load).not.toHaveBeenCalled();
    await user.press(screen.getByRole("button", { name: "Opérations détaillées" }));
    expect(await screen.findByText("Modèle : local-model-7b")).toBeOnTheScreen();
    expect(screen.getByText("python -m pytest")).toBeOnTheScreen();
    expect(screen.getByText("Durée non enregistrée")).toBeOnTheScreen();
    expect(screen.getByText("Durée enregistrée : 0 ms")).toBeOnTheScreen();
    expect(screen.getByText(/Seules les preuves enregistrées/)).toBeOnTheScreen();
    expect(screen.queryByText("Révision : revision_1")).not.toBeOnTheScreen();
    await user.press(screen.getByRole("button", { name: "Détails de Vérification du projet" }));
    expect(screen.getByText("Révision : revision_1")).toBeOnTheScreen();
    expect(load).toHaveBeenCalledWith("goal", "goal_1", undefined, expect.any(Function));
  });
  it("deduplicates pagination and replaces paged rows on live refresh", async () => {
    const first = activityItem("first", { title: "Opération récente" });
    const old = activityItem("old", { title: "Opération ancienne" });
    load.mockResolvedValueOnce(activityPage([first], { next_cursor: "page_two", has_more: true }))
      .mockResolvedValueOnce(activityPage([first, old]))
      .mockResolvedValueOnce(activityPage([activityItem("new", { title: "Nouveau relevé" })]));
    const user = await open();
    await user.press(await screen.findByRole("button", { name: "Voir les opérations précédentes" }));
    expect(await screen.findByText("Opération ancienne")).toBeOnTheScreen();
    expect(screen.getAllByText("Opération récente")).toHaveLength(1);
    expect(load.mock.calls[1][2]).toBe("page_two");
    await screen.rerender(<ActivityTimeline {...props} refreshKey={1} />);
    expect(await screen.findByText("Nouveau relevé")).toBeOnTheScreen();
    expect(screen.queryByText("Opération ancienne")).not.toBeOnTheScreen();
    expect(screen.queryByText("Opération récente")).not.toBeOnTheScreen();
    expect(load.mock.calls[2][2]).toBeUndefined();
  });
  it("requires explicit refresh after 409 even across live events", async () => {
    load.mockResolvedValueOnce(activityPage([activityItem()], { has_more: true, next_cursor: "old_cursor" }))
      .mockRejectedValueOnce(new ApiError(409, "private cursor context"))
      .mockResolvedValueOnce(activityPage([activityItem("new", { title: "Relevé renouvelé" })]));
    const user = await open();
    await user.press(await screen.findByRole("button", { name: "Voir les opérations précédentes" }));
    expect(await screen.findByText(/L’historique a changé/)).toBeOnTheScreen();
    expect(screen.getByRole("button", { name: "Voir les opérations précédentes" })).toBeDisabled();
    await screen.rerender(<ActivityTimeline {...props} refreshKey={1} />);
    expect(load).toHaveBeenCalledTimes(2);
    await user.press(screen.getByRole("button", { name: "Actualiser les opérations" }));
    expect(await screen.findByText("Relevé renouvelé")).toBeOnTheScreen();
    expect(load.mock.calls[2][2]).toBeUndefined();
    expect(screen.queryByText("private cursor context")).not.toBeOnTheScreen();
  });
  it("reports 404 availability, not empty evidence", async () => {
    load.mockRejectedValueOnce(new ApiError(404, "not deployed")); await open();
    expect(await screen.findByText(/ne sont pas disponibles pour ce travail/)).toBeOnTheScreen();
    expect(screen.queryByText(/Aucune opération enregistrée/)).not.toBeOnTheScreen();
  });
  it("shows empty evidence only after successful loading", async () => {
    const pending = deferred<ActivityPage>(); load.mockReturnValue(pending.promise); await open();
    expect(screen.getByText("Chargement des preuves enregistrées…")).toBeOnTheScreen();
    expect(screen.queryByText(/Aucune opération enregistrée/)).not.toBeOnTheScreen();
    await act(async () => pending.resolve(activityPage([])));
    expect(screen.getByText("Aucune opération enregistrée dans ce relevé.")).toBeOnTheScreen();
  });
  it("retains prior evidence as unconfirmed after a read error", async () => {
    load.mockResolvedValueOnce(activityPage()).mockRejectedValueOnce(new Error("secret host log"));
    const user = await open(); await user.press(screen.getByRole("button", { name: "Actualiser les opérations" }));
    expect(await screen.findByText(/n’ont pas pu être chargées/)).toBeOnTheScreen();
    expect(screen.getByText("Travail d’agent")).toBeOnTheScreen();
    expect(screen.getByText(/Dernier relevé conservé/)).toBeOnTheScreen();
    expect(screen.queryByText("secret host log")).not.toBeOnTheScreen();
  });
  it.each(["pairing", "identity", "scope", "offline", "closed", "unmount"] as const)("invalidates delayed data after %s changes", async (reason) => {
    const pending = deferred<ActivityPage>(); load.mockReturnValue(pending.promise);
    const user = await open(); const accepts = load.mock.calls[0][3]!; expect(accepts()).toBe(true);
    if (reason === "pairing") await act(async () => notifyConnectionChanged());
    if (reason === "identity") await screen.rerender(<ActivityTimeline {...props} id="goal_2" />);
    if (reason === "scope") await screen.rerender(<ActivityTimeline {...props} scope="task" />);
    if (reason === "offline") await screen.rerender(<ActivityTimeline {...props} enabled={false} />);
    if (reason === "closed") await user.press(screen.getByRole("button", { name: "Opérations détaillées" }));
    if (reason === "unmount") await screen.unmount();
    expect(accepts()).toBe(false);
    await act(async () => pending.resolve(activityPage([activityItem("late", { title: "Réponse périmée" })])));
    if (reason !== "unmount") expect(screen.queryByText("Réponse périmée")).not.toBeOnTheScreen();
    if (reason === "offline") expect(screen.getByRole("button", { name: "Actualiser les opérations" })).toBeDisabled();
    expect(load).toHaveBeenCalledTimes(1);
  });
  it("clears loaded rows and their cursor on a pairing change", async () => {
    load.mockResolvedValue(activityPage([activityItem()], { has_more: true, next_cursor: "private_cursor" })); await open();
    await act(async () => notifyConnectionChanged());
    expect(screen.queryByText("Travail d’agent")).not.toBeOnTheScreen();
    expect(screen.queryByRole("button", { name: "Voir les opérations précédentes" })).not.toBeOnTheScreen();
    expect(screen.getByText(/Le jumelage a changé/)).toBeOnTheScreen();
  });
  it("refresh supersedes an older page which resolves last", async () => {
    const pending = deferred<ActivityPage>();
    load.mockResolvedValueOnce(activityPage([activityItem()], { has_more: true, next_cursor: "old_page" }))
      .mockReturnValueOnce(pending.promise).mockResolvedValueOnce(activityPage([activityItem("fresh", { title: "Page actuelle" })]));
    const user = await open(); await user.press(await screen.findByRole("button", { name: "Voir les opérations précédentes" }));
    const acceptsOlder = load.mock.calls[1][3]!;
    await screen.rerender(<ActivityTimeline {...props} refreshKey={2} />);
    expect(acceptsOlder()).toBe(false); expect(await screen.findByText("Page actuelle")).toBeOnTheScreen();
    await act(async () => pending.resolve(activityPage([activityItem("late", { title: "Page ancienne retardée" })])));
    expect(screen.queryByText("Page ancienne retardée")).not.toBeOnTheScreen();
    expect(screen.getByText("Page actuelle")).toBeOnTheScreen();
  });
  it("reloads only open panels on live activity revisions", async () => {
    const user = userEvent.setup();
    const view = (revision: number) => <LiveSyncContextProvider value={{ revision, error: null, state: "connected" }}><ActivityTimeline {...props} /></LiveSyncContextProvider>;
    await render(view(0)); await screen.rerender(view(1));
    await new Promise((resolve) => setTimeout(resolve, 180));
    expect(load).not.toHaveBeenCalled();
    await user.press(screen.getByRole("button", { name: "Opérations détaillées" }));
    load.mockResolvedValueOnce(activityPage([activityItem("live", { title: "Nouvelle opération enregistrée" })]));
    await screen.rerender(view(2));
    expect(await screen.findByText("Nouvelle opération enregistrée")).toBeOnTheScreen();
    expect(load).toHaveBeenCalledTimes(2);
  });
  it("invalidates background reads and obtains a fresh first page when active", async () => {
    let listener: ((state: import("react-native").AppStateStatus) => void) | undefined;
    jest.spyOn(AppState, "addEventListener").mockImplementation((_event, callback) => { listener = callback; return { remove: jest.fn() }; });
    const pending = deferred<ActivityPage>();
    load.mockReturnValueOnce(pending.promise).mockResolvedValueOnce(activityPage([activityItem("active", { title: "Relevé au retour" })]));
    await open(); const accepts = load.mock.calls[0][3]!;
    await act(async () => listener!("background")); expect(accepts()).toBe(false);
    await act(async () => pending.resolve(activityPage([activityItem("background", { title: "Ancienne réponse" })])));
    expect(screen.queryByText("Ancienne réponse")).not.toBeOnTheScreen();
    await act(async () => listener!("active"));
    expect(await screen.findByText("Relevé au retour")).toBeOnTheScreen();
    expect(load.mock.calls[1][2]).toBeUndefined();
  });

});
