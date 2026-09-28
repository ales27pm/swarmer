import { act, fireEvent, render, renderHook, screen, userEvent, waitFor } from "@testing-library/react-native";
import { beforeEach, describe, expect, it, jest } from "@jest/globals";
import { AppState } from "react-native";
import { getProjectEvidence, putProjectEvidence } from "@/lib/application-api/server";
import type { ProjectEvidenceView } from "@/lib/api/project-evidence";
import { evidenceFixture, evidenceMappingFixture } from "@/testing/project-evidence-fixtures";
import { ProjectRequirementEvidence, useProjectEvidence, type ProjectEvidenceState } from "./project-requirement-evidence";

const mockListeners = new Set<() => void>();
let mockLiveState = "connected";
jest.mock("@/lib/application-api/server", () => ({ getProjectEvidence: jest.fn(), putProjectEvidence: jest.fn() }));
jest.mock("@/lib/connection-events", () => ({ subscribeConnectionChanges: (listener: () => void) => { mockListeners.add(listener); return () => mockListeners.delete(listener); } }));
jest.mock("@/lib/sync/live-sync-context", () => ({ useLiveRefresh: jest.fn(), useLiveSync: () => ({ state: mockLiveState }) }));

function stateFor(view = evidenceFixture()): ProjectEvidenceState {
  return { view, busy: false, saving: false, stale: false, error: null, connectionGeneration: 0,
    refresh: jest.fn(async () => {}), save: jest.fn(async () => null) };
}
function deferred<T>() {
  let resolve!: (value: T) => void;
  return { promise: new Promise<T>((settle) => { resolve = settle; }), resolve: (value: T) => resolve(value) };
}
function writeBody(view = evidenceFixture()) {
  const revision = view.current_revision!;
  return { expected_version: view.criteria[0].mapping?.version ?? 0, context_sha256: view.context_sha256,
    conversation_revision: view.conversation_revision, criterion_sha256: view.criteria[0].sha256,
    project_id: revision.project_id, node_id: revision.node_id, revision_id: revision.id, revision_sha256: revision.sha256,
    file_ids: ["file_1"], check_ids: [], review_status: "linked" as const, public_explanation: "" };
}
const requirement = "Exigence 1 : Le planning respecte mes disponibilités";

beforeEach(() => {
  jest.clearAllMocks(); mockListeners.clear(); mockLiveState = "connected";
  Object.defineProperty(AppState, "currentState", { configurable: true, value: "active" });
  jest.mocked(getProjectEvidence).mockResolvedValue(evidenceFixture());
  jest.mocked(putProjectEvidence).mockResolvedValue(evidenceFixture());
});

it("keeps criteria collapsed and permits review only when every selected check passes", async () => {
  const state = stateFor(); const user = userEvent.setup();
  await render(<ProjectRequirementEvidence state={state} />);
  expect(screen.queryByRole("checkbox")).not.toBeOnTheScreen();
  await user.press(screen.getByRole("button", { name: requirement }));
  expect(screen.getByRole("button", { name: "Enregistrer les liens de preuve" })).toBeDisabled();
  await user.press(screen.getByRole("checkbox", { name: "Fichier : planning.md" }));
  expect(screen.getByRole("checkbox", { name: "J’ai revu ces preuves pour cette exigence" })).toBeDisabled();
  await user.press(screen.getByRole("button", { name: "Enregistrer les liens de preuve" }));
  expect(state.save).toHaveBeenLastCalledWith(0, expect.objectContaining({ file_ids: ["file_1"], check_ids: [], review_status: "linked" }));
  await user.press(screen.getByRole("checkbox", { name: /Contrôle 2/ }));
  expect(screen.getByRole("checkbox", { name: "J’ai revu ces preuves pour cette exigence" })).toBeDisabled();
  await user.press(screen.getByRole("checkbox", { name: /Contrôle 1/ }));
  expect(screen.getByRole("checkbox", { name: "J’ai revu ces preuves pour cette exigence" })).toBeDisabled();
  await user.press(screen.getByRole("button", { name: "Enregistrer les liens de preuve" }));
  expect(state.save).toHaveBeenLastCalledWith(0, expect.objectContaining({ check_ids: ["check_2", "check_1"], review_status: "linked" }));
  await user.press(screen.getByRole("checkbox", { name: /Contrôle 2/ }));
  await user.press(screen.getByRole("checkbox", { name: "J’ai revu ces preuves pour cette exigence" }));
  await user.press(screen.getByRole("button", { name: "Enregistrer la revue explicite" }));
  expect(state.save).toHaveBeenLastCalledWith(0, expect.objectContaining({ check_ids: ["check_1"], review_status: "reviewed" }));
});

it("does not accept a passed label whose exit code is nonzero", async () => {
  const state = stateFor(); state.view!.current_revision!.checks[0].exit_code = 1;
  const user = userEvent.setup(); await render(<ProjectRequirementEvidence state={state} />);
  await user.press(screen.getByRole("button", { name: requirement }));
  await user.press(screen.getByRole("checkbox", { name: /Contrôle 1/ }));
  expect(screen.getByRole("checkbox", { name: "J’ai revu ces preuves pour cette exigence" })).toBeDisabled();
});

it("shows linked and stale mappings without claiming an explicit review", async () => {
  const view = evidenceFixture(); view.criteria[0].mapping = evidenceMappingFixture(view); view.criteria[0].status = "linked";
  const state = stateFor(view); const rendered = await render(<ProjectRequirementEvidence state={state} />);
  expect(screen.getByText("0/1 exigences revues explicitement · 1 avec des preuves liées")).toBeOnTheScreen();
  expect(screen.queryByText(/Revu explicitement/)).not.toBeOnTheScreen();
  view.criteria[0].status = "stale"; view.criteria[0].mapping.stale_reasons = ["revision_changed"];
  await rendered.rerender(<ProjectRequirementEvidence state={{ ...state }} />);
  expect(screen.getByText(/Preuves à revoir/)).toBeOnTheScreen();
  expect(screen.getByText("0/1 exigences revues explicitement · 0 avec des preuves liées")).toBeOnTheScreen();
});

it("shows the saved revision and producer separately from the replacement evidence candidates", async () => {
  const view = evidenceFixture(); view.criteria[0].mapping = evidenceMappingFixture(view); view.criteria[0].status = "stale";
  view.criteria[0].mapping.stale_reasons = ["revision_changed", "producer_changed"];
  view.current_revision!.id = "revision_2"; view.current_revision!.revision = 2; view.current_revision!.node_id = "node_revised_plan";
  await render(<ProjectRequirementEvidence state={stateFor(view)} />);
  await userEvent.setup().press(screen.getByRole("button", { name: requirement }));
  expect(screen.getByText("Révision liée : revision_1")).toBeOnTheScreen();
  expect(screen.getByText("Exigence liée : Le planning respecte mes disponibilités")).toBeOnTheScreen();
  expect(screen.getByText("Étape liée : node_plan")).toBeOnTheScreen();
  expect(screen.getByText("Choisir les preuves · révision 2")).toBeOnTheScreen();
  expect(screen.getByText(/Étape productrice : node_revised_plan/)).toBeOnTheScreen();
});

it("preserves the note offline and requires reselecting evidence after a snapshot change", async () => {
  const state = stateFor(); const user = userEvent.setup(); const rendered = await render(<ProjectRequirementEvidence state={state} />);
  await user.press(screen.getByRole("button", { name: requirement }));
  await user.press(screen.getByRole("checkbox", { name: "Fichier : planning.md" }));
  await fireEvent.changeText(screen.getByLabelText("Note de revue pour l’exigence 1"), "Mon observation conservée");
  await rendered.rerender(<ProjectRequirementEvidence state={{ ...state, stale: true }} disabled />);
  expect(screen.getByLabelText("Note de revue pour l’exigence 1")).toHaveProp("value", "Mon observation conservée");
  expect(screen.getByRole("button", { name: "Enregistrer les liens de preuve" })).toBeDisabled();
  const next = evidenceFixture(); next.context_sha256 = "e".repeat(64);
  await rendered.rerender(<ProjectRequirementEvidence state={{ ...state, view: next }} />);
  expect(screen.getByRole("button", { name: "Enregistrer les liens de preuve" })).toBeDisabled();
  await user.press(screen.getByRole("button", { name: "Utiliser la révision actuelle" }));
  expect(screen.getByRole("checkbox", { name: "Fichier : planning.md" })).toHaveProp("accessibilityState", expect.objectContaining({ checked: false }));
  expect(screen.getByLabelText("Note de revue pour l’exigence 1")).toHaveProp("value", "Mon observation conservée");
  expect(state.save).not.toHaveBeenCalled();
});

it("blocks mapping a revision produced by a prior goal", async () => {
  const state = stateFor(); state.view!.current_revision!.goal_run_id = "older_goal";
  const user = userEvent.setup(); await render(<ProjectRequirementEvidence state={state} />);
  await user.press(screen.getByRole("button", { name: requirement }));
  expect(screen.getByText(/Cette révision provient d’un autre but/)).toBeOnTheScreen();
  expect(screen.getByRole("button", { name: "Enregistrer les liens de preuve" })).toBeDisabled();
});

it("does not carry a selection or positive review badge across a new pairing with identical data", async () => {
  const view = evidenceFixture(); view.criteria[0].mapping = evidenceMappingFixture(view);
  view.criteria[0].mapping.review_status = "reviewed"; view.criteria[0].mapping.reviewed_at = view.observed_at;
  view.criteria[0].status = "reviewed";
  const state = stateFor(view); const user = userEvent.setup(); const rendered = await render(<ProjectRequirementEvidence state={state} />);
  expect(screen.getByText(/Revu explicitement/)).toBeOnTheScreen();
  await user.press(screen.getByRole("button", { name: requirement }));
  await fireEvent.changeText(screen.getByLabelText("Note de revue pour l’exigence 1"), "Note en cours");
  await rendered.rerender(<ProjectRequirementEvidence state={{ ...state, connectionGeneration: 1, stale: true }} />);
  expect(screen.queryByText(/Revu explicitement/)).not.toBeOnTheScreen();
  await rendered.rerender(<ProjectRequirementEvidence state={{ ...state, connectionGeneration: 1 }} />);
  expect(screen.getByRole("button", { name: "Enregistrer les liens de preuve" })).toBeDisabled();
  expect(screen.getByLabelText("Note de revue pour l’exigence 1")).toHaveProp("value", "Note en cours");
  await user.press(screen.getByRole("button", { name: "Utiliser la révision actuelle" }));
  expect(screen.getByRole("checkbox", { name: "Fichier : planning.md" })).toHaveProp("accessibilityState", expect.objectContaining({ checked: false }));
});

describe("evidence connection and mutation fencing", () => {
  it("keeps received coverage unconfirmed on disconnect and pairing change", async () => {
    const { result, rerender } = await renderHook(() => useProjectEvidence("goal_preview", true, "v1"));
    await waitFor(() => expect(result.current.stale).toBe(false));
    mockLiveState = "disconnected"; await rerender(undefined);
    expect(result.current.view).not.toBeNull(); expect(result.current.stale).toBe(true);
    await act(async () => { mockListeners.forEach((listener) => listener()); });
    mockLiveState = "connected"; await rerender(undefined);
    expect(getProjectEvidence).toHaveBeenCalledTimes(1);
    await act(async () => result.current.save(0, writeBody()));
    expect(putProjectEvidence).not.toHaveBeenCalled();
    await act(async () => result.current.refresh(true));
    expect(result.current.stale).toBe(false);
  });
  it("fences pending reads from a former pairing", async () => {
    const pending = deferred<ProjectEvidenceView>(); jest.mocked(getProjectEvidence).mockReturnValue(pending.promise);
    const { result } = await renderHook(() => useProjectEvidence("goal_preview", true, "v1"));
    const accepts = jest.mocked(getProjectEvidence).mock.calls[0][1]!;
    await act(async () => { mockListeners.forEach((listener) => listener()); pending.resolve(evidenceFixture()); });
    expect(accepts()).toBe(false); expect(result.current.view).toBeNull();
  });
  it("fences a submitted write and never replays it on a connection change", async () => {
    const pending = deferred<ProjectEvidenceView>(); jest.mocked(putProjectEvidence).mockReturnValue(pending.promise);
    const { result } = await renderHook(() => useProjectEvidence("goal_preview", true, "v1"));
    await waitFor(() => expect(result.current.stale).toBe(false));
    let save!: Promise<ProjectEvidenceView | null>;
    await act(async () => { save = result.current.save(0, writeBody()); });
    const accepts = jest.mocked(putProjectEvidence).mock.calls[0][3]!;
    await act(async () => { mockListeners.forEach((listener) => listener()); pending.resolve(evidenceFixture()); await save; });
    expect(accepts()).toBe(false); expect(result.current.stale).toBe(true);
    await act(async () => result.current.refresh(true));
    expect(putProjectEvidence).toHaveBeenCalledTimes(1);
  });
  it("requires refresh after an uncertain write; explicit identical retry keeps its request id", async () => {
    jest.mocked(putProjectEvidence).mockRejectedValue(new Error("Network failure"));
    const { result } = await renderHook(() => useProjectEvidence("goal_preview", true, "v1"));
    await waitFor(() => expect(result.current.stale).toBe(false));
    await act(async () => result.current.save(0, writeBody()));
    expect(result.current.stale).toBe(true);
    await act(async () => result.current.save(0, writeBody()));
    expect(putProjectEvidence).toHaveBeenCalledTimes(1);
    await act(async () => result.current.refresh(true));
    await act(async () => result.current.save(0, writeBody()));
    const calls = jest.mocked(putProjectEvidence).mock.calls;
    expect(calls).toHaveLength(2); expect(calls[1][2].request_id).toBe(calls[0][2].request_id);
  });
});
