import { act, fireEvent, render, screen, userEvent, within } from "@testing-library/react-native";
import { afterEach, describe, expect, it, jest } from "@jest/globals";
import { AppState, type AppStateStatus, type NativeEventSubscription } from "react-native";

import {
  ActionButton,
  ApprovalDecisionCard,
  ErrorBanner,
  StatusBadge,
} from "@/components/swarm-ui";
import type { Approval } from "@/lib/api/client";

const approval: Approval = {
  id: "apr_clock",
  task_id: "tsk_clock",
  tool_call_id: "call_clock",
  action_digest: `sha256:${"c".repeat(64)}`,
  action: "workspace.write_text",
  action_preview: {
    operation: "Write workspace text",
    target: "notes/clock.txt",
    details: ["5 UTF-8 bytes; content hidden"],
    arguments_redacted: true,
  },
  binding_valid: true,
  requester: { type: "device", id: "device-clock", name: "Clock Phone" },
  policy: {
    rule_id: "ask-workspace-write",
    decision: "ask",
    reason: "Writing workspace file content requires explicit one-use approval.",
  },
  affected_data_summary: "Writes 5 UTF-8 bytes to notes/clock.txt; content hidden.",
  audit_id: 101,
  consent_context_valid: true,
  summary: "Write text to a workspace file",
  risk: "medium",
  status: "pending",
  created_at: "2030-01-01T11:59:00Z",
  expires_at: "2030-01-01T12:00:01Z",
  decided_at: null,
  decision: null,
};

afterEach(() => {
  jest.clearAllTimers();
  jest.useRealTimers();
  jest.restoreAllMocks();
});

describe("swarm UI primitives", () => {
  it("permits only an explicit one-use decision for a verified redacted project revision", async () => {
    const projectApproval: Approval = {
      ...approval,
      action: "workspace.write_project",
      summary: "Save a reviewed project revision",
      action_preview: {
        operation: "Save reviewed project revision",
        target: "generated/project_1/revisions/revision_2",
        details: ["2 files; source content hidden", "Existing project revisions are preserved"],
        arguments_redacted: true,
      },
    };
    const onDecision = jest.fn();
    const user = userEvent.setup();
    await render(<ApprovalDecisionCard approval={projectApproval} busy={null} onDecision={onDecision} />);
    expect(screen.getByText(/generated\/project_1\/revisions\/revision_2/)).toBeOnTheScreen();
    expect(onDecision).not.toHaveBeenCalled();
    await user.press(screen.getByRole("button", { name: /^Autoriser une fois/ }));
    expect(onDecision).toHaveBeenCalledWith("approve");
  });

  it.each([false, true])("rejects malformed project preview redaction=%s", async (redacted) => {
    const malformed = {
      ...approval, action: "workspace.write_project", summary: "Save a reviewed project revision",
      action_preview: { ...approval.action_preview, arguments_redacted: redacted, ...(redacted ? { files: [{ content: "private source" }] } : {}) },
    } as unknown as Approval;
    await render(<ApprovalDecisionCard approval={malformed} busy={null} onDecision={jest.fn()} />);
    expect(screen.getByRole("button", { name: /^Autoriser une fois/ })).toBeDisabled();
    expect(screen.queryByText("private source")).not.toBeOnTheScreen();
  });

  it("exposes task status and errors to assistive technology", async () => {
    await render(
      <>
        <StatusBadge status="waiting_permission" />
        <ErrorBanner message="Le serveur a refusé la décision." />
      </>,
    );

    expect(screen.getByLabelText("Statut: Permission")).toBeOnTheScreen();
    expect(screen.getByRole("alert")).toHaveTextContent("Le serveur a refusé la décision.");
  });

  it("blocks a disabled destructive action", async () => {
    const onPress = jest.fn();
    const user = userEvent.setup();
    await render(
      <ActionButton
        disabled
        label="Supprimer"
        onPress={onPress}
        variant="danger"
      />,
    );

    const button = screen.getByRole("button", { name: "Supprimer" });
    expect(button).toBeDisabled();
    await user.press(button);
    expect(onPress).not.toHaveBeenCalled();
  });

  it("keeps trusted consent evidence separate from masked model details", async () => {
    await render(
      <ApprovalDecisionCard approval={approval} busy={null} onDecision={jest.fn()} />,
    );

    const trusted = screen.getByTestId("approval-trusted-context-apr_clock");
    const model = screen.getByTestId("approval-model-context-apr_clock");
    expect(within(trusted).getByText(/Clock Phone \(device device-clock\)/)).toBeOnTheScreen();
    expect(within(trusted).getByText(/Audit : 101/)).toBeOnTheScreen();
    expect(within(trusted).queryByText(approval.summary)).not.toBeOnTheScreen();
    expect(within(model).getByText(`Libellé public : ${approval.summary}`)).toBeOnTheScreen();
    expect(within(model).getByText("Détails du modèle masqués")).toBeOnTheScreen();
    expect(within(model).getByText("Ce texte n’autorise pas l’action.")).toBeOnTheScreen();
  });

  it("fails closed when a contradictory payload claims validity without evidence", async () => {
    const contradictory = {
      ...approval,
      requester: null,
      policy: null,
      affected_data_summary: null,
      audit_id: null,
      consent_context_valid: true,
    } as unknown as Approval;
    const onDecision = jest.fn();
    const user = userEvent.setup();
    await render(
      <ApprovalDecisionCard approval={contradictory} busy={null} onDecision={onDecision} />,
    );

    expect(screen.getByRole("header", { name: "Contexte serveur non vérifiable" })).toBeOnTheScreen();
    const allow = screen.getByRole("button", { name: /^Autoriser une fois/ });
    expect(allow).toBeDisabled();
    await user.press(allow);
    expect(onDecision).not.toHaveBeenCalled();
  });

  it("renders malformed scalar context as non-verifiable instead of crashing", async () => {
    const malformed = {
      ...approval,
      requester: { type: "device", id: 123, name: "Clock Phone" },
      consent_context_valid: true,
    } as unknown as Approval;
    await render(
      <ApprovalDecisionCard approval={malformed} busy={null} onDecision={jest.fn()} />,
    );

    expect(screen.getByRole("header", { name: "Contexte serveur non vérifiable" })).toBeOnTheScreen();
    expect(screen.getByRole("button", { name: /^Autoriser une fois/ })).toBeDisabled();
  });

  it("renders malformed exact-action evidence safely and blocks both decisions", async () => {
    const malformed = {
      ...approval,
      action_preview: {
        operation: "Write workspace text",
        target: "notes/clock.txt",
        details: null,
        arguments_redacted: true,
      },
    } as unknown as Approval;
    const onDecision = jest.fn();
    const user = userEvent.setup();

    await render(
      <ApprovalDecisionCard approval={malformed} busy={null} onDecision={onDecision} />,
    );

    expect(screen.getByRole("header", { name: "Action exacte indisponible" })).toBeOnTheScreen();
    expect(screen.getByText(/détails liés à cette action sont incomplets ou invalides/)).toBeOnTheScreen();
    const allow = screen.getByRole("button", { name: /^Autoriser une fois/ });
    const deny = screen.getByRole("button", { name: /^Refuser/ });
    expect(allow).toBeDisabled();
    expect(deny).toBeDisabled();
    await user.press(allow);
    await user.press(deny);
    expect(onDecision).not.toHaveBeenCalled();
  });

  it("requires a canonical digest and action-specific preview fields", async () => {
    const onDecision = jest.fn();
    const view = await render(
      <ApprovalDecisionCard
        approval={{ ...approval, action_digest: "sha256:not-canonical" }}
        busy={null}
        onDecision={onDecision}
      />,
    );
    expect(screen.getByRole("button", { name: /^Autoriser une fois/ })).toBeDisabled();
    expect(screen.getByText("Empreinte indisponible ou invalide")).toBeOnTheScreen();

    await view.rerender(
      <ApprovalDecisionCard
        approval={
          {
            ...approval,
            action_preview: { ...approval.action_preview, target: undefined },
          } as unknown as Approval
        }
        busy={null}
        onDecision={onDecision}
      />,
    );
    expect(screen.getByRole("button", { name: /^Autoriser une fois/ })).toBeDisabled();

    await view.rerender(
      <ApprovalDecisionCard
        approval={
          {
            ...approval,
            action: "process.run",
            summary: "Run a sandboxed process",
            risk: "high",
            action_preview: {
              operation: "Run sandboxed process",
              target: "entire non-protected configured workspace (read-write)",
              working_directory: "server/tests",
              command: [],
              details: ["network denied by sandbox policy"],
              arguments_redacted: true,
            },
          } as unknown as Approval
        }
        busy={null}
        onDecision={onDecision}
      />,
    );
    expect(screen.getByRole("button", { name: /^Autoriser une fois/ })).toBeDisabled();
  });

  it("rechecks exact-action evidence at press time", async () => {
    const mutableApproval: Approval = {
      ...approval,
      action_preview: {
        ...approval.action_preview,
        details: [...approval.action_preview.details],
      },
    };
    const onDecision = jest.fn();
    await render(
      <ApprovalDecisionCard approval={mutableApproval} busy={null} onDecision={onDecision} />,
    );
    const allow = screen.getByRole("button", { name: /^Autoriser une fois/ });
    expect(allow).toBeEnabled();

    mutableApproval.action_preview.details.splice(0);
    await fireEvent.press(allow);

    expect(onDecision).not.toHaveBeenCalled();
  });

  it("shows the validated process working directory", async () => {
    await render(
      <ApprovalDecisionCard
        approval={{
          ...approval,
          action: "process.run",
          summary: "Run a sandboxed process",
          risk: "high",
          action_preview: {
            operation: "Run sandboxed process",
            target: "entire non-protected configured workspace (read-write)",
            working_directory: "server/tests",
            command: ["pytest"],
            details: ["network denied by sandbox policy"],
            arguments_redacted: false,
          },
        }}
        busy={null}
        onDecision={jest.fn()}
      />,
    );

    expect(screen.getByText("Dossier de travail : server/tests")).toBeOnTheScreen();
    expect(screen.getByRole("button", { name: /^Autoriser une fois/ })).toBeEnabled();
  });

  it("expires locally at the deadline and prevents either stale decision", async () => {
    jest.useFakeTimers();
    jest.setSystemTime(new Date("2030-01-01T12:00:00Z"));
    const onDecision = jest.fn();
    const user = userEvent.setup({ advanceTimers: jest.advanceTimersByTime });
    await render(
      <ApprovalDecisionCard approval={approval} busy={null} onDecision={onDecision} />,
    );

    const allow = screen.getByRole("button", { name: /^Autoriser une fois/ });
    const deny = screen.getByRole("button", { name: /^Refuser/ });
    expect(allow).toBeEnabled();
    expect(deny).toBeEnabled();

    await act(() => {
      jest.advanceTimersByTime(1_001);
    });

    expect(screen.getByTestId("approval-status-apr_clock")).toHaveTextContent("Expiré");
    expect(screen.getByText("Expiré : cette demande n’est plus actionnable.")).toBeOnTheScreen();
    expect(allow).toBeDisabled();
    expect(deny).toBeDisabled();
    await user.press(allow);
    await user.press(deny);
    expect(onDecision).not.toHaveBeenCalled();
  });

  it.each([
    ["an already-expired deadline", "2029-12-31T23:59:59Z"],
    ["a malformed deadline", "not-a-date"],
  ])("fails closed for %s", async (_label, expiresAt) => {
    jest.useFakeTimers();
    jest.setSystemTime(new Date("2030-01-01T12:00:00Z"));
    await render(
      <ApprovalDecisionCard
        approval={{ ...approval, expires_at: expiresAt }}
        busy={null}
        onDecision={jest.fn()}
      />,
    );

    expect(screen.getByRole("button", { name: /^Autoriser une fois/ })).toBeDisabled();
    expect(screen.getByTestId("approval-status-apr_clock")).toHaveTextContent("Expiré");
  });

  it("fails closed immediately when refreshed props move the deadline into the past", async () => {
    jest.useFakeTimers();
    jest.setSystemTime(new Date("2030-01-01T12:00:00Z"));
    const view = await render(
      <ApprovalDecisionCard
        approval={{ ...approval, expires_at: "2030-01-01T12:01:00Z" }}
        busy={null}
        onDecision={jest.fn()}
      />,
    );
    expect(screen.getByRole("button", { name: /^Autoriser une fois/ })).toBeEnabled();

    await view.rerender(
      <ApprovalDecisionCard
        approval={{ ...approval, expires_at: "2030-01-01T11:59:59Z" }}
        busy={null}
        onDecision={jest.fn()}
      />,
    );

    expect(screen.getByRole("button", { name: /^Autoriser une fois/ })).toBeDisabled();
    expect(screen.getByTestId("approval-status-apr_clock")).toHaveTextContent("Expiré");
  });

  it("rechecks the wall clock at press time even when the deadline timer is delayed", async () => {
    jest.useFakeTimers();
    jest.setSystemTime(new Date("2030-01-01T12:00:00Z"));
    const onDecision = jest.fn();
    await render(
      <ApprovalDecisionCard approval={approval} busy={null} onDecision={onDecision} />,
    );
    const allow = screen.getByRole("button", { name: /^Autoriser une fois/ });
    expect(allow).toBeEnabled();

    jest.setSystemTime(new Date("2030-01-01T12:00:02Z"));
    await fireEvent.press(allow);

    expect(onDecision).not.toHaveBeenCalled();
  });

  it("recalculates expiry when the app returns to the foreground", async () => {
    jest.useFakeTimers();
    jest.setSystemTime(new Date("2030-01-01T12:00:00Z"));
    let appStateListener: ((state: AppStateStatus) => void) | undefined;
    jest.spyOn(AppState, "addEventListener").mockImplementation((_type, listener) => {
      appStateListener = listener;
      return { remove: jest.fn() } as NativeEventSubscription;
    });
    await render(
      <ApprovalDecisionCard approval={approval} busy={null} onDecision={jest.fn()} />,
    );
    expect(screen.getByRole("button", { name: /^Autoriser une fois/ })).toBeEnabled();

    jest.setSystemTime(new Date("2030-01-01T12:00:02Z"));
    await act(() => appStateListener?.("active"));

    expect(screen.getByTestId("approval-status-apr_clock")).toHaveTextContent("Expiré");
    expect(screen.getByRole("button", { name: /^Autoriser une fois/ })).toBeDisabled();
  });
});
