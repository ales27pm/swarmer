import { act, fireEvent, render, screen, userEvent, waitFor } from "@testing-library/react-native";
import { beforeEach, describe, expect, it, jest } from "@jest/globals";
import { AppState, type AppStateStatus } from "react-native";

import ChatScreen from "@/../app/(main)/index";
import {
  bootstrapSync,
  createGoal,
  listMessages,
  planTask,
  sendChat,
  startGoal,
  type Bootstrap,
  type GoalDetail,
  type Message,
  type Task,
} from "@/lib/api/client";
import { LiveSyncContextProvider } from "@/lib/sync/live-sync-context";
import { notifyConnectionChanged } from "@/lib/connection-events";

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
jest.mock("react-native-safe-area-context", () => ({ useSafeAreaInsets: () => ({ top: 0, bottom: 0, left: 0, right: 0 }) }));
jest.mock("@/lib/api/client", () => ({
  bootstrapSync: jest.fn(),
  createGoal: jest.fn(),
  startGoal: jest.fn(),
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

const mockCreateGoal = jest.mocked(createGoal);
const goal = { goal: { id: "goal_new" }, nodes: [], result: null } as unknown as GoalDetail;

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
    mockCreateGoal.mockReset().mockResolvedValue(goal);
    mockBootstrap.mockResolvedValue(bootstrap);
    mockListMessages.mockReset().mockResolvedValue([message]);
    mockSendChat.mockReset().mockResolvedValue({ conversation_id: "conv_test", task });
    mockPlanTask.mockReset().mockResolvedValue({
      task_id: task.id,
      proposal: { tool_name: "none", arguments: {}, summary: "Proposal only" },
      task: { ...task, status: "planned" },
    });
  });

  it("identifies the server connection and offers a personal-assistant starting point", async () => {
    await render(<ChatScreen />);

    expect(await screen.findByText("Serveur connecté")).toBeOnTheScreen();
    expect(screen.getByText("Organiser ma semaine")).toBeOnTheScreen();
    expect(
      screen.getByText(
        "Une idée, une recherche, un projet.",
      ),
    ).toBeOnTheScreen();
    expect(
      screen.getByText("Échange avec ton assistant, sans lancer de tâche."),
    ).toBeOnTheScreen();
  });

  it("prepares a suggestion without sending and keeps the draft when switching modes", async () => {
    const user = userEvent.setup();
    await render(<ChatScreen />);
    await screen.findByText("Serveur connecté");
    await user.press(screen.getByRole("button", { name: "Organiser ma semaine" }));
    expect(screen.getByDisplayValue("Aide-moi à organiser ma semaine.")).toBeOnTheScreen();
    await user.press(screen.getByTestId("interaction-mode-task"));
    expect(screen.getByDisplayValue("Aide-moi à organiser ma semaine.")).toBeOnTheScreen();
    expect(screen.getByTestId("interaction-mode-task")).toHaveProp("accessibilityState", { selected: true, disabled: false });
    expect(mockSendChat).not.toHaveBeenCalled();
    expect(mockPlanTask).not.toHaveBeenCalled();
  });

  it("explains disabled sending and opens connection settings without clearing the draft", async () => {
    mockBootstrap.mockRejectedValue(new Error("not paired"));
    const user = userEvent.setup();
    await render(<ChatScreen />);
    await user.type(screen.getByLabelText("Demande pour l’assistant"), "Préparer ma semaine");
    expect(screen.getByTestId("send-button")).toBeDisabled();
    await user.press(screen.getByRole("button", { name: "Connecter le serveur pour envoyer" }));
    expect(mockPush).toHaveBeenCalledWith("/settings");
    expect(screen.getByDisplayValue("Préparer ma semaine")).toBeOnTheScreen();
    expect(mockSendChat).not.toHaveBeenCalled();
  });

  it("locks mode and suggestions during an in-flight request", async () => {
    const response = deferred<Awaited<ReturnType<typeof sendChat>>>();
    mockSendChat.mockReturnValue(response.promise);
    const user = userEvent.setup();
    await render(<ChatScreen />);
    await screen.findByText("Serveur connecté");
    await user.type(screen.getByLabelText("Demande pour l’assistant"), "Aide-moi à planifier");
    await user.press(screen.getByTestId("send-button"));
    expect(screen.getByTestId("interaction-mode-task")).toBeDisabled();
    expect(screen.getByRole("button", { name: "Organiser ma semaine" })).toBeDisabled();
    expect(screen.getByLabelText("Demande pour l’assistant")).toHaveProp("editable", false);
    await act(async () => response.resolve({ conversation_id: "conv_test", task: null }));
    expect(await screen.findByText("Réponse conversationnelle reçue. Aucune tâche n’a été créée.")).toBeOnTheScreen();
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

    expect(await screen.findByText("Serveur connecté")).toBeOnTheScreen();
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
    await screen.findByText("Serveur connecté");
    await user.type(screen.getByLabelText("Demande pour l’assistant"), task.input);
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
    expect(screen.getByText("Connexion au serveur à vérifier")).toBeOnTheScreen();
    await user.type(screen.getByLabelText("Demande pour l’assistant"), task.input);
    expect(screen.getByRole("button", { name: "Envoyer" })).toBeDisabled();

    mockBootstrap.mockResolvedValue(bootstrap);
    await refocusChat();

    expect(await screen.findByText("Serveur connecté")).toBeOnTheScreen();
    expect(screen.getByRole("button", { name: "Envoyer" })).toBeEnabled();
    expect(mockBootstrap).toHaveBeenCalledTimes(2);
  });

  it("ignores a stale failed refresh after a newer authenticated focus", async () => {
    const firstRefresh = deferred<Bootstrap>();
    mockBootstrap.mockReset();
    mockBootstrap.mockReturnValueOnce(firstRefresh.promise).mockResolvedValueOnce(bootstrap);
    await render(<ChatScreen />);

    await waitFor(() => expect(mockBootstrap).toHaveBeenCalledTimes(1));
    expect(screen.getByText("Connexion au serveur à vérifier")).toBeOnTheScreen();

    await refocusChat();
    expect(await screen.findByText("Serveur connecté")).toBeOnTheScreen();

    await act(async () => {
      firstRefresh.reject(new Error("Ancienne requête en échec"));
      await Promise.resolve();
    });

    expect(screen.getByText("Serveur connecté")).toBeOnTheScreen();
    expect(screen.queryByText("Connexion au serveur à vérifier")).not.toBeOnTheScreen();
  });

  it("refreshes authentication when iOS becomes active while Chat stays focused", async () => {
    mockBootstrap.mockRejectedValueOnce(new Error("Réseau suspendu"));
    const rendered = await render(<ChatScreen />);

    await waitFor(() => expect(mockBootstrap).toHaveBeenCalledTimes(1));
    expect(screen.getByText("Connexion au serveur à vérifier")).toBeOnTheScreen();

    mockBootstrap.mockResolvedValue(bootstrap);
    await act(async () => {
      mockAppStateListener?.("active");
    });

    expect(await screen.findByText("Serveur connecté")).toBeOnTheScreen();
    await rendered.unmount();
    expect(mockRemoveAppStateListener).toHaveBeenCalledTimes(1);
  });

  it("keeps a persisted proposal-only model message explicitly unverified", async () => {
    mockListMessages.mockResolvedValue([proposalOnlyMessage]);
    const user = userEvent.setup();
    await render(<ChatScreen />);

    await screen.findByText("Serveur connecté");
    await user.type(screen.getByLabelText("Demande pour l’assistant"), task.input);
    await user.press(screen.getByRole("button", { name: "Envoyer" }));

    expect(await screen.findByText("Proposition du modèle — non vérifiée")).toBeOnTheScreen();
    expect(screen.getByText(proposalOnlyMessage.content)).toBeOnTheScreen();
  });

  it("never plans an unexpected task returned to ordinary chat", async () => {
    const user = userEvent.setup();
    await render(<ChatScreen />);
    await screen.findByText("Serveur connecté");
    await user.type(screen.getByLabelText("Demande pour l’assistant"), task.input);
    await user.press(screen.getByRole("button", { name: "Envoyer" }));
    expect(await screen.findByText(/Aucun démarrage n’a été demandé depuis Discuter/)).toBeOnTheScreen();
    expect(mockSendChat).toHaveBeenCalledWith(task.input, undefined, "normal", false, expect.any(Function));
    expect(mockPlanTask).not.toHaveBeenCalled();
    expect(mockCreateGoal).not.toHaveBeenCalled();
  });

  it("entrusts a URL research request to the goal pathway without legacy task planning or implicit start", async () => {
    const user = userEvent.setup();
    await render(<ChatScreen />);
    await screen.findByText("Serveur connecté");
    await user.press(screen.getByRole("button", { name: "Confier une tâche" }));
    const objective = "Recherche la documentation officielle sur https://sqlite.org puis rédige une note de 150 à 200 mots.";
    await user.type(screen.getByLabelText("Demande pour l’assistant"), objective);
    await user.press(screen.getByRole("button", { name: "Envoyer" }));
    await waitFor(() => expect(mockPush).toHaveBeenCalledWith({ pathname: "/goal/[id]", params: { id: "goal_new" } }));
    expect(mockCreateGoal).toHaveBeenCalledWith({ objective, autonomy_profile: "autonomous", client_request_id: expect.stringMatching(/^reply_/), }, expect.any(Function));
    expect(mockSendChat).not.toHaveBeenCalled();
    expect(mockPlanTask).not.toHaveBeenCalled();
    expect(screen.getByLabelText("Demande pour l’assistant")).toHaveDisplayValue("");
    expect(startGoal).not.toHaveBeenCalled();
    expect(screen.getByRole("button", { name: "Voir le projet et ses résultats" })).toBeOnTheScreen();
  });

  it("reuses the request identity after uncertain creation without silently resending on refresh", async () => {
    mockCreateGoal.mockRejectedValueOnce(new Error("Connexion interrompue"));
    const user = userEvent.setup();
    await render(<ChatScreen />);
    await screen.findByText("Serveur connecté");
    await user.press(screen.getByRole("button", { name: "Confier une tâche" }));
    await user.type(screen.getByLabelText("Demande pour l’assistant"), "Comparer SQLite et JSON");
    await user.press(screen.getByRole("button", { name: "Envoyer" }));
    expect(await screen.findByText(/une nouvelle tentative identique retrouvera le même projet/)).toBeOnTheScreen();
    const requestId = mockCreateGoal.mock.calls[0][0].client_request_id;
    expect(screen.getByDisplayValue("Comparer SQLite et JSON")).toBeOnTheScreen();
    await refocusChat();
    expect(mockCreateGoal).toHaveBeenCalledTimes(1);
    await user.press(screen.getByRole("button", { name: "Envoyer" }));
    await waitFor(() => expect(mockCreateGoal).toHaveBeenCalledTimes(2));
    expect(mockCreateGoal.mock.calls[1][0].client_request_id).toBe(requestId);
    expect(mockSendChat).not.toHaveBeenCalled();
  });

  it("preserves conversation identity when entrusting work without flattening messages into the objective", async () => {
    mockBootstrap.mockResolvedValue({ ...bootstrap, conversations: [{ id: "conv_test", title: "Mon agenda", last_message: null, created_at: task.created_at, updated_at: task.updated_at }] });
    const user = userEvent.setup();
    await render(<ChatScreen />);
    await screen.findByText("Serveur connecté");
    await user.press(screen.getByRole("button", { name: "Historique" }));
    await user.press(screen.getByRole("button", { name: /Mon agenda/ }));
    await screen.findByText(message.content);
    await user.press(screen.getByRole("button", { name: "Confier une tâche" }));
    await user.type(screen.getByLabelText("Demande pour l’assistant"), "Prépare la comparaison discutée");
    await user.press(screen.getByRole("button", { name: "Envoyer" }));
    await waitFor(() => expect(mockCreateGoal).toHaveBeenCalledTimes(1));
    expect(mockCreateGoal.mock.calls[0][0]).toMatchObject({ objective: "Prépare la comparaison discutée", conversation_id: "conv_test" });
    expect(mockSendChat).not.toHaveBeenCalled();
  });

  it("does not submit or truncate an oversized goal objective", async () => {
    const user = userEvent.setup();
    await render(<ChatScreen />);
    await screen.findByText("Serveur connecté");
    await user.press(screen.getByRole("button", { name: "Confier une tâche" }));
    const input = "x".repeat(4001);
    await fireEvent.changeText(screen.getByLabelText("Demande pour l’assistant"), input);
    await user.press(screen.getByRole("button", { name: "Envoyer" }));
    expect(await screen.findByText(/limitée à 4 000 caractères/)).toBeOnTheScreen();
    expect(screen.getByDisplayValue(input)).toBeOnTheScreen();
    expect(mockCreateGoal).not.toHaveBeenCalled();
    expect(mockSendChat).not.toHaveBeenCalled();
  });

  it("changes request identity when the user changes an uncertain objective", async () => {
    mockCreateGoal.mockRejectedValueOnce(new Error("Réponse perdue"));
    const user = userEvent.setup();
    await render(<ChatScreen />);
    await screen.findByText("Serveur connecté");
    await user.press(screen.getByRole("button", { name: "Confier une tâche" }));
    await user.type(screen.getByLabelText("Demande pour l’assistant"), "Comparer SQLite et JSON");
    await user.press(screen.getByRole("button", { name: "Envoyer" }));
    await screen.findByText(/une nouvelle tentative identique retrouvera le même projet/);
    const previous = mockCreateGoal.mock.calls[0][0].client_request_id;
    await user.type(screen.getByLabelText("Demande pour l’assistant"), " pour un CRM");
    await user.press(screen.getByRole("button", { name: "Envoyer" }));
    await waitFor(() => expect(mockCreateGoal).toHaveBeenCalledTimes(2));
    expect(mockCreateGoal.mock.calls[1][0].client_request_id).not.toBe(previous);
  });

  it("links the pending approval count directly to the decision queue", async () => {
    mockBootstrap.mockResolvedValue({
      ...bootstrap,
      counts: { ...bootstrap.counts, approvals_pending: 2 },
    });
    const user = userEvent.setup();
    await render(<ChatScreen />);

    await user.press(await screen.findByRole("button", { name: "Voir 2 autorisations en attente" }));
    expect(mockPush).toHaveBeenCalledWith("/approvals");
  });

  it("keeps connection settings directly accessible", async () => {
    const user = userEvent.setup();
    await render(<ChatScreen />);

    await user.press(await screen.findByRole("button", { name: "Ouvrir les réglages" }));
    expect(mockPush).toHaveBeenCalledWith("/settings");
  });

  it("clears the previous pairing's conversation and authority while preserving the draft and mode", async () => {
    mockBootstrap.mockResolvedValue({ ...bootstrap, counts: { ...bootstrap.counts, approvals_pending: 2 } });
    const user = userEvent.setup();
    await render(<ChatScreen />);
    await screen.findByText("Serveur connecté · 2 autorisations en attente");
    await user.type(screen.getByLabelText("Demande pour l’assistant"), task.input);
    await user.press(screen.getByTestId("send-button"));
    await waitFor(() => expect(screen.getByTestId("interaction-mode-task")).toBeEnabled());
    expect(screen.getByText(message.content)).toBeOnTheScreen();
    expect(screen.getByRole("button", { name: "Voir la tâche et ses preuves" })).toBeOnTheScreen();
    await user.press(screen.getByTestId("interaction-mode-task"));
    await user.type(screen.getByLabelText("Demande pour l’assistant"), "Préparer mon agenda");

    await act(async () => notifyConnectionChanged());
    expect(screen.queryByText(message.content)).not.toBeOnTheScreen();
    expect(screen.queryByRole("button", { name: "Voir la tâche et ses preuves" })).not.toBeOnTheScreen();
    expect(screen.queryByRole("button", { name: "Voir 2 autorisations en attente" })).not.toBeOnTheScreen();
    expect(screen.getByText("Connexion au serveur à vérifier")).toBeOnTheScreen();
    expect(screen.getByDisplayValue("Préparer mon agenda")).toBeOnTheScreen();
    expect(screen.getByTestId("interaction-mode-task")).toHaveProp("accessibilityState", { selected: true, disabled: false });
    expect(screen.getByTestId("send-button")).toBeDisabled();
    expect(mockSendChat).toHaveBeenCalledTimes(1);

    mockSendChat.mockResolvedValueOnce({ conversation_id: "conv_new", task: null });
    mockListMessages.mockResolvedValue([]);
    await refocusChat();
    await user.press(screen.getByTestId("send-button"));
    await waitFor(() => expect(mockCreateGoal).toHaveBeenCalledTimes(1));
    expect(mockCreateGoal.mock.calls[0][0]).not.toHaveProperty("conversation_id");
    expect(mockSendChat).toHaveBeenCalledTimes(1);
  });

  it.each(["resolve", "reject"] as const)("ignores a late chat %s after pairing changes without a follow-up request", async (outcome) => {
    const pending = deferred<Awaited<ReturnType<typeof sendChat>>>();
    mockSendChat.mockReturnValueOnce(pending.promise);
    const user = userEvent.setup();
    await render(<ChatScreen />);
    await screen.findByText("Serveur connecté");
    await user.type(screen.getByLabelText("Demande pour l’assistant"), task.input);
    await user.press(screen.getByTestId("send-button"));
    const isCurrent = mockSendChat.mock.calls[0]?.[4];
    expect(isCurrent?.()).toBe(true);

    await act(async () => notifyConnectionChanged());
    expect(isCurrent?.()).toBe(false);
    await act(async () => {
      if (outcome === "resolve") pending.resolve({ conversation_id: "conv_test", task });
      else pending.reject(new Error("Ancienne requête en échec"));
    });
    expect(screen.getByDisplayValue(task.input)).toBeOnTheScreen();
    expect(screen.getByText(/Une opération déjà envoyée peut se poursuivre/)).toBeOnTheScreen();
    expect(screen.queryByText("Ancienne requête en échec")).not.toBeOnTheScreen();
    expect(screen.queryByRole("button", { name: "Voir la tâche et ses preuves" })).not.toBeOnTheScreen();
    expect(mockSendChat).toHaveBeenCalledTimes(1);
    expect(mockListMessages).not.toHaveBeenCalled();
    expect(mockPlanTask).not.toHaveBeenCalled();
    expect(mockBootstrap).toHaveBeenCalledTimes(1);
    expect(mockPush).not.toHaveBeenCalled();
  });

  it("restores the submitted draft and never starts a plan after a late message read", async () => {
    const pending = deferred<Message[]>();
    mockListMessages.mockReturnValueOnce(pending.promise);
    const user = userEvent.setup();
    await render(<ChatScreen />);
    await screen.findByText("Serveur connecté");
    await user.type(screen.getByLabelText("Demande pour l’assistant"), task.input);
    await user.press(screen.getByTestId("send-button"));
    await waitFor(() => expect(mockListMessages).toHaveBeenCalledTimes(1));
    expect(screen.getByLabelText("Demande pour l’assistant")).toHaveDisplayValue("");
    const isCurrent = mockListMessages.mock.calls[0]?.[1];

    await act(async () => notifyConnectionChanged());
    expect(isCurrent?.()).toBe(false);
    await act(async () => pending.resolve([message]));
    expect(screen.queryByText(message.content)).not.toBeOnTheScreen();
    expect(screen.getByDisplayValue(task.input)).toBeOnTheScreen();
    expect(mockSendChat).toHaveBeenCalledTimes(1);
    expect(mockListMessages).toHaveBeenCalledTimes(1);
    expect(mockPlanTask).not.toHaveBeenCalled();
    expect(mockBootstrap).toHaveBeenCalledTimes(1);
  });

  it("discards a late goal creation response after pairing changes and preserves the draft", async () => {
    const pending = deferred<GoalDetail>();
    mockCreateGoal.mockReturnValueOnce(pending.promise);
    const user = userEvent.setup();
    await render(<ChatScreen />);
    await screen.findByText("Serveur connecté");
    await user.press(screen.getByRole("button", { name: "Confier une tâche" }));
    await user.type(screen.getByLabelText("Demande pour l’assistant"), task.input);
    await user.press(screen.getByRole("button", { name: "Envoyer" }));
    const accepts = mockCreateGoal.mock.calls[0][1]!;
    expect(accepts()).toBe(true);
    await act(async () => notifyConnectionChanged());
    expect(accepts()).toBe(false);
    await act(async () => pending.resolve(goal));
    expect(screen.getByDisplayValue(task.input)).toBeOnTheScreen();
    expect(mockPush).not.toHaveBeenCalled();
    expect(mockCreateGoal).toHaveBeenCalledTimes(1);
    expect(mockBootstrap).toHaveBeenCalledTimes(1);
    expect(mockListMessages).not.toHaveBeenCalled();
  });

  it("does not let an old submission's finally unlock a newer request on the replacement pairing", async () => {
    const oldResponse = deferred<Awaited<ReturnType<typeof sendChat>>>();
    const newResponse = deferred<Awaited<ReturnType<typeof sendChat>>>();
    mockSendChat.mockReturnValueOnce(oldResponse.promise).mockReturnValueOnce(newResponse.promise);
    const user = userEvent.setup();
    await render(<ChatScreen />);
    await screen.findByText("Serveur connecté");
    await user.type(screen.getByLabelText("Demande pour l’assistant"), task.input);
    await user.press(screen.getByTestId("send-button"));
    await act(async () => notifyConnectionChanged());
    await refocusChat();
    await user.press(screen.getByTestId("send-button"));
    await waitFor(() => expect(mockSendChat).toHaveBeenCalledTimes(2));

    await act(async () => oldResponse.resolve({ conversation_id: "conv_test", task }));
    expect(screen.getByTestId("send-button")).toBeDisabled();
    expect(screen.getByLabelText("Demande pour l’assistant")).toHaveProp("editable", false);
    expect(mockListMessages).not.toHaveBeenCalled();
    expect(mockPlanTask).not.toHaveBeenCalled();
    expect(mockBootstrap).toHaveBeenCalledTimes(2);
    expect(mockSendChat.mock.calls[1]?.[4]?.()).toBe(true);

    mockListMessages.mockResolvedValue([]);
    await act(async () => newResponse.resolve({ conversation_id: "conv_new", task: null }));
    expect(await screen.findByText("Réponse conversationnelle reçue. Aucune tâche n’a été créée.")).toBeOnTheScreen();
    await waitFor(() => expect(screen.getByLabelText("Demande pour l’assistant")).toHaveProp("editable", true));
    expect(mockSendChat).toHaveBeenCalledTimes(2);
    expect(mockListMessages).toHaveBeenCalledTimes(2);
    expect(mockListMessages).toHaveBeenLastCalledWith("conv_new", expect.any(Function));
  });

  it("invalidates a pending authentication refresh as soon as the pairing changes", async () => {
    const pending = deferred<Bootstrap>();
    mockBootstrap.mockReturnValueOnce(pending.promise);
    await render(<ChatScreen />);
    await waitFor(() => expect(mockBootstrap).toHaveBeenCalledTimes(1));
    const isCurrent = mockBootstrap.mock.calls[0]?.[0];
    await act(async () => notifyConnectionChanged());
    expect(isCurrent?.()).toBe(false);
    await act(async () => pending.resolve(bootstrap));
    expect(screen.getByText("Connexion au serveur à vérifier")).toBeOnTheScreen();
    expect(screen.getByTestId("send-button")).toBeDisabled();
    expect(mockSendChat).not.toHaveBeenCalled();
  });

  it("resumes a selected conversation and sends its identifier", async () => {
    mockBootstrap.mockResolvedValue({ ...bootstrap, conversations: [{ id: "conv_test", title: "Mon agenda", last_message: "Déjà prévu", created_at: task.created_at, updated_at: task.updated_at }] });
    mockSendChat.mockResolvedValue({ conversation_id: "conv_test", task: null });
    const user = userEvent.setup();
    await render(<ChatScreen />);
    await screen.findByText("Serveur connecté");
    await user.press(screen.getByTestId("open-chat-history"));
    await user.press(screen.getByRole("button", { name: "Reprendre Mon agenda" }));
    expect(await screen.findByText(message.content)).toBeOnTheScreen();
    expect(mockListMessages).toHaveBeenCalledWith("conv_test", expect.any(Function));
    await user.type(screen.getByLabelText("Demande pour l’assistant"), "Ajoute mes priorités");
    await user.press(screen.getByTestId("send-button"));
    await waitFor(() => expect(mockSendChat).toHaveBeenCalledWith("Ajoute mes priorités", "conv_test", "normal", false, expect.any(Function)));
  });

  it("preserves drafts per conversation and does not resurrect a sent new-chat draft", async () => {
    mockBootstrap.mockResolvedValue({ ...bootstrap, conversations: [{ id: "conv_test", title: "Mon agenda", last_message: null, created_at: task.created_at, updated_at: task.updated_at }] });
    mockSendChat.mockResolvedValue({ conversation_id: "conv_sent", task: null });
    const user = userEvent.setup();
    await render(<ChatScreen />);
    await screen.findByText("Serveur connecté");
    await user.type(screen.getByLabelText("Demande pour l’assistant"), "Brouillon nouveau");
    await user.press(screen.getByTestId("open-chat-history"));
    await user.press(screen.getByRole("button", { name: "Reprendre Mon agenda" }));
    await screen.findByText(message.content);
    await user.type(screen.getByLabelText("Demande pour l’assistant"), "Brouillon agenda");
    await user.press(screen.getByTestId("new-chat-button"));
    expect(screen.getByLabelText("Demande pour l’assistant")).toHaveDisplayValue("Brouillon nouveau");
    await user.press(screen.getByTestId("send-button"));
    await screen.findByText("Réponse conversationnelle reçue. Aucune tâche n’a été créée.");
    await waitFor(() => expect(screen.getByTestId("new-chat-button")).not.toBeDisabled());
    await user.press(screen.getByTestId("new-chat-button"));
    expect(screen.getByLabelText("Demande pour l’assistant")).toHaveDisplayValue("");
    await user.press(screen.getByTestId("open-chat-history"));
    await user.press(screen.getByRole("button", { name: "Reprendre Mon agenda" }));
    expect(screen.getByLabelText("Demande pour l’assistant")).toHaveDisplayValue("Brouillon agenda");
    expect(mockSendChat).toHaveBeenCalledTimes(1);
  });

  it("discards a late history read after pairing changes and keeps its draft", async () => {
    mockBootstrap.mockResolvedValue({ ...bootstrap, conversations: [{ id: "conv_test", title: "Mon agenda", last_message: null, created_at: task.created_at, updated_at: task.updated_at }] });
    const pending = deferred<Message[]>();
    mockListMessages.mockReturnValueOnce(pending.promise);
    const user = userEvent.setup();
    await render(<ChatScreen />);
    await screen.findByText("Serveur connecté");
    await user.press(screen.getByTestId("open-chat-history"));
    await user.press(screen.getByRole("button", { name: "Reprendre Mon agenda" }));
    expect(screen.getByText("Chargement de la discussion…")).toBeOnTheScreen();
    await act(async () => notifyConnectionChanged());
    await act(async () => pending.resolve([message]));
    expect(screen.queryByText(message.content)).not.toBeOnTheScreen();
    expect(screen.queryByText("Chargement de la discussion…")).not.toBeOnTheScreen();
    expect(screen.getByTestId("send-button")).toBeDisabled();
    expect(mockSendChat).not.toHaveBeenCalled();
  });

  it("does not let an earlier history read overwrite a newer live read", async () => {
    mockBootstrap.mockResolvedValue({ ...bootstrap, conversations: [{ id: "conv_test", title: "Mon agenda", last_message: null, created_at: task.created_at, updated_at: task.updated_at }] });
    const pending = deferred<Message[]>();
    mockListMessages.mockReturnValueOnce(pending.promise).mockResolvedValue([{ ...message, content: "Message plus récent" }]);
    const user = userEvent.setup();
    const view = (revision: number) => <LiveSyncContextProvider value={{ error: null, revision, state: "connected" }}><ChatScreen /></LiveSyncContextProvider>;
    const rendered = await render(view(0));
    await screen.findByText("Serveur connecté");
    await user.press(screen.getByTestId("open-chat-history"));
    await user.press(screen.getByRole("button", { name: "Reprendre Mon agenda" }));
    await rendered.rerender(view(1));
    expect(await screen.findByText("Message plus récent")).toBeOnTheScreen();
    await act(async () => pending.resolve([message]));
    expect(screen.queryByText(message.content)).not.toBeOnTheScreen();
    expect(screen.queryByText("Chargement de la discussion…")).not.toBeOnTheScreen();
    expect(screen.getByText("Message plus récent")).toBeOnTheScreen();
  });

  it("clears a superseded refresh spinner when switching conversation", async () => {
    const withHistory = { ...bootstrap, conversations: [{ id: "conv_test", title: "Mon agenda", last_message: null, created_at: task.created_at, updated_at: task.updated_at }] };
    mockBootstrap.mockResolvedValue(withHistory);
    const user = userEvent.setup();
    await render(<ChatScreen />);
    await screen.findByText("Serveur connecté");
    const pending = deferred<Bootstrap>();
    mockBootstrap.mockReturnValueOnce(pending.promise);
    await refocusChat();
    await user.press(screen.getByTestId("open-chat-history"));
    await user.press(screen.getByRole("button", { name: "Reprendre Mon agenda" }));
    await screen.findByText(message.content);
    await act(async () => pending.resolve(withHistory));
    expect(screen.getByTestId("chat-screen").props.refreshControl.props.refreshing).toBe(false);
  });

  it("offers explicit project creation without submitting the current chat draft", async () => {
    const user = userEvent.setup();
    await render(<ChatScreen />);
    await screen.findByText("Serveur connecté");
    await user.type(screen.getByLabelText("Demande pour l’assistant"), "Garder ma note");
    await user.press(screen.getByTestId("chat-new-project"));
    expect(mockPush).toHaveBeenCalledWith({ pathname: "/swarm", params: { create: "1" } });
    expect(screen.getByDisplayValue("Garder ma note")).toBeOnTheScreen();
    expect(mockSendChat).not.toHaveBeenCalled();
  });

});
