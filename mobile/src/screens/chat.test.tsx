import { act, render, screen, userEvent, waitFor } from "@testing-library/react-native";
import { beforeEach, describe, expect, it, jest } from "@jest/globals";
import { AppState, type AppStateStatus } from "react-native";

import ChatScreen from "@/../app/(main)/index";
import {
  bootstrapSync,
  listMessages,
  planTask,
  sendChat,
  type Bootstrap,
  type Message,
  type Task,
  type ToolCall,
} from "@/lib/api/client";
import { LiveSyncContextProvider } from "@/lib/sync/live-sync-context";

const mockPush = jest.fn();
let mockSearchParams: { draft?: string; intentMode?: string } = {};
let mockFocusEffect: (() => void | (() => void)) | undefined;
let mockFocusCleanup: (() => void) | undefined;
let mockAppStateListener: ((state: AppStateStatus) => void) | undefined;
const mockRemoveAppStateListener = jest.fn();

jest.spyOn(AppState, "addEventListener").mockImplementation((_event, listener) => {
  mockAppStateListener = listener;
  return { remove: mockRemoveAppStateListener };
});

jest.mock("expo-router", () => {
  const React = jest.requireActual<typeof import("react")>("react");

  return {
    useRouter: () => ({ push: mockPush }),
    useLocalSearchParams: () => mockSearchParams,
    useFocusEffect: (effect: () => void | (() => void)) => {
      mockFocusEffect = effect;
      React.useEffect(() => {
        const cleanup = effect();
        mockFocusCleanup = typeof cleanup === "function" ? cleanup : undefined;
        return () => {
          mockFocusCleanup?.();
          mockFocusCleanup = undefined;
        };
      }, [effect]);
    },
  };
});
jest.mock("@/lib/api/client", () => ({
  bootstrapSync: jest.fn(),
  listMessages: jest.fn(),
  planTask: jest.fn(),
  sendChat: jest.fn(),
}));

const task: Task = {
  id: "tsk_test",
  title: "Inspecter le projet",
  input: "Inspecter le projet",
  mode: "normal",
  source: "test-phone",
  conversation_id: "conv_test",
  status: "created",
  priority: 0,
  created_at: "2026-09-04T12:00:00Z",
  updated_at: "2026-09-04T12:00:00Z",
  completed_at: null,
  error_json: null,
};

const message: Message = {
  id: "msg_test",
  conversation_id: "conv_test",
  task_id: task.id,
  role: "user",
  agent_id: null,
  content: task.input,
  metadata: null,
  created_at: task.created_at,
};

const proposalOnlyMessage: Message = {
  id: "msg_proposal_only",
  conversation_id: "conv_test",
  task_id: task.id,
  role: "agent",
  agent_id: "local-orchestrator",
  content: "Deployment completed successfully",
  metadata: { verified_status: "proposal_only" },
  created_at: task.created_at,
};

function completedToolCallWith(result: unknown): ToolCall {
  return {
    id: "call_completed",
    task_id: task.id,
    tool_name: "workspace.list_dir",
    arguments: { path: "." },
    summary: "List a workspace directory",
    risk: "low",
    status: "completed",
    approval_id: null,
    result,
    error: null,
    created_at: task.created_at,
    updated_at: task.updated_at,
  } as unknown as ToolCall;
}

const bootstrap: Bootstrap = {
  server_time: "2026-09-04T12:00:00Z",
  tasks: [],
  approvals: [],
  tool_calls: [],
  conversations: [],
  agents: [],
  pinned_memory: [],
  counts: {
    tasks: 0,
    messages: 0,
    agents: 0,
    approvals_pending: 0,
    memory_items: 0,
    audit_events: 0,
  },
  cursor: "0",
};

const mockBootstrap = jest.mocked(bootstrapSync);
const mockListMessages = jest.mocked(listMessages);
const mockPlanTask = jest.mocked(planTask);
const mockSendChat = jest.mocked(sendChat);

function deferred<T>() {
  let reject!: (cause?: unknown) => void;
  let resolve!: (value: T | PromiseLike<T>) => void;
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, reject, resolve };
}

async function refocusChat() {
  await act(async () => {
    mockFocusCleanup?.();
    const cleanup = mockFocusEffect?.();
    mockFocusCleanup = typeof cleanup === "function" ? cleanup : undefined;
  });
}

describe("ChatScreen", () => {
  beforeEach(() => {
    jest.clearAllMocks();
    mockFocusEffect = undefined;
    mockFocusCleanup = undefined;
    mockAppStateListener = undefined;
    mockSearchParams = {};
    mockBootstrap.mockResolvedValue(bootstrap);
    mockListMessages.mockResolvedValue([message]);
    mockSendChat.mockResolvedValue({ conversation_id: "conv_test", task });
    mockPlanTask.mockResolvedValue({
      task_id: task.id,
      proposal: { tool_name: "none", arguments: {}, summary: "Proposal only" },
      task: { ...task, status: "planned" },
    });
  });

  it("identifies the control-plane planner and offers a capability-matched root listing", async () => {
    await render(<ChatScreen />);

    expect(await screen.findByText("Control plane authentifié")).toBeOnTheScreen();
    expect(screen.getByText("Liste les fichiers à la racine du projet.")).toBeOnTheScreen();
    expect(
      screen.getByText(
        "Décris une intention. Le modèle du control plane propose; l’exécuteur prouve; les actions sensibles attendent ton accord.",
      ),
    ).toBeOnTheScreen();
    expect(
      screen.getByText("Prêt à discuter. Passe en mode Tâche lorsque tu veux agir."),
    ).toBeOnTheScreen();
  });

  it("prefills but does not submit an App Intent task draft", async () => {
    mockSearchParams = { draft: "Inspecte les tests", intentMode: "task" };
    await render(<ChatScreen />);

    expect(await screen.findByDisplayValue("Inspecte les tests")).toBeOnTheScreen();
    expect(mockSendChat).not.toHaveBeenCalled();
    expect(mockPlanTask).not.toHaveBeenCalled();
  });

  it("reports REST authentication and live transport as separate states", async () => {
    await render(
      <LiveSyncContextProvider value={{ error: null, revision: 0, state: "connected" }}>
        <ChatScreen />
      </LiveSyncContextProvider>,
    );

    expect(await screen.findByText("Control plane authentifié")).toBeOnTheScreen();
    expect(screen.getByText("Temps réel connecté")).toBeOnTheScreen();
  });

  it("refreshes the rendered conversation after a live message invalidation", async () => {
    const pushedMessage: Message = {
      ...proposalOnlyMessage,
      id: "msg_live",
      content: "Nouvelle réponse reçue en temps réel",
    };
    const user = userEvent.setup();
    const rendered = await render(
      <LiveSyncContextProvider value={{ error: null, revision: 0, state: "connected" }}>
        <ChatScreen />
      </LiveSyncContextProvider>,
    );
    await screen.findByText("Control plane authentifié");
    await user.type(screen.getByLabelText("Intention pour le swarm"), task.input);
    await user.press(screen.getByRole("button", { name: "Envoyer" }));
    await waitFor(() => expect(mockListMessages).toHaveBeenCalledTimes(2));
    expect(screen.queryByText(pushedMessage.content)).not.toBeOnTheScreen();

    mockListMessages.mockResolvedValue([message, pushedMessage]);
    await rendered.rerender(
      <LiveSyncContextProvider value={{ error: null, revision: 1, state: "connected" }}>
        <ChatScreen />
      </LiveSyncContextProvider>,
    );

    expect(await screen.findByText(pushedMessage.content)).toBeOnTheScreen();
    expect(mockListMessages).toHaveBeenLastCalledWith("conv_test", expect.any(Function));
  });

  it("refreshes authentication when Chat regains focus after pairing", async () => {
    mockBootstrap.mockRejectedValueOnce(new Error("Non jumelé"));
    const user = userEvent.setup();
    await render(<ChatScreen />);

    await waitFor(() => expect(mockBootstrap).toHaveBeenCalledTimes(1));
    expect(screen.getByText("Control plane injoignable ou non jumelé")).toBeOnTheScreen();
    await user.type(screen.getByLabelText("Intention pour le swarm"), task.input);
    expect(screen.getByRole("button", { name: "Envoyer" })).toBeDisabled();

    mockBootstrap.mockResolvedValue(bootstrap);
    await refocusChat();

    expect(await screen.findByText("Control plane authentifié")).toBeOnTheScreen();
    expect(screen.getByRole("button", { name: "Envoyer" })).toBeEnabled();
    expect(mockBootstrap).toHaveBeenCalledTimes(2);
  });

  it("ignores a stale failed refresh after a newer authenticated focus", async () => {
    const firstRefresh = deferred<Bootstrap>();
    mockBootstrap.mockReset();
    mockBootstrap.mockReturnValueOnce(firstRefresh.promise).mockResolvedValueOnce(bootstrap);
    await render(<ChatScreen />);

    await waitFor(() => expect(mockBootstrap).toHaveBeenCalledTimes(1));
    expect(screen.getByText("Control plane injoignable ou non jumelé")).toBeOnTheScreen();

    await refocusChat();
    expect(await screen.findByText("Control plane authentifié")).toBeOnTheScreen();

    await act(async () => {
      firstRefresh.reject(new Error("Ancienne requête en échec"));
      await Promise.resolve();
    });

    expect(screen.getByText("Control plane authentifié")).toBeOnTheScreen();
    expect(screen.queryByText("Control plane injoignable ou non jumelé")).not.toBeOnTheScreen();
  });

  it("refreshes authentication when iOS becomes active while Chat stays focused", async () => {
    mockBootstrap.mockRejectedValueOnce(new Error("Réseau suspendu"));
    const rendered = await render(<ChatScreen />);

    await waitFor(() => expect(mockBootstrap).toHaveBeenCalledTimes(1));
    expect(screen.getByText("Control plane injoignable ou non jumelé")).toBeOnTheScreen();

    mockBootstrap.mockResolvedValue(bootstrap);
    await act(async () => {
      mockAppStateListener?.("active");
    });

    expect(await screen.findByText("Control plane authentifié")).toBeOnTheScreen();
    await rendered.unmount();
    expect(mockRemoveAppStateListener).toHaveBeenCalledTimes(1);
  });

  it("keeps a persisted proposal-only model message explicitly unverified", async () => {
    mockListMessages.mockResolvedValue([proposalOnlyMessage]);
    const user = userEvent.setup();
    await render(<ChatScreen />);

    await screen.findByText("Control plane authentifié");
    await user.type(screen.getByLabelText("Intention pour le swarm"), task.input);
    await user.press(screen.getByRole("button", { name: "Envoyer" }));

    expect(await screen.findByText("Proposition du modèle — non vérifiée")).toBeOnTheScreen();
    expect(screen.getByText(proposalOnlyMessage.content)).toBeOnTheScreen();
  });

  it("labels a no-tool model response as a proposal rather than completion", async () => {
    const user = userEvent.setup();
    await render(<ChatScreen />);
    await screen.findByText("Control plane authentifié");

    await user.type(screen.getByLabelText("Intention pour le swarm"), task.input);
    await user.press(screen.getByRole("button", { name: "Envoyer" }));

    await waitFor(() => expect(mockPlanTask).toHaveBeenCalledWith(task.id));
    expect(
      screen.getByText("Le modèle a produit une proposition, sans prétendre l’avoir exécutée."),
    ).toBeOnTheScreen();
    expect(screen.getByRole("button", { name: "Voir la tâche et ses preuves" })).toBeEnabled();
  });

  it("claims verified completion only when the executor result is an object", async () => {
    mockPlanTask.mockResolvedValue(completedToolCallWith({ entries: [] }));
    const user = userEvent.setup();
    await render(<ChatScreen />);
    await screen.findByText("Control plane authentifié");

    await user.type(screen.getByLabelText("Intention pour le swarm"), task.input);
    await user.press(screen.getByRole("button", { name: "Envoyer" }));

    expect(
      await screen.findByText("L’exécuteur local a terminé et enregistré un résultat vérifié."),
    ).toBeOnTheScreen();
  });

  it.each([
    ["null", null],
    ["an array", []],
  ])("fails closed for a completed call with %s result", async (_label, result) => {
    mockPlanTask.mockResolvedValue(completedToolCallWith(result));
    const user = userEvent.setup();
    await render(<ChatScreen />);
    await screen.findByText("Control plane authentifié");

    await user.type(screen.getByLabelText("Intention pour le swarm"), task.input);
    await user.press(screen.getByRole("button", { name: "Envoyer" }));

    expect(
      await screen.findByText(
        "L’appel signale une fin sans résultat d’exécution vérifié; aucune réussite n’est confirmée.",
      ),
    ).toBeOnTheScreen();
    expect(
      screen.queryByText("L’exécuteur local a terminé et enregistré un résultat vérifié."),
    ).not.toBeOnTheScreen();
  });

  it("links the pending approval count directly to the decision queue", async () => {
    mockBootstrap.mockResolvedValue({
      ...bootstrap,
      counts: { ...bootstrap.counts, approvals_pending: 2 },
    });
    const user = userEvent.setup();
    await render(<ChatScreen />);

    await user.press(await screen.findByRole("button", { name: "Voir 2 accords en attente" }));
    expect(mockPush).toHaveBeenCalledWith("/approvals");
  });

  it("keeps connection settings directly accessible", async () => {
    const user = userEvent.setup();
    await render(<ChatScreen />);

    await user.press(await screen.findByRole("button", { name: "Ouvrir les réglages" }));
    expect(mockPush).toHaveBeenCalledWith("/settings");
  });

  it("preserves fail-closed error feedback and refresh after task creation", async () => {
    mockPlanTask.mockRejectedValueOnce(new Error("Planification indisponible"));
    const user = userEvent.setup();
    await render(<ChatScreen />);
    await screen.findByText("Control plane authentifié");

    await user.type(screen.getByLabelText("Intention pour le swarm"), task.input);
    await user.press(screen.getByRole("button", { name: "Envoyer" }));

    expect(await screen.findByText("Planification indisponible")).toBeOnTheScreen();
    expect(
      screen.getByText(
        "La tâche a été créée, mais aucune planification ou exécution réussie n’a été confirmée.",
      ),
    ).toBeOnTheScreen();
    expect(
      screen.queryByText("L’exécuteur local a terminé et enregistré un résultat vérifié."),
    ).not.toBeOnTheScreen();
    expect(mockListMessages).toHaveBeenCalledTimes(2);
    expect(mockBootstrap).toHaveBeenCalledTimes(2);
  });
});
