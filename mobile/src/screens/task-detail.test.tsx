import { act, render, screen, userEvent, waitFor, within } from "@testing-library/react-native";
import { afterEach, beforeEach, describe, expect, it, jest } from "@jest/globals";
import { Alert } from "react-native";

import TaskDetailScreen from "@/../app/task/[id]";
import {
  cancelTask,
  decideApproval,
  getServerUrl,
  getTask,
  type Approval,
  type ApprovalDecisionReceipt,
  type ApprovalDecisionResult,
  type TaskDetail,
  type ToolCall,
} from "@/lib/api/client";
import { localApprovals, localTask } from "@/lib/state/replica";

jest.mock("expo-router", () => ({ useLocalSearchParams: () => ({ id: "tsk_test" }) }));
jest.mock("@/lib/api/client", () => ({
  ApiError: Error,
  cancelTask: jest.fn(),
  createFeedback: jest.fn(),
  decideApproval: jest.fn(),
  getServerUrl: jest.fn(),
  getTask: jest.fn(),
  planTask: jest.fn(),
}));
jest.mock("@/lib/state/replica", () => ({
  localApprovals: jest.fn(),
  localTask: jest.fn(),
}));

const detail: TaskDetail = {
  task: {
    id: "tsk_test",
    title: "Inspecter le projet",
    input: "Inspecter le projet",
    mode: "normal",
    source: "test-phone",
    conversation_id: null,
    status: "planned",
    priority: 0,
    created_at: "2026-09-04T12:00:00Z",
    updated_at: "2026-09-04T12:00:00Z",
    completed_at: null,
    error_json: null,
  },
  messages: [],
  approvals: [],
  tool_calls: [],
};

const proposalOnlyMessage: TaskDetail["messages"][number] = {
  id: "msg_proposal_only",
  conversation_id: "conv_test",
  task_id: detail.task.id,
  role: "agent",
  agent_id: "local-orchestrator",
  content: "The release is complete",
  metadata: { verified_status: "proposal_only" },
  created_at: detail.task.created_at,
};

const mockCancelTask = jest.mocked(cancelTask);
const mockDecideApproval = jest.mocked(decideApproval);
const mockGetTask = jest.mocked(getTask);
const mockGetServerUrl = jest.mocked(getServerUrl);
const mockLocalApprovals = jest.mocked(localApprovals);
const mockLocalTask = jest.mocked(localTask);
const hiddenWriteContent = "private data";

function decisionReceipt(
  authoritativeResult: ApprovalDecisionResult,
  localReplicaError: string | null = null,
): ApprovalDecisionReceipt {
  return { authoritativeResult, localReplicaError };
}

const approval: Approval = {
  id: "apr_detail",
  task_id: "tsk_test",
  tool_call_id: "call_detail",
  action_digest: `sha256:${"b".repeat(64)}`,
  action: "workspace.write_text",
  action_preview: {
    operation: "Write workspace text",
    target: "release/result.txt",
    details: ["12 UTF-8 bytes; content hidden"],
    arguments_redacted: true,
  },
  binding_valid: true,
  requester: { type: "device", id: "iphone-detail", name: "Ales iPhone" },
  policy: {
    rule_id: "ask-workspace-write",
    decision: "ask",
    reason: "Writing workspace file content requires explicit one-use approval.",
  },
  affected_data_summary: "Writes 12 UTF-8 bytes to release/result.txt; content hidden.",
  audit_id: 84,
  consent_context_valid: true,
  summary: "Write text to a workspace file",
  risk: "medium",
  status: "pending",
  created_at: "2026-09-04T12:00:00Z",
  expires_at: "2099-09-04T12:05:00Z",
  decided_at: null,
  decision: null,
};

const failedToolCall: ToolCall = {
  id: approval.tool_call_id,
  task_id: approval.task_id,
  tool_name: "workspace.write_text",
  arguments: {
    path: "release/result.txt",
    content: `<redacted: ${hiddenWriteContent.length} UTF-8 bytes>`,
    arguments_redacted: true,
  },
  summary: approval.summary,
  risk: approval.risk,
  status: "failed",
  approval_id: approval.id,
  result: null,
  error: "sandbox unavailable",
  created_at: approval.created_at,
  updated_at: approval.created_at,
};

const hiddenProcessOutput = "credential-like-runtime-output";
const failedProcessToolCall: ToolCall = {
  id: "call_process_failure",
  task_id: approval.task_id,
  tool_name: "process.run",
  arguments: {
    argv: ["<redacted>"],
    argument_count: 3,
    arguments_redacted: true,
  },
  summary: "Run a sandboxed process",
  risk: "high",
  status: "failed",
  approval_id: "apr_process_failure",
  result: {
    output_redacted: true,
    returncode: 1,
    stdout: `<redacted: ${hiddenProcessOutput.length} UTF-8 bytes>`,
    stderr: "<redacted: 0 UTF-8 bytes>",
    stdout_truncated: false,
    stderr_truncated: false,
    sandbox: "bubblewrap",
    network: "denied",
  },
  error: "sandboxed process failed; detailed error retained locally",
  created_at: approval.created_at,
  updated_at: approval.created_at,
};

describe("TaskDetailScreen", () => {
  beforeEach(() => {
    jest.clearAllMocks();
    mockGetServerUrl.mockResolvedValue("https://control.example");
    mockGetTask.mockResolvedValue(detail);
    mockLocalApprovals.mockResolvedValue([]);
    mockLocalTask.mockResolvedValue(null);
    mockCancelTask.mockResolvedValue({ ...detail.task, status: "cancelled" });
    mockDecideApproval.mockResolvedValue(decisionReceipt(approval));
  });

  afterEach(() => {
    jest.restoreAllMocks();
  });

  it("shows an explicit initial loading state without claiming the task is missing", async () => {
    let resolveRequest!: (value: TaskDetail) => void;
    mockGetTask.mockImplementation(
      () =>
        new Promise<TaskDetail>((resolve) => {
          resolveRequest = resolve;
        }),
    );

    await render(<TaskDetailScreen />);

    expect(screen.getByText("Chargement des preuves authentifiées…")).toBeOnTheScreen();
    expect(screen.queryByText("Tâche introuvable")).not.toBeOnTheScreen();

    await act(() => {
      resolveRequest(detail);
    });
    expect(screen.queryByText("Chargement des preuves authentifiées…")).not.toBeOnTheScreen();
  });

  it("shows cached task evidence read-only when authenticated refresh fails", async () => {
    mockGetTask.mockRejectedValue(new Error("Control plane indisponible"));
    mockLocalTask.mockResolvedValue({ ...detail.task, status: "waiting_permission" });
    mockLocalApprovals.mockResolvedValue([approval]);
    const user = userEvent.setup();

    await render(<TaskDetailScreen />);

    expect(await screen.findByText(/Copie locale hors ligne/)).toBeOnTheScreen();
    expect(screen.getByTestId("detail-allow-button")).toBeDisabled();
    expect(screen.getByTestId("detail-deny-button")).toBeDisabled();
    expect(screen.queryByRole("button", { name: "Annuler la tâche" })).not.toBeOnTheScreen();
    await user.press(screen.getByTestId("detail-allow-button"));
    expect(mockDecideApproval).not.toHaveBeenCalled();
  });

  it("locks previously loaded evidence after a refresh loses authentication", async () => {
    mockGetTask
      .mockResolvedValueOnce({ ...detail, approvals: [approval] })
      .mockRejectedValueOnce(new Error("Session indisponible"));
    const user = userEvent.setup();

    await render(<TaskDetailScreen />);
    expect(await screen.findByTestId("detail-allow-button")).toBeEnabled();

    await user.press(screen.getByRole("button", { name: "Actualiser les preuves" }));

    expect(await screen.findByText(/Copie locale hors ligne/)).toBeOnTheScreen();
    expect(screen.getByTestId("detail-allow-button")).toBeDisabled();
    expect(screen.getByTestId("detail-deny-button")).toBeDisabled();
    expect(screen.queryByRole("button", { name: "Annuler la tâche" })).not.toBeOnTheScreen();
  });

  it("requires explicit confirmation before cancelling a task", async () => {
    const alert = jest.spyOn(Alert, "alert").mockImplementation(() => undefined);
    const user = userEvent.setup();
    await render(<TaskDetailScreen />);

    await user.press(await screen.findByRole("button", { name: "Annuler la tâche" }));
    expect(mockCancelTask).not.toHaveBeenCalled();
    expect(alert).toHaveBeenCalledWith(
      "Annuler cette tâche?",
      expect.stringContaining("preuves déjà enregistrés resteront consultables"),
      expect.any(Array),
    );

    const actions = alert.mock.calls[0][2];
    const destructive = actions?.find((action) => action.style === "destructive");
    await act(async () => {
      destructive?.onPress?.();
    });

    await waitFor(() => expect(mockCancelTask).toHaveBeenCalledWith("tsk_test"));
  });

  it("separates authoritative consent evidence from the model summary", async () => {
    mockGetTask.mockResolvedValue({ ...detail, approvals: [approval] });
    const user = userEvent.setup();
    await render(<TaskDetailScreen />);

    expect(await screen.findByText("Cible exacte : release/result.txt")).toBeOnTheScreen();
    expect(screen.getByText(`Empreinte sha256:${"b".repeat(64)}`)).toBeOnTheScreen();
    const trusted = screen.getByTestId("approval-trusted-context-apr_detail");
    const model = screen.getByTestId("approval-model-context-apr_detail");
    expect(within(trusted).getByText(/Ales iPhone \(device iphone-detail\)/)).toBeOnTheScreen();
    expect(
      within(trusted).getByText(/Writing workspace file content requires explicit one-use approval/),
    ).toBeOnTheScreen();
    expect(within(trusted).getByText(/Writes 12 UTF-8 bytes to release\/result.txt/)).toBeOnTheScreen();
    expect(within(trusted).getByText(/Audit : 84/)).toBeOnTheScreen();
    expect(within(trusted).queryByText(approval.summary)).not.toBeOnTheScreen();
    expect(within(model).getByText(`Libellé public : ${approval.summary}`)).toBeOnTheScreen();

    await user.press(screen.getByTestId("detail-allow-button"));
    await waitFor(() => expect(mockDecideApproval).toHaveBeenCalledWith("apr_detail", "approve"));
  });

  it("keeps a persisted proposal-only timeline message explicitly unverified", async () => {
    mockGetTask.mockResolvedValue({ ...detail, messages: [proposalOnlyMessage] });
    await render(<TaskDetailScreen />);

    expect(await screen.findByText("Proposition du modèle — non vérifiée")).toBeOnTheScreen();
    expect(screen.getByText(proposalOnlyMessage.content)).toBeOnTheScreen();
  });

  it("labels persisted tool summaries as unverified and arguments as redacted public data", async () => {
    mockGetTask.mockResolvedValue({ ...detail, tool_calls: [failedToolCall] });
    await render(<TaskDetailScreen />);

    expect(await screen.findByText("Libellé public généré par le serveur")).toBeOnTheScreen();
    expect(screen.getByText("Arguments publics expurgés")).toBeOnTheScreen();
    expect(screen.getByText(/"content": "<redacted: 12 UTF-8 bytes>"/)).toBeOnTheScreen();
    expect(screen.queryByText(hiddenWriteContent)).not.toBeOnTheScreen();
  });

  it("renders only redacted verified evidence for a failed process", async () => {
    mockGetTask.mockResolvedValue({ ...detail, tool_calls: [failedProcessToolCall] });
    await render(<TaskDetailScreen />);

    expect(await screen.findByText("Libellé public généré par le serveur")).toBeOnTheScreen();
    expect(screen.getByText("Résultat public vérifié")).toBeOnTheScreen();
    expect(
      screen.getByText(new RegExp(`<redacted: ${hiddenProcessOutput.length} UTF-8 bytes>`)),
    ).toBeOnTheScreen();
    expect(
      screen.getByText("sandboxed process failed; detailed error retained locally"),
    ).toBeOnTheScreen();
    expect(screen.queryByText(hiddenProcessOutput)).not.toBeOnTheScreen();
  });

  it("disables approval when the server reports an invalid binding", async () => {
    mockGetTask.mockResolvedValue({
      ...detail,
      approvals: [{ ...approval, binding_valid: false }],
    });
    const user = userEvent.setup();
    await render(<TaskDetailScreen />);

    expect(await screen.findByText("Liaison invalide : cet accord ne peut pas être autorisé.")).toBeOnTheScreen();
    await user.press(screen.getByTestId("detail-allow-button"));
    expect(mockDecideApproval).not.toHaveBeenCalled();
  });

  it("announces a failed linked execution after deciding from task detail", async () => {
    mockGetTask.mockResolvedValue({ ...detail, approvals: [approval] });
    mockDecideApproval.mockResolvedValue(
      decisionReceipt({
        approval: { ...approval, status: "approved" },
        tool_call: failedToolCall,
      }),
    );
    const user = userEvent.setup();
    await render(<TaskDetailScreen />);

    await user.press(await screen.findByTestId("detail-allow-button"));

    expect(await screen.findByText("Échec de l’appel lié: sandbox unavailable")).toBeOnTheScreen();
    expect(
      screen.getByText("Autorisation unique consommée; l’exécution n’a pas réussi."),
    ).toBeOnTheScreen();
  });

  it("retains the server 409 recovery path and refreshes task evidence", async () => {
    mockGetTask
      .mockResolvedValueOnce({ ...detail, approvals: [approval] })
      .mockResolvedValueOnce({
        ...detail,
        approvals: [{ ...approval, status: "expired" }],
      });
    mockDecideApproval.mockRejectedValue(Object.assign(new Error("conflict"), { status: 409 }));
    const user = userEvent.setup();
    await render(<TaskDetailScreen />);

    await user.press(await screen.findByTestId("detail-allow-button"));

    expect(
      await screen.findByText("L’état a changé avant cette action. La tâche a été actualisée."),
    ).toBeOnTheScreen();
    await waitFor(() => expect(mockGetTask).toHaveBeenCalledTimes(2));
    expect(screen.queryByTestId("detail-allow-button")).not.toBeOnTheScreen();
  });

  it("locks a conflicted decision when refresh fails until later authenticated evidence arrives", async () => {
    mockGetTask
      .mockResolvedValueOnce({ ...detail, approvals: [approval] })
      .mockRejectedValueOnce(new Error("Actualisation des preuves indisponible"))
      .mockResolvedValueOnce({
        ...detail,
        approvals: [{ ...approval, status: "expired" }],
      });
    mockDecideApproval.mockRejectedValue(Object.assign(new Error("conflict"), { status: 409 }));
    const user = userEvent.setup();
    await render(<TaskDetailScreen />);

    const allow = await screen.findByTestId("detail-allow-button");
    await user.press(allow);

    expect(
      await screen.findByText(/Impossible d’actualiser les preuves authentifiées : Actualisation des preuves indisponible/),
    ).toBeOnTheScreen();
    expect(
      screen.getByText(/Décision déjà transmise : cette carte reste verrouillée/),
    ).toBeOnTheScreen();
    expect(allow).toBeDisabled();
    expect(screen.getByTestId("detail-deny-button")).toBeDisabled();
    expect(screen.queryByText("L’état a changé avant cette action. La tâche a été actualisée.")).not.toBeOnTheScreen();
    await user.press(allow);
    expect(mockDecideApproval).toHaveBeenCalledTimes(1);

    await user.press(screen.getByRole("button", { name: "Actualiser les preuves" }));
    await waitFor(() => expect(mockGetTask).toHaveBeenCalledTimes(3));
    expect(screen.queryByTestId("detail-allow-button")).not.toBeOnTheScreen();
  });

  it("locks a server decision when both the local replica and evidence refresh fail", async () => {
    mockGetTask
      .mockResolvedValueOnce({ ...detail, approvals: [approval] })
      .mockRejectedValueOnce(new Error("Actualisation des preuves indisponible"))
      .mockResolvedValueOnce({
        ...detail,
        approvals: [{ ...approval, status: "approved" }],
      });
    mockDecideApproval.mockResolvedValue(
      decisionReceipt(
        {
          ...approval,
          status: "approved",
          decided_at: "2026-09-04T12:01:00Z",
          decision: { decision: "approve", actor_id: "iphone-detail", user_note: null },
        },
        "Réplique SQLite indisponible",
      ),
    );
    const user = userEvent.setup();
    await render(<TaskDetailScreen />);

    const allow = await screen.findByTestId("detail-allow-button");
    await user.press(allow);

    expect(
      await screen.findByText(
        /décision a été enregistrée par le serveur.*Réplique SQLite indisponible.*Impossible d’actualiser les preuves authentifiées : Actualisation des preuves indisponible/,
      ),
    ).toBeOnTheScreen();
    expect(
      screen.getByText(/Décision déjà transmise : cette carte reste verrouillée/),
    ).toBeOnTheScreen();
    expect(allow).toBeDisabled();
    expect(screen.getByTestId("detail-deny-button")).toBeDisabled();
    await user.press(allow);
    expect(mockDecideApproval).toHaveBeenCalledTimes(1);

    await user.press(screen.getByRole("button", { name: "Actualiser les preuves" }));
    await waitFor(() => expect(mockGetTask).toHaveBeenCalledTimes(3));
    expect(screen.queryByTestId("detail-allow-button")).not.toBeOnTheScreen();
  });
});
