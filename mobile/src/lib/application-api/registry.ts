import * as server from "@/lib/api/client";
import type { SwiftValidationAttempt, SwiftValidationOptions } from "@/lib/api/project";
import Constants from "expo-constants";
import { AppState, Platform } from "react-native";
import type { GoalCreateInput, GoalFeedbackInput, TaskMode, TaskStatus } from "@/lib/api/types";
import * as inference from "@/lib/local-inference";
import * as embeddings from "@/lib/local-embeddings";
import { LOCAL_MODEL_PRESETS } from "@/lib/local-model-presets";
import * as settings from "@/lib/local-model-settings";
import { applicationSessions } from "./sessions";
import { outputDescriptor, type CommandOutputDescriptor } from "./outputs";
import { pairApplicationConnection, type PairApplicationConnectionInput } from "./connection";
import { readApplicationSyncState } from "./sync-state";
import { submitReviewedToolProposal } from "./tool-proposal";
import { assertGoalPlanSnapshotCurrent, buildLocalGoalPlanPrompt, localPlanPreparationError, parseCompletedLocalGoalPlan, readInitialGoal, startReviewedLocalGoalPlan, type GoalPlanSession, type GoalPlanSnapshot } from "./goal-plan";
import {
  ApplicationApiError, boolean, choice, identifier, integer, list, number, object,
  serializableResult, text, validateInput, type JsonSchema, type JsonValue,
} from "./schema";

export type CommandSource = "authoritative" | "device" | "cache";
export type ApplicationCommand = {
  name: string;
  version: "1.0";
  available: boolean;
  unavailableReason?: string;
  effect: "read" | "mutation";
  source: CommandSource;
  execution: "immediate" | "job";
  requiresForeground: boolean;
  osInteraction: boolean;
  readiness: "device" | "paired_server" | "loaded_model" | "review_required";
  inputSchema: JsonSchema;
  output: CommandOutputDescriptor;
};
export type ApplicationResult = { data: JsonValue; metadata: {
  source: CommandSource; observedAt: string; resultAvailable?: false; resultError?: { code: string; message: string };
} };
type InvocationContext = {
  shouldAccept?: () => boolean;
  // In-process ownership only: never accepted from command JSON or exposed over HTTP.
  nativeOwner?: symbol;
  expectedModel?: Pick<inference.LocalInferenceStatus, "runtime" | "modelId" | "revision">;
};
type Definition = ApplicationCommand & {
  handler?: (input: Record<string, unknown>, context: InvocationContext) => Promise<unknown>;
  publicResult?: (value: unknown) => unknown;
};
const definitions = new Map<string, Definition>();

function register<T extends object>(
  name: string, inputSchema: JsonSchema, handler: (input: T, context: InvocationContext) => unknown | Promise<unknown>,
  options: Partial<Omit<ApplicationCommand, "name" | "version" | "inputSchema">> & { publicResult?: (value: unknown) => unknown } = {},
): void {
  if (definitions.has(name)) throw new Error("Duplicate application command");
  definitions.set(name, {
    name, version: "1.0", available: true, effect: "read", source: "authoritative", execution: "immediate",
    requiresForeground: false, osInteraction: false, readiness: "paired_server", ...options, inputSchema,
    output: outputDescriptor(name), handler: async (input, context) => handler(input as T, context),
  });
}

const device = { source: "device", readiness: "device" } as const;
const mutation = { effect: "mutation", execution: "job" } as const;
const runtime = choice("coreml", "mlx", "llama.cpp");
const generationProperties = { maxTokens: integer(1, 512), temperature: number(0, 2) };
const taskMode = choice("normal", "commandant", "review", "autonome");
const taskStatus = choice("created", "planned", "queued", "running", "blocked", "waiting_permission", "completed", "failed", "cancelled");
const idInput = object({ id: identifier });
const noInput = object();
type NativeOperation = { kind: "load" | "unload" | "generate" | "download" | "import"; cancelled: boolean; owner?: symbol };
let nativeOperation: NativeOperation | null = null;

async function withNativeOperation<T>(kind: NativeOperation["kind"], operation: (assertActive: () => void) => Promise<T>, owner?: symbol): Promise<T> {
  const interrupts = kind === "unload" && nativeOperation && nativeOperation.kind !== "unload";
  if (nativeOperation && !interrupts) throw new ApplicationApiError("busy", "Une opération locale est déjà en cours.");
  const ticket = { kind, cancelled: false, owner };
  nativeOperation = ticket;
  const assertActive = () => {
    if (ticket.cancelled || nativeOperation !== ticket) throw new ApplicationApiError("cancelled", "L’opération locale a été annulée.");
  };
  try { return await operation(assertActive); } finally { if (nativeOperation === ticket) nativeOperation = null; }
}

register("app.status", noInput, async () => ({
  appVersion: Constants.expoConfig?.version ?? null,
  appVersionSource: "expo_config" as const,
  nativeBuildNumber: Constants.platform?.ios?.buildNumber ?? null,
  configuredBuildNumber: Constants.expoConfig?.ios?.buildNumber ?? null,
  platform: Platform.OS,
  appState: AppState.currentState,
  pairedCredentialStored: await server.hasDeviceToken(),
  localInferenceAvailable: inference.isLocalInferenceAvailable(),
  ...(inference.isLocalInferenceAvailable() ? { localInference: await inference.getLocalInferenceStatus() } : {}),
  // Stored credentials and an available native module do not prove server authentication or model readiness.
}), device);
register("connection.status", noInput, async () => ({ credentialStored: await server.hasDeviceToken() }), device);
register("connection.origin", noInput, () => server.getServerUrl(), device);
register<PairApplicationConnectionInput>("connection.pair", object({ code: { ...text(6), pattern: "^[0-9]{6}$" }, deviceName: text(200), serverUrl: text(2000) }),
  (input) => pairApplicationConnection(input), { ...mutation, readiness: "device", publicResult: (value) => {
    const receipt = value as server.PairingResult;
    return { serverUrl: receipt.serverUrl, counts: receipt.bootstrap.counts, cursor: receipt.bootstrap.cursor };
  } });
register("sync.status", noInput, () => readApplicationSyncState(), device);
register("sync.refresh", noInput, (_, context) => server.bootstrapSync(context.shouldAccept), { ...mutation, publicResult: (value) => {
  const result = value as server.Bootstrap;
  return { counts: result.counts, cursor: result.cursor };
} });
register("goals.list", noInput, (_, context) => context.shouldAccept ? server.listGoals(context.shouldAccept) : server.listGoals());
register<{ id: string }>("goals.get", idInput, ({ id }, context) => context.shouldAccept ? server.getGoal(id, context.shouldAccept) : server.getGoal(id));
register<{ id: string }>("goals.nodes", idInput, ({ id }, context) => context.shouldAccept ? server.listGoalNodes(id, context.shouldAccept) : server.listGoalNodes(id));
register<{ id: string }>("goals.result", idInput, ({ id }, context) => context.shouldAccept ? server.getGoalResult(id, context.shouldAccept) : server.getGoalResult(id));
register<{ goalId: string; nodeId: string; workerJobId: string }>("goals.writing-draft", object({ goalId: identifier, nodeId: identifier, workerJobId: identifier }),
  ({ goalId, nodeId, workerJobId }, context) => server.getGoalWritingDraft(goalId, nodeId, workerJobId, context.shouldAccept));
register<GoalCreateInput>("goals.create", object({
  objective: text(4000), autonomy_profile: choice("manual", "assisted", "autonomous"),
  completion_criteria: list(text(500), 20), max_steps: integer(1, 20), max_parallelism: integer(1, 3),
  max_replans: integer(0, 10), max_runtime_seconds: integer(30, 86_400), max_model_calls: integer(1, 100),
}, ["objective", "autonomy_profile"]), (input) => server.createGoal(input), mutation);
register<{ id: string }>("goals.start", idInput, ({ id }) => server.startGoal(id), mutation);
register<{ id: string; reason?: string }>("goals.replan", object({ id: identifier, reason: text(500) }, ["id"]),
  ({ id, reason }) => server.replanGoal(id, reason), mutation);
register<{ id: string }>("goals.cancel", idInput, ({ id }) => server.cancelGoal(id), mutation);
register<{ id: string }>("goals.messages", idInput, async ({ id }) => (await server.getGoalConversation(id)).conversation);
register<{ id: string; feedback: GoalFeedbackInput }>("goals.feedback", object({ id: identifier, feedback: object({
  score: number(0, 5), note: text(4000, 0), corrected_final_answer: text(32_000, 0), corrected_plan_summary: text(16_000, 0),
}, ["score"]) }), ({ id, feedback }) => server.createGoalFeedback(id, feedback), mutation);

register<{ status?: TaskStatus }>("tasks.list", object({ status: taskStatus }, []), ({ status }) => server.listTasks(status));
register<{ id: string }>("tasks.get", idInput, ({ id }) => server.getTask(id));
register<{ input: string; mode?: TaskMode }>("tasks.create", object({ input: text(32_000), mode: taskMode }, ["input"]),
  ({ input, mode }) => server.createTask(input, mode), mutation);
register<{ id: string }>("tasks.plan", idInput, ({ id }) => server.planTask(id), mutation);
register<{ id: string }>("tasks.cancel", idInput, ({ id }) => server.cancelTask(id), mutation);
register<{ conversationId: string }>("chat.messages", object({ conversationId: identifier }),
  ({ conversationId }, context) => context.shouldAccept ? server.listMessages(conversationId, context.shouldAccept) : server.listMessages(conversationId));
register<{ content: string; conversationId?: string; mode?: TaskMode; startTask?: boolean }>("chat.send", object({
  content: text(32_000), conversationId: identifier, mode: taskMode, startTask: boolean,
}, ["content"]), ({ content, conversationId, mode, startTask }) => server.sendChat(content, conversationId, mode, startTask), mutation);

register<{ status?: server.Approval["status"] }>("approvals.list", object({ status: choice("pending", "approved", "denied", "expired", "cancelled") }, []),
  ({ status }) => server.listApprovals(status));
register<{ id: string; decision: "approve" | "deny"; confirm: true }>("approvals.decide", object({
  id: identifier, decision: choice("approve", "deny"), confirm: { type: "boolean", enum: [true] },
}), ({ id, decision }) => server.decideApproval(id, decision), { ...mutation, readiness: "review_required", publicResult: (value) => {
  const receipt = value as server.ApprovalDecisionReceipt;
  return { authoritativeResult: receipt.authoritativeResult, localReplicaError: receipt.localReplicaError ? "La décision est enregistrée ; la copie locale doit être actualisée." : null };
} });
register("memory.list", noInput, () => server.listMemory());
register("memory.status", noInput, () => server.getMemoryProviderStatus());
register<{ goalId: string }>("context.inspect", object({ goalId: identifier }), ({ goalId }) => server.getProjectContext(goalId), mutation);
register<{ goalId: string }>("context.compact", object({ goalId: identifier }), ({ goalId }) => server.compactProjectContext(goalId), mutation);
register<{ goalId: string; sourceId: string }>("context.source", object({ goalId: identifier, sourceId: identifier }), ({ goalId, sourceId }) => server.getProjectContextSource(goalId, sourceId));
const embeddingAvailability = { available: embeddings.isLocalEmbeddingAvailable(), unavailableReason: "Cette version native ne fournit pas les embeddings." };
register("embeddings.status", noInput, () => embeddings.getLocalEmbeddingStatus(), { ...device, ...embeddingAvailability });
register<embeddings.LocalEmbeddingLoadInput>("embeddings.load", object({ modelId: text(200), revision: text(40), experimental: boolean }), (input) => embeddings.loadLocalEmbedder(input), { ...device, ...mutation, ...embeddingAvailability, requiresForeground: true });
register<embeddings.LocalEmbeddingInput>("embeddings.generate", object({ texts: list(text(16384), 8), kind: choice("query", "document") }), (input) => embeddings.embedLocalTexts(input), { ...device, ...mutation, ...embeddingAvailability, requiresForeground: true });
register("embeddings.unload", noInput, () => embeddings.unloadLocalEmbedder(), { ...device, ...mutation, ...embeddingAvailability, requiresForeground: true });
register<{ query: string }>("memory.search", object({ query: text(2000) }), ({ query }) => server.searchMemory(query));
register<Parameters<typeof server.rememberMemory>[0]>("memory.create", object({
  content: text(32_000), summary: text(2000, 0), scope: text(100), kind: text(100), pinned: boolean,
}, ["content"]), (input) => server.rememberMemory(input), mutation);
register<{ id: string; content?: string; summary?: string; pinned?: boolean }>("memory.update", object({
  id: identifier, content: text(32_000), summary: text(2000, 0), pinned: boolean,
}, ["id"]), ({ id, ...input }) => server.updateMemory(id, input), mutation);
register<{ id: string }>("memory.delete", idInput, ({ id }) => server.deleteMemory(id), mutation);
register("agents.list", noInput, () => server.listAgents());
register("activities.catalog", noInput, () => server.getActivityCatalog());
register<{ limit?: number }>("audit.summary", object({ limit: integer(1, 200) }, []), ({ limit }) => server.listAudit(limit ?? 30), {
  publicResult: (value) => (value as server.AuditEvent[]).map(({ id, event_type, created_at, hash }) => ({ id, event_type, created_at, hash })),
});
register("cache.summary", noInput, async () => {
  // Read the same scoped replica as the interface; no payloads or write authority leave this projection.
  // eslint-disable-next-line @typescript-eslint/no-require-imports
  const { localSwarmSnapshot } = require("@/lib/state/replica") as typeof import("@/lib/state/replica");
  const snapshot = await localSwarmSnapshot(await server.getServerUrl());
  return {
    available: snapshot !== null, cursor: snapshot?.cursor ?? null,
    counts: snapshot ? { goals: snapshot.goals.length, nodes: snapshot.plan_nodes.length,
      results: snapshot.goal_results.length, agents: snapshot.agents.length } : null,
    authorizesSensitiveActions: false,
  };
}, { ...device, source: "cache" });
register<Parameters<typeof server.createFeedback>[0]>("feedback.create", object({
  task_id: identifier, agent_id: identifier, score: number(0, 5), notes: text(4000, 0), label: text(200, 0),
}, []), (input) => server.createFeedback(input), mutation);
register("outbox.status", noInput, async () => {
  // Load native storage only when this command is invoked, including in the UI test runtime.
  // eslint-disable-next-line @typescript-eslint/no-require-imports
  const { mutationOutbox } = require("@/lib/state/mutation-outbox") as typeof import("@/lib/state/mutation-outbox");
  return { pending: await mutationOutbox.pendingCount(await server.getServerUrl()) };
}, { ...device, source: "cache" });
register<{ limit?: number }>("outbox.drain", object({ limit: integer(1, 100) }, []), ({ limit }) => server.drainMutationOutbox(limit), mutation);

register("models.capabilities", noInput, () => inference.getLocalInferenceCapabilities(), device);
register("models.list", noInput, () => inference.listLocalModels(), device);
register("models.status", noInput, () => inference.getLocalInferenceStatus(), device);
register("models.presets", noInput, () => LOCAL_MODEL_PRESETS, device);
register<{ runtime: inference.LocalInferenceRuntime }>("models.import", object({ runtime }),
  ({ runtime: selectedRuntime }, context) => withNativeOperation("import", async (assertActive) => {
    const assertCurrent = () => {
      assertActive();
      if (context.shouldAccept && !context.shouldAccept()) throw new ApplicationApiError("cancelled", "Importation annulée.");
    };
    assertCurrent();
    if (selectedRuntime !== "llama.cpp") return inference.pickAndImportLocalModelDirectory(selectedRuntime);
    // The OS picker supplies the only accepted URI; callers never submit filesystem paths.
    // eslint-disable-next-line @typescript-eslint/no-require-imports
    const picker = require("expo-document-picker") as typeof import("expo-document-picker");
    const picked = await picker.getDocumentAsync({ copyToCacheDirectory: true, multiple: false, type: "*/*" });
    assertCurrent();
    if (picked.canceled) throw new ApplicationApiError("cancelled", "Importation annulée.");
    const asset = picked.assets[0];
    if (!asset || !asset.name.toLowerCase().endsWith(".gguf")) throw new ApplicationApiError("invalid_selection", "Choisis un fichier .gguf.");
    return inference.importLocalModel({ runtime: selectedRuntime, uri: asset.uri, displayName: asset.name });
  }),
  { ...device, ...mutation, requiresForeground: true, osInteraction: true });
register<Parameters<typeof inference.loadLocalModel>[0]>("models.load", object({
  runtime, modelId: text(200), revision: { ...text(40), pattern: "^[a-fA-F0-9]{40}$" },
}, ["runtime", "modelId"]), (input) => withNativeOperation("load", () => inference.loadLocalModel(input)), { ...device, ...mutation, requiresForeground: true });
register("models.unload", noInput, () => withNativeOperation("unload", () => inference.unloadLocalModel()), { ...device, ...mutation, requiresForeground: true });
register<{ preset: "dolphin-gguf" }>("models.download", object({ preset: choice("dolphin-gguf") }),
  () => withNativeOperation("download", () => inference.downloadLocalGgufModel(LOCAL_MODEL_PRESETS["llama.cpp"].download!)), { ...device, ...mutation, requiresForeground: true });
register("models.download.cancel", noInput, () => {
  if (nativeOperation?.kind !== "download") throw new ApplicationApiError("not_running", "Aucun téléchargement de cette API n’est en cours.");
  nativeOperation.cancelled = true;
  return inference.cancelLocalModelDownload();
}, { ...device, ...mutation, requiresForeground: true });
register<Parameters<typeof inference.generateLocalProposal>[0]>("inference.generate", object({ prompt: text(32_000), ...generationProperties }, ["prompt"]),
  (input, context) => withNativeOperation("generate", async (assertActive) => {
    if (context.expectedModel) {
      const status = await inference.getLocalInferenceStatus();
      if (status.state !== "ready" || status.runtime !== context.expectedModel.runtime
          || status.modelId !== context.expectedModel.modelId || status.revision !== context.expectedModel.revision) {
        throw new ApplicationApiError("model_not_ready", "Le modèle chargé ne correspond plus au modèle affiché. Actualisez son état.");
      }
    }
    assertActive();
    if (context.shouldAccept && !context.shouldAccept()) throw new ApplicationApiError("cancelled", "Cette session locale est fermée.");
    return inference.generateLocalProposal(input);
  }, context.nativeOwner), { ...device, ...mutation, readiness: "loaded_model", requiresForeground: true });
register("inference.cancel", noInput, (_, context) => {
  if (nativeOperation?.kind !== "generate" || (context.nativeOwner !== undefined && nativeOperation.owner !== context.nativeOwner)) {
    throw new ApplicationApiError("not_running", "Aucune génération appartenant à cette session n’est en cours.");
  }
  nativeOperation.cancelled = true;
  return inference.cancelLocalGeneration();
}, { ...device, ...mutation, requiresForeground: true });
type ToolReview = { intent: string; proposal: server.ToolProposalInput; state: "review" | "sending" | "submitted" | "uncertain"; taskId: string | null };
register<{ intent: string; maxTokens?: number; temperature?: number }>("inference.proposal.generate", object({ intent: text(30_000), ...generationProperties }, ["intent"]),
  ({ intent, maxTokens, temperature }) => withNativeOperation("generate", async (assertActive) => {
    const result = await inference.generateLocalProposal({ prompt: inference.buildLocalProposalPrompt(intent), maxTokens, temperature });
    assertActive();
    if (result.finishReason !== "stop") throw new ApplicationApiError(result.finishReason === "length" ? "generation_truncated" : "cancelled", "La génération n’est pas complète ; aucune proposition ne peut être soumise.");
    const proposal = inference.parseLocalToolProposal(result.text);
    const handle = inference.isActionableToolProposal(proposal)
      ? applicationSessions.put<ToolReview>("tool-review", { intent, proposal, state: "review", taskId: null }) : null;
    return { handle, proposal, tokenCount: result.tokenCount, finishReason: result.finishReason };
  }), { ...device, ...mutation, readiness: "loaded_model", requiresForeground: true });
register<{ handle: string; confirm: true }>("tasks.proposal.submit", object({ handle: identifier, confirm: { type: "boolean", enum: [true] } }), async ({ handle }) => {
  const review = applicationSessions.get<ToolReview>(handle, "tool-review");
  if (review.state !== "review") throw new ApplicationApiError("invalid_state", "Cette proposition a déjà été envoyée. Vérifiez la tâche avant de poursuivre.");
  review.state = "sending";
  try {
    const receipt = await submitReviewedToolProposal(review.intent, review.proposal, (task) => { review.taskId = task.id; }, () => {
      if (applicationSessions.get<ToolReview>(handle, "tool-review") !== review) throw new ApplicationApiError("session_expired", "Cette revue n’est plus actuelle.");
    });
    review.state = "submitted";
    return receipt;
  } catch {
    review.state = "uncertain";
    throw new ApplicationApiError("outcome_unknown", "La soumission n’est pas confirmée. Consultez l’état de la proposition et la liste des tâches avant toute nouvelle tentative.");
  }
}, mutation);
register<{ handle: string }>("tasks.proposal.status", object({ handle: identifier }), ({ handle }) => {
  const review = applicationSessions.get<ToolReview>(handle, "tool-review");
  return { state: review.state, taskId: review.taskId };
}, device);
register("settings.local.read", noInput, () => settings.readLocalModelSettings(), device);
register<settings.LocalModelSettings>("settings.local.update", object({
  runtime, modelId: text(200, 0), revision: text(40, 0), ...generationProperties,
}), (input) => settings.saveLocalModelSettings(input), { ...device, ...mutation });

const confirmedHandle = object({ handle: identifier, confirm: { type: "boolean", enum: [true] } });
register<{ id: string }>("goals.conversation.open", idInput, ({ id }) => server.getGoalConversation(id), {
  publicResult: (value) => {
    const session = value as server.GoalConversationSession;
    return { handle: applicationSessions.put("conversation", session), conversation: session.conversation };
  },
});
register<{ handle: string; message: string; planningMode?: "iphone_local" }>("goals.reply.prepare", object({
  handle: identifier, message: text(4000), planningMode: choice("iphone_local"),
}, ["handle", "message"]), ({ handle, message, planningMode }) => {
  const session = applicationSessions.get<server.GoalConversationSession>(handle, "conversation");
  const attempt = session.prepareReply(message, planningMode ? { planningMode } : undefined);
  return { handle: applicationSessions.put("reply", attempt), clientMessageId: attempt.clientMessageId };
}, { ...device, ...mutation });
register<{ handle: string; confirm: true }>("goals.reply.send", confirmedHandle,
  ({ handle }) => applicationSessions.get<server.GoalReplyAttempt>(handle, "reply").send(), mutation);
register<{ id: string; nodeId: string }>("code.review", object({ id: identifier, nodeId: identifier }),
  ({ id, nodeId }) => server.reviewGoalCodeProposal(id, nodeId), { publicResult: (value) => {
    const review = value as server.GoalCodeProposalReview;
    return { handle: applicationSessions.put("code-review", review), proposal: review.proposal };
  } });
register<{ handle: string; confirm: true }>("code.prepareApproval", confirmedHandle,
  ({ handle }) => applicationSessions.get<server.GoalCodeProposalReview>(handle, "code-review").prepareApproval(), mutation);
register<{ id: string }>("project.review", idInput, ({ id }) => server.reviewGoalProject(id), { publicResult: (value) => {
  const review = value as server.ProjectReview;
  return { handle: applicationSessions.put("project-review", review), project: review.project };
} });
register<{ handle: string; confirm: true }>("project.prepareApproval", confirmedHandle,
  ({ handle }) => applicationSessions.get<server.ProjectReview>(handle, "project-review").prepareApproval(), mutation);

register<{ handle: string; options: SwiftValidationOptions }>("project.swift.prepare", object({
  handle: identifier, options: object({ agentId: identifier, operation: choice("build", "test"), target: object({
    kind: choice("swiftpm", "xcode"), project: text(240), scheme: text(100), destination: text(64),
  }, ["kind"]) }),
}), ({ handle, options }) => applicationSessions.get<server.ProjectReview>(handle, "project-review").prepareSwiftValidation(options), {
  ...device, publicResult: (value) => {
    const attempt = value as SwiftValidationAttempt;
    return { handle: applicationSessions.put("swift-validation", attempt), idempotencyKey: attempt.idempotencyKey };
  },
});
register<{ handle: string; confirm: true }>("project.swift.submit", confirmedHandle,
  ({ handle }) => applicationSessions.get<SwiftValidationAttempt>(handle, "swift-validation").send(), mutation);
register<{ id: string }>("project.swift.status", idInput, ({ id }) => server.getSwiftProjectValidation(id));
register<{ id: string; validationId: string; confirm: true }>("project.swift.cancel", object({
  id: identifier, validationId: identifier, confirm: { type: "boolean", enum: [true] },
}), ({ id, validationId }) => server.cancelSwiftProjectValidation(id, validationId), mutation);

type PlanReview = {
  goalId: string; session: GoalPlanSession; snapshot: GoalPlanSnapshot;
  state: "prepared" | "generating" | "generated" | "starting" | "started" | "uncertain";
  rawText?: string;
};
register<{ id: string }>("goals.plan.prepare", idInput, async ({ id }) => {
  try {
    const session = await server.createLocalGoalPlanSession();
    const snapshot = await readInitialGoal(session, id);
    const review: PlanReview = { goalId: id, session, snapshot, state: "prepared" };
    return { handle: applicationSessions.put("plan", review), goal: snapshot.detail.goal,
      memory: snapshot.context.memory, agents: snapshot.context.agents };
  } catch (cause) { throw localPlanPreparationError(cause, "context_unavailable"); }
}, mutation);

const capabilityTransport = () => {
  // Metro supports a statically named lazy require without eagerly initializing every native capability.
  // eslint-disable-next-line @typescript-eslint/no-require-imports
  return (require("@/lib/iphone-capabilities/runtime") as typeof import("@/lib/iphone-capabilities/runtime")).iphoneCapabilityTransport;
};
register("iphone.requests.list", noInput, async () => (await capabilityTransport()).refresh());
register<{ id: string }>("iphone.requests.get", idInput, async ({ id }) => (await capabilityTransport()).load(id));
register<{ id: string; decision: "approve" | "deny"; confirm: true }>("iphone.requests.decide", object({
  id: identifier, decision: choice("approve", "deny"), confirm: { type: "boolean", enum: [true] },
}), async ({ id, decision }) => (await capabilityTransport()).authorize(id, decision), { ...mutation, readiness: "review_required", publicResult: (value) => {
  const receipt = value as import("@/lib/iphone-capabilities/types").CapabilityAuthorizationResponse;
  return { status: receipt.status };
} });
register<{ id: string; confirm: true }>("iphone.requests.execute", object({ id: identifier, confirm: { type: "boolean", enum: [true] } }),
  async ({ id }) => (await capabilityTransport()).execute(id), { ...device, ...mutation, readiness: "review_required", requiresForeground: true, osInteraction: true });
register<{ handle: string; runtime: inference.LocalInferenceRuntime; modelId: string; revision?: string; temperature?: number }>(
  "goals.plan.generate", object({ handle: identifier, runtime, modelId: text(200), revision: text(40), temperature: number(0, 2) }, ["handle", "runtime", "modelId"]),
  async ({ handle, runtime: selectedRuntime, modelId, revision, temperature }) => {
    const review = applicationSessions.get<PlanReview>(handle, "plan");
    const assertReview = () => {
      if (applicationSessions.get<PlanReview>(handle, "plan") !== review) throw new ApplicationApiError("session_expired", "Cette session de planification n’est plus actuelle.");
    };
    if (!["prepared", "generated"].includes(review.state)) throw new ApplicationApiError("busy", "Cette session de planification ne peut pas être rejouée.");
    review.state = "generating";
    review.rawText = undefined;
    try {
      return await withNativeOperation("generate", async (assertActive) => {
        const snapshot = await readInitialGoal(review.session, review.goalId).catch((cause: unknown) => {
          throw localPlanPreparationError(cause, "context_unavailable");
        });
        assertActive();
        assertReview();
        assertGoalPlanSnapshotCurrent(snapshot, review.snapshot);
        const current = await inference.getLocalInferenceStatus();
        if (current.state !== "ready" || current.runtime !== selectedRuntime || current.modelId !== modelId || (revision !== undefined && current.revision !== revision)) {
          throw new ApplicationApiError("model_not_ready", "Le modèle chargé ne correspond pas au modèle demandé.");
        }
        const capabilities = await inference.getLocalInferenceCapabilities();
        if (!(selectedRuntime === "mlx" ? capabilities.mlx : selectedRuntime === "coreml" ? capabilities.coreml : capabilities.llamaCpp)) {
          throw new ApplicationApiError("unsupported_runtime", "Ce runtime n’est pas disponible sur cet appareil.");
        }
        await review.session.assertCurrent();
        assertActive();
        assertReview();
        const result = await inference.generateLocalProposal({ prompt: buildLocalGoalPlanPrompt(snapshot), maxTokens: 512, temperature: temperature ?? 0.1 });
        assertActive();
        assertReview();
        const plan = parseCompletedLocalGoalPlan(result, snapshot);
        review.snapshot = snapshot;
        review.rawText = result.text;
        review.state = "generated";
        return { handle, plan, tokenCount: result.tokenCount, finishReason: result.finishReason,
          model: { runtime: current.runtime, modelId: current.modelId, revision: current.revision } };
      });
    } catch (cause) { throw localPlanPreparationError(cause, "generation_failed"); }
    finally { if (review.state === "generating") review.state = "prepared"; }
  }, { ...device, ...mutation, readiness: "loaded_model", requiresForeground: true });
register<{ handle: string; confirm: true }>("goals.plan.start", confirmedHandle, async ({ handle }) => {
  const review = applicationSessions.get<PlanReview>(handle, "plan");
  if (review.state !== "generated" || review.rawText === undefined) throw new ApplicationApiError("invalid_state", "Un plan local complet doit être généré et relu avant le démarrage.");
  review.state = "starting";
  let attempted = false;
  try {
    const result = await startReviewedLocalGoalPlan(review.session, review.goalId, review.snapshot, review.rawText, {
      assertReviewCurrent: () => {
        if (applicationSessions.get<PlanReview>(handle, "plan") !== review) throw new ApplicationApiError("session_expired", "Cette session de planification n’est plus actuelle.");
      },
      onAttempt: () => { attempted = true; },
    });
    review.state = "started";
    return result;
  } catch (cause) {
    review.state = attempted ? "uncertain" : "prepared";
    if (attempted) throw new ApplicationApiError("outcome_unknown", "Le démarrage a été envoyé. Vérifiez le but avant toute nouvelle tentative.");
    throw localPlanPreparationError(cause, "context_unavailable");
  }
}, mutation);

/** In-process UI entry: identical validated handlers; retain typed service errors and values for existing views. */
export async function invokeApplicationCommand<T>(command: string, input: object, context: InvocationContext = {}): Promise<T> {
  const definition = definitions.get(command);
  if (!definition) throw new ApplicationApiError("unknown_command", "Cette commande n’existe pas.");
  if (!definition.available || !definition.handler) throw new ApplicationApiError("unavailable", definition.unavailableReason!);
  validateInput(definition.inputSchema, input);
  if (JSON.stringify(input).length > 65_536) throw new ApplicationApiError("invalid_arguments", "Les arguments dépassent la taille autorisée.");
  return await definition.handler(input as Record<string, unknown>, context) as T;
}

export const applicationApi = {
  catalog() {
    // A caller inspecting the catalogue must not be able to mutate validation rules.
    const commands = [...definitions.values()].map(({ handler: _handler, publicResult: _publicResult, ...metadata }) => metadata);
    return { schemaVersion: "1.0" as const, commands: JSON.parse(JSON.stringify(commands)) as ApplicationCommand[] };
  },
  async execute(command: string, input: unknown): Promise<ApplicationResult> {
    const definition = definitions.get(command);
    if (!definition) throw new ApplicationApiError("unknown_command", "Cette commande n’existe pas.");
    if (!definition.available || !definition.handler) throw new ApplicationApiError("unavailable", definition.unavailableReason!);
    try {
      const value = await invokeApplicationCommand(command, input as object);
      const metadata = { source: definition.source, observedAt: new Date().toISOString() };
      try {
        return { data: serializableResult(definition.publicResult ? definition.publicResult(value) : value), metadata };
      } catch (cause) {
        if (definition.effect !== "mutation") throw cause;
        // The operation succeeded. A projection limit must never relabel the committed action as failed.
        return { data: null, metadata: { ...metadata, resultAvailable: false, resultError: {
          code: "result_unavailable", message: "L’opération est confirmée, mais son résultat dépasse le format public. Consultez la ressource avant de poursuivre.",
        } } };
      }
    } catch (cause) {
      if (cause instanceof ApplicationApiError) throw cause;
      if (cause instanceof server.ConnectionChangedError) {
        throw new ApplicationApiError(cause.outcomeUnknown ? "outcome_unknown" : "connection_changed",
          cause.outcomeUnknown ? "L’envoi a eu lieu avant le changement de connexion. Vérifiez son résultat avant toute nouvelle tentative."
            : "La connexion jumelée a changé. Rechargez le contexte avant de poursuivre.");
      }
      if (cause instanceof server.ApiError) {
        if (definition.effect === "mutation" && cause.status >= 500) {
          throw new ApplicationApiError("outcome_unknown", "Le serveur n’a pas confirmé le résultat de l’envoi. Vérifiez la ressource avant toute nouvelle tentative.", cause.status);
        }
        const code = cause.status === 401 ? "not_paired" : cause.status === 409 ? "conflict" : "server_rejected";
        throw new ApplicationApiError(code, `Le serveur a refusé cette opération (HTTP ${cause.status}).`, cause.status);
      }
      throw new ApplicationApiError(definition.effect === "mutation" ? "outcome_unknown" : "operation_failed",
        "L’opération n’a pas pu être confirmée. Vérifiez son état avant toute nouvelle tentative.");
    }
  },
};
