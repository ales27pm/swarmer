import { act, render, screen, userEvent } from "@testing-library/react-native";
import { beforeEach, describe, expect, it, jest } from "@jest/globals";

import { GoalCodeProposalReview } from "@/components/goal-code-proposal";
import { reviewGoalCodeProposal, type GoalCodeProposal } from "@/lib/api/client";

jest.mock("@/lib/api/client", () => ({ reviewGoalCodeProposal: jest.fn() }));

const readProposal = jest.mocked(reviewGoalCodeProposal);
const openTask = jest.fn();
const prepareApproval = jest.fn<() => Promise<{ task_id: string; tool_call_id: string; approval_id: string }>>();
const proposal: GoalCodeProposal = {
  node_id: "node_code",
  path: "generated/goal_1/node_code/app.py",
  content: "print('draft only')\n",
  sha256: "a".repeat(64),
  summary: "Une proposition Python à relire.",
  status: "proposal",
  task_id: null,
};
const props = { goalId: "goal_1", nodeId: "node_code", disabled: false, onOpenTask: openTask };

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((settle) => { resolve = settle; });
  return { promise, resolve };
}

describe("GoalCodeProposalReview", () => {
  beforeEach(() => {
    jest.clearAllMocks();
    readProposal.mockResolvedValue({ proposal, prepareApproval });
    prepareApproval.mockResolvedValue({ task_id: "tsk_write", tool_call_id: "call_write", approval_id: "apr_write" });
  });

  it("fetches and renders code only on explicit review, then separately prepares the approval", async () => {
    const user = userEvent.setup();
    await render(<GoalCodeProposalReview {...props} />);
    expect(readProposal).not.toHaveBeenCalled();
    expect(screen.queryByText(proposal.content)).not.toBeOnTheScreen();

    await user.press(screen.getByRole("button", { name: "Examiner le code proposé" }));
    expect(readProposal).toHaveBeenCalledWith("goal_1", "node_code");
    expect(await screen.findByText(proposal.content)).toBeOnTheScreen();
    expect(screen.getByText(`Fichier proposé : ${proposal.path}`)).toBeOnTheScreen();
    expect(screen.getByText(`SHA-256 du contenu proposé : ${proposal.sha256}`)).toBeOnTheScreen();
    expect(screen.getByText("Code généré — non vérifié")).toBeOnTheScreen();
    expect(screen.getByText(/Cette revue n’exécute pas le code/)).toBeOnTheScreen();
    expect(prepareApproval).not.toHaveBeenCalled();

    await user.press(screen.getByRole("button", { name: "Préparer l’autorisation d’écriture" }));
    expect(prepareApproval).toHaveBeenCalledTimes(1);
    expect(openTask).toHaveBeenCalledWith("tsk_write");
    expect(screen.getByRole("button", { name: "Préparer l’autorisation d’écriture" })).toBeDisabled();
  });

  it("locks a pending apply against repeated presses", async () => {
    const user = userEvent.setup();
    const pending = deferred<{ task_id: string; tool_call_id: string; approval_id: string }>();
    prepareApproval.mockImplementationOnce(async () => pending.promise);
    await render(<GoalCodeProposalReview {...props} />);
    await user.press(screen.getByRole("button", { name: "Examiner le code proposé" }));
    await user.press(screen.getByRole("button", { name: "Préparer l’autorisation d’écriture" }));
    expect(screen.getByRole("button", { name: "Actualiser la proposition" })).toBeDisabled();
    await user.press(screen.getByRole("button", { name: "Préparer l’autorisation d’écriture" }));
    expect(prepareApproval).toHaveBeenCalledTimes(1);
    await act(async () => pending.resolve({ task_id: "tsk_write", tool_call_id: "call_write", approval_id: "apr_write" }));
    expect(openTask).toHaveBeenCalledTimes(1);
  });

  it("requires an explicit retry after a failed preview fetch", async () => {
    const user = userEvent.setup();
    readProposal.mockRejectedValueOnce(new Error("Proposition indisponible"));
    await render(<GoalCodeProposalReview {...props} />);
    await user.press(screen.getByRole("button", { name: "Examiner le code proposé" }));
    expect(await screen.findByText("Proposition indisponible")).toBeOnTheScreen();
    expect(readProposal).toHaveBeenCalledTimes(1);
    expect(prepareApproval).not.toHaveBeenCalled();

    await user.press(screen.getByRole("button", { name: "Réessayer le chargement" }));
    expect(await screen.findByText(proposal.content)).toBeOnTheScreen();
    expect(readProposal).toHaveBeenCalledTimes(2);
  });

  it("reconciles an uncertain apply by explicit fetch and opens the existing approval task", async () => {
    const user = userEvent.setup();
    prepareApproval.mockRejectedValueOnce(new Error("Réponse perdue"));
    readProposal.mockResolvedValueOnce({ proposal, prepareApproval }).mockResolvedValue({
      proposal: { ...proposal, status: "waiting_permission", task_id: "tsk_existing" },
      prepareApproval,
    });
    await render(<GoalCodeProposalReview {...props} />);
    await user.press(screen.getByRole("button", { name: "Examiner le code proposé" }));
    await user.press(screen.getByRole("button", { name: "Préparer l’autorisation d’écriture" }));
    expect(await screen.findByText(/La demande peut avoir été créée/)).toBeOnTheScreen();
    expect(screen.getByRole("button", { name: "Préparer l’autorisation d’écriture" })).toBeDisabled();
    expect(readProposal).toHaveBeenCalledTimes(1);
    expect(openTask).not.toHaveBeenCalled();

    await user.press(screen.getByRole("button", { name: "Actualiser la proposition" }));
    expect(screen.queryByRole("button", { name: "Préparer l’autorisation d’écriture" })).not.toBeOnTheScreen();
    await user.press(screen.getByRole("button", { name: "Voir la demande d’autorisation" }));
    expect(openTask).toHaveBeenCalledWith("tsk_existing");
    expect(prepareApproval).toHaveBeenCalledTimes(1);
  });

  it("does not fetch offline and locks a loaded proposal when online authority is lost", async () => {
    const user = userEvent.setup();
    await render(<GoalCodeProposalReview {...props} disabled />);
    await user.press(screen.getByRole("button", { name: "Examiner le code proposé" }));
    expect(readProposal).not.toHaveBeenCalled();
    await screen.rerender(<GoalCodeProposalReview {...props} />);
    await user.press(screen.getByRole("button", { name: "Examiner le code proposé" }));
    await screen.rerender(<GoalCodeProposalReview {...props} disabled />);
    expect(screen.getByRole("button", { name: "Préparer l’autorisation d’écriture" })).toBeDisabled();
    await user.press(screen.getByRole("button", { name: "Préparer l’autorisation d’écriture" }));
    expect(prepareApproval).not.toHaveBeenCalled();
  });
});
