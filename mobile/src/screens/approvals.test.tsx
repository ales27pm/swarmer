import { render, screen, userEvent, waitFor, within } from "@testing-library/react-native";
import { beforeEach, describe, expect, it, jest } from "@jest/globals";

import ApprovalsScreen from "@/../app/(main)/approvals";
import {
  decideApproval,
  getServerUrl,
  listApprovals,
  type Approval,
  type ApprovalDecisionReceipt,
  type ApprovalDecisionResult,
  type ToolCall,
} from "@/lib/api/client";
import { localApprovals } from "@/lib/state/replica";

const mockPush = jest.fn();

jest.mock("expo-router", () => ({ useRouter: () => ({ push: mockPush }) }));

jest.mock("@/lib/api/client", () => ({
  ApiError: Error,
  decideApproval: jest.fn(),
  getServerUrl: jest.fn(),
  listApprovals: jest.fn(),
}));
jest.mock("@/lib/state/replica", () => ({ localApprovals: jest.fn() }));

const approval: Approval = {
  id: "apr_test",
  task_id: "tsk_test",
  tool_call_id: "call_test",
  action_digest: `sha256:${"a".repeat(64)}`,
  action: "workspace.write_text",
  action_preview: {
    operation: "Write workspace text",
    target: "notes/result.txt",
    details: ["8 UTF-8 bytes; content hidden"],
    arguments_redacted: true,
  },
  binding_valid: true,
  requester: { type: "device", id: "iphone-15", name: "Ales iPhone" },
  policy: {
    rule_id: "ask-workspace-write",
    decision: "ask",
    reason: "Writing workspace file content requires explicit one-use approval.",
  },
  affected_data_summary: "Writes 8 UTF-8 bytes to notes/result.txt; content hidden.",
  audit_id: 42,
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
  id: "call_test",
  task_id: approval.task_id,
  tool_name: "workspace.write_text",
  arguments: {
    path: "notes/result.txt",
    content: "<redacted: 8 UTF-8 bytes>",
    arguments_redacted: true,
  },
  summary: approval.summary,
  risk: "medium",
  status: "failed",
  approval_id: approval.id,
  result: null,
  error: "disk full",
  created_at: approval.created_at,
  updated_at: approval.created_at,
};

const mockListApprovals = jest.mocked(listApprovals);
const mockDecideApproval = jest.mocked(decideApproval);
const mockGetServerUrl = jest.mocked(getServerUrl);
const mockLocalApprovals = jest.mocked(localApprovals);

function decisionReceipt(
  authoritativeResult: ApprovalDecisionResult,
  localReplicaError: string | null = null,
): ApprovalDecisionReceipt {
  return { authoritativeResult, localReplicaError };
}

describe("ApprovalsScreen", () => {
  beforeEach(() => {
    jest.clearAllMocks();
    mockGetServerUrl.mockResolvedValue("https://control.example");
    mockListApprovals.mockResolvedValue([approval]);
    mockDecideApproval.mockResolvedValue(decisionReceipt(approval));
    mockLocalApprovals.mockRejectedValue(new Error("Cache indisponible"));
  });

  it("presents an explicit one-shot decision and sends it once", async () => {
    const user = userEvent.setup();
    await render(<ApprovalsScreen />);

    const allow = await screen.findByRole("button", { name: /^Autoriser une fois/ });
    expect(screen.getByText("Cible exacte : notes/result.txt")).toBeOnTheScreen();
    expect(screen.getByText(`Empreinte sha256:${"a".repeat(64)}`)).toBeOnTheScreen();
    const trusted = screen.getByTestId("approval-trusted-context-apr_test");
    const model = screen.getByTestId("approval-model-context-apr_test");
    expect(within(trusted).getByText(/Ales iPhone \(device iphone-15\)/)).toBeOnTheScreen();
    expect(
      within(trusted).getByText(/Writing workspace file content requires explicit one-use approval/),
    ).toBeOnTheScreen();
    expect(within(trusted).getByText(/Writes 8 UTF-8 bytes to notes\/result.txt/)).toBeOnTheScreen();
    expect(within(trusted).getByText(/Audit : 42/)).toBeOnTheScreen();
    expect(within(trusted).queryByText(approval.summary)).not.toBeOnTheScreen();
    expect(within(model).getByText(`Libellé public : ${approval.summary}`)).toBeOnTheScreen();
    await user.press(allow);

    await waitFor(() => expect(mockDecideApproval).toHaveBeenCalledTimes(1));
    expect(mockDecideApproval).toHaveBeenCalledWith("apr_test", "approve");
    await user.press(await screen.findByRole("button", { name: "Voir le résultat et les preuves" }));
    expect(mockPush).toHaveBeenCalledWith({
      pathname: "/task/[id]",
      params: { id: "tsk_test" },
    });
  });

  it("surfaces a failed linked call instead of claiming execution success", async () => {
    mockDecideApproval.mockResolvedValue(
      decisionReceipt({
        approval: { ...approval, status: "approved" },
        tool_call: failedToolCall,
      }),
    );
    const user = userEvent.setup();
    await render(<ApprovalsScreen />);

    await user.press(await screen.findByRole("button", { name: /^Autoriser une fois/ }));

    expect(await screen.findByText("Échec de l’appel lié: disk full")).toBeOnTheScreen();
    expect(screen.getByText("Autorisation unique consommée; l’exécution n’a pas réussi.")).toBeOnTheScreen();
    expect(screen.queryByText(/résultat d’exécution vérifié/)).not.toBeOnTheScreen();
  });

  it("retains the server 409 recovery path and refreshes authoritative state", async () => {
    mockListApprovals.mockResolvedValueOnce([approval]).mockResolvedValueOnce([]);
    mockDecideApproval.mockRejectedValue(Object.assign(new Error("conflict"), { status: 409 }));
    const user = userEvent.setup();
    await render(<ApprovalsScreen />);

    await user.press(await screen.findByRole("button", { name: /^Autoriser une fois/ }));

    expect(
      await screen.findByText(
        "Cette autorisation a déjà été décidée, a expiré ou a été annulée. La liste a été actualisée.",
      ),
    ).toBeOnTheScreen();
    await waitFor(() => expect(mockListApprovals).toHaveBeenCalledTimes(2));
    expect(screen.queryByTestId("approval-card-apr_test")).not.toBeOnTheScreen();
  });

  it("locks a server decision when both the local replica and refresh fail", async () => {
    mockListApprovals
      .mockResolvedValueOnce([approval])
      .mockRejectedValueOnce(new Error("Actualisation des accords indisponible"))
      .mockResolvedValueOnce([]);
    mockDecideApproval.mockResolvedValue(
      decisionReceipt(
        {
          ...approval,
          status: "approved",
          decided_at: "2026-09-04T12:01:00Z",
          decision: { decision: "approve", actor_id: "iphone-15", user_note: null },
        },
        "Réplique SQLite indisponible",
      ),
    );
    const user = userEvent.setup();
    await render(<ApprovalsScreen />);

    const allow = await screen.findByRole("button", { name: /^Autoriser une fois/ });
    await user.press(allow);

    expect(
      await screen.findByText(
        /décision a été enregistrée par le serveur.*Réplique SQLite indisponible.*Impossible d’actualiser l’état authentifié : Actualisation des accords indisponible/,
      ),
    ).toBeOnTheScreen();
    expect(
      screen.getByText(/Décision déjà transmise : cette carte reste verrouillée/),
    ).toBeOnTheScreen();
    expect(allow).toBeDisabled();
    expect(screen.getByRole("button", { name: /^Refuser/ })).toBeDisabled();
    await user.press(allow);
    expect(mockDecideApproval).toHaveBeenCalledTimes(1);

    await user.press(screen.getByRole("button", { name: "Actualiser les accords" }));
    await waitFor(() => expect(mockListApprovals).toHaveBeenCalledTimes(3));
    expect(screen.queryByTestId("approval-card-apr_test")).not.toBeOnTheScreen();
  });

  it("fails closed when the server reports an invalid action binding", async () => {
    mockListApprovals.mockResolvedValue([{ ...approval, binding_valid: false }]);
    const user = userEvent.setup();
    await render(<ApprovalsScreen />);

    const allow = await screen.findByRole("button", { name: /^Autoriser une fois/ });
    expect(screen.getByText("Liaison invalide : cet accord ne peut pas être autorisé.")).toBeOnTheScreen();
    await user.press(allow);

    expect(mockDecideApproval).not.toHaveBeenCalled();
  });

  it("fails closed when authoritative consent context is unavailable", async () => {
    mockListApprovals.mockResolvedValue([
      { ...approval, requester: null, audit_id: null, consent_context_valid: false },
    ]);
    const user = userEvent.setup();
    await render(<ApprovalsScreen />);

    const allow = await screen.findByRole("button", { name: /^Autoriser une fois/ });
    expect(
      screen.getByText("Contexte de consentement invalide : cet accord ne peut pas être autorisé."),
    ).toBeOnTheScreen();
    await user.press(allow);

    expect(mockDecideApproval).not.toHaveBeenCalled();
  });

  it("shows cached approvals offline but keeps both decisions disabled", async () => {
    mockListApprovals.mockRejectedValue(new Error("Accords indisponibles"));
    mockLocalApprovals.mockResolvedValue([approval]);
    const user = userEvent.setup();

    await render(<ApprovalsScreen />);

    const allow = await screen.findByRole("button", { name: /^Autoriser une fois/ });
    const deny = screen.getByRole("button", { name: /^Refuser/ });
    expect(allow).toBeDisabled();
    expect(deny).toBeDisabled();
    expect(screen.getByText(/Hors ligne — accords en cache, décisions désactivées/)).toBeOnTheScreen();
    await user.press(allow);
    expect(mockDecideApproval).not.toHaveBeenCalled();
  });

  it("does not claim the approval queue is empty when loading fails", async () => {
    mockListApprovals.mockRejectedValue(new Error("Accords indisponibles"));
    await render(<ApprovalsScreen />);

    expect(await screen.findByText("Accords indisponibles")).toBeOnTheScreen();
    expect(screen.queryByText("Aucun accord en attente")).not.toBeOnTheScreen();
  });

  it("offers a non-drag refresh action", async () => {
    const user = userEvent.setup();
    await render(<ApprovalsScreen />);

    await screen.findByText("Cible exacte : notes/result.txt");
    await user.press(screen.getByRole("button", { name: "Actualiser les accords" }));
    await waitFor(() => expect(mockListApprovals).toHaveBeenCalledTimes(2));
  });
});
