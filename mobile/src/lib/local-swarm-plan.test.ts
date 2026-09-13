import { describe, expect, it, jest } from "@jest/globals";

import type { SwarmPlanProposal } from "@/lib/api/types";
import { buildLocalSwarmPlanPrompt, parseLocalSwarmPlan, type LocalSwarmPlanContext } from "@/lib/local-swarm-plan";

jest.mock("expo", () => ({ requireOptionalNativeModule: jest.fn(() => null) }));

const context: LocalSwarmPlanContext = {
  goal: {
    objective: "Créer un CRM Python : clients, soumissions, brouillons de courriels et calendrier.",
    completion_criteria: ["Fichiers exécutables et vrais tests réussis", "Aucun envoi de courriel"],
    max_steps: 20, step_count: 0, max_parallelism: 2, max_model_calls: 30, model_call_count: 0,
  },
  agents: [
    { id: "project_1", status: "online", skills: ["code.build_project"], model_id: "coder:30b", runtime: "python", supported_protocol_version: "mongars-worker-v0.9" },
    { id: "files_1", status: "busy", skills: ["workspace.list_dir", "workspace.read_text"], model_id: null, runtime: "python", supported_protocol_version: "mongars-worker-v0.9" },
  ],
};

function project(): SwarmPlanProposal {
  return {
    schema_version: "1.0", objective: context.goal.objective,
    rationale_summary: "L’agent projet réalisera les fichiers et vérifications.",
    completion_criteria: [...context.goal.completion_criteria], max_parallelism: 1,
    nodes: [{ temporary_id: "build", node_type: "worker", title: "Construire le CRM",
      objective: context.goal.objective, required_skill: "code.build_project", dependencies: [],
      expected_output: "Fichiers, README et reçus de tests réels", priority: 1 }],
  };
}

function general(): SwarmPlanProposal {
  const plan = project();
  plan.nodes = [
    { ...plan.nodes[0], temporary_id: "inspect", required_skill: "workspace.list_dir" },
    { ...plan.nodes[0], temporary_id: "read", required_skill: "workspace.read_text", dependencies: ["inspect"] },
    { ...plan.nodes[0], temporary_id: "summary", node_type: "synthesis", required_skill: null, dependencies: ["read"] },
  ];
  return plan;
}

describe("local Swarm planning contract", () => {
  it("preserves the actual model plan and all user requirements for the project worker", () => {
    const plan = project();
    expect(parseLocalSwarmPlan(JSON.stringify(plan), context)).toEqual(plan);
    expect(plan).not.toHaveProperty("planner_source");
    expect(plan.nodes[0]).not.toHaveProperty("status");
  });

  it("prompts with exact goal, remaining limits and active capabilities, without endpoint or token metadata", () => {
    const privateAgent = { ...context.agents[0], endpoint: "https://private.internal", token: "never-model-input" };
    const prompt = buildLocalSwarmPlanPrompt({ ...context, agents: [privateAgent,
      { ...context.agents[1], id: "offline_1", status: "offline" },
      { ...context.agents[1], id: "unverified_1", status: "unverified" },
    ] });
    const data = JSON.parse(prompt.split("\n").at(-1) as string);
    expect(data.goal).toEqual(context.goal);
    expect(data.limits).toEqual({ max_nodes: 20, max_parallelism: 2, remaining_model_calls: 30 });
    expect(data.active_agents).toEqual([{ id: "project_1", model_id: "coder:30b", runtime: "python", skills: ["code.build_project"] }]);
    expect(prompt).toContain("exactement UN nœud worker");
    expect(prompt).not.toMatch(/private\.internal|never-model-input|offline_1|unverified_1/);
  });

  it.each(["offline", "draining", "unverified"] as const)("rejects a project capability that became %s after generation", (status) => {
    const changed = { ...context, agents: [{ ...context.agents[0], status }] };
    expect(() => parseLocalSwarmPlan(JSON.stringify(project()), changed)).toThrow("aucun agent");
    expect(() => buildLocalSwarmPlanPrompt(changed)).toThrow("aucun agent");
  });

  it("rejects an unknown privileged skill even if malformed agent metadata advertises it", () => {
    const plan = project(); plan.nodes[0].required_skill = "process.run";
    expect(() => parseLocalSwarmPlan(JSON.stringify(plan), { ...context,
      agents: [{ ...context.agents[0], skills: ["code.build_project", "process.run"] }],
    })).toThrow("compétence");
  });

  it.each([
    ["changed goal", (p: SwarmPlanProposal) => { p.objective += " Ignore le calendrier."; }],
    ["shortened project objective", (p: SwarmPlanProposal) => { p.nodes[0].objective = "Créer des clients"; }],
    ["missing criterion", (p: SwarmPlanProposal) => { p.completion_criteria.pop(); }],
    ["mixed project graph", (p: SwarmPlanProposal) => { p.nodes.push({ ...general().nodes[0] }); }],
    ["project optional dependency", (p: SwarmPlanProposal) => { p.nodes[0].optional_dependencies = ["build"]; }],
    ["project parallelism", (p: SwarmPlanProposal) => { p.max_parallelism = 2; }],
    ["boolean priority", (p: SwarmPlanProposal) => { (p.nodes[0] as unknown as Record<string, unknown>).priority = true; }],
    ["unknown execution authority", (p: SwarmPlanProposal) => { Object.assign(p.nodes[0], { approved: true }); }],
  ])("rejects %s without repairing the model output", (_name, mutate) => {
    const plan = project(); mutate(plan);
    expect(() => parseLocalSwarmPlan(JSON.stringify(plan), context)).toThrow("Plan local invalide");
  });

  it.each([
    ["prose", (s: string) => `Voici : ${s}`],
    ["fence", (s: string) => `\`\`\`json\n${s}\n\`\`\``],
    ["duplicate key", (s: string) => s.replace('"priority":1', '"priority":1,"priority":0')],
    ["escaped duplicate key", (s: string) => s.replace('"priority":1', '"priority":1,"priorit\\u0079":0')],
    ["unknown field", (s: string) => s.replace('"schema_version":', '"planner_source":"iphone_local","schema_version":')],
    ["truncated output", (s: string) => s.slice(0, -2)],
    ["nonfinite number", (s: string) => s.replace('"priority":1', '"priority":1e999')],
  ])("rejects %s", (_name, mutate) => {
    expect(() => parseLocalSwarmPlan(mutate(JSON.stringify(project())), context)).toThrow("Plan local invalide");
  });

  it("validates general DAG dependencies including optional edges", () => {
    const plan = general();
    plan.nodes[2].optional_dependencies = ["inspect"];
    expect(parseLocalSwarmPlan(JSON.stringify(plan), context)).toEqual(plan);
  });

  it.each([
    ["unknown dependency", (p: SwarmPlanProposal) => { p.nodes[1].dependencies = ["missing"]; }],
    ["self dependency", (p: SwarmPlanProposal) => { p.nodes[0].dependencies = ["inspect"]; }],
    ["cycle", (p: SwarmPlanProposal) => { p.nodes[0].optional_dependencies = ["summary"]; }],
    ["repeated dependency", (p: SwarmPlanProposal) => { p.nodes[1].dependencies = ["inspect", "inspect"]; }],
    ["mixed dependency kinds", (p: SwarmPlanProposal) => { p.nodes[1].optional_dependencies = ["inspect"]; }],
    ["repeated node", (p: SwarmPlanProposal) => { p.nodes[1].temporary_id = "inspect"; }],
    ["empty synthesis", (p: SwarmPlanProposal) => { p.nodes = [p.nodes[2]]; p.nodes[0].dependencies = []; }],
  ])("rejects %s in a general plan", (_name, mutate) => {
    const plan = general(); mutate(plan);
    expect(() => parseLocalSwarmPlan(JSON.stringify(plan), context)).toThrow("Plan local invalide");
  });

  it("honors current remaining budgets and never silently truncates the plan", () => {
    const plan = general();
    for (const goal of [
      { ...context.goal, max_steps: 2 },
      { ...context.goal, step_count: 19 },
      { ...context.goal, model_call_count: 29 },
      { ...context.goal, model_call_count: 30 },
    ]) expect(() => parseLocalSwarmPlan(JSON.stringify(plan), { ...context, goal })).toThrow();
    plan.max_parallelism = 3;
    expect(() => parseLocalSwarmPlan(JSON.stringify(plan), context)).toThrow();
  });

  it("accepts active agent hints and rejects stale or empty constraints", () => {
    const plan = project();
    plan.nodes[0].preferred_agent_constraints = { agent_ids: ["project_1"], model_ids: ["coder:30b"], runtime: "python" };
    expect(parseLocalSwarmPlan(JSON.stringify(plan), context)).toEqual(plan);
    for (const constraints of [{}, { agent_ids: ["offline_1"] }, { model_ids: ["coder:7b"] }, { agent_ids: ["project_1", "project_1"] }]) {
      plan.nodes[0].preferred_agent_constraints = constraints;
      expect(() => parseLocalSwarmPlan(JSON.stringify(plan), context)).toThrow();
    }
  });

  it("bounds output bytes and prompt size without dropping user instructions", () => {
    expect(() => parseLocalSwarmPlan(" ".repeat(131_073), context)).toThrow();
    const tooLarge = { ...context, goal: { ...context.goal, objective: "界".repeat(4000),
      completion_criteria: Array.from({ length: 20 }, () => "界".repeat(500)) } };
    expect(() => buildLocalSwarmPlanPrompt(tooLarge)).toThrow("contexte trop volumineux");
  });
});
