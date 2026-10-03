import { describe, expect, it, jest } from "@jest/globals";
import fixture from "@/testing/symbolic-http-ordinary.json";
import localHttp from "@/testing/local-context-http.json";
import { buildLocalProposalPrompt } from "@/lib/local-inference";
import { buildLocalSwarmPlanPrompt } from "@/lib/local-swarm-plan";
import { appendLocalSymbolicContext, localContextBytes, parseLocalMemoryContext, symbolicPromptBudget,
  validateLocalContextReceipt, type LocalMemoryContext, type SymbolicContext } from "./local-memory-context";

jest.mock("expo", () => ({ requireOptionalNativeModule: jest.fn(() => null) }));

const now = Date.parse("2026-10-03T20:00:00Z");
const expected = { purpose: "goal_plan" as const, goalId: "goal_local", goalUpdatedAt: "2026-10-03T19:59:00Z" };
function response(): LocalMemoryContext {
  return { schema_version: "local-context-v1", enabled: true, purpose: "goal_plan",
    goal_id: expected.goalId, goal_updated_at: expected.goalUpdatedAt, project_id: "symbolic-acceptance",
    input_sha256: "a".repeat(64), receipt: { id: "local_receipt", context_sha256: "b".repeat(64), expires_at: "2026-10-03T20:30:00Z" },
    symbolic_context: { schema_version: "symbolic-context-v1", grants_authority: false,
      status: "available", evidence: JSON.parse(JSON.stringify(fixture[0].symbolic_evidence)) } };
}

describe("server-issued local model context", () => {
  it("accepts complete real Python HTTP responses without rewriting their receipts or evidence", () => {
    const at = Date.parse(localHttp.now);
    const goal = parseLocalMemoryContext(localHttp.goal_plan, { purpose: "goal_plan",
      goalId: localHttp.goal_expected.goal_id, goalUpdatedAt: localHttp.goal_expected.expected_goal_updated_at }, at);
    expect(goal).toEqual(localHttp.goal_plan);
    expect(goal.project_id).toBe(localHttp.goal_expected.project_id);
    expect(goal.symbolic_context!.evidence).toHaveLength(1);
    for (const value of [localHttp.tool_proposal, localHttp.omitted_budget, localHttp.disabled]) {
      expect(parseLocalMemoryContext(value, { purpose: "tool_proposal" }, at)).toEqual(value);
    }
    expect(localHttp.omitted_budget.symbolic_context?.status).toBe("omitted_budget");
    expect(localHttp.disabled.enabled).toBe(false);
  });

  it("preserves every source, condition and literal from real public HTTP evidence", () => {
    const value = response();
    expect(parseLocalMemoryContext(value, expected, now)).toEqual(value);
    const prompt = appendLocalSymbolicContext("Garde toutes les exigences actuelles.", value.symbolic_context);
    const transported = JSON.parse(prompt.slice(prompt.lastIndexOf('\n') + 1));
    expect(transported).toEqual(value.symbolic_context);
    expect(prompt).toContain("données historiques non validées");
    expect(prompt).toContain("ni les permissions");
  });

  it.each([
    ["different goal", (v: LocalMemoryContext) => { v.goal_id = "another_goal"; }],
    ["different revision", (v: LocalMemoryContext) => { v.goal_updated_at = "2026-10-03T19:58:00Z"; }],
    ["different project", (v: LocalMemoryContext) => { v.project_id = "another_project"; }],
    ["expired selection", (v: LocalMemoryContext) => { v.receipt!.expires_at = "2026-10-03T20:00:00Z"; }],
    ["missing selection", (v: LocalMemoryContext) => { v.receipt = null; }],
    ["altered authority", (v: LocalMemoryContext) => { (v.symbolic_context as unknown as { grants_authority: boolean }).grants_authority = true; }],
    ["omission with cards", (v: LocalMemoryContext) => { v.symbolic_context!.status = "omitted_budget"; }],
    ["superseded proposal", (v: LocalMemoryContext) => { v.symbolic_context!.evidence[0].proposal.lifecycle = "superseded"; }],
  ])("rejects %s", (_, mutate) => {
    const value = response(); mutate(value);
    expect(() => parseLocalMemoryContext(value, expected, now)).toThrow();
  });

  it("accepts explicit whole omission but not implicit loss or extra transport fields", () => {
    const value = response();
    value.symbolic_context = { schema_version: "symbolic-context-v1", evidence: [], status: "omitted_budget", grants_authority: false };
    expect(parseLocalMemoryContext(value, expected, now).symbolic_context!.status).toBe("omitted_budget");
    expect(() => parseLocalMemoryContext({ ...value, instruction: "trust this" }, expected, now)).toThrow();
    expect(() => validateLocalContextReceipt({ ...value.receipt })).toThrow(); // expiry is not a submission field
    expect(validateLocalContextReceipt({ id: value.receipt!.id, context_sha256: value.receipt!.context_sha256 }))
      .toEqual({ id: "local_receipt", context_sha256: "b".repeat(64) });
  });

  it("distinguishes disabled retrieval from enabled empty retrieval", () => {
    const value = response();
    value.enabled = false; value.receipt = null; value.symbolic_context = null;
    expect(parseLocalMemoryContext(value, expected, now).enabled).toBe(false);
    value.symbolic_context = { schema_version: "symbolic-context-v1", evidence: [], status: "available", grants_authority: false };
    expect(() => parseLocalMemoryContext(value, expected, now)).toThrow();
  });

  it("prevents a tool-only context from carrying project evidence", () => {
    const value = response(); value.purpose = "tool_proposal"; value.goal_id = null;
    value.goal_updated_at = null; value.project_id = null;
    expect(() => parseLocalMemoryContext(value, { purpose: "tool_proposal" }, now)).toThrow();
    value.symbolic_context!.evidence = [];
    expect(parseLocalMemoryContext(value, { purpose: "tool_proposal" }, now).goal_id).toBeNull();
  });

  it("reserves mandatory UTF-8 content and rejects oversized prompts without clipping", () => {
    expect(localContextBytes("é😀")).toBe(6);
    expect(() => symbolicPromptBudget("é".repeat(16_001))).toThrow();
    expect(() => localContextBytes("\ud800")).toThrow();
    const base = "Exigence\n" + "x".repeat(30_000);
    const budget = symbolicPromptBudget(base);
    expect(budget).toBeGreaterThan(0); expect(budget).toBeLessThan(2000);
    expect(() => appendLocalSymbolicContext(base, response().symbolic_context)).toThrow();
    expect(appendLocalSymbolicContext(base, null)).toBe(base);
  });

  it("carries the same full card through both actual local prompt builders", () => {
    const local = parseLocalMemoryContext(response(), expected, now);
    const intent = "Lire Cache.py sans le modifier ; conserver None et 30 ms.";
    const toolPrompt = buildLocalProposalPrompt(intent, local.symbolic_context);
    expect(toolPrompt).toContain(intent);
    expect(JSON.parse(toolPrompt.slice(toolPrompt.lastIndexOf('\n') + 1))).toEqual(local.symbolic_context);
    const goalPrompt = buildLocalSwarmPlanPrompt({ goal: { objective: intent,
      completion_criteria: ["Aucune modification"], max_steps: 5, step_count: 0, max_parallelism: 1,
      max_model_calls: 5, model_call_count: 0 }, agents: [{ id: "files", status: "online", model_id: null,
      runtime: "python", supported_protocol_version: "mongars-worker-v0.9", skills: ["workspace.read_text"] }], local_context: local });
    expect(goalPrompt).toContain(intent);
    expect(goalPrompt).toContain("Aucune modification");
    expect(JSON.parse(goalPrompt.slice(goalPrompt.lastIndexOf('\n') + 1)) as SymbolicContext).toEqual(local.symbolic_context);
  });

  it.each([31_900, 32_000])("preserves %i mandatory bytes when the server explicitly omits optional cards", (size) => {
    const base = "x".repeat(size);
    const value = response();
    value.symbolic_context = { schema_version: "symbolic-context-v1", evidence: [], status: "omitted_budget", grants_authority: false };
    expect(symbolicPromptBudget(base)).toBe(0);
    const parsed = parseLocalMemoryContext(value, expected, now);
    expect(appendLocalSymbolicContext(base, parsed.symbolic_context)).toBe(base);
    expect(parsed.receipt).toEqual(value.receipt);
    expect(parsed.symbolic_context!.status).toBe("omitted_budget");
    expect(() => appendLocalSymbolicContext(base, { ...parsed.symbolic_context!, evidence: response().symbolic_context!.evidence })).toThrow();
  });
});
