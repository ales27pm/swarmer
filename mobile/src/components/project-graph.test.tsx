import { act, fireEvent, render, renderHook, screen, userEvent, waitFor } from "@testing-library/react-native";
import { beforeEach, describe, expect, it, jest } from "@jest/globals";
import { AppState } from "react-native";
import { ProjectGraphEvidence, ProjectGraphNodeEvidence, ProjectGraphPlan, projectDependencyLayout, projectLayers, useProjectGraph } from "./project-graph";
import { graphNode, projectGraphFixture } from "@/testing/project-graph-fixtures";
import { evidenceFixture, evidenceMappingFixture } from "@/testing/project-evidence-fixtures";
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

function linkedGraph() {
  const graph = projectGraphFixture(); const evidence = evidenceFixture();
  graph.latest_revision = evidence.current_revision;
  graph.criteria = evidence.criteria.map(({ id, index, text }) => ({ id, index, text, coverage: "not_mapped" }));
  evidence.criteria[0].mapping = evidenceMappingFixture(evidence);
  evidence.criteria[0].status = "linked";
  return { graph, evidence };
}

describe("recorded requirement connections", () => {
  it("adds numbered evidence only to the recorded producer, never other completed nodes", async () => {
    const { graph, evidence } = linkedGraph();
    await render(<ProjectGraphPlan state={{ graph, busy: false, stale: false, error: null, refresh: jest.fn(async () => {}) }}
      fallbackNodes={[]} enabled selectedNodeId={null} onSelectNode={jest.fn()} evidence={evidence} />);
    expect(screen.getByText("Exigences 1")).toBeOnTheScreen();
    expect(screen.getByRole("button", { name: "Étape : Préparer le planning" }).props.accessibilityHint).toContain("Exigences 1");
    expect(screen.getByRole("button", { name: "Étape : Lire les disponibilités" }).props.accessibilityHint).not.toContain("Exigences");
    expect(screen.getByRole("button", { name: "Étape : Classer les priorités" }).props.accessibilityHint).not.toContain("Exigences");
  });

  it("shows original criterion and exact revision, separates links from review and opens Results", async () => {
    const { graph, evidence } = linkedGraph(); const open = jest.fn();
    const { rerender } = await render(<ProjectGraphNodeEvidence graph={graph} evidence={evidence} nodeId="node_plan" onOpenEvidence={open} />);
    expect(screen.getByText("Liées : 1 · Revues explicitement : 0 · À revoir : 0")).toBeOnTheScreen();
    expect(screen.getByText(`Exigence 1 : ${evidence.criteria[0].text}`)).toBeOnTheScreen();
    expect(screen.getByText("Révision liée : revision_1 · 1 fichier(s) · 1 contrôle(s)")).toBeOnTheScreen();
    expect(screen.getByText("Preuves liées, sans revue explicite")).toBeOnTheScreen();
    await userEvent.setup().press(screen.getByRole("button", { name: "Voir et modifier les preuves dans Résultats" }));
    expect(open).toHaveBeenCalledTimes(1);
    evidence.criteria[0].status = "reviewed";
    evidence.criteria[0].mapping = { ...evidence.criteria[0].mapping!, review_status: "reviewed", reviewed_at: evidence.observed_at };
    await rerender(<ProjectGraphNodeEvidence graph={graph} evidence={evidence} nodeId="node_plan" onOpenEvidence={open} />);
    expect(screen.getByText("Liées : 0 · Revues explicitement : 1 · À revoir : 0")).toBeOnTheScreen();
    expect(screen.getByText("Revue explicite enregistrée")).toBeOnTheScreen();
  });

  it.each(["offline", "revision", "criterion", "conversation", "receipt", "producer", "file"] as const)("never counts a prior review as current when %s differs", async (difference) => {
    const { graph, evidence } = linkedGraph();
    evidence.criteria[0].status = "reviewed";
    evidence.criteria[0].mapping = { ...evidence.criteria[0].mapping!, review_status: "reviewed", reviewed_at: evidence.observed_at };
    if (difference === "revision") graph.latest_revision = { ...graph.latest_revision!, id: "revision_2" };
    if (difference === "criterion") graph.criteria[0].text = "Nouvelle exigence";
    if (difference === "conversation") graph.conversation_revision += 1;
    if (difference === "receipt") graph.latest_revision = { ...graph.latest_revision!, checks: graph.latest_revision!.checks.map((check) => ({ ...check, status: "failed", exit_code: 1 })) };
    if (difference === "producer") graph.latest_revision = { ...graph.latest_revision!, worker_job_id: "replacement_job" };
    if (difference === "file") graph.latest_revision = { ...graph.latest_revision!, files: graph.latest_revision!.files.map((file) => ({ ...file, sha256: "f".repeat(64) })) };
    await render(<ProjectGraphNodeEvidence graph={graph} evidence={evidence} nodeId="node_plan" stale={difference === "offline"} onOpenEvidence={jest.fn()} />);
    expect(screen.getByText("Liées : 0 · Revues explicitement : 0 · À revoir : 1")).toBeOnTheScreen();
    expect(screen.queryByText("Revue explicite enregistrée")).not.toBeOnTheScreen();
    expect(screen.getByText(difference === "offline" || difference === "conversation" ? "État du lien non confirmé" : "Lien historique — à revoir")).toBeOnTheScreen();
    expect(screen.getByText(/Révision liée : revision_1/)).toBeOnTheScreen();
  });

  it("retains a removed criterion as an historical producer link without inventing current coverage", async () => {
    const { graph, evidence } = linkedGraph();
    evidence.unmatched_mappings = [evidence.criteria[0].mapping!]; evidence.criteria = []; graph.criteria = [];
    await render(<ProjectGraphNodeEvidence graph={graph} evidence={evidence} nodeId="node_plan" onOpenEvidence={jest.fn()} />);
    expect(screen.getByText("Lien historique — à revoir")).toBeOnTheScreen();
    expect(screen.getByText("Liées : 0 · Revues explicitement : 0 · À revoir : 1")).toBeOnTheScreen();
  });

  it("does not join evidence from another goal even if producer IDs coincide", async () => {
    const { graph, evidence } = linkedGraph(); evidence.goal_run_id = "another_goal";
    await render(<ProjectGraphPlan state={{ graph, busy: false, stale: false, error: null, refresh: jest.fn(async () => {}) }}
      fallbackNodes={[]} enabled selectedNodeId={null} onSelectNode={jest.fn()} evidence={evidence} />);
    expect(screen.queryByText("Exigences 1")).not.toBeOnTheScreen();
  });
});

it("rejects a cycle rather than drawing invented order", () => {
  const graph = projectGraphFixture(); graph.dependencies.push({ from_node_id: "node_review", to_node_id: "node_plan", dependency_type: "hard" });
  expect(projectLayers(graph.nodes, graph.dependencies)).toBeNull();
  expect(projectDependencyLayout(graph.nodes, graph.dependencies, 240)).toBeNull();
});

describe("dependency route geometry", () => {
  it("routes Lire → Planning beside the independent Classer card on a narrow screen", () => {
    const graph = projectGraphFixture();
    const layout = projectDependencyLayout(graph.nodes, graph.dependencies, 240)!;
    const read = layout.positions.get("node_availability")!;
    const classify = layout.positions.get("node_priorities")!;
    expect(read.level).toBe(classify.level);
    expect(read.x).toBe(classify.x);
    const route = layout.routes.find(({ edge }) => edge.from_node_id === "node_availability")!;
    const sideSegment = route.points.slice(1).some((point, index) => {
      const previous = route.points[index];
      return point.x === previous.x && point.x < classify.x
        && previous.y < classify.y && point.y > classify.y + classify.height;
    });
    expect(sideSegment).toBe(true);
    expect(route.edge.dependency_type).toBe("hard");
    expect(layout.routes.find(({ edge }) => edge.from_node_id === "node_priorities")!.edge.dependency_type).toBe("optional");
  });

  it.each([160, 240, 269, 270, 320, 700].flatMap((width) => [[width, 90], [width, 116]]))("never crosses an unrelated node at width %i and node height %i, including wrapped layers and level skips", (width, nodeHeight) => {
    const nodes = Array.from({ length: 20 }, (_, index) => graphNode(`node_${index}`));
    const edges: ReturnType<typeof projectGraphFixture>["dependencies"] = [];
    // Four layers, each with five independent cards (three visual rows even at two columns).
    for (let index = 5; index < nodes.length; index += 1) {
      edges.push({ from_node_id: nodes[index - 5].id, to_node_id: nodes[index].id, dependency_type: index % 2 ? "hard" : "optional" });
    }
    edges.push({ from_node_id: "node_0", to_node_id: "node_19", dependency_type: "hard" },
      { from_node_id: "node_3", to_node_id: "node_18", dependency_type: "optional" });
    const layout = projectDependencyLayout(nodes, edges, width, nodeHeight)!;
    expect(layout.routes).toHaveLength(edges.length);
    expect(new Set([...layout.positions.values()].map((position) => position.level)).size).toBe(4);
    for (const { edge, points } of layout.routes) {
      const source = layout.positions.get(edge.from_node_id)!;
      const target = layout.positions.get(edge.to_node_id)!;
      // Every visible route has explicit ports at its own source and target.
      expect(points[0]).toEqual({ x: source.x + source.width / 2, y: source.y + source.height + 3 });
      expect(points.at(-1)).toEqual({ x: target.x + target.width / 2, y: target.y - 3 });
      for (let index = 1; index < points.length; index += 1) {
        const a = points[index - 1]; const b = points[index];
        expect(a.x === b.x || a.y === b.y).toBe(true);
        expect(Math.min(a.x, b.x)).toBeGreaterThanOrEqual(0);
        expect(Math.max(a.x, b.x)).toBeLessThanOrEqual(width);
        for (const [id, card] of layout.positions) {
          if (id === edge.from_node_id || id === edge.to_node_id) continue;
          // Independent rectangle/segment overlap check, including a visible two-pixel gap.
          const overlapsX = Math.max(a.x, b.x) >= card.x - 2 && Math.min(a.x, b.x) <= card.x + card.width + 2;
          const overlapsY = Math.max(a.y, b.y) >= card.y - 2 && Math.min(a.y, b.y) <= card.y + card.height + 2;
          expect({ edge: `${edge.from_node_id}→${edge.to_node_id}`, card: id, intersects: overlapsX && overlapsY })
            .toEqual({ edge: `${edge.from_node_id}→${edge.to_node_id}`, card: id, intersects: false });
        }
      }
    }
  });
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
