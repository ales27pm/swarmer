import { act, render, screen, userEvent, waitFor } from "@testing-library/react-native";
import { beforeEach, describe, expect, it, jest } from "@jest/globals";

import SwarmScreen from "@/../app/(main)/swarm";
import {
  bootstrapSync,
  createGoal,
  getServerUrl,
  type Bootstrap,
  type GoalDetail,
} from "@/lib/api/client";
import { localSwarmSnapshot } from "@/lib/state/replica";

const mockPush = jest.fn();
let refreshFromLiveEvent: (() => void | Promise<unknown>) | undefined;

jest.mock("expo-router", () => ({ useRouter: () => ({ push: mockPush }) }));
jest.mock("@/lib/api/client", () => ({
  bootstrapSync: jest.fn(),
  createGoal: jest.fn(),
  getServerUrl: jest.fn(),
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
    refreshFromLiveEvent = undefined;
    mockBootstrap.mockResolvedValue(bootstrap);
    mockCreateGoal.mockResolvedValue(goalDetail);
    mockGetServerUrl.mockResolvedValue("https://control.example");
    mockLocalSnapshot.mockResolvedValue(null);
  });

  it("shows authoritative goals and creates without automatically starting", async () => {
    const user = userEvent.setup();
    await render(<SwarmScreen />);

    expect(await screen.findByText("Qualifier le runtime distribué")).toBeOnTheScreen();
    expect(screen.getByText("1 agent en travail")).toBeOnTheScreen();
    expect(screen.getByText("Worker Alpha")).toBeOnTheScreen();

    await user.type(screen.getByLabelText("Objectif du but"), "Préparer une qualification");
    await user.press(screen.getByRole("button", { name: "Autonome" }));
    await user.press(screen.getByRole("button", { name: "Créer le but" }));

    await waitFor(() => expect(mockCreateGoal).toHaveBeenCalledWith({
      autonomy_profile: "autonomous",
      objective: "Préparer une qualification",
    }));
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
    expect(screen.getByRole("button", { name: "Créer le but" })).toBeDisabled();
    await user.press(screen.getByRole("button", { name: /Ouvrir le but Qualifier/ }));
    expect(mockPush).toHaveBeenCalledWith({ pathname: "/goal/[id]", params: { id: "goal_1" } });
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
    const cache = deferred<Awaited<ReturnType<typeof localSwarmSnapshot>>>();
    mockBootstrap.mockRejectedValue(new Error("Serveur indisponible"));
    mockLocalSnapshot.mockImplementationOnce(async () => cache.promise);
    mockGetServerUrl
      .mockResolvedValueOnce("https://old.example")
      .mockResolvedValueOnce("https://new.example");
    await render(<SwarmScreen />);

    await act(async () => cache.resolve({
      origin: "https://old.example",
      goals: [goalDetail.goal],
      plan_nodes: goalDetail.nodes,
      goal_results: [],
      agents: [],
      counts: null,
      cursor: null,
    }));

    await waitFor(() => expect(mockGetServerUrl).toHaveBeenCalledTimes(2));
    expect(screen.queryByText("Qualifier le runtime distribué")).not.toBeOnTheScreen();
    expect(screen.getByRole("button", { name: "Créer le but" })).toBeDisabled();
  });
});
