import { render, screen, userEvent, waitFor } from "@testing-library/react-native";
import { beforeEach, describe, expect, it, jest } from "@jest/globals";

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

const mockPush = jest.fn();

jest.mock("expo-router", () => ({ useRouter: () => ({ push: mockPush }) }));
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

describe("ChatScreen", () => {
  beforeEach(() => {
    jest.clearAllMocks();
    mockBootstrap.mockResolvedValue(bootstrap);
    mockListMessages.mockResolvedValue([message]);
    mockSendChat.mockResolvedValue({ conversation_id: "conv_test", task });
    mockPlanTask.mockResolvedValue({
      task_id: task.id,
      proposal: { tool_name: "none", arguments: {}, summary: "Proposal only" },
      task: { ...task, status: "planned" },
    });
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
