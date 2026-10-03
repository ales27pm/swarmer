import { act, render, screen, userEvent } from "@testing-library/react-native";
import { beforeEach, describe, expect, it, jest } from "@jest/globals";
import { AppState } from "react-native";
import { LiveSyncContextProvider } from "@/lib/sync/live-sync-context";
import { ActivityTimeline } from "@/components/activity-timeline";
import { ApiError, getActivity, type ActivityPage, type PlanNode } from "@/lib/application-api/server";
import { notifyConnectionChanged } from "@/lib/connection-events";
import { activityItem, activityPage } from "@/testing/activity-fixtures";
import { projectGraphFixture } from "@/testing/project-graph-fixtures";

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
  it.each([
    { role: "memory_normalizer", label: "Préparation de la recherche" },
    { role: "memory_reviewer", label: "Vérification de la traduction" },
    { role: "memory_presenter", label: "Présentation des souvenirs" },
    { role: "memory_presentation_reviewer", label: "Vérification de la présentation" },
    { role: "memory_embedder", label: "Recherche sémantique" },
  ] as const)("labels $role in the live summary and detailed history", async ({ role, label }) => {
    load.mockResolvedValue(activityPage([activityItem(`model_call:${role}`, {
      kind: "model_call", role, title: "Appel modèle", node_id: null, tool_name: null,
    })]));
    const user = userEvent.setup();
    await render(<ActivityTimeline {...props} follow />);
    expect(await screen.findByText(`Modèle · ${label} · Terminée`)).toBeOnTheScreen();
    expect(screen.queryByText(new RegExp(role))).not.toBeOnTheScreen();
    await user.press(screen.getByRole("button", { name: "Opérations détaillées" }));
    expect(await screen.findByText(`Modèle · ${label}`)).toBeOnTheScreen();
    expect(screen.queryByText(new RegExp(role))).not.toBeOnTheScreen();
    expect(load).toHaveBeenCalledTimes(1);
  });
  it("shows compact evidence and discloses technical details without estimating unknown timing", async () => {
    const model = activityItem("model_call:model_1", { kind: "model_call", role: "planner", title: "Appel modèle", model_id: "local-model-7b", duration_ms: null });
    const check = activityItem("project_check:rev_1:00", { kind: "project_check", title: "Vérification du projet", duration_ms: 0,
      detail: { revision_id: "revision_1", check_index: 0, file_count: 4, exit_code: 0, command: ["python", "-m", "pytest"] } });
    load.mockResolvedValue(activityPage([model, check]));
    const user = userEvent.setup(); await render(<ActivityTimeline {...props} />);
    expect(load).not.toHaveBeenCalled();
    await user.press(screen.getByRole("button", { name: "Opérations détaillées" }));
    expect(await screen.findByText("Appel modèle")).toBeOnTheScreen();
    expect(screen.queryByText("Modèle : local-model-7b")).not.toBeOnTheScreen();
    expect(screen.queryByText("python -m pytest")).not.toBeOnTheScreen();
    expect(screen.getByText("Durée non enregistrée")).toBeOnTheScreen();
    expect(screen.getByText("Durée enregistrée : 0 s")).toBeOnTheScreen();
    expect(screen.getByText(/Seules les preuves enregistrées/)).toBeOnTheScreen();
    expect(screen.queryByText("Révision : revision_1")).not.toBeOnTheScreen();
    await user.press(screen.getByRole("button", { name: "Détails de Vérification du projet" }));
    expect(screen.getByText("Révision : revision_1")).toBeOnTheScreen();
    expect(screen.getByText("python -m pytest")).toBeOnTheScreen();
    expect(screen.getByText("Code de sortie : 0")).toBeOnTheScreen();
    expect(screen.getByText("Fichiers enregistrés : 4")).toBeOnTheScreen();
    await user.press(screen.getByRole("button", { name: "Détails de Appel modèle" }));
    expect(screen.getByText("Modèle : local-model-7b")).toBeOnTheScreen();
    await user.press(screen.getByRole("button", { name: "Détails de Vérification du projet" }));
    expect(screen.queryByText("python -m pytest")).not.toBeOnTheScreen();
    expect(load).toHaveBeenCalledTimes(1);
    expect(load).toHaveBeenCalledWith("goal", "goal_1", undefined, expect.any(Function));
  });
  it("filters only loaded records and preserves the filter when older pages arrive", async () => {
    const model = activityItem("model_1", { kind: "model_call", title: "Planification locale" });
    const first = activityItem("first", { title: "Agent de recherche" });
    const tool = activityItem("tool_1", { kind: "tool_call", title: "Recherche web" });
    load.mockResolvedValueOnce(activityPage([model, first], { next_cursor: "page_two", has_more: true }))
      .mockResolvedValueOnce(activityPage([first, tool]));
    const user = await open();
    await user.press(await screen.findByRole("button", { name: "Filtrer les opérations" }));
    expect(screen.getByText("Le filtre s’applique uniquement aux opérations déjà chargées.")).toBeOnTheScreen();
    await user.press(screen.getByTestId("activity-filter-tool_call"));
    expect(screen.getByText("0 sur 2 opérations chargées")).toBeOnTheScreen();
    expect(screen.getByText("Aucune opération de ce type parmi celles déjà chargées.")).toBeOnTheScreen();
    expect(screen.queryByText("Aucune opération enregistrée dans ce relevé.")).not.toBeOnTheScreen();
    expect(screen.queryByText("Planification locale")).not.toBeOnTheScreen();
    await user.press(screen.getByRole("button", { name: "Filtrer les opérations" }));
    expect(screen.getByRole("button", { name: "Filtrer les opérations" }).props.accessibilityValue).toEqual({ text: "Outils" });
    expect(screen.getByRole("button", { name: "Filtrer les opérations" }).props.accessibilityState).toMatchObject({ expanded: false });
    expect(screen.queryByTestId("activity-filter-tool_call")).not.toBeOnTheScreen();
    expect(load).toHaveBeenCalledTimes(1);
    await user.press(screen.getByRole("button", { name: "Voir les opérations précédentes" }));
    expect(await screen.findByText("Recherche web")).toBeOnTheScreen();
    expect(screen.getByText("1 sur 3 opérations chargées")).toBeOnTheScreen();
    await user.press(screen.getByRole("button", { name: "Filtrer les opérations" }));
    expect(screen.getByTestId("activity-filter-tool_call").props.accessibilityState).toMatchObject({ selected: true });
    expect(load.mock.calls[1][2]).toBe("page_two");
    await user.press(screen.getByTestId("activity-filter-all"));
    expect(screen.getByText("3 sur 3 opérations chargées")).toBeOnTheScreen();
    expect(screen.getAllByText("Agent de recherche")).toHaveLength(1);
    expect(screen.getByText("Planification locale")).toBeOnTheScreen();
    expect(load).toHaveBeenCalledTimes(2);
  });
  it.each(["pairing", "scope"] as const)("resets presentation filters and expanded details after a %s change", async (change) => {
    load.mockResolvedValueOnce(activityPage([activityItem("old_model", { kind: "model_call", title: "Ancien modèle", model_id: "old-private-model" })]))
      .mockResolvedValueOnce(activityPage([activityItem("new_agent", { title: "Nouvel agent" })]));
    const user = await open();
    await user.press(await screen.findByRole("button", { name: "Filtrer les opérations" }));
    await user.press(screen.getByTestId("activity-filter-model_call"));
    await user.press(screen.getByRole("button", { name: "Détails de Ancien modèle" }));
    expect(screen.getByText("Modèle : old-private-model")).toBeOnTheScreen();
    if (change === "pairing") {
      await act(async () => notifyConnectionChanged());
      expect(screen.queryByText("Modèle : old-private-model")).not.toBeOnTheScreen();
      expect(screen.queryByTestId("activity-filter-model_call")).not.toBeOnTheScreen();
      await user.press(screen.getByRole("button", { name: "Actualiser les opérations" }));
    } else {
      await screen.rerender(<ActivityTimeline {...props} scope="task" id="task_2" />);
      expect(screen.queryByText("Modèle : old-private-model")).not.toBeOnTheScreen();
      await user.press(screen.getByRole("button", { name: "Opérations détaillées" }));
    }
    expect(await screen.findByText("Nouvel agent")).toBeOnTheScreen();
    expect(screen.queryByText("Ancien modèle")).not.toBeOnTheScreen();
    await user.press(screen.getByRole("button", { name: "Filtrer les opérations" }));
    expect(screen.getByTestId("activity-filter-all").props.accessibilityState).toMatchObject({ selected: true });
    expect(screen.queryByText("Agent : agent_1")).not.toBeOnTheScreen();
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

  it("follows recorded operations while collapsed and reveals who is working and its linked task", async () => {
    const onOpenTask = jest.fn();
    const node: PlanNode = { id: "node_1", goal_run_id: "goal_1", node_type: "worker", title: "Chercher les horaires", objective: "Trouver les horaires publiés", status: "running", priority: 1, depends_on: [], created_at: "2026-09-27T12:00:00Z", updated_at: "2026-09-27T12:00:00Z" };
    load.mockResolvedValue(activityPage([activityItem("search", { title: "Recherche web", status: "running", model_id: "modele-local", completed_at: null, duration_ms: null })], { has_more: true, next_cursor: "older" }));
    const view = (revision: number) => <LiveSyncContextProvider value={{ revision, state: "connected", error: null }}><ActivityTimeline {...props} follow nodes={[node]} onOpenTask={onOpenTask} /></LiveSyncContextProvider>;
    await render(view(0));
    expect(await screen.findByText("Recherche web")).toBeOnTheScreen();
    expect(screen.queryByText("Agent : agent_1")).not.toBeOnTheScreen();
    expect(screen.queryByText("Modèle : modele-local")).not.toBeOnTheScreen();
    expect(screen.getByText("Étape liée : Chercher les horaires")).toBeOnTheScreen();
    expect(screen.getByText(/Relevé partiel/)).toBeOnTheScreen();
    expect(screen.getByRole("button", { name: "Opérations détaillées" })).toBeCollapsed();
    const user = userEvent.setup();
    await user.press(screen.getByRole("button", { name: "Ouvrir la tâche de Recherche web" }));
    expect(onOpenTask).toHaveBeenCalledWith("task_1");
    await user.press(screen.getByRole("button", { name: "Opérations détaillées" }));
    expect(screen.queryByText("Agent : agent_1")).not.toBeOnTheScreen();
    await user.press(screen.getByRole("button", { name: "Détails de Recherche web" }));
    expect(screen.getByText("Agent : agent_1")).toBeOnTheScreen();
    expect(screen.getByText("Modèle : modele-local")).toBeOnTheScreen();
    expect(screen.getByText("Outil / compétence : research.query")).toBeOnTheScreen();
    await user.press(screen.getByRole("button", { name: "Opérations détaillées" }));
    load.mockResolvedValue(activityPage([activityItem("search", { title: "Recherche web terminée" })]));
    await screen.rerender(view(1));
    expect(await screen.findByText("Recherche web terminée")).toBeOnTheScreen();
    expect(screen.getByText("Aucune opération active dans ce relevé")).toBeOnTheScreen();
    expect(load).toHaveBeenCalledTimes(2);
  });

  it("marks followed evidence unconfirmed when the event connection drops without inventing progress", async () => {
    load.mockResolvedValue(activityPage([activityItem("running", { status: "running", duration_ms: null, completed_at: null })]));
    const view = (state: "connected" | "disconnected") => <LiveSyncContextProvider value={{ revision: 0, state, error: null }}><ActivityTimeline {...props} follow /></LiveSyncContextProvider>;
    await render(view("connected"));
    await screen.findByText("Travail d’agent");
    await screen.rerender(view("disconnected"));
    expect(screen.getByText("Synchronisation interrompue")).toBeOnTheScreen();
    expect(screen.getByText(/Dernier relevé conservé/)).toBeOnTheScreen();
    expect(screen.getByText("En cours au dernier relevé", { exact: false })).toBeOnTheScreen();
    expect(screen.queryByRole("progressbar")).not.toBeOnTheScreen();
    expect(load).toHaveBeenCalledTimes(1);
  });

  it("clears followed private activity after pairing changes and fences its delayed response", async () => {
    const pending = deferred<ActivityPage>(); load.mockReturnValue(pending.promise);
    await render(<ActivityTimeline {...props} follow />);
    const accepts = load.mock.calls[0][3]!;
    await act(() => notifyConnectionChanged());
    expect(accepts()).toBe(false);
    await act(() => pending.resolve(activityPage([activityItem("private", { title: "Ancien serveur" })])));
    expect(screen.queryByText("Ancien serveur")).not.toBeOnTheScreen();
    expect(screen.getByText(/Le jumelage a changé/)).toBeOnTheScreen();
    expect(screen.queryByText(/Aucune opération active/)).not.toBeOnTheScreen();
  });

  it("filters recorded operations to a graph node without losing details or starting work", async () => {
    load.mockResolvedValue(activityPage([
      activityItem("one", { title: "Premier travail", node_id: "node_1" }),
      activityItem("two", { title: "Autre travail", node_id: "node_2" }),
    ]));
    const user = userEvent.setup();
    await render(<ActivityTimeline {...props} follow selectedNodeId="node_1" />);
    await user.press(screen.getByRole("button", { name: "Opérations détaillées" }));
    expect(await screen.findByText("Premier travail")).toBeOnTheScreen();
    expect(screen.queryByText("Autre travail")).not.toBeOnTheScreen();
    await user.press(screen.getByRole("button", { name: "Détails de Premier travail" }));
    expect(screen.getByText("Opération : one")).toBeOnTheScreen();
    await screen.rerender(<ActivityTimeline {...props} follow selectedNodeId={null} />);
    expect(screen.getByText("Autre travail")).toBeOnTheScreen();
    expect(screen.getByText("Opération : one")).toBeOnTheScreen();
    expect(load).toHaveBeenCalledTimes(1);
  });

  it("links public plan explanations and a declared result by recorded producer IDs", async () => {
    const node: PlanNode = { id: "node_1", goal_run_id: "goal_1", task_id: "task_1", worker_job_id: "job_1", node_type: "worker", title: "Comparer les options", objective: "Comparer les stockages pour une personne", expected_output: "Une note avec deux sources", result_summary: "Une note a été proposée.", status: "completed", priority: 1, depends_on: [], created_at: "2026-09-27T12:00:00Z", updated_at: "2026-09-27T12:00:00Z" };
    const decision = { ...projectGraphFixture().planning_decisions[0], goal_run_id: "goal_1", node_ids: [node.id], rationale_summary: "Chercher les sources avant de comparer." };
    load.mockResolvedValue(activityPage([activityItem()]));
    const results = jest.fn(); const plan = jest.fn();
    await render(<ActivityTimeline {...props} follow intent="Choisir un stockage local" nodes={[node]} planningDecisions={[decision]} contextStale onOpenResults={results} onOpenPlan={plan} />);
    expect(await screen.findByText("Objectif de l’étape : Comparer les stockages pour une personne")).toBeOnTheScreen();
    expect(screen.getByText("Résultat attendu : Une note avec deux sources")).toBeOnTheScreen();
    expect(screen.getByText(decision.rationale_summary)).toBeOnTheScreen();
    expect(screen.getByText(/Elle décrit le plan, pas une preuve/)).toBeOnTheScreen();
    expect(screen.getByText("Résultat déclaré par l’agent : Une note a été proposée.")).toBeOnTheScreen();
    expect(screen.getByText(/Contexte de l’étape conservé/)).toBeOnTheScreen();
    const user = userEvent.setup();
    await user.press(screen.getByRole("button", { name: "Voir les résultats du projet" }));
    await user.press(screen.getByRole("button", { name: "Comprendre le plan" }));
    expect(results).toHaveBeenCalledTimes(1); expect(plan).toHaveBeenCalledTimes(1);
    expect(load).toHaveBeenCalledTimes(1);
  });

  it.each(["goal", "task", "node", "job"] as const)("does not attach another %s's result or explanation", async (mismatch) => {
    const node: PlanNode = { id: "node_1", goal_run_id: "goal_1", task_id: "task_1", worker_job_id: "job_1", node_type: "worker", title: "Étape", objective: "Objectif lié", result_summary: "Résultat réservé", status: "completed", priority: 1, depends_on: [], created_at: "2026-09-27T12:00:00Z", updated_at: "2026-09-27T12:00:00Z" };
    const decision = { ...projectGraphFixture().planning_decisions[0], goal_run_id: "goal_1", node_ids: [node.id], rationale_summary: "Explication réservée" };
    const changes = mismatch === "goal" ? { goal_run_id: "other_goal" } : mismatch === "task" ? { task_id: "other_task" } : mismatch === "node" ? { node_id: "other_node" } : { id: "worker_job:old_job" };
    load.mockResolvedValue(activityPage([activityItem(undefined, changes)]));
    await render(<ActivityTimeline {...props} follow nodes={[node]} planningDecisions={[decision]} />);
    await screen.findByText("Travail d’agent");
    expect(screen.queryByText(/Résultat déclaré par l’agent/)).not.toBeOnTheScreen();
    if (mismatch !== "job") expect(screen.queryByText("Explication réservée")).not.toBeOnTheScreen();
  });

  it("joins a planner explanation only to the exact model call and exposes a proved task/project link", async () => {
    const decision = { ...projectGraphFixture().planning_decisions[0], goal_run_id: "goal_1", model_call_id: "call_1", rationale_summary: "Explication du plan accepté" };
    load.mockResolvedValue(activityPage([activityItem("model_call:call_1", { kind: "model_call", node_id: null, title: "Plan accepté" }), activityItem("model_call:call_2", { kind: "model_call", node_id: null, title: "Autre plan" })]));
    const user = userEvent.setup(); const onOpenGoal = jest.fn();
    await render(<ActivityTimeline {...props} scope="task" id="task_1" planningDecisions={[decision]} onOpenGoal={onOpenGoal} />);
    await user.press(screen.getByRole("button", { name: "Opérations détaillées" }));
    expect(await screen.findAllByText(decision.rationale_summary)).toHaveLength(1);
    await user.press(screen.getByRole("button", { name: "Ouvrir le projet lié" }));
    expect(onOpenGoal).toHaveBeenCalledWith("goal_1");
  });

  it.each<[number, string]>([[34878, "34,9 s"], [62000, "1 min 2 s"], [3605000, "1 h 0 min"], [2, "moins de 1 s"]])("formats a recorded %i ms duration for humans", async (duration_ms, label) => {
    load.mockResolvedValue(activityPage([activityItem(undefined, { duration_ms })]));
    await open();
    expect(await screen.findByText(`Durée enregistrée : ${label}`)).toBeOnTheScreen();
  });

});
