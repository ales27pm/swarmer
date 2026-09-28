import { beforeEach, describe, expect, it, jest } from "@jest/globals";
import { fetch } from "expo/fetch";
import * as SecureStore from "expo-secure-store";
import { parseProjectGraph } from "./project-graph";
import { ConnectionChangedError, getProjectGraph } from "./client";
import { projectGraphFixture } from "@/testing/project-graph-fixtures";

jest.mock("expo-secure-store", () => ({ deleteItemAsync: jest.fn(), getItemAsync: jest.fn(), setItemAsync: jest.fn() }));
jest.mock("expo/fetch", () => ({ fetch: jest.fn() }));
jest.mock("@/lib/state/mutation-outbox", () => ({ mutationOutbox: {} }));
jest.mock("@/lib/state/replica", () => ({ applyBootstrap: jest.fn(), upsertEvent: jest.fn() }));
const parse = (value: unknown) => parseProjectGraph(value, "goal_preview");
const response = (value: unknown) => ({ ok: true, status: 200, json: async () => value }) as never;

describe("project graph public contract", () => {
  it("preserves facts, optional dependencies and attributed public explanations", () => {
    expect(parse(projectGraphFixture())).toEqual(projectGraphFixture());
  });
  it("never forwards uncontracted model reasoning or audit payloads", () => {
    const fixture = projectGraphFixture();
    const result = parse({ ...fixture, raw_payload: "private", goal: { ...fixture.goal, chain_of_thought: "private" },
      planning_decisions: [{ ...fixture.planning_decisions[0], hidden_reasoning: "private" }] });
    expect(JSON.stringify(result)).not.toContain("private");
  });
  it.each([
    { schema_version: "2.0" }, { nodes: Array(21).fill(projectGraphFixture().nodes[0]) },
    { goal: { ...projectGraphFixture().goal, id: "goal_other" } },
    { dependencies: [{ from_node_id: "unknown", to_node_id: "node_plan", dependency_type: "hard" }] },
    { dependencies: [{ from_node_id: "node_plan", to_node_id: "node_plan", dependency_type: "hard" }] },
    { evaluations: [{ ...projectGraphFixture().evaluations[0], goal_run_id: "goal_other" }] },
    { planning_decisions: [{ ...projectGraphFixture().planning_decisions[0], authority: "verified" }] },
    { planning_decisions: [{ ...projectGraphFixture().planning_decisions[0], goal_run_id: "goal_other" }] },
    { coverage: { ...projectGraphFixture().coverage, criterion_mapping: "verified" } },
    { nodes: [{ ...projectGraphFixture().nodes[0], title: "bad\ud800" }] },
  ])("rejects malformed or misleading projection %p", (patch) => expect(() => parse({ ...projectGraphFixture(), ...patch })).toThrow());
  it("requires all envelope fields and exact dependency coverage", () => {
    const fixture = projectGraphFixture();
    for (const missing of Object.keys(fixture)) {
      expect(() => parse(Object.fromEntries(Object.entries(fixture).filter(([key]) => key !== missing)))).toThrow();
    }
    expect(() => parse({ ...fixture, dependencies: fixture.dependencies.slice(1) })).toThrow();
    fixture.nodes[0].depends_on = ["node_review"];
    fixture.dependencies.push({ from_node_id: "node_review", to_node_id: "node_availability", dependency_type: "hard" });
    expect(() => parse(fixture)).toThrow();
  });
  it("permits older plan-node identities and prior-goal revision without relabelling them current", () => {
    const fixture = projectGraphFixture();
    fixture.planning_decisions[0].node_ids = ["old_node"];
    fixture.latest_revision = { id: "revision_1", project_id: "project_preview", goal_run_id: "goal_older", node_id: "old_node", worker_job_id: "old_job", revision: 1,
      sha256: "a".repeat(64), created_at: fixture.observed_at, files: [], checks: [] };
    expect(parse(fixture).latest_revision?.goal_run_id).toBe("goal_older");
    expect(parse(fixture).planning_decisions[0].node_ids).toEqual(["old_node"]);
  });
  it("supports unicode explanatory text and strips absent model identity to null", () => {
    const fixture = projectGraphFixture(); fixture.planning_decisions[0].rationale_summary = "🧪 Comparer les horaires";
    fixture.planning_decisions[0].model_id = null;
    expect(parse(fixture).planning_decisions[0].model_id).toBeNull();
  });
});

describe("project graph transport fencing", () => {
  let connection: { baseUrl: string; token: string };
  beforeEach(() => {
    jest.clearAllMocks(); connection = { baseUrl: "https://control.example", token: "fixture-token" };
    jest.mocked(SecureStore.getItemAsync).mockImplementation(async (key) => key === "mongars.connection.v1" ? JSON.stringify(connection) : null);
    jest.mocked(fetch).mockResolvedValue(response(projectGraphFixture()));
  });
  it("uses the readonly authenticated goal endpoint", async () => {
    await getProjectGraph("goal_preview");
    expect(jest.mocked(fetch).mock.calls[0][0]).toBe("https://control.example/goals/goal_preview/graph");
    expect(jest.mocked(fetch).mock.calls[0][1]?.method ?? "GET").toBe("GET");
  });
  it("rejects an old screen response", async () => {
    await expect(getProjectGraph("goal_preview", () => false)).rejects.toThrow();
  });
  it("rejects a response when pairing changes while fetching", async () => {
    jest.mocked(fetch).mockImplementation(async () => {
      connection = { ...connection, token: "new-token" }; return response(projectGraphFixture());
    });
    await expect(getProjectGraph("goal_preview")).rejects.toBeInstanceOf(ConnectionChangedError);
  });
});
