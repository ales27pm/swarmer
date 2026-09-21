import { beforeEach, describe, expect, it, jest } from "@jest/globals";
import * as server from "@/lib/api/client";
import * as native from "@/lib/local-inference";
import { applicationApi, ApplicationApiError } from "./index";
import { getGoalWritingDraft, listAgents } from "./server";
import { createLocalGenerationSession, generateLocalProposal, pickAndImportLocalModel } from "./local-inference";
import { applicationSessions, ApplicationSessions } from "./sessions";
import { notifyConnectionChanged } from "@/lib/connection-events";
import { projectFixture, projectGoalFixture } from "@/testing/project-fixtures";
import type { LocalPlanContextHandle, GeneratedLocalPlan } from "./outputs";
import { localSwarmSnapshot } from "@/lib/state/replica";
import { AppState } from "react-native";
import { getDocumentAsync } from "expo-document-picker";
import { createApplicationProtocol } from "./protocol";

jest.mock("expo/fetch", () => ({ fetch: jest.fn() }));
jest.mock("expo-document-picker", () => ({ getDocumentAsync: jest.fn() }));
jest.mock("@/lib/state/mutation-outbox", () => ({ mutationOutbox: { pendingCount: jest.fn() } }));
jest.mock("@/lib/state/replica", () => ({ applyBootstrap: jest.fn(), upsertEvent: jest.fn(), localSwarmSnapshot: jest.fn() }));
jest.mock("expo-constants", () => ({ __esModule: true, default: {
  expoConfig: { version: "0.1.0", ios: { buildNumber: "configured-build" } }, platform: { ios: { buildNumber: "native-build" } },
} }));

jest.mock("@/lib/api/client", () => ({
  ...jest.requireActual<typeof import("@/lib/api/client")>("@/lib/api/client"),
  listAgents: jest.fn(), createGoal: jest.fn(), createGoalFeedback: jest.fn(), getGoal: jest.fn(),
  listAudit: jest.fn(), getGoalWritingDraft: jest.fn(),
  createLocalGoalPlanSession: jest.fn(), reviewGoalProject: jest.fn(), getGoalConversation: jest.fn(),
}));
jest.mock("@/lib/local-inference", () => ({
  ...jest.requireActual<typeof import("@/lib/local-inference")>("@/lib/local-inference"),
  generateLocalProposal: jest.fn(), loadLocalModel: jest.fn(), cancelLocalGeneration: jest.fn(), unloadLocalModel: jest.fn(), downloadLocalGgufModel: jest.fn(),
  importLocalModel: jest.fn(),
  getLocalInferenceCapabilities: jest.fn(), getLocalInferenceStatus: jest.fn(),
}));

describe("application API contract", () => {
  beforeEach(() => { jest.clearAllMocks(); applicationSessions.clear(); });

  it("publishes versioned real commands with no placeholder implementations", () => {
    const catalog = applicationApi.catalog();
    expect(catalog.schemaVersion).toBe("1.0");
    expect(new Set(catalog.commands.map((command) => command.name)).size).toBe(catalog.commands.length);
    expect(catalog.commands.find((command) => command.name === "inference.generate")).toMatchObject({
      available: true, execution: "job", effect: "mutation", source: "device", readiness: "loaded_model",
    });
    expect(catalog.commands.find((command) => command.name === "iphone.requests.execute")).toMatchObject({
      available: true, osInteraction: true, readiness: "review_required",
    });
    for (const command of catalog.commands) {
      expect(command.inputSchema).toMatchObject({ type: "object", additionalProperties: false });
      expect(command).not.toHaveProperty("handler");
      expect(command.output).toMatchObject({ envelope: "ApplicationResult", contractVersion: "1.0" });
      if (command.available) expect(command.output.dataType).not.toBe("unavailable");
      expect(command.available).toBe(true);
    }
    expect(() => JSON.stringify(catalog)).not.toThrow();
  });

  it("exposes background status through the existing model status command without adding execution authority", async () => {
    const status: native.LocalInferenceStatus = { state: "ready", runtime: "mlx", modelId: "test/dolphin", revision: null,
      backgroundExecution: { supported: true, reason: "permission_unverified", osSupported: true, gpuSupported: true,
        entitlementGranted: null, executionDevice: null, active: false, operationId: null, outputBytes: 0, state: "idle" } };
    jest.mocked(native.getLocalInferenceStatus).mockResolvedValue(status);
    expect((await applicationApi.execute("models.status", {})).data).toEqual(status);
    expect(applicationApi.catalog().commands).toHaveLength(68);
    expect(applicationApi.catalog().commands.find((command) => command.name === "models.status")).toMatchObject({ effect: "read", output: { dataType: "LocalInferenceStatus", validation: "existing_parser" } });
    expect(native.generateLocalProposal).not.toHaveBeenCalled();
    expect(native.loadLocalModel).not.toHaveBeenCalled();
  });

  it("preserves an admitted CPU fallback in the same read-only models.status contract", async () => {
    const status: native.LocalInferenceStatus = { state: "generating", runtime: "mlx", modelId: "test/dolphin", revision: null,
      backgroundExecution: { supported: true, reason: "cpu_fallback", osSupported: true, gpuSupported: false,
        entitlementGranted: null, executionDevice: "cpu", active: true, operationId: "cpu-operation", outputBytes: 42, state: "active" } };
    jest.mocked(native.getLocalInferenceStatus).mockResolvedValue(status);
    expect((await applicationApi.execute("models.status", {})).data).toEqual(status);
    expect(applicationApi.catalog().commands).toHaveLength(68);
    expect(native.generateLocalProposal).not.toHaveBeenCalled();
  });

  it("distinguishes the native installed build from the versioned JavaScript configuration", async () => {
    const previousState = AppState.currentState;
    AppState.currentState = "active";
    try {
      const result = await applicationApi.execute("app.status", {});
      expect(result.data).toMatchObject({ appVersion: "0.1.0", appVersionSource: "expo_config", nativeBuildNumber: "native-build",
        configuredBuildNumber: "configured-build", platform: "ios", appState: "active" });
      expect(result.metadata.source).toBe("device");
    } finally { AppState.currentState = previousState; }
  });

  it("exposes the full writing draft through the same read-only API used by the UI", async () => {
    const draft = { schema_version: "1.0" as const, content_trust: "untrusted" as const,
      goal_run_id: "goal_1", node_id: "node_1", worker_job_id: "job_1", text: "Un plan à relire.", summary: "Plan proposé", sha256: "a".repeat(64) };
    jest.mocked(server.getGoalWritingDraft).mockResolvedValue(draft);
    const shouldAccept = () => true;
    expect(await getGoalWritingDraft("goal_1", "node_1", "job_1", shouldAccept)).toEqual(draft);
    expect(server.getGoalWritingDraft).toHaveBeenCalledWith("goal_1", "node_1", "job_1", shouldAccept);
    expect((await applicationApi.execute("goals.writing-draft", { goalId: "goal_1", nodeId: "node_1", workerJobId: "job_1" })).data).toEqual(draft);
    expect(applicationApi.catalog().commands.find((command) => command.name === "goals.writing-draft")).toMatchObject({
      available: true, effect: "read", source: "authoritative", execution: "immediate", requiresForeground: false,
      output: { dataType: "GoalWritingDraft", validation: "existing_parser" },
    });
    expect(server.createGoal).not.toHaveBeenCalled();
  });

  it.each([
    { goalId: "goal_1", nodeId: "node_1" },
    { goalId: "../goal_1", nodeId: "node_1", workerJobId: "job_1" },
    { goalId: "goal_1", nodeId: "node_1", workerJobId: "job_1", execute: true },
  ])("rejects malformed writing-draft identities or added execution fields", async (input) => {
    await expect(applicationApi.execute("goals.writing-draft", input)).rejects.toMatchObject({ code: "invalid_arguments" });
    expect(server.getGoalWritingDraft).not.toHaveBeenCalled();
  });

  it("projects a bounded audit summary without raw actors, payloads or traces", async () => {
    jest.mocked(server.listAudit).mockResolvedValue([{ id: 1, event_type: "goal.created", created_at: "2026-09-16T00:00:00Z", hash: "hash",
      actor_id: "private", actor_type: "device", task_id: null, prev_hash: null, trace_id: "trace", payload: { message: "private content" } }]);
    await expect(applicationApi.execute("audit.summary", { limit: 201 })).rejects.toMatchObject({ code: "invalid_arguments" });
    expect(server.listAudit).not.toHaveBeenCalled();
    const result = await applicationApi.execute("audit.summary", { limit: 200 });
    expect(server.listAudit).toHaveBeenCalledWith(200);
    expect(result.data).toEqual([{ id: 1, event_type: "goal.created", created_at: "2026-09-16T00:00:00Z", hash: "hash" }]);
  });

  it("counts only the scoped replica and never exposes cached payloads or action authority", async () => {
    jest.mocked(localSwarmSnapshot).mockResolvedValue({ goals: [{ objective: "private" }], plan_nodes: [], goal_results: [],
      agents: [{ id: "private" }, { id: "private2" }], cursor: "42" } as unknown as NonNullable<Awaited<ReturnType<typeof localSwarmSnapshot>>>);
    const result = await applicationApi.execute("cache.summary", {});
    expect(localSwarmSnapshot).toHaveBeenCalledWith(await server.getServerUrl());
    expect(result.data).toEqual({ available: true, cursor: "42", counts: { goals: 1, nodes: 0, results: 0, agents: 2 }, authorizesSensitiveActions: false });
    expect(result.metadata.source).toBe("cache");
    jest.mocked(localSwarmSnapshot).mockResolvedValue(null);
    expect((await applicationApi.execute("cache.summary", {})).data).toEqual({ available: false, cursor: null, counts: null, authorizesSensitiveActions: false });
  });

  it("imports GGUF from the same OS picker for UI and API callers without accepting paths", async () => {
    jest.mocked(getDocumentAsync).mockResolvedValue({ canceled: false, assets: [{ uri: "file:///picked/model.gguf", name: "model.gguf", lastModified: 0 }] });
    const model: native.LocalModel = { modelId: "model_1", runtime: "llama.cpp", displayName: "model.gguf", source: "model.gguf", sizeBytes: 10, importedAt: "2026-09-16T00:00:00Z" };
    jest.mocked(native.importLocalModel).mockResolvedValue(model);
    expect(await pickAndImportLocalModel("llama.cpp", () => true)).toEqual(model);
    expect((await applicationApi.execute("models.import", { runtime: "llama.cpp" })).data).toEqual(model);
    expect(native.importLocalModel).toHaveBeenCalledTimes(2);
    expect(native.importLocalModel).toHaveBeenLastCalledWith({ runtime: "llama.cpp", uri: "file:///picked/model.gguf", displayName: "model.gguf" });
    expect(getDocumentAsync).toHaveBeenCalledWith({ copyToCacheDirectory: true, multiple: false, type: "*/*" });
  });

  it.each(["unmounted", "cancelled", "wrong_extension"] as const)("never imports a GGUF selection after %s", async (reason) => {
    let accepts = true;
    jest.mocked(getDocumentAsync).mockImplementation(async () => {
      if (reason === "unmounted") accepts = false;
      return reason === "cancelled" ? { canceled: true, assets: null } : { canceled: false, assets: [{ uri: "file:///picked/model", name: reason === "wrong_extension" ? "model.txt" : "model.gguf", lastModified: 0 }] };
    });
    await expect(pickAndImportLocalModel("llama.cpp", () => accepts)).rejects.toMatchObject({ code: reason === "wrong_extension" ? "invalid_selection" : "cancelled" });
    expect(native.importLocalModel).not.toHaveBeenCalled();
  });

  it("does not let catalogue consumers weaken live input validation", async () => {
    const catalog = applicationApi.catalog();
    catalog.commands.find((command) => command.name === "goals.get")!.inputSchema.additionalProperties = undefined;
    catalog.commands.find((command) => command.name === "goals.get")!.inputSchema.properties = {};
    await expect(applicationApi.execute("goals.get", { arbitrary: "field" })).rejects.toMatchObject({ code: "invalid_arguments" });
    expect(server.getGoal).not.toHaveBeenCalled();
  });

  it("binds project review handles to a fresh explicit confirmation and invalidates them on re-pair", async () => {
    const prepareApproval = jest.fn<server.ProjectReview["prepareApproval"]>().mockResolvedValue({ task_id: "task_1", approval_id: "approval_1", status: "waiting_permission" } as never);
    jest.mocked(server.reviewGoalProject).mockResolvedValue({ project: projectFixture, prepareApproval });
    const result = await applicationApi.execute("project.review", { id: "goal_1" });
    const { handle } = result.data as { handle: string };
    expect(JSON.stringify(result)).not.toContain("prepareApproval");
    await expect(applicationApi.execute("project.prepareApproval", { handle, confirm: false })).rejects.toMatchObject({ code: "invalid_arguments" });
    expect(prepareApproval).not.toHaveBeenCalled();
    notifyConnectionChanged();
    await expect(applicationApi.execute("project.prepareApproval", { handle, confirm: true })).rejects.toMatchObject({ code: "session_expired" });
    expect(prepareApproval).not.toHaveBeenCalled();
  });

  it("bounds and expires private sessions without serializing their closures", () => {
    let now = 0;
    const store = new ApplicationSessions(() => now, 1, 100);
    const closure = () => "private";
    const id = store.put("review", closure);
    expect(store.get(id, "review")).toBe(closure);
    expect(() => store.put("review", closure)).toThrow(ApplicationApiError);
    expect(() => store.get(id, "other")).toThrow(ApplicationApiError);
    now = 100;
    expect(() => store.get(id, "review")).toThrow(ApplicationApiError);
    expect(() => store.put("review", closure)).not.toThrow();
  });

  function setupPlan() {
    const detail: server.GoalDetail = { ...projectGoalFixture, goal: { ...projectGoalFixture.goal,
      status: "planning", started_at: null, step_count: 0, model_call_count: 0, current_phase: "planning" } };
    const memory: server.GoalMemoryContext = {
      schema_version: "1.0", goal_id: detail.goal.id, project_id: null, conversation_revision: 0, base_revision_id: null,
      provider_fingerprint: "a".repeat(64), context_fingerprint: "b".repeat(64), mode: "lexical", reason: "no_linked_project", items: [],
      embedding: { configured: false, model: null, model_revision: null, storage: "ubuntu_sqlite" },
      local_planning_eligible: true, planning_embedding_call_count: 0, recent_conversation: [],
    };
    const session = {
      getGoal: jest.fn<() => Promise<server.GoalDetail>>().mockResolvedValue(detail),
      assertCurrent: jest.fn<() => Promise<void>>().mockResolvedValue(),
      memoryContext: jest.fn<() => Promise<server.GoalMemoryContext>>().mockResolvedValue(memory),
      bootstrapSync: jest.fn<() => Promise<server.Bootstrap>>().mockResolvedValue({ agents: [{ id: "agent_project", status: "online", skills: ["code.build_project"],
        model_id: "builder", runtime: "python", supported_protocol_version: "mongars-worker-v0.9" }] } as server.Bootstrap),
      startGoal: jest.fn<() => Promise<server.GoalDetail>>().mockResolvedValue({ ...detail, goal: { ...detail.goal, status: "running", planner_source: "iphone_local" } }),
    };
    jest.mocked(server.createLocalGoalPlanSession).mockResolvedValue(session);
    jest.mocked(native.getLocalInferenceStatus).mockResolvedValue({ state: "ready", runtime: "mlx", modelId: "test/model", revision: "c".repeat(40) });
    jest.mocked(native.getLocalInferenceCapabilities).mockResolvedValue({ mlx: true, coreml: false, llamaCpp: false, platform: "ios" });
    const plan = { schema_version: "1.0", objective: detail.goal.objective, rationale_summary: "Construire le projet", max_parallelism: 1,
      completion_criteria: detail.goal.completion_criteria, nodes: [{ temporary_id: "build", node_type: "worker", title: "Construire", objective: detail.goal.objective,
        required_skill: "code.build_project", dependencies: [], expected_output: "Sources et tests", priority: 1 }] };
    jest.mocked(native.generateLocalProposal).mockResolvedValue({ text: JSON.stringify(plan), tokenCount: 120, finishReason: "stop" });
    return { detail, memory, session, plan };
  }

  async function planProtocolResult(command: string, input: object) {
    let completion!: Promise<unknown>;
    const protocol = createApplicationProtocol({ catalog: () => applicationApi.catalog(), execute: (name, arguments_) => {
      completion = applicationApi.execute(name, arguments_);
      return completion;
    } }, () => new Date(), "plantest");
    protocol.handle({ method: "POST", path: "/v1/commands", body: JSON.stringify({
      command, input, instanceId: "plantest", idempotencyKey: "local-plan-regression-1",
    }) });
    await completion.catch(() => undefined);
    await Promise.resolve();
    return protocol.handle({ method: "GET", path: "/v1/jobs/job_plantest_1", body: "" }).body;
  }

  it.each(["", "```json\n{\"tool_name\":\"Flask\"}\n```", "{\"tool_name\":\"Flask\",\"arguments\":{},\"summary\":\"private model text\"}"])(
    "classifies local plan format rejection as failed without a startable plan", async (text) => {
      const { session } = setupPlan();
      const { handle } = (await applicationApi.execute("goals.plan.prepare", { id: "goal_1" })).data as { handle: string };
      jest.mocked(native.generateLocalProposal).mockResolvedValue({ text, tokenCount: text ? 85 : 1, finishReason: "stop" });
      const result = await planProtocolResult("goals.plan.generate", { handle, runtime: "mlx", modelId: "test/model" });
      expect(result).toMatchObject({ job: { state: "failed", error: { code: "invalid_plan" } } });
      expect(JSON.stringify(result)).not.toContain("private model text");
      await expect(applicationApi.execute("goals.plan.start", { handle, confirm: true })).rejects.toMatchObject({ code: "invalid_state" });
      expect(session.startGoal).not.toHaveBeenCalled();
      expect(native.generateLocalProposal).toHaveBeenCalledTimes(1);
    },
  );

  it.each(["prepare", "generate", "start"] as const)("classifies context failures before %s without implying a start was sent", async (phase) => {
    const { session } = setupPlan();
    let handle: string | undefined;
    if (phase !== "prepare") {
      handle = ((await applicationApi.execute("goals.plan.prepare", { id: "goal_1" })).data as { handle: string }).handle;
      if (phase === "start") await applicationApi.execute("goals.plan.generate", { handle, runtime: "mlx", modelId: "test/model" });
    }
    jest.mocked(native.generateLocalProposal).mockClear();
    session.memoryContext.mockRejectedValue(new server.ApiError(503, "private server payload"));
    const input = phase === "prepare" ? { id: "goal_1" } : phase === "generate" ? { handle, runtime: "mlx", modelId: "test/model" } : { handle, confirm: true };
    const result = await planProtocolResult(`goals.plan.${phase}`, input);
    expect(result).toMatchObject({ job: { state: "failed", error: { code: "context_unavailable" } } });
    expect(JSON.stringify(result)).not.toContain("private server payload");
    expect(session.startGoal).not.toHaveBeenCalled();
    expect(native.generateLocalProposal).not.toHaveBeenCalled();
  });

  it("classifies an unusable prompt snapshot before inference as invalid_context", async () => {
    const { session } = setupPlan();
    session.bootstrapSync.mockResolvedValue({ agents: [] } as unknown as server.Bootstrap);
    const { handle } = (await applicationApi.execute("goals.plan.prepare", { id: "goal_1" })).data as { handle: string };
    const result = await planProtocolResult("goals.plan.generate", { handle, runtime: "mlx", modelId: "test/model" });
    expect(result).toMatchObject({ job: { state: "failed", error: { code: "invalid_context" } } });
    expect(native.generateLocalProposal).not.toHaveBeenCalled();
    expect(session.startGoal).not.toHaveBeenCalled();
  });

  it("classifies a local generation exception as failed without sending a start", async () => {
    const { session } = setupPlan();
    const { handle } = (await applicationApi.execute("goals.plan.prepare", { id: "goal_1" })).data as { handle: string };
    jest.mocked(native.generateLocalProposal).mockRejectedValue(new Error("private native runtime details"));
    const result = await planProtocolResult("goals.plan.generate", { handle, runtime: "mlx", modelId: "test/model" });
    expect(result).toMatchObject({ job: { state: "failed", error: { code: "generation_failed" } } });
    expect(JSON.stringify(result)).not.toContain("private native runtime details");
    expect(session.startGoal).not.toHaveBeenCalled();
  });

  it("preserves uncertain and one attempt after the authoritative start was sent", async () => {
    const { session } = setupPlan();
    const { handle } = (await applicationApi.execute("goals.plan.prepare", { id: "goal_1" })).data as { handle: string };
    await applicationApi.execute("goals.plan.generate", { handle, runtime: "mlx", modelId: "test/model" });
    session.startGoal.mockRejectedValue(new Error("private lost response"));
    expect(await planProtocolResult("goals.plan.start", { handle, confirm: true })).toMatchObject({ job: { state: "uncertain", error: { code: "outcome_unknown" } } });
    await expect(applicationApi.execute("goals.plan.start", { handle, confirm: true })).rejects.toMatchObject({ code: "invalid_state" });
    expect(session.startGoal).toHaveBeenCalledTimes(1);
  });

  it("generates an actual local plan, then sends only that reviewed output with fresh memory and never auto-starts", async () => {
    const { session, plan } = setupPlan();
    const prepared = (await applicationApi.execute("goals.plan.prepare", { id: "goal_1" })).data as unknown as LocalPlanContextHandle;
    expect(session.startGoal).not.toHaveBeenCalled();
    const generated = (await applicationApi.execute("goals.plan.generate", { handle: prepared.handle, runtime: "mlx", modelId: "test/model" })).data as unknown as GeneratedLocalPlan;
    expect(generated.plan).toEqual(plan);
    expect(native.generateLocalProposal).toHaveBeenCalledWith(expect.objectContaining({ maxTokens: 512, prompt: expect.stringContaining(plan.objective) }));
    expect(session.startGoal).not.toHaveBeenCalled();
    await applicationApi.execute("goals.plan.start", { handle: prepared.handle, confirm: true });
    expect(session.startGoal).toHaveBeenCalledWith("goal_1", { plan_proposal: plan, planner_source: "iphone_local", memory_context_fingerprint: "b".repeat(64) });
    await expect(applicationApi.execute("goals.plan.start", { handle: prepared.handle, confirm: true })).rejects.toMatchObject({ code: "invalid_state" });
    expect(session.startGoal).toHaveBeenCalledTimes(1);
  });

  it.each(["length", "cancelled"] as const)("never turns a %s generation into a startable plan", async (finishReason) => {
    const { session } = setupPlan();
    jest.mocked(native.generateLocalProposal).mockResolvedValue({ text: "{}", tokenCount: 1, finishReason });
    const { handle } = (await applicationApi.execute("goals.plan.prepare", { id: "goal_1" })).data as { handle: string };
    await expect(applicationApi.execute("goals.plan.generate", { handle, runtime: "mlx", modelId: "test/model" })).rejects.toMatchObject({ code: finishReason === "length" ? "generation_truncated" : "cancelled" });
    await expect(applicationApi.execute("goals.plan.start", { handle, confirm: true })).rejects.toMatchObject({ code: "invalid_state" });
    expect(session.startGoal).not.toHaveBeenCalled();
  });

  it("rejects changed memory before sending the reviewed local plan", async () => {
    const { session, memory } = setupPlan();
    const { handle } = (await applicationApi.execute("goals.plan.prepare", { id: "goal_1" })).data as { handle: string };
    await applicationApi.execute("goals.plan.generate", { handle, runtime: "mlx", modelId: "test/model" });
    session.memoryContext.mockResolvedValue({ ...memory, context_fingerprint: "d".repeat(64) });
    await expect(applicationApi.execute("goals.plan.start", { handle, confirm: true })).rejects.toMatchObject({ code: "stale_context" });
    expect(session.startGoal).not.toHaveBeenCalled();
  });

  it.each(["generate", "start"] as const)("revalidates handle expiry after asynchronous context reads before %s", async (phase) => {
    const { session, memory } = setupPlan();
    const now = jest.spyOn(Date, "now").mockReturnValue(1_000_000);
    try {
      const { handle } = (await applicationApi.execute("goals.plan.prepare", { id: "goal_1" })).data as { handle: string };
      if (phase === "start") await applicationApi.execute("goals.plan.generate", { handle, runtime: "mlx", modelId: "test/model" });
      jest.mocked(native.generateLocalProposal).mockClear();
      session.memoryContext.mockImplementation(async () => { now.mockReturnValue(1_600_001); return memory; });
      const input = phase === "start" ? { handle, confirm: true } : { handle, runtime: "mlx", modelId: "test/model" };
      await expect(applicationApi.execute(`goals.plan.${phase}`, input)).rejects.toMatchObject({ code: "session_expired" });
      expect(native.generateLocalProposal).not.toHaveBeenCalled();
      expect(session.startGoal).not.toHaveBeenCalled();
    } finally { now.mockRestore(); }
  });

  it.each([
    ["shell.run", {}], ["models.import", { uri: "file:///private/secret" }],
    ["goals.create", { objective: "Test", autonomy_profile: "assisted", max_parallelism: 4 }],
    ["goals.get", { id: "../secret" }], ["memory.delete", { id: "mem_one", arbitrary: true }],
    ["inference.generate", { prompt: "Test", maxTokens: 513 }],
    ["inference.generate", { prompt: "Test", temperature: NaN }],
    ["goals.create", { objective: "Test", autonomy_profile: "assisted", completion_criteria: [null] }],
  ])("refuses %s before calling a service", async (name, input) => {
    await expect(applicationApi.execute(name as string, input)).rejects.toBeInstanceOf(ApplicationApiError);
    expect(server.createGoal).not.toHaveBeenCalled();
    expect(server.getGoal).not.toHaveBeenCalled();
    expect(native.generateLocalProposal).not.toHaveBeenCalled();
  });

  it("routes UI and network callers through the same existing adapter", async () => {
    const agents = [{ id: "agent_one", auth_token_hash: "never-export" }] as unknown as server.Agent[];
    jest.mocked(server.listAgents).mockResolvedValue(agents);
    expect(await listAgents()).toBe(agents);
    const result = await applicationApi.execute("agents.list", {});
    expect(result.data).toEqual([{ id: "agent_one" }]);
    expect(result.metadata).toEqual({ source: "authoritative", observedAt: expect.any(String) });
    expect(server.listAgents).toHaveBeenCalledTimes(2);
  });

  it("preserves real goal bounds and the existing 0–5 feedback scale", async () => {
    jest.mocked(server.createGoal).mockResolvedValue({ goal: { id: "goal_one" }, nodes: [], result: null } as unknown as server.GoalDetail);
    await applicationApi.execute("goals.create", { objective: "Catalogue de livres", autonomy_profile: "assisted", max_steps: 20, max_parallelism: 3 });
    expect(server.createGoal).toHaveBeenCalledWith({ objective: "Catalogue de livres", autonomy_profile: "assisted", max_steps: 20, max_parallelism: 3 });
    await applicationApi.execute("goals.feedback", { id: "goal_one", feedback: { score: 5 } });
    expect(server.createGoalFeedback).toHaveBeenCalledWith("goal_one", { score: 5 });
  });

  it("shares the native operation lock across UI and HTTP and cancels without starting another generation", async () => {
    let finish!: (value: native.LocalGenerationResult) => void;
    jest.mocked(native.generateLocalProposal).mockReturnValue(new Promise((resolve) => { finish = resolve; }));
    const pending = generateLocalProposal({ prompt: "Réponds en JSON", maxTokens: 32 });
    await expect(applicationApi.execute("models.load", { runtime: "mlx", modelId: "test" })).rejects.toMatchObject({ code: "busy" });
    expect(native.loadLocalModel).not.toHaveBeenCalled();
    await applicationApi.execute("inference.cancel", {});
    expect(native.cancelLocalGeneration).toHaveBeenCalledTimes(1);
    finish({ text: "", tokenCount: 0, finishReason: "cancelled" });
    await pending;
    await expect(applicationApi.execute("inference.cancel", {})).rejects.toMatchObject({ code: "not_running" });
    expect(native.generateLocalProposal).toHaveBeenCalledTimes(1);
  });

  it("only cancels the generation owned by a closing UI session, never a newer API operation", async () => {
    const status = { state: "ready", runtime: "mlx", modelId: "test/model", revision: "a".repeat(40) } as const;
    jest.mocked(native.getLocalInferenceStatus).mockResolvedValue(status);
    let finish!: (value: native.LocalGenerationResult) => void;
    jest.mocked(native.generateLocalProposal).mockImplementation(() => new Promise((resolve) => { finish = resolve; }));
    jest.mocked(native.cancelLocalGeneration).mockResolvedValue();
    const owner = createLocalGenerationSession();
    const other = createLocalGenerationSession();
    const pending = owner.generate({ prompt: "Test" }, status);
    await Promise.resolve();
    await other.close();
    expect(native.cancelLocalGeneration).not.toHaveBeenCalled();
    await owner.cancel();
    expect(native.cancelLocalGeneration).toHaveBeenCalledTimes(1);
    finish({ text: "", tokenCount: 0, finishReason: "cancelled" });
    await pending;
    const apiPending = applicationApi.execute("inference.generate", { prompt: "API" });
    await owner.close();
    expect(native.cancelLocalGeneration).toHaveBeenCalledTimes(1);
    expect(native.unloadLocalModel).not.toHaveBeenCalled();
    finish({ text: "API_OK", tokenCount: 2, finishReason: "stop" });
    await apiPending;
    await expect(owner.generate({ prompt: "Too late" }, status)).rejects.toMatchObject({ code: "cancelled" });
    expect(native.generateLocalProposal).toHaveBeenCalledTimes(2);
  });

  it("closing a UI generation during its asynchronous preflight prevents native inference", async () => {
    const status = { state: "ready", runtime: "mlx", modelId: "test/model", revision: null } as const;
    let resolveStatus!: (value: native.LocalInferenceStatus) => void;
    jest.mocked(native.getLocalInferenceStatus).mockReturnValue(new Promise((resolve) => { resolveStatus = resolve; }));
    const owner = createLocalGenerationSession();
    const pending = owner.generate({ prompt: "Test" }, status);
    const rejected = expect(pending).rejects.toMatchObject({ code: "cancelled" });
    await owner.close();
    resolveStatus(status);
    await rejected;
    expect(native.generateLocalProposal).not.toHaveBeenCalled();
    expect(native.unloadLocalModel).not.toHaveBeenCalled();
  });

  it("rejects UI inference if the globally loaded model changed before admission", async () => {
    jest.mocked(native.getLocalInferenceStatus).mockResolvedValue({ state: "ready", runtime: "mlx", modelId: "other/model", revision: null });
    const owner = createLocalGenerationSession();
    await expect(owner.generate({ prompt: "Test" }, { runtime: "mlx", modelId: "test/model", revision: null })).rejects.toMatchObject({ code: "model_not_ready" });
    expect(native.generateLocalProposal).not.toHaveBeenCalled();
    await owner.close();
    expect(native.cancelLocalGeneration).not.toHaveBeenCalled();
  });

  it.each(["load", "download"] as const)("lets unload interrupt a pending %s without losing the unload lock", async (kind) => {
    let finishOld!: (value: never) => void;
    let finishUnload!: () => void;
    const old = new Promise<never>((resolve) => { finishOld = resolve; });
    jest.mocked(native.loadLocalModel).mockReturnValue(old);
    jest.mocked(native.downloadLocalGgufModel).mockReturnValue(old);
    jest.mocked(native.unloadLocalModel).mockReturnValue(new Promise<void>((resolve) => { finishUnload = resolve; }));
    const pending = applicationApi.execute(kind === "load" ? "models.load" : "models.download",
      kind === "load" ? { runtime: "mlx", modelId: "test" } : { preset: "dolphin-gguf" });
    const unloading = applicationApi.execute("models.unload", {});
    expect(native.unloadLocalModel).toHaveBeenCalledTimes(1);
    finishOld(undefined as never);
    await pending;
    await expect(applicationApi.execute("inference.generate", { prompt: "Test" })).rejects.toMatchObject({ code: "busy" });
    finishUnload();
    await unloading;
  });

  it("does not return service exception details or credentials", async () => {
    jest.mocked(server.getGoal).mockRejectedValue(new server.ApiError(409, "secret bearer private stack"));
    await expect(applicationApi.execute("goals.get", { id: "goal_one" })).rejects.toMatchObject({ code: "conflict", message: "Le serveur a refusé cette opération (HTTP 409)." });
    jest.mocked(server.getGoal).mockRejectedValue(new Error("secret bearer private stack"));
    await expect(applicationApi.execute("goals.get", { id: "goal_one" })).rejects.toMatchObject({ code: "operation_failed" });
    try { await applicationApi.execute("goals.get", { id: "goal_one" }); } catch (error) { expect(String(error)).not.toContain("secret"); }
  });

  it("rejects non-JSON or oversized service values rather than silently claiming a valid result", async () => {
    jest.mocked(server.getGoal).mockResolvedValue({ result: () => "unsafe" } as unknown as server.GoalDetail);
    await expect(applicationApi.execute("goals.get", { id: "goal_one" })).rejects.toMatchObject({ code: "invalid_response" });
    jest.mocked(server.getGoal).mockResolvedValue({ result: "x".repeat(70_000) } as unknown as server.GoalDetail);
    await expect(applicationApi.execute("goals.get", { id: "goal_one" })).rejects.toMatchObject({ code: "invalid_response" });
  });

  it("keeps a committed mutation successful when its response cannot be projected, and classifies HTTP 500 as uncertain", async () => {
    jest.mocked(server.createGoal).mockResolvedValue({ goal: { id: "goal_one", objective: "x".repeat(70_000) } } as unknown as server.GoalDetail);
    const input = { objective: "Catalogue", autonomy_profile: "assisted" };
    expect(await applicationApi.execute("goals.create", input)).toMatchObject({ data: null, metadata: { resultAvailable: false } });
    expect(server.createGoal).toHaveBeenCalledTimes(1);
    jest.mocked(server.createGoal).mockRejectedValue(new server.ApiError(500, "private error"));
    await expect(applicationApi.execute("goals.create", input)).rejects.toMatchObject({ code: "outcome_unknown" });
    expect(server.createGoal).toHaveBeenCalledTimes(2);
  });
});
