import { act, render, screen, userEvent } from "@testing-library/react-native";
import { beforeEach, describe, expect, it, jest } from "@jest/globals";

import { GoalWritingDraft } from "@/components/goal-writing-draft";
import { getGoalWritingDraft, type GoalWritingDraft as WritingDraft } from "@/lib/application-api/server";
import { notifyConnectionChanged } from "@/lib/connection-events";

jest.mock("@/lib/application-api/server", () => ({ getGoalWritingDraft: jest.fn() }));
const load = jest.mocked(getGoalWritingDraft);
const props = { goalId: "goal_1", nodeId: "node_1", workerJobId: "job_1", disabled: false };
const draft: WritingDraft = {
  schema_version: "1.0", content_trust: "untrusted", goal_run_id: "goal_1", node_id: "node_1", worker_job_id: "job_1",
  text: "Plan proposé\n<script>ne pas exécuter</script>\nContacts, soumissions et calendrier.", summary: "Plan CRM à relire", sha256: "a".repeat(64),
};
function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((settle) => { resolve = settle; });
  return { promise, resolve };
}
beforeEach(() => { jest.clearAllMocks(); load.mockResolvedValue(draft); });

describe("GoalWritingDraft", () => {
  it("loads only on request and presents complete selectable plain text", async () => {
    const user = userEvent.setup();
    await render(<GoalWritingDraft {...props} />);
    expect(load).not.toHaveBeenCalled();
    await user.press(screen.getByRole("button", { name: "Lire le document complet" }));
    expect(load).toHaveBeenCalledWith("goal_1", "node_1", "job_1", expect.any(Function));
    expect(await screen.findByText(draft.text)).toHaveProp("selectable", true);
    expect(screen.getByText(draft.summary)).toHaveProp("selectable", true);
    expect(screen.getByText(/Les actions décrites ne sont pas exécutées/)).toBeOnTheScreen();
    expect(screen.queryByRole("button", { name: /Exécuter|Appliquer|Autoriser/ })).not.toBeOnTheScreen();
  });

  it("does not fetch while offline", async () => {
    const user = userEvent.setup();
    await render(<GoalWritingDraft {...props} disabled />);
    await user.press(screen.getByRole("button", { name: "Lire le document complet" }));
    expect(load).not.toHaveBeenCalled();
  });

  it("prevents overlapping loads", async () => {
    const pending = deferred<WritingDraft>(); load.mockReturnValue(pending.promise);
    const user = userEvent.setup();
    await render(<GoalWritingDraft {...props} />);
    const button = screen.getByRole("button", { name: "Lire le document complet" });
    await user.press(button); await user.press(button);
    expect(load).toHaveBeenCalledTimes(1);
    await act(async () => pending.resolve(draft));
    expect(screen.getByText(draft.text)).toBeOnTheScreen();
  });

  it("offers an explicit retry without exposing transport errors", async () => {
    load.mockRejectedValueOnce(new Error("private transport detail"));
    const user = userEvent.setup();
    await render(<GoalWritingDraft {...props} />);
    await user.press(screen.getByRole("button", { name: "Lire le document complet" }));
    expect(await screen.findByRole("button", { name: "Réessayer le document" })).toBeOnTheScreen();
    expect(screen.queryByText("private transport detail")).not.toBeOnTheScreen();
    await user.press(screen.getByRole("button", { name: "Réessayer le document" }));
    expect(await screen.findByText(draft.text)).toBeOnTheScreen();
    expect(load).toHaveBeenCalledTimes(2);
  });

  it("clears a loaded document when pairing changes", async () => {
    const user = userEvent.setup();
    await render(<GoalWritingDraft {...props} />);
    await user.press(screen.getByRole("button", { name: "Lire le document complet" }));
    expect(await screen.findByText(draft.text)).toBeOnTheScreen();
    await act(async () => notifyConnectionChanged());
    expect(screen.queryByText(draft.text)).not.toBeOnTheScreen();
    expect(load).toHaveBeenCalledTimes(1);
  });

  it.each(["pairing", "identity", "offline", "unmount"] as const)("discards a pending response after %s changes", async (reason) => {
    const pending = deferred<WritingDraft>(); load.mockReturnValue(pending.promise);
    const user = userEvent.setup();
    await render(<GoalWritingDraft {...props} />);
    await user.press(screen.getByRole("button", { name: "Lire le document complet" }));
    const accepts = load.mock.calls[0][3]!;
    expect(accepts()).toBe(true);
    if (reason === "pairing") await act(async () => notifyConnectionChanged());
    if (reason === "identity") await screen.rerender(<GoalWritingDraft {...props} workerJobId="job_new" />);
    if (reason === "offline") await screen.rerender(<GoalWritingDraft {...props} disabled />);
    if (reason === "unmount") await screen.unmount();
    expect(accepts()).toBe(false);
    await act(async () => pending.resolve(draft));
    if (reason !== "unmount") expect(screen.queryByText(draft.text)).not.toBeOnTheScreen();
    expect(load).toHaveBeenCalledTimes(1);
  });
});
