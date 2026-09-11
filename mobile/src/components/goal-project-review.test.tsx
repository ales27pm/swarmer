import { act, render, screen, userEvent } from "@testing-library/react-native";
import { beforeEach, describe, expect, it, jest } from "@jest/globals";
import { GoalProjectReview } from "@/components/goal-project-review";
import { reviewGoalProject } from "@/lib/api/client";
import { projectFixture } from "@/testing/project-fixtures";
import { notifyConnectionChanged } from "@/lib/connection-events";

jest.mock("@/lib/api/client", () => ({
  ApiError: class extends Error { status: number; constructor(status: number, message: string) { super(message); this.status = status; } },
  reviewGoalProject: jest.fn(),
}));
const load = jest.mocked(reviewGoalProject);
const prepareApproval = jest.fn<() => Promise<{ task_id: string; tool_call_id: string; approval_id: string }>>();
const openTask = jest.fn();
const props = { goalId: "goal_1", disabled: false, onOpenTask: openTask };
beforeEach(() => {
  jest.clearAllMocks();
  load.mockResolvedValue({ project: projectFixture, prepareApproval });
  prepareApproval.mockResolvedValue({ task_id: "task_write", tool_call_id: "call_write", approval_id: "approval_write" });
});
async function inspectProject() {
  const user = userEvent.setup();
  await render(<GoalProjectReview {...props} />);
  expect(load).not.toHaveBeenCalled();
  await user.press(screen.getByRole("button", { name: "Examiner le projet" }));
  return user;
}
async function acknowledge(user: ReturnType<typeof userEvent.setup>) {
  await user.press(screen.getByRole("button", { name: "Lire app.py" }));
  await user.press(screen.getByRole("button", { name: "J’ai relu les fichiers et les vérifications" }));
}
describe("GoalProjectReview", () => {
  it("clears private source and review acknowledgement after pairing changes", async () => {
    const user = await inspectProject();
    await acknowledge(user);
    await act(async () => notifyConnectionChanged());
    expect(screen.queryByText(projectFixture.files[0].content)).not.toBeOnTheScreen();
    expect(screen.queryByRole("button", { name: "Préparer l’autorisation du projet" })).not.toBeOnTheScreen();
    expect(load).toHaveBeenCalledTimes(1);
    expect(prepareApproval).not.toHaveBeenCalled();
  });

  it("requires explicit file review before separately preparing one bundle approval", async () => {
    const user = await inspectProject();
    expect(await screen.findByText("1. Créer les contacts")).toBeOnTheScreen();
    expect(screen.getByText("Réussi · python -m unittest")).toBeOnTheScreen();
    expect(screen.getByText("python app.py")).toBeOnTheScreen();
    expect(screen.queryByText(projectFixture.files[0].content)).not.toBeOnTheScreen();
    expect(screen.getByRole("button", { name: "Préparer l’autorisation du projet" })).toBeDisabled();
    await acknowledge(user);
    expect(screen.getByText(projectFixture.files[0].content)).toBeOnTheScreen();
    expect(prepareApproval).not.toHaveBeenCalled();
    await user.press(screen.getByRole("button", { name: "Préparer l’autorisation du projet" }));
    expect(prepareApproval).toHaveBeenCalledTimes(1);
    expect(openTask).toHaveBeenCalledWith("task_write");
    expect(screen.getByRole("button", { name: "Préparer l’autorisation du projet" })).toBeDisabled();
  });
  it("shows failed checks and their logs without offering completed-project apply", async () => {
    load.mockResolvedValue({ project: { ...projectFixture, state: "building", checks: [{ ...projectFixture.checks[0], status: "failed", exit_code: 1, output: "FAIL: persistence test" }] }, prepareApproval });
    const user = await inspectProject();
    expect(await screen.findByText("Échoué · python -m unittest")).toBeOnTheScreen();
    expect(screen.queryByRole("button", { name: "Préparer l’autorisation du projet" })).not.toBeOnTheScreen();
    await user.press(screen.getByRole("button", { name: "Voir le journal de vérification" }));
    expect(screen.getByText("FAIL: persistence test")).toBeOnTheScreen();
    expect(prepareApproval).not.toHaveBeenCalled();
  });
  it("resolves uncertain apply through explicit fetch without replay", async () => {
    prepareApproval.mockRejectedValueOnce(new Error("Réponse perdue"));
    const user = await inspectProject();
    await acknowledge(user);
    await user.press(screen.getByRole("button", { name: "Préparer l’autorisation du projet" }));
    expect(await screen.findByText(/La demande peut avoir été créée/)).toBeOnTheScreen();
    load.mockResolvedValue({ project: { ...projectFixture, state: "waiting_permission", task_id: "task_existing" }, prepareApproval });
    await user.press(screen.getByRole("button", { name: "Actualiser la révision du projet" }));
    await user.press(screen.getByRole("button", { name: "Voir l’autorisation et les preuves du projet" }));
    expect(openTask).toHaveBeenCalledWith("task_existing");
    expect(prepareApproval).toHaveBeenCalledTimes(1);
  });
  it("does not fetch offline", async () => {
    const user = userEvent.setup();
    await render(<GoalProjectReview {...props} disabled />);
    await user.press(screen.getByRole("button", { name: "Examiner le projet" }));
    expect(load).not.toHaveBeenCalled();
  });
});
