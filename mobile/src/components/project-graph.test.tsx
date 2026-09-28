import { act, fireEvent, render, renderHook, screen, userEvent, waitFor } from "@testing-library/react-native";
import { beforeEach, describe, expect, it, jest } from "@jest/globals";
import { AppState } from "react-native";
import { ProjectGraphEvidence, ProjectGraphPlan, projectLayers, useProjectGraph } from "./project-graph";
import { projectGraphFixture } from "@/testing/project-graph-fixtures";
import { getProjectGraph } from "@/lib/application-api/server";

const mockListeners = new Set<() => void>();
let mockLiveState = "connected";
jest.mock("@/lib/application-api/server", () => ({ getProjectGraph: jest.fn(), ApiError: class extends Error { status = 500; } }));
jest.mock("@/lib/connection-events", () => ({ subscribeConnectionChanges: (listener: () => void) => { mockListeners.add(listener); return () => mockListeners.delete(listener); } }));
jest.mock("@/lib/sync/live-sync-context", () => ({ useLiveRefresh: jest.fn(), useLiveSync: () => ({ state: mockLiveState }) }));

beforeEach(() => { jest.clearAllMocks(); mockListeners.clear(); mockLiveState = "connected";
  Object.defineProperty(AppState, "currentState", { configurable: true, value: "active" });
  jest.mocked(getProjectGraph).mockResolvedValue(projectGraphFixture()); });

it("shows parallel branches with explicit dependencies, selection and only recorded explanations", async () => {
  const graph = projectGraphFixture(); const select = jest.fn();
  await render(<ProjectGraphPlan state={{ graph, busy: false, stale: false, error: null, refresh: jest.fn(async () => {}) }}
    fallbackNodes={[]} enabled selectedNodeId="node_plan" onSelectNode={select} />);
  expect(screen.getByText(graph.planning_decisions[0].rationale_summary)).toBeOnTheScreen();
  expect(screen.getByText("Après : Lire les disponibilités")).toBeOnTheScreen();
  expect(screen.getByText("Apport facultatif : Classer les priorités")).toBeOnTheScreen();
  expect(screen.queryByText(/Modèle : planner-preview/)).not.toBeOnTheScreen();
  await userEvent.setup().press(screen.getByRole("button", { name: "Étape : Préparer le planning" }));
  expect(select).toHaveBeenCalledWith("node_plan");
  expect(projectLayers(graph.nodes, graph.dependencies)?.map((layer) => layer.map((node) => node.id))).toEqual([
    ["node_availability", "node_priorities"], ["node_plan"], ["node_review"],
  ]);
  await fireEvent(screen.getByTestId("project-dependency-map"), "layout", { nativeEvent: { layout: { width: 240 } } });
  expect(screen.getAllByRole("button", { name: /^Étape :/ })).toHaveLength(4);
});

it("does not invent a reason for historical plans or infer criterion completion", async () => {
  const graph = projectGraphFixture(); graph.planning_decisions = []; graph.coverage.planner_rationale = "not_recorded";
  await render(<ProjectGraphPlan state={{ graph, busy: false, stale: true, error: null, refresh: jest.fn(async () => {}) }} fallbackNodes={[]} enabled selectedNodeId={null} onSelectNode={jest.fn()} />);
  expect(screen.getByText("Aucune explication initiale du plan n’a été enregistrée.")).toBeOnTheScreen();
  expect(screen.getByText(/état actuel non confirmé/)).toBeOnTheScreen();
  expect(screen.queryByRole("progressbar")).not.toBeOnTheScreen();
});

it("labels revision provenance and unestablished freshness without calling the project complete", async () => {
  const graph = projectGraphFixture(); graph.latest_revision = { id: "rev1", project_id: "project_preview", goal_run_id: "older_goal", node_id: "older_node", worker_job_id: "older_job", revision: 1, sha256: "a".repeat(64), created_at: graph.observed_at, files: [], checks: [] };
  await render(<ProjectGraphEvidence graph={graph} stale={false} />);
  expect(screen.getByText(/elle n’a pas été produite par ce but/)).toBeOnTheScreen();
  expect(screen.getByText(/Leur actualité et la couverture des exigences ne sont pas établies/)).toBeOnTheScreen();
});

it("rejects a cycle rather than drawing invented order", () => {
  const graph = projectGraphFixture(); graph.dependencies.push({ from_node_id: "node_review", to_node_id: "node_plan", dependency_type: "hard" });
  expect(projectLayers(graph.nodes, graph.dependencies)).toBeNull();
});

describe("scoped graph refresh", () => {
  it("loads a readonly graph and clears it on pairing change; explicit refresh is required", async () => {
    const { result } = await renderHook(() => useProjectGraph("goal_preview", true, "v1"));
    await waitFor(() => expect(result.current.graph?.goal.id).toBe("goal_preview"));
    await act(async () => { mockListeners.forEach((listener) => listener()); });
    expect(result.current.graph).toBeNull();
    await act(async () => result.current.refresh());
    expect(getProjectGraph).toHaveBeenCalledTimes(1);
    await act(async () => result.current.refresh(true));
    expect(getProjectGraph).toHaveBeenCalledTimes(2);
  });
  it("fences pending responses after a pairing change", async () => {
    let resolve!: (value: ReturnType<typeof projectGraphFixture>) => void;
    jest.mocked(getProjectGraph).mockReturnValue(new Promise((settle) => { resolve = settle; }));
    const { result } = await renderHook(() => useProjectGraph("goal_preview", true, "v1"));
    await act(async () => { mockListeners.forEach((listener) => listener()); resolve(projectGraphFixture()); });
    expect(result.current.graph).toBeNull(); expect(result.current.stale).toBe(true);
  });
  it("keeps a received graph visibly unconfirmed when live synchronization disconnects", async () => {
    const { result, rerender } = await renderHook(() => useProjectGraph("goal_preview", true, "v1"));
    await waitFor(() => expect(result.current.graph).not.toBeNull());
    mockLiveState = "disconnected"; await rerender(undefined);
    expect(result.current.graph).not.toBeNull(); expect(result.current.stale).toBe(true);
  });
});
