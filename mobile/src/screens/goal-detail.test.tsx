import { act, render, renderHook, screen, userEvent, waitFor } from "@testing-library/react-native";
import { beforeEach, describe, expect, it, jest } from "@jest/globals";
import { Alert } from "react-native";

import GoalDetailScreen from "@/../app/goal/[id]";
import {
  cancelGoal,
  createGoalFeedback,
  getGoal,
  getServerUrl,
  replanGoal,
  startGoal,
  type GoalDetail,
} from "@/lib/api/client";
import { localGoalDetail } from "@/lib/state/replica";
import { useGoalDetailController } from "@/screens/goal-detail-content";

const mockPush = jest.fn();
const mockStackScreen = jest.fn();
let refreshFromLiveEvent: (() => void | Promise<unknown>) | undefined;

jest.mock("expo-router", () => {
  function StackScreen(props: unknown) {
    mockStackScreen(props);
    return null;
  }
  return {
    Stack: { Screen: StackScreen },
    useLocalSearchParams: () => ({ id: "goal_1" }),
    useRouter: () => ({ push: mockPush }),
  };
});
jest.mock("@/lib/api/client", () => ({
  cancelGoal: jest.fn(),
  createGoalFeedback: jest.fn(),
  getGoal: jest.fn(),
  getServerUrl: jest.fn(),
  replanGoal: jest.fn(),
  startGoal: jest.fn(),
}));
jest.mock("@/lib/state/replica", () => ({ localGoalDetail: jest.fn() }));
jest.mock("@/lib/sync/live-sync-context", () => ({
  useLiveRefresh: (refresh: () => void | Promise<unknown>) => {
    refreshFromLiveEvent = refresh;
  },
}));

const mockCancelGoal = jest.mocked(cancelGoal);
const mockCreateFeedback = jest.mocked(createGoalFeedback);
const mockGetGoal = jest.mocked(getGoal);
const mockGetServerUrl = jest.mocked(getServerUrl);
const mockLocalGoal = jest.mocked(localGoalDetail);
const mockReplanGoal = jest.mocked(replanGoal);
const mockStartGoal = jest.mocked(startGoal);

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((settle) => {
    resolve = settle;
  });
  return { promise, resolve };
}

const detail: GoalDetail = {
  goal: {
    id: "goal_1",
    root_task_id: "tsk_root",
    objective: "Qualifier le runtime distribué",
    status: "completed",
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
    current_phase: "evaluation",
    evaluator_status: "done",
    evaluator_summary: "Les critères publics sont satisfaits.",
    created_at: "2030-01-01T00:00:00Z",
    updated_at: "2030-01-01T00:04:00Z",
    started_at: "2030-01-01T00:01:00Z",
    completed_at: "2030-01-01T00:04:00Z",
  },
  nodes: [{
    id: "node_1",
    goal_run_id: "goal_1",
    node_type: "worker",
    title: "Vérifier les invariants",
    objective: "Lire les preuves",
    required_skill: "code_review.git_status",
    status: "waiting_permission",
    priority: 0,
    depends_on: [],
    assigned_agent_id: "review-worker",
    task_id: "tsk_child",
    expected_output: "Résumé public",
    created_at: "2030-01-01T00:00:00Z",
    updated_at: "2030-01-01T00:02:00Z",
  }, {
    id: "node_2",
    goal_run_id: "goal_1",
    node_type: "synthesis",
    title: "Synthétiser",
    objective: "Produire la conclusion",
    status: "completed",
    priority: 1,
    depends_on: ["node_1"],
    result_summary: "Synthèse vérifiée",
    created_at: "2030-01-01T00:02:00Z",
    updated_at: "2030-01-01T00:04:00Z",
    completed_at: "2030-01-01T00:04:00Z",
  }],
  result: {
    goal_run_id: "goal_1",
    root_task_id: "tsk_root",
    status: "completed",
    answer: "Le runtime respecte les invariants observés.",
    completed_nodes: ["node_2"],
    failed_nodes: [],
    agents_used: ["review-worker"],
    memory_ids: ["mem_1"],
    episode_ids: ["ep_1"],
    started_at: "2030-01-01T00:01:00Z",
    completed_at: "2030-01-01T00:04:00Z",
    limitations: ["Validation physique encore requise"],
  },
};

const waitingForWorkers: GoalDetail = {
  ...detail,
  goal: {
    ...detail.goal,
    status: "planning",
    current_phase: "waiting_for_workers",
    step_count: 0,
    model_call_count: 0,
    evaluator_summary: undefined,
    completed_at: undefined,
    failure_reason: "no online workers are available",
  },
  nodes: [],
  result: null,
};

describe("GoalDetailScreen", () => {
  beforeEach(() => {
    jest.clearAllMocks();
    refreshFromLiveEvent = undefined;
    mockGetGoal.mockResolvedValue(detail);
    mockGetServerUrl.mockResolvedValue("https://control.example");
    mockLocalGoal.mockResolvedValue(null);
    mockCancelGoal.mockResolvedValue(detail);
    mockStartGoal.mockResolvedValue(detail);
    mockReplanGoal.mockResolvedValue(detail);
    mockCreateFeedback.mockResolvedValue({ accepted: true });
  });

  it("renders only public goal, node, evaluator, and final-result evidence", async () => {
    const user = userEvent.setup();
    const privateDetail = {
      ...detail,
      goal: { ...detail.goal, chain_of_thought: "PRIVATE GOAL REASONING" },
      nodes: detail.nodes.map((node) => ({ ...node, rationale: "PRIVATE NODE REASONING" })),
    } as GoalDetail;
    mockGetGoal.mockResolvedValue(privateDetail);
    await render(<GoalDetailScreen />);

    expect(await screen.findByText("Qualifier le runtime distribué")).toBeOnTheScreen();
    expect(screen.getByText(/Les critères publics sont satisfaits/)).toBeOnTheScreen();
    expect(screen.getByText("Vérifier les invariants")).toBeOnTheScreen();
    expect(screen.getByText("Le runtime respecte les invariants observés.")).toBeOnTheScreen();
    expect(screen.getByText(/Agents en cours : review-worker/)).toBeOnTheScreen();
    expect(screen.queryByText("PRIVATE GOAL REASONING")).not.toBeOnTheScreen();
    expect(screen.queryByText("PRIVATE NODE REASONING")).not.toBeOnTheScreen();

    await user.press(screen.getByRole("button", { name: "Voir la tâche" }));
    expect(mockPush).toHaveBeenCalledWith({ pathname: "/task/[id]", params: { id: "tsk_child" } });
    await user.press(screen.getByRole("button", { name: "Voir les accords" }));
    expect(mockPush).toHaveBeenCalledWith("/approvals");
  });

  it("renders cached evidence read-only and never invokes a sensitive action", async () => {
    const user = userEvent.setup();
    mockGetGoal.mockRejectedValue(new Error("Serveur indisponible"));
    mockLocalGoal.mockResolvedValue({
      ...detail,
      goal: { ...detail.goal, status: "running", autonomy_profile: "manual" },
    });
    await render(<GoalDetailScreen />);

    expect(await screen.findByText(/Copie locale possiblement périmée/)).toBeOnTheScreen();
    expect(screen.queryByRole("button", { name: "Annuler le but" })).not.toBeOnTheScreen();
    expect(screen.queryByRole("button", { name: "Démarrer le but" })).not.toBeOnTheScreen();
    expect(screen.queryByRole("button", { name: "Continuer le but" })).not.toBeOnTheScreen();
    expect(screen.queryByRole("button", { name: "Demander une replanification" })).not.toBeOnTheScreen();
    expect(screen.getByRole("button", { name: "Noter le résultat 5 sur 5" })).toBeDisabled();

    await user.press(screen.getByRole("button", { name: "Noter le résultat 5 sur 5" }));
    expect(mockCreateFeedback).not.toHaveBeenCalled();
    expect(mockCancelGoal).not.toHaveBeenCalled();
    expect(mockStartGoal).not.toHaveBeenCalled();
    expect(mockReplanGoal).not.toHaveBeenCalled();
  });

  it("confirms an online cancellation and then refetches authoritative state", async () => {
    const user = userEvent.setup();
    const running = { ...detail, goal: { ...detail.goal, status: "running" as const }, result: null };
    mockGetGoal.mockResolvedValue(running);
    mockCancelGoal.mockResolvedValue({
      ...running,
      goal: { ...running.goal, status: "cancelled" },
    });
    const alert = jest.spyOn(Alert, "alert").mockImplementation(() => undefined);
    await render(<GoalDetailScreen />);
    await screen.findByRole("button", { name: "Annuler le but" });

    await user.press(screen.getByRole("button", { name: "Annuler le but" }));
    const buttons = alert.mock.calls[0]?.[2];
    const destructive = buttons?.find((button) => button.style === "destructive");
    await act(async () => destructive?.onPress?.());

    await waitFor(() => expect(mockCancelGoal).toHaveBeenCalledWith("goal_1"));
    await waitFor(() => expect(mockGetGoal).toHaveBeenCalledTimes(2));
    alert.mockRestore();
  });

  it("explains the worker wait and retries planning once before refreshing the server state", async () => {
    const user = userEvent.setup();
    const retry = deferred<GoalDetail>();
    mockGetGoal.mockResolvedValueOnce(waitingForWorkers).mockResolvedValue(detail);
    mockStartGoal.mockImplementationOnce(async () => retry.promise);
    await render(<GoalDetailScreen />);

    expect(await screen.findByText("En attente d’un agent")).toBeOnTheScreen();
    expect(screen.getByText(/Connectez un agent d’exécution, puis réessayez/)).toBeOnTheScreen();
    expect(screen.getByText(/Aucun appel modèle n’est lancé pendant cette attente/)).toBeOnTheScreen();
    expect(screen.getByText("Aucun agent en cours.")).toBeOnTheScreen();
    expect(screen.queryByText("En cours")).not.toBeOnTheScreen();
    expect(screen.queryByText(/waiting_for_workers|no online workers/)).not.toBeOnTheScreen();
    expect(screen.queryByRole("button", { name: "Démarrer le but" })).not.toBeOnTheScreen();
    expect(mockStartGoal).not.toHaveBeenCalled();

    await user.press(screen.getByRole("button", { name: "Réessayer la planification" }));
    expect(mockStartGoal).toHaveBeenCalledWith("goal_1");
    expect(screen.getByRole("button", { name: "Réessayer la planification" })).toBeDisabled();
    await user.press(screen.getByRole("button", { name: "Réessayer la planification" }));
    expect(mockStartGoal).toHaveBeenCalledTimes(1);

    await act(async () => retry.resolve(detail));
    expect(await screen.findByText("Le runtime respecte les invariants observés.")).toBeOnTheScreen();
    expect(mockGetGoal).toHaveBeenCalledTimes(2);
    expect(screen.queryByText("En attente d’un agent")).not.toBeOnTheScreen();
  });

  it("keeps a cached worker wait read-only", async () => {
    mockGetGoal.mockRejectedValue(new Error("Serveur indisponible"));
    mockLocalGoal.mockResolvedValue(waitingForWorkers);
    await render(<GoalDetailScreen />);

    expect(await screen.findByText("En attente d’un agent")).toBeOnTheScreen();
    expect(screen.getByText(/Copie locale possiblement périmée/)).toBeOnTheScreen();
    expect(screen.queryByRole("button", { name: "Réessayer la planification" })).not.toBeOnTheScreen();
    expect(mockStartGoal).not.toHaveBeenCalled();
  });

  it("guards controller retries while busy and after falling back to an offline worker wait", async () => {
    const retry = deferred<GoalDetail>();
    mockGetGoal.mockResolvedValue(waitingForWorkers);
    mockStartGoal.mockImplementationOnce(async () => retry.promise);
    const { result } = await renderHook(() => useGoalDetailController("goal_1"));
    await waitFor(() => expect(result.current.online).toBe(true));

    await act(() => { void result.current.start(); });
    expect(result.current.busy).toBe("start");
    await act(async () => result.current.start());
    expect(mockStartGoal).toHaveBeenCalledTimes(1);

    await act(async () => retry.resolve(waitingForWorkers));
    await waitFor(() => expect(result.current.busy).toBeNull());
    mockGetGoal.mockRejectedValue(new Error("Serveur indisponible"));
    mockLocalGoal.mockResolvedValue(waitingForWorkers);
    await act(async () => result.current.refresh());
    expect(result.current.online).toBe(false);
    await act(async () => result.current.start());
    expect(mockStartGoal).toHaveBeenCalledTimes(1);
  });

  it.each([
    ["planner_invalid_response", "Plan proposé invalide"],
    ["planner_request_rejected", "Demande de planification refusée"],
    ["planner_invalid_context", "Contexte de planification invalide"],
    ["planner_unavailable", "Planificateur indisponible"],
  ])("explains the recoverable %s phase without exposing its raw failure", async (phase, label) => {
    mockGetGoal.mockResolvedValue({
      ...waitingForWorkers,
      goal: { ...waitingForWorkers.goal, current_phase: phase, failure_reason: "RAW PROVIDER ERROR" },
    });
    await render(<GoalDetailScreen />);

    expect(await screen.findByText(`Phase : ${label}`)).toBeOnTheScreen();
    expect(screen.getByRole("button", { name: "Réessayer la planification" })).toBeOnTheScreen();
    expect(screen.queryByText(/RAW PROVIDER ERROR/)).not.toBeOnTheScreen();
    expect(screen.queryByText(`Phase : ${phase}`)).not.toBeOnTheScreen();
  });

  it("advances a running manual goal only after an explicit continuation", async () => {
    const user = userEvent.setup();
    const running: GoalDetail = {
      ...detail,
      goal: { ...detail.goal, status: "running", autonomy_profile: "manual", current_phase: "execution" },
      nodes: detail.nodes.map((node, index) => ({
        ...node,
        node_type: "worker",
        status: index === 0 ? "completed" : "ready",
      })),
      result: null,
    };
    const continuation = deferred<GoalDetail>();
    mockGetGoal.mockResolvedValue(running);
    mockStartGoal.mockImplementationOnce(async () => continuation.promise);
    await render(<GoalDetailScreen />);

    const button = await screen.findByRole("button", { name: "Continuer le but" });
    expect(mockStartGoal).not.toHaveBeenCalled();
    await user.press(button);

    expect(mockStartGoal).toHaveBeenCalledWith("goal_1");
    expect(screen.getByRole("button", { name: "Continuer le but" })).toBeDisabled();
    await user.press(screen.getByRole("button", { name: "Continuer le but" }));
    expect(mockStartGoal).toHaveBeenCalledTimes(1);

    await act(async () => continuation.resolve(running));
    await waitFor(() => expect(mockGetGoal).toHaveBeenCalledTimes(2));
    expect(await screen.findByRole("button", { name: "Continuer le but", disabled: false })).toBeOnTheScreen();
    expect(mockStartGoal).toHaveBeenCalledTimes(1);
  });

  it.each(["assisted", "autonomous"] as const)(
    "does not offer manual continuation for a running %s goal",
    async (profile) => {
      mockGetGoal.mockResolvedValue({
        ...detail,
        goal: { ...detail.goal, status: "running", autonomy_profile: profile },
        result: null,
      });
      await render(<GoalDetailScreen />);
      await screen.findByText("Qualifier le runtime distribué");

      expect(screen.queryByRole("button", { name: "Continuer le but" })).not.toBeOnTheScreen();
    },
  );

  it.each(["waiting_permission", "completed", "failed", "cancelled", "budget_exhausted"] as const)(
    "does not offer manual continuation when the goal is %s",
    async (status) => {
      mockGetGoal.mockResolvedValue({
        ...detail,
        goal: { ...detail.goal, status, autonomy_profile: "manual" },
      });
      await render(<GoalDetailScreen />);
      await screen.findByText("Qualifier le runtime distribué");

      expect(screen.queryByRole("button", { name: "Continuer le but" })).not.toBeOnTheScreen();
    },
  );

  it.each(["running", "waiting_permission"] as const)(
    "offers replan while the authoritative goal is %s",
    async (status) => {
      mockGetGoal.mockResolvedValue({
        ...detail,
        goal: { ...detail.goal, status },
        result: null,
      });

      await render(<GoalDetailScreen />);

      expect(
        await screen.findByRole("button", { name: "Demander une replanification" }),
      ).toBeOnTheScreen();
    },
  );

  it.each(["planning", "completed", "failed", "cancelled", "budget_exhausted"] as const)(
    "does not offer replan when the authoritative goal is %s",
    async (status) => {
      mockGetGoal.mockResolvedValue({
        ...detail,
        goal: { ...detail.goal, status },
      });

      await render(<GoalDetailScreen />);
      await screen.findByText("Qualifier le runtime distribué");

      expect(
        screen.queryByRole("button", { name: "Demander une replanification" }),
      ).not.toBeOnTheScreen();
    },
  );

  it("does not automatically replay an uncertain final feedback submission", async () => {
    const user = userEvent.setup();
    mockCreateFeedback.mockRejectedValue(new Error("Réponse perdue"));
    await render(<GoalDetailScreen />);
    await screen.findByText("Le runtime respecte les invariants observés.");

    await user.press(screen.getByRole("button", { name: "Noter le résultat 5 sur 5" }));
    expect(await screen.findByText(/feedback incertain n’est pas renvoyé automatiquement/)).toBeOnTheScreen();
    expect(screen.getByRole("button", { name: "Noter le résultat 5 sur 5" })).toBeDisabled();
    await user.press(screen.getByRole("button", { name: "Noter le résultat 5 sur 5" }));
    expect(mockCreateFeedback).toHaveBeenCalledTimes(1);
  });

  it.each(["manual continuation", "worker retry"])(
    "locks %s while authoritative reconciliation is unresolved",
    async (action) => {
      const pending: GoalDetail = action === "worker retry" ? waitingForWorkers : {
        ...detail,
        goal: { ...detail.goal, status: "running", autonomy_profile: "manual" },
        result: null,
      };
      const label = action === "worker retry" ? "Réessayer la planification" : "Continuer le but";
      const reconciliation = deferred<GoalDetail>();
      mockGetGoal
        .mockResolvedValueOnce(pending)
        .mockImplementationOnce(async () => reconciliation.promise);
      await render(<GoalDetailScreen />);
      expect(await screen.findByRole("button", { name: "Annuler le but" })).toBeOnTheScreen();
      expect(screen.getByRole("button", { name: label })).toBeOnTheScreen();

      await act(async () => {
        void refreshFromLiveEvent?.();
        await Promise.resolve();
      });
      expect(screen.queryByRole("button", { name: "Annuler le but" })).not.toBeOnTheScreen();
      expect(screen.queryByRole("button", { name: label })).not.toBeOnTheScreen();
      expect(mockCancelGoal).not.toHaveBeenCalled();
      expect(mockStartGoal).not.toHaveBeenCalled();

      await act(async () => reconciliation.resolve(pending));
      expect(await screen.findByRole("button", { name: "Annuler le but" })).toBeOnTheScreen();
      expect(screen.getByRole("button", { name: label })).toBeOnTheScreen();
    },
  );

  it("fences a slow detail refresh after a newer live refresh", async () => {
    const older = deferred<GoalDetail>();
    const newer = {
      ...detail,
      goal: { ...detail.goal, objective: "But plus récent" },
    };
    mockGetGoal
      .mockImplementationOnce(async () => older.promise)
      .mockResolvedValueOnce(newer);
    await render(<GoalDetailScreen />);

    await act(async () => {
      await refreshFromLiveEvent?.();
    });
    expect(await screen.findByText("But plus récent")).toBeOnTheScreen();

    await act(async () => older.resolve(detail));
    expect(screen.queryByText("Qualifier le runtime distribué")).not.toBeOnTheScreen();
    expect(screen.getByText("But plus récent")).toBeOnTheScreen();
  });
});
