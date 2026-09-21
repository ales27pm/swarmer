import { act, fireEvent, render, screen, userEvent } from "@testing-library/react-native";
import { beforeEach, describe, expect, it, jest } from "@jest/globals";
import { GoalConversation } from "@/components/goal-conversation";
import { ApiError, getGoalConversation, type GoalConversationSession, type GoalDetail } from "@/lib/api/client";
import { notifyConnectionChanged } from "@/lib/connection-events";
import { projectGoalFixture } from "@/testing/project-fixtures";
import { PROJECT_MODEL_TIMEOUT_PAUSE_DIAGNOSTIC } from "@/lib/project-pause";

jest.mock("@/lib/api/client", () => ({
  ApiError: class extends Error { status: number; constructor(status: number, message: string) { super(message); this.status = status; } },
  getGoalConversation: jest.fn(),
}));
const load = jest.mocked(getGoalConversation);
const send = jest.fn<() => Promise<GoalDetail>>();
const prepareReply = jest.fn(() => ({ clientMessageId: "reply_stable_1234567890", send }));
const openGoal = jest.fn();
const openLocalPlan = jest.fn();
const updated = jest.fn<() => Promise<void>>();
const question = { id: "question_1", goal_run_id: "goal_1", role: "assistant" as const, content: "Application web ou mobile?", created_at: "2030-01-01T00:01:00Z" };
const conversation = { messages: [question], active_goal_id: "goal_1", pending_question_id: "question_1" };
const props = { goal: projectGoalFixture.goal, disabled: false, onOpenGoal: openGoal, onUpdated: updated };
const localContinuation: GoalDetail = { ...projectGoalFixture, goal: { ...projectGoalFixture.goal,
  id: "goal_next", status: "planning", started_at: null, current_phase: "awaiting_local_plan",
  step_count: 0, replan_count: 0, model_call_count: 0 }, nodes: [], result: null };
beforeEach(() => {
  jest.clearAllMocks();
  load.mockResolvedValue({ conversation, prepareReply });
  send.mockResolvedValue(projectGoalFixture);
  updated.mockResolvedValue();
});
describe("GoalConversation", () => {
  it("presents a persisted model timeout as a technical pause and requires an explicit bound reply", async () => {
    const user = userEvent.setup();
    load.mockResolvedValue({ conversation: { ...conversation, messages: [{ ...question, content: PROJECT_MODEL_TIMEOUT_PAUSE_DIAGNOSTIC }] }, prepareReply });
    await render(<GoalConversation {...props} />);
    expect(await screen.findByText(/Le modèle a dépassé son délai deux fois/)).toBeOnTheScreen();
    expect(screen.queryByText(/Une précision est demandée/)).not.toBeOnTheScreen();
    expect(screen.queryByRole("button", { name: "Répondre à la question" })).not.toBeOnTheScreen();
    expect(screen.getByRole("button", { name: "Envoyer au projet" })).toBeDisabled();
    expect(send).not.toHaveBeenCalled();
    await fireEvent.changeText(screen.getByLabelText("Message pour reprendre le projet"), "Reprends avec une étape plus courte.");
    await user.press(screen.getByRole("button", { name: "Envoyer au projet" }));
    expect(prepareReply).toHaveBeenCalledWith("Reprends avec une étape plus courte.");
    expect(send).toHaveBeenCalledTimes(1);
  });

  it.each([
    "Quel délai de timeout voulez-vous pour l’application?",
    `${PROJECT_MODEL_TIMEOUT_PAUSE_DIAGNOSTIC} Quelle interface voulez-vous?`,
  ])("preserves clarification semantics for unknown or extended text: %s", async (content) => {
    load.mockResolvedValue({ conversation: { ...conversation, messages: [{ ...question, content }] }, prepareReply });
    await render(<GoalConversation {...props} />);
    expect(await screen.findByText(/Une précision est demandée/)).toBeOnTheScreen();
    expect(screen.queryByLabelText("Message pour reprendre le projet")).not.toBeOnTheScreen();
    expect(screen.getByRole("button", { name: "Répondre à la question" })).toBeDisabled();
    expect(send).not.toHaveBeenCalled();
  });

  it("does not display an old technical pause after its pending binding was cleared", async () => {
    load.mockResolvedValue({ conversation: { ...conversation, pending_question_id: null, messages: [{ ...question, content: PROJECT_MODEL_TIMEOUT_PAUSE_DIAGNOSTIC }] }, prepareReply });
    await render(<GoalConversation {...props} />);
    await screen.findByText(PROJECT_MODEL_TIMEOUT_PAUSE_DIAGNOSTIC);
    expect(screen.queryByText(/Le modèle a dépassé son délai deux fois/)).not.toBeOnTheScreen();
    expect(screen.getByLabelText("Message pour le projet")).toBeOnTheScreen();
  });

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
    expect(screen.getByText(/Votre réponse sera liée à cette question et permettra de poursuivre le travail sur le projet/)).toBeOnTheScreen();
    expect(screen.getByText(/L’application des fichiers dans votre espace de travail nécessitera une approbation distincte/)).toBeOnTheScreen();
    expect(screen.queryByText(/n’autorise aucune écriture/)).not.toBeOnTheScreen();
    const answer = "Une application web avec gestion des contacts. ".repeat(60);
    await fireEvent.changeText(screen.getByLabelText("Réponse à la question du projet"), answer);
    await user.press(screen.getByRole("button", { name: "Répondre à la question" }));
    expect(prepareReply).toHaveBeenCalledWith(answer);
    expect(send).toHaveBeenCalledTimes(1);
    expect(openGoal).not.toHaveBeenCalled();
    expect(updated).toHaveBeenCalledTimes(1);
    expect(screen.getByText("Message enregistré dans le projet.")).toBeOnTheScreen();
  });
  it("recovers a reply reload interrupted by a parent refresh without sending twice", async () => {
    const user = userEvent.setup();
    let resolveReload!: (value: GoalConversationSession) => void;
    const answer = "Fiches clients, soumissions/projet, courriels, calendrier";
    const answered = {
      ...conversation,
      messages: [...conversation.messages, { ...question, id: "answer_1", role: "user" as const, content: answer }],
      pending_question_id: null,
    };
    load.mockResolvedValueOnce({ conversation, prepareReply });
    load.mockImplementationOnce(() => new Promise((done) => { resolveReload = done; }));
    load.mockResolvedValueOnce({ conversation: answered, prepareReply });
    const view = await render(<GoalConversation {...props} />);
    await screen.findByText(question.content);
    await fireEvent.changeText(screen.getByLabelText("Réponse à la question du projet"), answer);
    await user.press(screen.getByRole("button", { name: "Répondre à la question" }));
    expect(load).toHaveBeenCalledTimes(2);

    await view.rerender(<GoalConversation {...props} disabled />);
    await view.rerender(<GoalConversation {...props} disabled={false} />);
    await act(async () => resolveReload({ conversation, prepareReply }));

    expect(await screen.findByLabelText("Message pour le projet")).toHaveProp("value", "");
    expect(screen.getByText(answer)).toBeOnTheScreen();
    expect(screen.queryByText(/Une précision est demandée/)).not.toBeOnTheScreen();
    expect(screen.getByRole("button", { name: "Actualiser la conversation" })).toBeEnabled();
    await fireEvent.changeText(screen.getByLabelText("Message pour le projet"), "Ajoute une recherche.");
    expect(screen.getByRole("button", { name: "Envoyer au projet" })).toBeEnabled();
    expect(prepareReply).toHaveBeenCalledTimes(1);
    expect(send).toHaveBeenCalledTimes(1);
    expect(updated).toHaveBeenCalledTimes(1);
    expect(load).toHaveBeenCalledTimes(3);
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

  it("offers explicit local planning for a terminal linked project and opens the new waiting goal", async () => {
    const user = userEvent.setup();
    load.mockResolvedValue({ conversation: { ...conversation, pending_question_id: null, project_id: "project_1" }, prepareReply });
    send.mockResolvedValue(localContinuation);
    await render(<GoalConversation {...props} goal={{ ...props.goal, status: "completed" }} onOpenLocalPlan={openLocalPlan} />);
    await screen.findByText(question.content);
    expect(screen.getByRole("button", { name: "Planifier la suite sur l’iPhone" })).toBeDisabled();
    await fireEvent.changeText(screen.getByLabelText("Message pour le projet"), "Ajoute une recherche aux clients existants.");
    await user.press(screen.getByRole("button", { name: "Planifier la suite sur l’iPhone" }));
    expect(prepareReply).toHaveBeenCalledWith("Ajoute une recherche aux clients existants.", { planningMode: "iphone_local" });
    expect(send).toHaveBeenCalledTimes(1);
    expect(openLocalPlan).toHaveBeenCalledWith("goal_next");
    expect(openGoal).not.toHaveBeenCalled();
  });

  it.each([
    { status: "running" as const, project_id: "project_1", active_goal_id: "goal_1" },
    { status: "completed" as const, project_id: null, active_goal_id: "goal_1" },
    { status: "completed" as const, project_id: undefined, active_goal_id: "goal_1" },
    { status: "completed" as const, project_id: "project_1", active_goal_id: "goal_newer" },
  ])("does not offer a local continuation for unavailable context %j", async ({ status, project_id, active_goal_id }) => {
    load.mockResolvedValue({ conversation: { ...conversation, project_id, active_goal_id }, prepareReply });
    await render(<GoalConversation {...props} goal={{ ...props.goal, status }} onOpenLocalPlan={openLocalPlan} />);
    await screen.findByText(question.content);
    expect(screen.queryByRole("button", { name: "Planifier la suite sur l’iPhone" })).not.toBeOnTheScreen();
  });

  it("keeps the local continuation mode when explicitly retrying an uncertain creation", async () => {
    const user = userEvent.setup();
    load.mockResolvedValue({ conversation: { ...conversation, pending_question_id: null, project_id: "project_1" }, prepareReply });
    send.mockRejectedValueOnce(new Error("Réponse perdue"));
    send.mockResolvedValueOnce(localContinuation);
    await render(<GoalConversation {...props} goal={{ ...props.goal, status: "completed" }} onOpenLocalPlan={openLocalPlan} />);
    await screen.findByText(question.content);
    await fireEvent.changeText(screen.getByLabelText("Message pour le projet"), "Ajoute le calendrier.");
    await user.press(screen.getByRole("button", { name: "Planifier la suite sur l’iPhone" }));
    await screen.findByText(/son identifiant reste inchangé/);
    expect(screen.queryByRole("button", { name: "Planifier la suite sur l’iPhone" })).not.toBeOnTheScreen();
    await user.press(screen.getByRole("button", { name: "Réessayer le même envoi" }));
    expect(prepareReply).toHaveBeenCalledTimes(1);
    expect(prepareReply).toHaveBeenCalledWith("Ajoute le calendrier.", { planningMode: "iphone_local" });
    expect(send).toHaveBeenCalledTimes(2);
    expect(openLocalPlan).toHaveBeenCalledWith("goal_next");
    expect(openGoal).not.toHaveBeenCalled();
  });

  it("preserves a stale local continuation instruction after 409 without another POST", async () => {
    const user = userEvent.setup();
    load.mockResolvedValue({ conversation: { ...conversation, pending_question_id: null, project_id: "project_1" }, prepareReply });
    send.mockRejectedValueOnce(new ApiError(409, "newer goal is active"));
    await render(<GoalConversation {...props} goal={{ ...props.goal, status: "completed" }} onOpenLocalPlan={openLocalPlan} />);
    await screen.findByText(question.content);
    await fireEvent.changeText(screen.getByLabelText("Message pour le projet"), "Ajoute le calendrier.");
    await user.press(screen.getByRole("button", { name: "Planifier la suite sur l’iPhone" }));
    await screen.findByText(/Relisez la question actuelle/);
    expect(screen.getByLabelText("Message pour le projet")).toHaveProp("value", "Ajoute le calendrier.");
    expect(send).toHaveBeenCalledTimes(1);
    expect(openLocalPlan).not.toHaveBeenCalled();
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
  it("preserves the uncertain reply and warning while recovering a deferred refresh", async () => {
    const user = userEvent.setup();
    let rejectSend!: (error: Error) => void;
    send.mockImplementationOnce(() => new Promise((_, reject) => { rejectSend = reject; }));
    const view = await render(<GoalConversation {...props} />);
    await screen.findByText(question.content);
    await fireEvent.changeText(screen.getByLabelText("Réponse à la question du projet"), "Une interface web.");
    await user.press(screen.getByRole("button", { name: "Répondre à la question" }));
    await view.rerender(<GoalConversation {...props} disabled />);
    await view.rerender(<GoalConversation {...props} disabled={false} />);
    await act(async () => rejectSend(new Error("Connexion interrompue")));

    expect(await screen.findByText(/son identifiant reste inchangé/)).toBeOnTheScreen();
    expect(load).toHaveBeenCalledTimes(2);
    expect(screen.getByLabelText("Réponse à la question du projet")).toHaveProp("value", "Une interface web.");
    expect(screen.getByLabelText("Réponse à la question du projet")).toHaveProp("editable", false);
    expect(screen.getByRole("button", { name: "Réessayer le même envoi" })).toBeEnabled();
    expect(send).toHaveBeenCalledTimes(1);
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
