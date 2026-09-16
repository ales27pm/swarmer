import { act, render, screen, userEvent } from "@testing-library/react-native";
import { beforeEach, describe, expect, it, jest } from "@jest/globals";

import LocalModelScreen from "@/../app/local-model";
import { createLocalGoalPlanSession, sendChat, submitToolProposal, type Agent, type Bootstrap, type GoalDetail, type GoalMemoryContext } from "@/lib/api/client";
import { buildLocalSwarmPlanPrompt } from "@/lib/local-swarm-plan";
import { cancelLocalGeneration, generateLocalProposal, getLocalInferenceCapabilities, getLocalInferenceStatus, isLocalInferenceAvailable, listLocalModels, loadLocalModel, unloadLocalModel } from "@/lib/local-inference";
import { LOCAL_MODEL_PRESETS } from "@/lib/local-model-presets";
import { readLocalModelSettings } from "@/lib/local-model-settings";

const mockPush = jest.fn();
let mockParams: { goalId?: string } = { goalId: "goal_crm" };
jest.mock("expo-router", () => ({ useRouter: () => ({ push: mockPush }), useLocalSearchParams: () => mockParams, useFocusEffect: (effect: () => void) => jest.requireActual<typeof import("react")>("react").useEffect(effect, [effect]) }));
jest.mock("expo-document-picker", () => ({ getDocumentAsync: jest.fn() }));
jest.mock("@/lib/api/client", () => ({ createLocalGoalPlanSession: jest.fn(), sendChat: jest.fn(), submitToolProposal: jest.fn() }));
jest.mock("@/lib/local-model-settings", () => ({
  ...jest.requireActual<typeof import("@/lib/local-model-settings")>("@/lib/local-model-settings"),
  readLocalModelSettings: jest.fn(), saveLocalModelSettings: jest.fn(),
}));
jest.mock("@/lib/local-inference", () => ({
  ...jest.requireActual<typeof import("@/lib/local-inference")>("@/lib/local-inference"),
  getLocalInferenceCapabilities: jest.fn(), getLocalInferenceStatus: jest.fn(), isLocalInferenceAvailable: jest.fn(),
  listLocalModels: jest.fn(), loadLocalModel: jest.fn(), unloadLocalModel: jest.fn(),
  generateLocalProposal: jest.fn(), cancelLocalGeneration: jest.fn(),
}));

type Session = Awaited<ReturnType<typeof createLocalGoalPlanSession>>;
const getGoal = jest.fn<Session["getGoal"]>();
const memoryContext = jest.fn<Session["memoryContext"]>();
const bootstrapSync = jest.fn<Session["bootstrapSync"]>();
const assertCurrent = jest.fn<Session["assertCurrent"]>();
const startGoal = jest.fn<Session["startGoal"]>();
const detail: GoalDetail = {
  goal: {
    id: "goal_crm", root_task_id: "tsk_crm", objective: "Créer un CRM Python avec courriels en brouillon uniquement.",
    status: "planning", autonomy_profile: "assisted", planner_source: "ubuntu_local",
    max_steps: 20, max_parallelism: 3, max_replans: 3, max_runtime_seconds: 1800,
    max_model_calls: 30, step_count: 0, replan_count: 0, model_call_count: 0,
    completion_criteria: ["Tests réussis"], current_phase: "planning", started_at: null,
    created_at: "2026-09-13T22:30:00Z", updated_at: "2026-09-13T22:30:00Z",
  }, nodes: [], result: null,
};
const agent: Agent = {
  id: "agent_project", name: "Project worker", version: "1", endpoint: "https://worker.example",
  model_id: "project-model", status: "online", skills: ["code.build_project"],
  last_heartbeat_at: "2026-09-13T22:30:00Z", last_seen_at: "2026-09-13T22:30:00Z",
  max_concurrency: 1, capacity: {}, runtime: "python", supported_protocol_version: "mongars-worker-v0.9",
  active_jobs: 0, historical_score: 0, created_at: "2026-09-13T22:30:00Z", updated_at: "2026-09-13T22:30:00Z",
  agent_card: { agent_id: "agent_project", name: "Project worker", version: "1", skills: ["code.build_project"],
    model_id: "project-model", runtime: "python", max_concurrency: 1, supported_protocol_version: "mongars-worker-v0.9", capabilities: {} },
};
const bootstrap: Bootstrap = {
  server_time: "2026-09-13T22:30:00Z", agents: [agent], tasks: [], approvals: [], tool_calls: [],
  conversations: [], pinned_memory: [], cursor: "1", counts: { tasks: 0, messages: 0, agents: 1, approvals_pending: 0, memory_items: 0, audit_events: 0 },
};
const memory: GoalMemoryContext = {
  schema_version: "1.0", goal_id: "goal_crm", project_id: "project_crm", conversation_revision: 1,
  base_revision_id: "revision_1", provider_fingerprint: "a".repeat(64), context_fingerprint: "b".repeat(64),
  mode: "semantic", reason: "semantic_match", items: [{ id: "mem_1", source_id: "revision_1", summary: "CRM en Python, courriels en brouillon.", score: 0.8 }],
  embedding: { configured: true, model: "embedding-model", model_revision: "c".repeat(40), storage: "ubuntu_sqlite" },
  local_planning_eligible: true, planning_embedding_call_count: 0, recent_conversation: [],
};
const plan = {
  schema_version: "1.0", objective: detail.goal.objective, rationale_summary: "Construire puis vérifier le CRM.",
  nodes: [{ temporary_id: "crm", node_type: "worker", title: "Construire le CRM Python", objective: detail.goal.objective,
    required_skill: "code.build_project", dependencies: [], expected_output: "Fichiers Python et résultats des tests", priority: 0 }],
  completion_criteria: detail.goal.completion_criteria, max_parallelism: 1,
};
const promptGoal = {
  objective: detail.goal.objective, completion_criteria: detail.goal.completion_criteria,
  max_steps: detail.goal.max_steps, step_count: detail.goal.step_count,
  max_parallelism: detail.goal.max_parallelism, max_model_calls: detail.goal.max_model_calls,
  model_call_count: detail.goal.model_call_count,
};

async function loadedScreen() {
  const user = userEvent.setup();
  await render(<LocalModelScreen />);
  await screen.findByText(detail.goal.objective);
  await screen.findByText(/Choisis un modèle local/);
  await user.press(screen.getByRole("button", { name: "Charger le modèle" }));
  await screen.findByText(/Modèle chargé localement/);
  return user;
}

async function generatedScreen() {
  const user = await loadedScreen();
  await user.press(screen.getByRole("button", { name: "Générer le plan initial sur l’iPhone" }));
  await screen.findByText("Plan initial à relire");
  return user;
}

describe("initial local goal plan", () => {
  beforeEach(() => {
    jest.resetAllMocks();
    mockParams = { goalId: "goal_crm" };
    getGoal.mockResolvedValue(detail);
    memoryContext.mockResolvedValue(memory);
    bootstrapSync.mockResolvedValue(bootstrap);
    assertCurrent.mockResolvedValue();
    startGoal.mockResolvedValue({ ...detail, goal: { ...detail.goal, status: "running", planner_source: "iphone_local" } });
    jest.mocked(createLocalGoalPlanSession).mockResolvedValue({ getGoal, memoryContext, bootstrapSync, assertCurrent, startGoal });
    jest.mocked(readLocalModelSettings).mockResolvedValue(null);
    jest.mocked(isLocalInferenceAvailable).mockReturnValue(true);
    jest.mocked(getLocalInferenceCapabilities).mockResolvedValue({ coreml: true, mlx: true, llamaCpp: true, platform: "ios" });
    jest.mocked(listLocalModels).mockResolvedValue([]);
    jest.mocked(getLocalInferenceStatus).mockImplementation(async () => {
      const latest = jest.mocked(loadLocalModel).mock.results.at(-1);
      return latest?.type === "return" ? await latest.value : { state: "idle", runtime: null, modelId: null, revision: null };
    });
    jest.mocked(loadLocalModel).mockResolvedValue({ state: "ready", runtime: "mlx", modelId: LOCAL_MODEL_PRESETS.mlx.repoId, revision: LOCAL_MODEL_PRESETS.mlx.revision });
    jest.mocked(unloadLocalModel).mockResolvedValue();
    jest.mocked(cancelLocalGeneration).mockResolvedValue();
    jest.mocked(generateLocalProposal).mockResolvedValue({ text: JSON.stringify(plan), finishReason: "stop", tokenCount: 230 });
  });

  it("shows the immutable authenticated objective without loading, generating or starting automatically", async () => {
    await render(<LocalModelScreen />);
    expect(await screen.findByText(detail.goal.objective)).toBeOnTheScreen();
    expect(screen.queryByLabelText("Intention pour le modèle local")).not.toBeOnTheScreen();
    expect(screen.getByLabelText("Limite de jetons de sortie")).toHaveDisplayValue("512");
    expect(screen.getByRole("button", { name: "Générer le plan initial sur l’iPhone" })).toBeDisabled();
    expect(loadLocalModel).not.toHaveBeenCalled();
    expect(generateLocalProposal).not.toHaveBeenCalled();
    expect(startGoal).not.toHaveBeenCalled();
    expect(screen.getByText("Mémoire du projet · Ubuntu · SQLite")).toBeOnTheScreen();
    expect(screen.getByText(/Recherche sémantique · 1 extrait/)).toBeOnTheScreen();
    expect(screen.getByText(/Embeddings configurés sur Ubuntu : embedding-model/)).toBeOnTheScreen();
  });

  it("shows confirmed empty memory separately from retrieval failure", async () => {
    memoryContext.mockResolvedValue({ ...memory, project_id: null, mode: "lexical", reason: "no_linked_project", items: [],
      embedding: { configured: false, model: null, model_revision: null, storage: "ubuntu_sqlite" } });
    await render(<LocalModelScreen />);
    expect(await screen.findByText("Aucun projet lié : aucun historique disponible pour ce but.")).toBeOnTheScreen();
    expect(screen.getByText(/Recherche lexicale · 0 extrait/)).toBeOnTheScreen();
    expect(screen.getByText("Embeddings configurés sur Ubuntu : non configurés")).toBeOnTheScreen();
  });

  it("does not infer without a successful memory receipt", async () => {
    memoryContext.mockRejectedValue(new Error("Mémoire indisponible"));
    await render(<LocalModelScreen />);
    expect(await screen.findByText("Mémoire indisponible")).toBeOnTheScreen();
    expect(screen.queryByTestId("goal-memory-status")).not.toBeOnTheScreen();
    expect(generateLocalProposal).not.toHaveBeenCalled();
    expect(startGoal).not.toHaveBeenCalled();
  });

  it("shows and sends the latest CRM answer even when project memory is empty", async () => {
    const answer = "Fiches clients, soumissions/projet, courriels, calendrier.";
    memoryContext.mockResolvedValue({ ...memory, items: [], recent_conversation: [
      { role: "assistant", content: "Quelles fonctionnalités ?" }, { role: "user", content: answer },
    ] });
    const user = await loadedScreen();
    expect(screen.getByText("Dernière réponse utilisateur")).toBeOnTheScreen();
    expect(screen.getByText(answer)).toBeOnTheScreen();
    await user.press(screen.getByRole("button", { name: "Générer le plan initial sur l’iPhone" }));
    await screen.findByText("Plan initial à relire");
    const sent = JSON.parse(jest.mocked(generateLocalProposal).mock.calls[0][0].prompt.split("\n").at(-1)!);
    expect(sent.recent_conversation.messages.at(-1)).toEqual({ role: "user", content: answer });
    expect(sent.memory_context.excerpts).toEqual([]);
  });

  it("accepts only the server's traced embedding credits and re-reads the resulting goal", async () => {
    const credited = { ...detail, goal: { ...detail.goal, model_call_count: 1, updated_at: "2026-09-13T22:30:01Z" } };
    getGoal.mockResolvedValueOnce(detail).mockResolvedValue(credited);
    memoryContext.mockResolvedValue({ ...memory, planning_embedding_call_count: 1 });
    const user = await loadedScreen();
    await user.press(screen.getByRole("button", { name: "Générer le plan initial sur l’iPhone" }));
    await screen.findByText("Plan initial à relire");
    expect(getGoal).toHaveBeenCalledTimes(4);
    const sent = JSON.parse(jest.mocked(generateLocalProposal).mock.calls[0][0].prompt.split("\n").at(-1)!);
    expect(sent.goal.model_call_count).toBe(1);
    expect(sent.limits.remaining_model_calls).toBe(29);
  });

  it.each([false, true])("rejects untraced model calls even with eligibility=%s", async (eligible) => {
    getGoal.mockResolvedValue({ ...detail, goal: { ...detail.goal, model_call_count: 2 } });
    memoryContext.mockResolvedValue({ ...memory, local_planning_eligible: eligible, planning_embedding_call_count: 1 });
    await render(<LocalModelScreen />);
    expect(await screen.findByText(/n’est plus admissible/)).toBeOnTheScreen();
    expect(generateLocalProposal).not.toHaveBeenCalled();
  });

  it("rejects a goal changed during memory retrieval", async () => {
    getGoal.mockResolvedValueOnce(detail).mockResolvedValue({ ...detail, goal: { ...detail.goal, completion_criteria: ["Changé pendant la recherche"] } });
    await render(<LocalModelScreen />);
    expect(await screen.findByText(/Le but a changé pendant la lecture mémoire/)).toBeOnTheScreen();
    expect(generateLocalProposal).not.toHaveBeenCalled();
  });

  it("generates from fresh context on-device, reviews actual nodes and only starts on the explicit second action", async () => {
    const user = await generatedScreen();
    expect(getGoal).toHaveBeenCalledTimes(4);
    expect(bootstrapSync).toHaveBeenCalledTimes(2);
    expect(memoryContext).toHaveBeenCalledTimes(2);
    expect(memoryContext).toHaveBeenCalledWith("goal_crm", detail.goal.updated_at);
    expect(generateLocalProposal).toHaveBeenCalledWith({ prompt: buildLocalSwarmPlanPrompt({ goal: promptGoal, agents: [agent], memory }), maxTokens: 512, temperature: 0.1 });
    expect(screen.getByText("1. Construire le CRM Python")).toBeOnTheScreen();
    expect(startGoal).not.toHaveBeenCalled();
    expect(sendChat).not.toHaveBeenCalled();
    await user.press(screen.getByRole("button", { name: "Démarrer avec ce plan local" }));
    expect(startGoal).toHaveBeenCalledTimes(1);
    expect(startGoal).toHaveBeenCalledWith("goal_crm", { plan_proposal: plan, planner_source: "iphone_local", memory_context_fingerprint: memory.context_fingerprint });
    expect(getGoal).toHaveBeenCalledTimes(6);
    expect(memoryContext).toHaveBeenCalledTimes(3);
    expect(mockPush).toHaveBeenCalledWith({ pathname: "/goal/[id]", params: { id: "goal_crm" } });
    expect(submitToolProposal).not.toHaveBeenCalled();
  });

  it("rejects a started goal before invoking local inference", async () => {
    const user = await loadedScreen();
    getGoal.mockResolvedValue({ ...detail, goal: { ...detail.goal, started_at: "2026-09-13T22:31:00Z" } });
    await user.press(screen.getByRole("button", { name: "Générer le plan initial sur l’iPhone" }));
    expect(await screen.findByText(/Ce but a déjà démarré/)).toBeOnTheScreen();
    expect(generateLocalProposal).not.toHaveBeenCalled();
    expect(startGoal).not.toHaveBeenCalled();
  });

  it.each(["length", "cancelled"] as const)("never accepts or submits a %s generation", async (finishReason) => {
    jest.mocked(generateLocalProposal).mockResolvedValue({ text: JSON.stringify(plan), finishReason, tokenCount: 512 });
    const user = await loadedScreen();
    await user.press(screen.getByRole("button", { name: "Générer le plan initial sur l’iPhone" }));
    await screen.findByText(finishReason === "length" ? /La limite de génération/ : /Génération annulée;/);
    expect(screen.queryByRole("button", { name: "Démarrer avec ce plan local" })).not.toBeOnTheScreen();
    expect(startGoal).not.toHaveBeenCalled();
  });

  it("rejects changed requirements after review before sending anything", async () => {
    const user = await generatedScreen();
    getGoal.mockResolvedValue({ ...detail, goal: { ...detail.goal, completion_criteria: ["Autres critères"] } });
    await user.press(screen.getByRole("button", { name: "Démarrer avec ce plan local" }));
    expect(await screen.findByText(/ont changé depuis la génération/)).toBeOnTheScreen();
    expect(startGoal).not.toHaveBeenCalled();
    expect(screen.queryByText("Plan initial à relire")).not.toBeOnTheScreen();
  });

  it("rejects a worker capability change before start", async () => {
    const user = await generatedScreen();
    bootstrapSync.mockResolvedValue({ ...bootstrap, agents: [{ ...agent, skills: ["workspace.read_text"] }] });
    await user.press(screen.getByRole("button", { name: "Démarrer avec ce plan local" }));
    expect(await screen.findByText(/ont changé depuis la génération/)).toBeOnTheScreen();
    expect(startGoal).not.toHaveBeenCalled();
  });

  it("requires a new review before inference when memory changes since the screen opened", async () => {
    const user = await loadedScreen();
    memoryContext.mockResolvedValue({ ...memory, context_fingerprint: "d".repeat(64) });
    await user.press(screen.getByRole("button", { name: "Générer le plan initial sur l’iPhone" }));
    expect(await screen.findByText(/Relis le contexte actualisé/)).toBeOnTheScreen();
    expect(generateLocalProposal).not.toHaveBeenCalled();
  });

  it("invalidates a reviewed plan if the memory receipt changes before start", async () => {
    const user = await generatedScreen();
    memoryContext.mockResolvedValue({ ...memory, context_fingerprint: "d".repeat(64) });
    await user.press(screen.getByRole("button", { name: "Démarrer avec ce plan local" }));
    expect(await screen.findByText(/ont changé depuis la génération/)).toBeOnTheScreen();
    expect(startGoal).not.toHaveBeenCalled();
    expect(screen.queryByText("Plan initial à relire")).not.toBeOnTheScreen();
  });

  it("rejects an invalidated pairing before start", async () => {
    const user = await generatedScreen();
    assertCurrent.mockRejectedValue(new Error("La connexion jumelée a changé."));
    await user.press(screen.getByRole("button", { name: "Démarrer avec ce plan local" }));
    expect(await screen.findByText("La connexion jumelée a changé.")).toBeOnTheScreen();
    expect(startGoal).not.toHaveBeenCalled();
  });

  it("locks an uncertain start without retrying or falling back to the server planner", async () => {
    const user = await generatedScreen();
    startGoal.mockRejectedValue(new Error("Réponse réseau inconnue"));
    await user.press(screen.getByRole("button", { name: "Démarrer avec ce plan local" }));
    await screen.findByText(/Aucun renvoi automatique/);
    expect(startGoal).toHaveBeenCalledTimes(1);
    expect(screen.getByRole("button", { name: "Générer le plan initial sur l’iPhone" })).toBeDisabled();
    expect(screen.queryByRole("button", { name: "Démarrer avec ce plan local" })).not.toBeOnTheScreen();
    expect(sendChat).not.toHaveBeenCalled();
  });

  it("ignores a late local plan after explicit cancellation", async () => {
    let resolve!: (value: Awaited<ReturnType<typeof generateLocalProposal>>) => void;
    jest.mocked(generateLocalProposal).mockImplementation(() => new Promise((done) => { resolve = done; }));
    const user = await loadedScreen();
    await user.press(screen.getByRole("button", { name: "Générer le plan initial sur l’iPhone" }));
    await user.press(screen.getByRole("button", { name: "Annuler la génération" }));
    await act(async () => resolve({ text: JSON.stringify(plan), finishReason: "stop", tokenCount: 230 }));
    expect(screen.queryByText("Plan initial à relire")).not.toBeOnTheScreen();
    expect(startGoal).not.toHaveBeenCalled();
  });

  it("does not claim local provenance when the start response reports another planner", async () => {
    const user = await generatedScreen();
    startGoal.mockResolvedValue({ ...detail, goal: { ...detail.goal, status: "running", planner_source: "ubuntu_local" } });
    await user.press(screen.getByRole("button", { name: "Démarrer avec ce plan local" }));
    expect(await screen.findByText("Le serveur n’a pas confirmé ce plan initial iPhone.")).toBeOnTheScreen();
    expect(startGoal).toHaveBeenCalledTimes(1);
    expect(mockPush).not.toHaveBeenCalled();
    expect(screen.getByRole("button", { name: "Générer le plan initial sur l’iPhone" })).toBeDisabled();
  });

  it("does not start if the screen unmounts during its final context check", async () => {
    const user = await generatedScreen();
    let resolve!: (value: GoalDetail) => void;
    getGoal.mockImplementationOnce(() => new Promise((done) => { resolve = done; }));
    await user.press(screen.getByRole("button", { name: "Démarrer avec ce plan local" }));
    await screen.unmount();
    await act(async () => resolve(detail));
    expect(startGoal).not.toHaveBeenCalled();
  });
});
