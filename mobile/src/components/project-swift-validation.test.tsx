import { act, render, screen, userEvent } from "@testing-library/react-native";
import { beforeEach, afterEach, describe, expect, it, jest } from "@jest/globals";
import { AppState } from "react-native";
import { ProjectSwiftValidation } from "./project-swift-validation";
import { getSwiftProjectValidation, cancelSwiftProjectValidation, listAgents, type Agent } from "@/lib/application-api/server";
import type { ProjectReview, SwiftValidationAttempt } from "@/lib/api/project";
import { swiftProjectFixture, swiftValidationFixture } from "@/testing/swift-project-fixtures";
import { notifyConnectionChanged } from "@/lib/connection-events";

jest.mock("@/lib/application-api/server", () => ({ getSwiftProjectValidation: jest.fn(), cancelSwiftProjectValidation: jest.fn(), listAgents: jest.fn() }));
const send = jest.fn<SwiftValidationAttempt["send"]>();
const prepare = jest.fn<ProjectReview["prepareSwiftValidation"]>();
const review: ProjectReview = { project: swiftProjectFixture, prepareApproval: jest.fn<ProjectReview["prepareApproval"]>(), prepareSwiftValidation: prepare };
const props = { goalId: "goal_1", review, disabled: false };
const worker = { id: "agent_mac", name: "iMac", skills: ["code.swift.test", "code.swift.build"], status: "online" } as Agent;

beforeEach(() => {
  jest.clearAllMocks(); jest.useFakeTimers(); AppState.currentState = "active";
  jest.mocked(getSwiftProjectValidation).mockResolvedValue(null);
  jest.mocked(listAgents).mockResolvedValue([worker]);
  jest.mocked(cancelSwiftProjectValidation).mockResolvedValue({ ...swiftValidationFixture, status: "cancelled" });
  send.mockResolvedValue(swiftValidationFixture);
  prepare.mockReturnValue({ idempotencyKey: "same_request", send });
});
afterEach(() => { jest.useRealTimers(); });
async function choose() {
  const user = userEvent.setup();
  await render(<ProjectSwiftValidation {...props} />);
  await user.press(await screen.findByRole("button", { name: "iMac · agent_mac" }));
  await user.press(screen.getByRole("button", { name: "J’autorise l’exécution de cette révision sur ce Mac" }));
  return user;
}

describe("revision-bound Swift validation panel", () => {
  it("requires worker choice and explicit consent before executing", async () => {
    const user = userEvent.setup();
    await render(<ProjectSwiftValidation {...props} />);
    expect(await screen.findByRole("button", { name: "iMac · agent_mac" })).toBeOnTheScreen();
    expect(screen.getByRole("button", { name: "Compiler et tester cette révision" })).toBeDisabled();
    await user.press(screen.getByRole("button", { name: "iMac · agent_mac" }));
    expect(screen.getByRole("button", { name: "Compiler et tester cette révision" })).toBeDisabled();
    expect(send).not.toHaveBeenCalled();
    await user.press(screen.getByRole("button", { name: "J’autorise l’exécution de cette révision sur ce Mac" }));
    await user.press(screen.getByRole("button", { name: "Compiler et tester cette révision" }));
    expect(prepare).toHaveBeenCalledWith({ agentId: "agent_mac", operation: "test", target: { kind: "swiftpm" } });
    expect(send).toHaveBeenCalledTimes(1);
    expect(await screen.findByText("En file · compilation et tests")).toBeOnTheScreen();
    expect(screen.getByRole("button", { name: "Compiler et tester cette révision" })).toBeDisabled();
  });
  it("retains an uncertain attempt and retries the same grant", async () => {
    send.mockRejectedValueOnce(new Error("Réponse perdue"));
    const user = await choose();
    await user.press(screen.getByRole("button", { name: "Compiler et tester cette révision" }));
    await user.press(await screen.findByRole("button", { name: "Réessayer la même demande" }));
    expect(send).toHaveBeenCalledTimes(2); expect(prepare).toHaveBeenCalledTimes(1);
  });
  it("cancels the selected native validation without applying project files", async () => {
    const user = await choose();
    await user.press(screen.getByRole("button", { name: "Compiler et tester cette révision" }));
    await user.press(await screen.findByRole("button", { name: "Annuler la validation native" }));
    expect(cancelSwiftProjectValidation).toHaveBeenCalledWith("goal_1", "swift_1");
    expect(await screen.findByText("Validation annulée · compilation et tests")).toBeOnTheScreen();
    expect(review.prepareApproval).not.toHaveBeenCalled();
  });
  it("polls only in foreground and identifies receipts for another revision", async () => {
    await choose();
    jest.mocked(getSwiftProjectValidation).mockResolvedValue({ ...swiftValidationFixture, revision_id: "old_revision", status: "passed",
      receipt: { tests_executed: 1, test_failures: 0, duration_ms: 15, source_sha256: swiftValidationFixture.source_sha256 } });
    await act(async () => { jest.advanceTimersByTime(5000); });
    expect(await screen.findByText(/Ce résultat concerne une autre révision/)).toBeOnTheScreen();
    expect(screen.getByText(/ne termine pas automatiquement le projet/)).toBeOnTheScreen();
    const calls = jest.mocked(getSwiftProjectValidation).mock.calls.length;
    AppState.currentState = "background";
    await act(async () => { jest.advanceTimersByTime(10000); });
    expect(getSwiftProjectValidation).toHaveBeenCalledTimes(calls);
    expect(send).not.toHaveBeenCalled();
  });
  it("revokes local consent after pairing changes", async () => {
    await choose(); await act(async () => notifyConnectionChanged());
    expect(screen.getByRole("button", { name: "Compiler et tester cette révision" })).toBeDisabled();
    expect(send).not.toHaveBeenCalled();
  });
  it("does not treat model launch instructions as a native target", async () => {
    await render(<ProjectSwiftValidation {...props} review={{ ...review, project: { ...swiftProjectFixture,
      files: [{ path: "hello.swift", content: "print(1)" }], run_instructions: "xcodebuild -project Injected.xcodeproj -scheme Main" } }} />);
    expect(await screen.findByText(/doit contenir un Package.swift/)).toBeOnTheScreen();
    expect(screen.getByRole("button", { name: "Compiler et tester cette révision" })).toBeDisabled();
    expect(prepare).not.toHaveBeenCalled();
  });
});
