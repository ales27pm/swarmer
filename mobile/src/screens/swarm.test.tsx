import { act, render, screen, userEvent, waitFor } from "@testing-library/react-native";
import { beforeEach, describe, expect, it, jest } from "@jest/globals";

import SwarmScreen from "@/../app/(main)/swarm";
import {
  ApiError,
  ConnectionChangedError,
  bootstrapSync,
  createGoal,
  getServerUrl,
  type Bootstrap,
  type GoalDetail,
} from "@/lib/api/client";
import { notifyConnectionChanged } from "@/lib/connection-events";
import { localSwarmSnapshot } from "@/lib/state/replica";

const mockPush = jest.fn();
let mockSearchParams: { create?: string } = {};
const mockSetParams = jest.fn((params: { create?: string }) => { mockSearchParams = params; });
const mockRouter = { push: mockPush, setParams: mockSetParams };
let refreshFromLiveEvent: (() => void | Promise<unknown>) | undefined;

jest.mock("expo-router", () => ({ useRouter: () => mockRouter, useLocalSearchParams: () => mockSearchParams }));
jest.mock("@/lib/api/client", () => ({
  bootstrapSync: jest.fn(),
  createGoal: jest.fn(),
  getServerUrl: jest.fn(),
  ApiError: class extends Error {
    readonly status: number;
    constructor(status: number, message: string) { super(message); this.status = status; }
  },
  ConnectionChangedError: class extends Error {
    constructor() { super("La connexion jumelée a changé pendant la requête."); }
  },
}));
jest.mock("@/lib/state/replica", () => ({ localSwarmSnapshot: jest.fn() }));
jest.mock("@/lib/sync/live-sync-context", () => ({
  useLiveRefresh: (refresh: () => void | Promise<unknown>) => {
    refreshFromLiveEvent = refresh;
  },
}));

const mockBootstrap = jest.mocked(bootstrapSync);
const mockCreateGoal = jest.mocked(createGoal);
const mockGetServerUrl = jest.mocked(getServerUrl);
const mockLocalSnapshot = jest.mocked(localSwarmSnapshot);

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((settle) => {
    resolve = settle;
  });
  return { promise, resolve };
}

const goalDetail: GoalDetail = {
  goal: {
    id: "goal_1",
    root_task_id: "tsk_root",
    objective: "Qualifier le runtime distribué",
    status: "running",
    autonomy_profile: "assisted",
    planner_source: "ubuntu_local",
    max_steps: 8,
    max_parallelism: 2,
    max_replans: 1,
    max_runtime_seconds: 600,
    max_model_calls: 10,
    step_count: 2,
    replan_count: 0,
    model_call_count: 2,
    completion_criteria: ["Tests réussis"],
    current_phase: "execution",
    evaluator_summary: "Le plan progresse.",
    created_at: "2030-01-01T00:00:00Z",
    updated_at: "2030-01-01T00:01:00Z",
  },
  nodes: [{
    id: "node_1",
    goal_run_id: "goal_1",
    node_type: "worker",
    title: "Auditer",
    objective: "Auditer le runtime",
    status: "running",
    priority: 0,
    depends_on: [],
    assigned_agent_id: "agent_1",
    created_at: "2030-01-01T00:00:00Z",
    updated_at: "2030-01-01T00:01:00Z",
  }],
  result: null,
};

const bootstrap: Bootstrap = {
  server_time: "2030-01-01T00:01:00Z",
  tasks: [],
  approvals: [],
  tool_calls: [],
  conversations: [],
  agents: [{
    id: "agent_1",
    name: "Worker Alpha",
    version: "1.0.0",
    endpoint: "worker://alpha",
    model_id: null,
    status: "online",
    skills: ["workspace.list_dir"],
    last_heartbeat_at: "2030-01-01T00:01:00Z",
    last_seen_at: "2030-01-01T00:01:00Z",
    max_concurrency: 1,
    capacity: {},
    runtime: "python",
    supported_protocol_version: "mongars-worker-v0.9",
    active_jobs: 1,
    historical_score: 1,
    agent_card: {
      agent_id: "agent_1",
      name: "Worker Alpha",
      version: "1.0.0",
      skills: ["workspace.list_dir"],
      model_id: null,
      runtime: "python",
      max_concurrency: 1,
      supported_protocol_version: "mongars-worker-v0.9",
      capabilities: {},
    },
    created_at: "2030-01-01T00:00:00Z",
    updated_at: "2030-01-01T00:01:00Z",
  }],
  pinned_memory: [],
  goals: [goalDetail.goal],
  plan_nodes: goalDetail.nodes,
  goal_results: [],
  counts: {
    tasks: 0,
    messages: 0,
    agents: 1,
    approvals_pending: 0,
    memory_items: 0,
    audit_events: 0,
  },
  cursor: "audit:1",
};

describe("SwarmScreen", () => {
  beforeEach(() => {
    jest.clearAllMocks();
    mockSearchParams = {};
    refreshFromLiveEvent = undefined;
    mockBootstrap.mockResolvedValue(bootstrap);
    mockCreateGoal.mockResolvedValue(goalDetail);
    mockGetServerUrl.mockResolvedValue("https://control.example");
    mockLocalSnapshot.mockResolvedValue(null);
  });

  it("opens projects directly without duplicating team navigation", async () => {
    mockBootstrap.mockResolvedValueOnce({ ...bootstrap, goals: Array.from({ length: 30 }, (_, index) => ({
      ...goalDetail.goal, id: `goal_${index}`, objective: `Projet ${index}`,
    })) });
    await render(<SwarmScreen />);
    expect(await screen.findByText("Projet 0")).toBeOnTheScreen();
    expect(screen.getByRole("header", { name: "Tes projets" })).toBeOnTheScreen();
    expect(screen.queryByTestId("goal-objective-input")).not.toBeOnTheScreen();
    expect(screen.queryByRole("button", { name: "Tous les agents" })).not.toBeOnTheScreen();
    expect(mockCreateGoal).not.toHaveBeenCalled();
  });

  it("opens creation from the chat shortcut without creating automatically", async () => {
    mockSearchParams = { create: "1" };
    await render(<SwarmScreen />);
    expect(await screen.findByTestId("goal-objective-input")).toBeOnTheScreen();
    expect(mockSetParams).toHaveBeenCalledWith({ create: undefined });
    expect(mockCreateGoal).not.toHaveBeenCalled();
  });

  it("summarizes long project objectives while preserving the full accessible destination", async () => {
    const objective = "Organiser les rendez-vous, préparer les documents et suivre les tâches de la semaine. ".repeat(8);
    mockBootstrap.mockResolvedValueOnce({ ...bootstrap, goals: [{ ...goalDetail.goal, objective }] });
    const user = userEvent.setup();
    await render(<SwarmScreen />);
    expect((await screen.findByText(objective)).props.numberOfLines).toBe(3);
    const destination = screen.getByRole("button", { name: `Ouvrir le projet ${objective}` });
    await user.press(destination);
    expect(mockPush).toHaveBeenCalledWith({ pathname: "/goal/[id]", params: { id: "goal_1" } });
    expect(mockCreateGoal).not.toHaveBeenCalled();
  });

  it("does not suggest progress when a project has no recorded steps", async () => {
    mockBootstrap.mockResolvedValueOnce({ ...bootstrap, plan_nodes: [] });
    await render(<SwarmScreen />);
    expect(await screen.findByText("Aucune étape enregistrée")).toBeOnTheScreen();
    expect(screen.queryByRole("progressbar")).not.toBeOnTheScreen();
    expect(screen.queryByText("0/0 étapes terminées")).not.toBeOnTheScreen();
  });

  it("preserves the objective when the creation form is collapsed", async () => {
    const user = userEvent.setup();
    await render(<SwarmScreen />);
    await screen.findByText(goalDetail.goal.objective);
    await user.press(screen.getByTestId("new-project-disclosure"));
    await user.type(screen.getByTestId("goal-objective-input"), "Mon agenda");
    await user.press(screen.getByTestId("new-project-disclosure"));
    expect(screen.queryByTestId("goal-objective-input")).not.toBeOnTheScreen();
    await user.press(screen.getByTestId("new-project-disclosure"));
    expect(screen.getByTestId("goal-objective-input")).toHaveDisplayValue("Mon agenda");
    expect(mockCreateGoal).not.toHaveBeenCalled();
  });

  it("shows authoritative goals and creates without automatically starting", async () => {
    const user = userEvent.setup();
    await render(<SwarmScreen />);

    expect(await screen.findByText("Qualifier le runtime distribué")).toBeOnTheScreen();

    expect(screen.queryByTestId("goal-objective-input")).not.toBeOnTheScreen();
    await user.press(screen.getByRole("button", { name: "Nouveau projet" }));
    expect(mockCreateGoal).not.toHaveBeenCalled();
    await user.type(screen.getByLabelText("Objectif du projet"), "Préparer une qualification");
    await user.press(screen.getByRole("button", { name: "Autonome" }));
    await user.press(screen.getByRole("button", { name: "Créer le projet" }));

    await waitFor(() => expect(mockCreateGoal).toHaveBeenCalledWith({
      autonomy_profile: "autonomous",
      objective: "Préparer une qualification",
    }, expect.any(Function)));
    expect(mockPush).toHaveBeenCalledWith({ pathname: "/goal/[id]", params: { id: "goal_1" } });
    expect(mockBootstrap).toHaveBeenCalledTimes(1);
  });

  it("uses an origin-bound cache only for navigation and locks all mutations", async () => {
    const user = userEvent.setup();
    mockBootstrap.mockRejectedValue(new Error("Serveur indisponible"));
    mockLocalSnapshot.mockResolvedValue({
      origin: "https://control.example",
      goals: [goalDetail.goal],
      plan_nodes: goalDetail.nodes,
      goal_results: [],
      agents: bootstrap.agents,
      counts: bootstrap.counts,
      cursor: bootstrap.cursor,
    });
    await render(<SwarmScreen />);

    expect(await screen.findByText(/Les données peuvent être périmées/)).toBeOnTheScreen();
    expect(screen.getByText(goalDetail.goal.objective)).toBeOnTheScreen();
    expect(screen.queryByText("1 agent mobilisé")).not.toBeOnTheScreen();
    await user.press(screen.getByRole("button", { name: "Nouveau projet" }));
    expect(screen.getByRole("button", { name: "Créer le projet" })).toBeDisabled();
    await user.press(screen.getByRole("button", { name: /Ouvrir le projet Qualifier/ }));
    expect(mockPush).toHaveBeenCalledWith({ pathname: "/goal/[id]", params: { id: "goal_1" } });
    expect(mockCreateGoal).not.toHaveBeenCalled();
  });

  it("keeps project data unverified while loading and after a failure without cache", async () => {
    const user = userEvent.setup();
    const pending = deferred<Bootstrap>();
    mockBootstrap.mockImplementationOnce(async () => pending.promise);
    await render(<SwarmScreen />);
    expect(screen.getByTestId("swarm-open-settings")).toBeOnTheScreen();
    expect(screen.getByText("Chargement des projets…")).toBeOnTheScreen();
    expect(screen.queryByText("Aucun projet")).not.toBeOnTheScreen();
    expect(screen.queryByText("0 agents mobilisés")).not.toBeOnTheScreen();
    expect(screen.queryByText("Aucun projet")).not.toBeOnTheScreen();
    await user.press(screen.getByRole("button", { name: "Vérifier la connexion dans Réglages" }));
    expect(mockPush).toHaveBeenLastCalledWith("/settings");
    expect(mockCreateGoal).not.toHaveBeenCalled();

    mockBootstrap.mockRejectedValueOnce(new Error("Failed to fetch"));
    await act(async () => { await refreshFromLiveEvent?.(); });
    expect(screen.getByTestId("swarm-open-settings")).toBeOnTheScreen();
    expect(screen.getByText("Connecte le serveur pour vérifier les projets.")).toBeOnTheScreen();
    expect(screen.queryByText("Aucun agent mobilisé sur ces projets.")).not.toBeOnTheScreen();
    expect(screen.queryByText("0 agents mobilisés")).not.toBeOnTheScreen();
    await act(async () => pending.resolve(bootstrap));
    expect(screen.getByTestId("swarm-open-settings")).toBeOnTheScreen();
  });

  it("reports an empty project list only from a successful server response", async () => {
    mockBootstrap.mockResolvedValueOnce({ ...bootstrap, agents: [], plan_nodes: [], goals: [] });
    await render(<SwarmScreen />);
    expect(await screen.findByText("Aucun projet")).toBeOnTheScreen();
    expect(screen.queryByText("Équipe non vérifiée")).not.toBeOnTheScreen();
    expect(screen.getByText("Aucun projet")).toBeOnTheScreen();
    expect(screen.queryByTestId("swarm-open-settings")).not.toBeOnTheScreen();
  });

  it("distinguishes an empty cached project list from the current server state", async () => {
    mockBootstrap.mockRejectedValueOnce(new Error("Serveur indisponible"));
    mockLocalSnapshot.mockResolvedValueOnce({
      origin: "https://control.example", goals: [], plan_nodes: [], goal_results: [], agents: [],
      counts: bootstrap.counts, cursor: bootstrap.cursor,
    });
    const user = userEvent.setup();
    await render(<SwarmScreen />);
    expect(await screen.findByText("Aucun projet dans la copie locale. Reconnecte le serveur pour vérifier.")).toBeOnTheScreen();
    expect(screen.queryByText("Aucun projet")).not.toBeOnTheScreen();
    await user.press(screen.getByTestId("swarm-open-settings"));
    expect(mockPush).toHaveBeenLastCalledWith("/settings");
    await user.press(screen.getByTestId("new-project-disclosure"));
    expect(screen.getByRole("button", { name: "Créer le projet" })).toBeDisabled();
    expect(mockCreateGoal).not.toHaveBeenCalled();
  });

  it("does not let a slower stale refresh overwrite a newer authoritative view", async () => {
    const old = deferred<Bootstrap>();
    const newer = {
      ...bootstrap,
      goals: [{ ...goalDetail.goal, id: "goal_new", objective: "État plus récent" }],
      plan_nodes: [],
    };
    mockBootstrap
      .mockImplementationOnce(async () => old.promise)
      .mockResolvedValueOnce(newer);
    await render(<SwarmScreen />);

    await act(async () => {
      await refreshFromLiveEvent?.();
    });
    expect(await screen.findByText("État plus récent")).toBeOnTheScreen();

    await act(async () => old.resolve(bootstrap));
    expect(screen.queryByText("Qualifier le runtime distribué")).not.toBeOnTheScreen();
    expect(screen.getByText("État plus récent")).toBeOnTheScreen();
  });

  it("discards cached data if the active server origin changes during lookup", async () => {
    const user = userEvent.setup();
    const cache = deferred<Awaited<ReturnType<typeof localSwarmSnapshot>>>();
    mockBootstrap.mockRejectedValue(new Error("Serveur indisponible"));
    mockLocalSnapshot.mockImplementationOnce(async () => cache.promise);
    mockGetServerUrl.mockResolvedValue("https://old.example");
    await render(<SwarmScreen />);
    await waitFor(() => expect(mockLocalSnapshot).toHaveBeenCalledWith("https://old.example"));
    mockGetServerUrl.mockResolvedValue("https://new.example");

    await act(async () => cache.resolve({
      origin: "https://old.example",
      goals: [goalDetail.goal],
      plan_nodes: goalDetail.nodes,
      goal_results: [],
      agents: [],
      counts: null,
      cursor: null,
    }));

    expect(await screen.findByText(/Le jumelage a changé/)).toBeOnTheScreen();
    expect(screen.queryByText("Qualifier le runtime distribué")).not.toBeOnTheScreen();
    await user.press(screen.getByRole("button", { name: "Nouveau projet" }));
    expect(screen.getByRole("button", { name: "Créer le projet" })).toBeDisabled();
  });

  it("immediately clears authoritative rows and locks creation after pairing changes on the same origin", async () => {
    const user = userEvent.setup();
    await render(<SwarmScreen />);
    await screen.findByText(goalDetail.goal.objective);
    await user.press(screen.getByTestId("new-project-disclosure"));
    await user.type(screen.getByLabelText("Objectif du projet"), "Préparer mon agenda");
    expect(screen.getByTestId("create-goal-button")).not.toBeDisabled();
    await act(async () => notifyConnectionChanged());
    expect(screen.queryByText("Worker Alpha")).not.toBeOnTheScreen();
    expect(screen.queryByText(goalDetail.goal.objective)).not.toBeOnTheScreen();
    expect(screen.getByTestId("swarm-open-settings")).toBeOnTheScreen();
    expect(screen.getByTestId("create-goal-button")).toBeDisabled();
    expect(screen.getByTestId("goal-objective-input")).toHaveDisplayValue("Préparer mon agenda");
    expect(mockCreateGoal).not.toHaveBeenCalled();
    mockBootstrap.mockResolvedValueOnce({ ...bootstrap, goals: [], agents: [], plan_nodes: [] });
    await user.press(screen.getByTestId("swarm-retry"));
    expect(await screen.findByText("Aucun projet")).toBeOnTheScreen();
    expect(screen.getByTestId("create-goal-button")).not.toBeDisabled();
    expect(mockCreateGoal).not.toHaveBeenCalled();
  });

  it("discards an in-flight authoritative response after a pairing change", async () => {
    const pending = deferred<Bootstrap>();
    mockBootstrap.mockReturnValueOnce(pending.promise);
    await render(<SwarmScreen />);
    await waitFor(() => expect(mockBootstrap).toHaveBeenCalledTimes(1));
    await act(async () => notifyConnectionChanged());
    await act(async () => pending.resolve(bootstrap));
    expect(screen.queryByText("Worker Alpha")).not.toBeOnTheScreen();
    expect(screen.getByTestId("swarm-open-settings")).toBeOnTheScreen();
    expect(mockLocalSnapshot).not.toHaveBeenCalled();
  });

  it("discards an in-flight cache read after a same-origin pairing change", async () => {
    const pending = deferred<Awaited<ReturnType<typeof localSwarmSnapshot>>>();
    mockBootstrap.mockRejectedValueOnce(new Error("Offline"));
    mockLocalSnapshot.mockReturnValueOnce(pending.promise);
    await render(<SwarmScreen />);
    await waitFor(() => expect(mockLocalSnapshot).toHaveBeenCalledTimes(1));
    await act(async () => notifyConnectionChanged());
    await act(async () => pending.resolve({ origin: "https://control.example", goals: bootstrap.goals!,
      agents: bootstrap.agents, plan_nodes: bootstrap.plan_nodes!, goal_results: [], counts: null, cursor: null }));
    expect(screen.queryByText(goalDetail.goal.objective)).not.toBeOnTheScreen();
    expect(screen.getByTestId("swarm-open-settings")).toBeOnTheScreen();
  });

  it.each([401, 403, "pairing"])("clears an earlier authoritative state without reading cache after %s rejection", async (failure) => {
    const user = userEvent.setup();
    await render(<SwarmScreen />);
    await screen.findByText(goalDetail.goal.objective);
    mockBootstrap.mockRejectedValueOnce(failure === "pairing" ? new ConnectionChangedError() : new ApiError(Number(failure), "Accès refusé"));
    await act(async () => { await refreshFromLiveEvent?.(); });
    expect(screen.queryByText("Worker Alpha")).not.toBeOnTheScreen();
    expect(screen.getByTestId("swarm-open-settings")).toBeOnTheScreen();
    expect(mockLocalSnapshot).not.toHaveBeenCalled();
    await user.press(screen.getByTestId("new-project-disclosure"));
    expect(screen.getByTestId("create-goal-button")).toBeDisabled();
  });

  it("clears old-origin rows before refreshing another server and rejects a response whose origin changed", async () => {
    const pending = deferred<Bootstrap>();
    await render(<SwarmScreen />);
    await screen.findByText(goalDetail.goal.objective);
    mockGetServerUrl.mockResolvedValue("https://new.example");
    mockBootstrap.mockReturnValueOnce(pending.promise);
    await act(async () => { void refreshFromLiveEvent?.(); });
    await waitFor(() => expect(mockBootstrap).toHaveBeenCalledTimes(2));
    expect(screen.queryByText("Worker Alpha")).not.toBeOnTheScreen();
    mockGetServerUrl.mockResolvedValue("https://third.example");
    await act(async () => pending.resolve(bootstrap));
    expect(screen.queryByText(goalDetail.goal.objective)).not.toBeOnTheScreen();
    expect(screen.getByTestId("swarm-open-settings")).toBeOnTheScreen();
  });

  it("preserves the draft and never navigates or resends when pairing changes during creation", async () => {
    const pending = deferred<GoalDetail>();
    mockCreateGoal.mockReturnValueOnce(pending.promise);
    const user = userEvent.setup();
    await render(<SwarmScreen />);
    await screen.findByText(goalDetail.goal.objective);
    await user.press(screen.getByTestId("new-project-disclosure"));
    await user.type(screen.getByLabelText("Objectif du projet"), "Mon agenda");
    await user.press(screen.getByTestId("create-goal-button"));
    await waitFor(() => expect(mockCreateGoal).toHaveBeenCalledTimes(1));
    const isCurrent = mockCreateGoal.mock.calls[0]?.[1];
    expect(isCurrent?.()).toBe(true);
    await act(async () => notifyConnectionChanged());
    expect(isCurrent?.()).toBe(false);
    await act(async () => pending.resolve(goalDetail));
    expect(mockPush).not.toHaveBeenCalled();
    expect(mockCreateGoal).toHaveBeenCalledTimes(1);
    expect(screen.getByTestId("goal-objective-input")).toHaveDisplayValue("Mon agenda");
    expect(screen.getByTestId("create-goal-button")).toBeDisabled();
    expect(screen.getByText(/Le jumelage a changé pendant la création/)).toBeOnTheScreen();
  });
});
