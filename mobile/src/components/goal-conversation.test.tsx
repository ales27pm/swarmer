import { act, fireEvent, render, screen, userEvent } from "@testing-library/react-native";
import { beforeEach, describe, expect, it, jest } from "@jest/globals";
import { GoalConversation } from "@/components/goal-conversation";
import { ApiError, getGoalConversation, type GoalDetail } from "@/lib/api/client";
import { notifyConnectionChanged } from "@/lib/connection-events";
import { projectGoalFixture } from "@/testing/project-fixtures";

jest.mock("@/lib/api/client", () => ({
  ApiError: class extends Error { status: number; constructor(status: number, message: string) { super(message); this.status = status; } },
  getGoalConversation: jest.fn(),
}));
const load = jest.mocked(getGoalConversation);
const send = jest.fn<() => Promise<GoalDetail>>();
const prepareReply = jest.fn(() => ({ clientMessageId: "reply_stable_1234567890", send }));
const openGoal = jest.fn();
const updated = jest.fn<() => Promise<void>>();
const question = { id: "question_1", goal_run_id: "goal_1", role: "assistant" as const, content: "Application web ou mobile?", created_at: "2030-01-01T00:01:00Z" };
const conversation = { messages: [question], active_goal_id: "goal_1", pending_question_id: "question_1" };
const props = { goal: projectGoalFixture.goal, disabled: false, onOpenGoal: openGoal, onUpdated: updated };
beforeEach(() => {
  jest.clearAllMocks();
  load.mockResolvedValue({ conversation, prepareReply });
  send.mockResolvedValue(projectGoalFixture);
  updated.mockResolvedValue();
});
describe("GoalConversation", () => {
  it("refreshes a rejected stale question while preserving the unsent answer", async () => {
    const user = userEvent.setup();
    send.mockRejectedValueOnce(new ApiError(409, "stale question"));
    await render(<GoalConversation {...props} />);
    await screen.findByText(question.content);
    await fireEvent.changeText(screen.getByLabelText("Réponse à la question du projet"), "Web");
    await user.press(screen.getByRole("button", { name: "Répondre à la question" }));
    expect(await screen.findByText(/Relisez la question actuelle/)).toBeOnTheScreen();
    expect(screen.getByLabelText("Réponse à la question du projet")).toHaveProp("value", "Web");
    expect(send).toHaveBeenCalledTimes(1);
    expect(load).toHaveBeenCalledTimes(2);
  });

  it("clears the old conversation and draft after pairing changes", async () => {
    await render(<GoalConversation {...props} />);
    await screen.findByText(question.content);
    await fireEvent.changeText(screen.getByLabelText("Réponse à la question du projet"), "Ancienne connexion");
    await act(async () => notifyConnectionChanged());
    expect(screen.queryByText(question.content)).not.toBeOnTheScreen();
    expect(screen.getByLabelText("Message pour le projet")).toHaveProp("value", "");
    expect(screen.getByRole("button", { name: "Envoyer au projet" })).toBeDisabled();
  });

  it("restores conversation and sends a long answer to the same run", async () => {
    const user = userEvent.setup();
    await render(<GoalConversation {...props} />);
    await screen.findByText(question.content);
    expect(screen.getByText(/elle n’autorise aucune écriture/)).toBeOnTheScreen();
    const answer = "Une application web avec gestion des contacts. ".repeat(60);
    await fireEvent.changeText(screen.getByLabelText("Réponse à la question du projet"), answer);
    await user.press(screen.getByRole("button", { name: "Répondre à la question" }));
    expect(prepareReply).toHaveBeenCalledWith(answer);
    expect(send).toHaveBeenCalledTimes(1);
    expect(openGoal).not.toHaveBeenCalled();
    expect(updated).toHaveBeenCalledTimes(1);
    expect(screen.getByText("Message enregistré dans le projet.")).toBeOnTheScreen();
  });
  it("opens the linked new run when continuing a terminal goal", async () => {
    const user = userEvent.setup();
    load.mockResolvedValue({ conversation: { ...conversation, pending_question_id: null }, prepareReply });
    send.mockResolvedValue({ ...projectGoalFixture, goal: { ...projectGoalFixture.goal, id: "goal_next" } });
    await render(<GoalConversation {...props} goal={{ ...props.goal, status: "completed" }} />);
    await screen.findByText(question.content);
    await fireEvent.changeText(screen.getByLabelText("Message pour le projet"), "Ajoute une recherche aux contacts existants.");
    await user.press(screen.getByRole("button", { name: "Envoyer au projet" }));
    expect(openGoal).toHaveBeenCalledWith("goal_next");
  });
  it("keeps the same pending attempt for explicit uncertain retry", async () => {
    const user = userEvent.setup();
    send.mockRejectedValueOnce(new Error("Connexion interrompue"));
    await render(<GoalConversation {...props} />);
    await screen.findByText(question.content);
    await fireEvent.changeText(screen.getByLabelText("Réponse à la question du projet"), "Une interface web.");
    await user.press(screen.getByRole("button", { name: "Répondre à la question" }));
    expect(await screen.findByText(/son identifiant reste inchangé/)).toBeOnTheScreen();
    expect(send).toHaveBeenCalledTimes(1);
    expect(screen.getByLabelText("Réponse à la question du projet")).toHaveProp("editable", false);
    await user.press(screen.getByRole("button", { name: "Réessayer le même envoi" }));
    expect(prepareReply).toHaveBeenCalledTimes(1);
    expect(send).toHaveBeenCalledTimes(2);
  });
  it("restores history on remount and locks offline replies", async () => {
    const user = userEvent.setup();
    const view = await render(<GoalConversation {...props} />);
    await screen.findByText(question.content);
    await view.unmount();
    await render(<GoalConversation {...props} />);
    await screen.findByText(question.content);
    expect(load).toHaveBeenCalledTimes(2);
    await screen.rerender(<GoalConversation {...props} disabled />);
    expect(screen.getByRole("button", { name: "Répondre à la question" })).toBeDisabled();
    await user.press(screen.getByRole("button", { name: "Répondre à la question" }));
    expect(send).not.toHaveBeenCalled();
  });
  it("does not navigate when a pending response arrives after unmount", async () => {
    const user = userEvent.setup();
    let resolve!: (value: GoalDetail) => void;
    send.mockImplementationOnce(() => new Promise((done) => { resolve = done; }));
    const view = await render(<GoalConversation {...props} />);
    await screen.findByText(question.content);
    await fireEvent.changeText(screen.getByLabelText("Réponse à la question du projet"), "Web");
    await user.press(screen.getByRole("button", { name: "Répondre à la question" }));
    await view.unmount();
    await act(async () => resolve({ ...projectGoalFixture, goal: { ...props.goal, id: "goal_next" } }));
    expect(openGoal).not.toHaveBeenCalled();
  });
});
