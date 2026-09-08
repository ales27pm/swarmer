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
import { iphoneCapabilityTransport } from "@/lib/iphone-capabilities/runtime";
import type {
  CapabilityAuthorizationResponse,
  CapabilityRequestDetail,
  CapabilityRequestPreview,
  CapabilityResult,
} from "@/lib/iphone-capabilities/types";

const mockPush = jest.fn();

jest.mock("expo-router", () => ({ useRouter: () => ({ push: mockPush }) }));

jest.mock("@/lib/api/client", () => ({
  ApiError: Error,
  decideApproval: jest.fn(),
  getServerUrl: jest.fn(),
  listApprovals: jest.fn(),
}));
jest.mock("@/lib/state/replica", () => ({ localApprovals: jest.fn() }));
jest.mock("@/lib/iphone-capabilities/runtime", () => ({
  iphoneCapabilityTransport: {
    authorize: jest.fn(),
    execute: jest.fn(),
    load: jest.fn(),
    refresh: jest.fn(),
  },
}));

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
const mockCapabilityAuthorize = jest.mocked(iphoneCapabilityTransport.authorize);
const mockCapabilityExecute = jest.mocked(iphoneCapabilityTransport.execute);
const mockCapabilityLoad = jest.mocked(iphoneCapabilityTransport.load);
const mockCapabilityRefresh = jest.mocked(iphoneCapabilityTransport.refresh);

const capabilityPreview = {
  schema_version: "0.9",
  request_id: `iphreq_${"c".repeat(32)}`,
  task_id: `tsk_${"d".repeat(32)}`,
  agent_id: "mail-worker",
  target_device_id: "iphone_test",
  capability: "iphone.mail.compose",
  status: "waiting_approval",
  created_at: "2099-09-08T11:59:00.000Z",
  expires_at: "2099-09-08T12:05:00.000Z",
} satisfies CapabilityRequestPreview;

const capabilityDetail = {
  ...capabilityPreview,
  arguments: {
    recipients: ["alice.private@example.test"],
    subject: "Quarterly secret",
    body: "Private body payload",
  },
  action_digest: `sha256:${"e".repeat(64)}`,
  grant: null,
} satisfies CapabilityRequestDetail;

const capabilityAuthorization = {
  ...capabilityDetail,
  status: "approved",
  grant: {
    schema_version: "0.9",
    grant_id: `grt_${"f".repeat(64)}`,
    request_id: capabilityDetail.request_id,
    task_id: capabilityDetail.task_id,
    agent_id: capabilityDetail.agent_id,
    target_device_id: capabilityDetail.target_device_id,
    approval_id: `icapr_${"a".repeat(32)}`,
    audit_id: 43,
    capability: capabilityDetail.capability,
    action_digest: capabilityDetail.action_digest,
    issued_at: "2099-09-08T12:00:00.000Z",
    expires_at: "2099-09-08T12:01:00.000Z",
    use: "once",
  },
} satisfies CapabilityAuthorizationResponse;

const capabilityResult = {
  name: "iphone.mail.compose",
  status: "completed",
  value: { composed: true },
} satisfies CapabilityResult;

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
    mockCapabilityRefresh.mockResolvedValue([]);
    mockCapabilityLoad.mockResolvedValue(capabilityDetail);
    mockCapabilityAuthorize.mockResolvedValue(capabilityAuthorization);
    mockCapabilityExecute.mockResolvedValue(capabilityResult);
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

  it("loads authoritative iPhone requests without auto-authorizing or executing them", async () => {
    mockCapabilityRefresh.mockResolvedValue([capabilityPreview]);
    await render(<ApprovalsScreen />);

    const card = await screen.findByTestId(`iphone-capability-card-${capabilityPreview.request_id}`);
    expect(within(card).getByText("Composer un courriel")).toBeOnTheScreen();
    expect(within(card).getByText(`Expiration : ${capabilityPreview.expires_at}`)).toBeOnTheScreen();
    expect(within(card).getByText(`Empreinte exacte : ${capabilityDetail.action_digest}`)).toBeOnTheScreen();
    expect(within(card).getByText(/Destinataires masqués : 1\/20/)).toBeOnTheScreen();
    expect(screen.queryByText("alice.private@example.test")).not.toBeOnTheScreen();
    expect(screen.queryByText("Quarterly secret")).not.toBeOnTheScreen();
    expect(screen.queryByText("Private body payload")).not.toBeOnTheScreen();
    expect(mockCapabilityLoad).toHaveBeenCalledWith(capabilityPreview.request_id);
    expect(mockCapabilityAuthorize).not.toHaveBeenCalled();
    expect(mockCapabilityExecute).not.toHaveBeenCalled();
  });

  it("shows the exact contacts lookup query before consent", async () => {
    const contactPreview = {
      ...capabilityPreview,
      request_id: `iphreq_${"1".repeat(32)}`,
      capability: "iphone.contacts.lookup",
    } as const;
    const contactDetail = {
      ...contactPreview,
      arguments: { query: "Ada Lovelace" },
      action_digest: `sha256:${"2".repeat(64)}`,
      grant: null,
    } satisfies CapabilityRequestDetail;
    mockCapabilityRefresh.mockResolvedValue([contactPreview]);
    mockCapabilityLoad.mockResolvedValue(contactDetail);

    await render(<ApprovalsScreen />);

    const card = await screen.findByTestId(`iphone-capability-card-${contactPreview.request_id}`);
    expect(within(card).getByText("Recherche exacte : Ada Lovelace")).toBeOnTheScreen();
    expect(mockCapabilityAuthorize).not.toHaveBeenCalled();
  });

  it("denies an iPhone request without invoking its native execution flow", async () => {
    mockCapabilityRefresh.mockResolvedValue([capabilityPreview]);
    mockCapabilityAuthorize.mockResolvedValue({
      ...capabilityDetail,
      status: "denied",
      grant: null,
    });
    const user = userEvent.setup();
    await render(<ApprovalsScreen />);

    await user.press(await screen.findByRole("button", { name: "Refuser l’action iPhone" }));

    await waitFor(() => expect(mockCapabilityAuthorize).toHaveBeenCalledWith(
      capabilityPreview.request_id,
      "deny",
    ));
    expect(mockCapabilityExecute).not.toHaveBeenCalled();
    expect(await screen.findByText("Demande iPhone refusée.")).toBeOnTheScreen();
  });

  it("authorizes before invoking the transport's consume-and-native flow", async () => {
    const order: string[] = [];
    mockCapabilityRefresh.mockResolvedValue([capabilityPreview]);
    mockCapabilityAuthorize.mockImplementation(async () => {
      order.push("authorize");
      return capabilityAuthorization;
    });
    mockCapabilityExecute.mockImplementation(async () => {
      order.push("execute");
      return capabilityResult;
    });
    const user = userEvent.setup();
    await render(<ApprovalsScreen />);

    await user.press(
      await screen.findByRole("button", { name: "Autoriser l’action iPhone une fois" }),
    );

    await waitFor(() => expect(mockCapabilityExecute).toHaveBeenCalledWith(capabilityPreview.request_id));
    expect(order).toEqual(["authorize", "execute"]);
    expect(await screen.findByText("Action iPhone exécutée et résultat transmis.")).toBeOnTheScreen();
  });

  it("coalesces duplicate authoritative delivery into one decision card and execution", async () => {
    mockCapabilityRefresh.mockResolvedValue([capabilityPreview, capabilityPreview]);
    const user = userEvent.setup();
    await render(<ApprovalsScreen />);

    const allow = await screen.findByRole("button", {
      name: "Autoriser l’action iPhone une fois",
    });
    expect(screen.getAllByRole("button", {
      name: "Autoriser l’action iPhone une fois",
    })).toHaveLength(1);
    expect(mockCapabilityLoad).toHaveBeenCalledTimes(1);

    await user.press(allow);
    await waitFor(() => expect(mockCapabilityExecute).toHaveBeenCalledTimes(1));
    expect(mockCapabilityAuthorize).toHaveBeenCalledTimes(1);
  });

  it("offers explicit grant recovery after restart and never offers a new denial", async () => {
    const recoveryPreview = { ...capabilityPreview, status: "approved" as const };
    const recoveryDetail = { ...capabilityDetail, status: "approved" as const };
    mockCapabilityRefresh.mockResolvedValue([recoveryPreview]);
    mockCapabilityLoad.mockResolvedValue(recoveryDetail);
    const user = userEvent.setup();
    await render(<ApprovalsScreen />);

    expect(await screen.findByText(/son secret à usage unique n’est plus disponible/)).toBeOnTheScreen();
    expect(screen.queryByRole("button", { name: "Refuser l’action iPhone" })).not.toBeOnTheScreen();
    const recover = screen.getByRole("button", {
      name: "Récupérer l’autorisation iPhone une fois",
    });

    await user.press(recover);

    await waitFor(() => expect(mockCapabilityAuthorize).toHaveBeenCalledWith(
      capabilityPreview.request_id,
      "approve",
    ));
    expect(mockCapabilityExecute).toHaveBeenCalledWith(capabilityPreview.request_id);
  });

  it("fails closed for offline and expired iPhone request states", async () => {
    mockCapabilityRefresh.mockRejectedValueOnce(new Error("offline"));
    await render(<ApprovalsScreen />);

    expect(await screen.findByText(/Demandes iPhone indisponibles : offline/)).toBeOnTheScreen();
    expect(screen.queryByRole("button", {
      name: "Autoriser l’action iPhone une fois",
    })).not.toBeOnTheScreen();
    expect(mockCapabilityAuthorize).not.toHaveBeenCalled();

    mockCapabilityRefresh.mockResolvedValueOnce([
      { ...capabilityPreview, expires_at: "2000-09-08T12:05:00.000Z" },
    ]);
    mockCapabilityLoad.mockResolvedValueOnce({
      ...capabilityDetail,
      expires_at: "2000-09-08T12:05:00.000Z",
    });
    const user = userEvent.setup();
    await user.press(screen.getByRole("button", { name: "Actualiser les accords" }));

    const expiredAllow = await screen.findByRole("button", {
      name: "Autoriser l’action iPhone une fois",
    });
    expect(expiredAllow).toBeDisabled();
    expect(screen.getByText("Demande expirée — actualisez la liste.")).toBeOnTheScreen();
  });
});
