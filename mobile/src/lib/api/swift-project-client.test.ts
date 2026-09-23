import { beforeEach, describe, expect, it, jest } from "@jest/globals";
import { fetch } from "expo/fetch";
import * as SecureStore from "expo-secure-store";
import { cancelSwiftProjectValidation, getSwiftProjectValidation, reviewGoalProject } from "./client";
import { parseSwiftTarget, parseSwiftValidation } from "./project";
import { swiftProjectFixture, swiftReceiptFixture, swiftValidationFixture } from "@/testing/swift-project-fixtures";

jest.mock("expo-secure-store", () => ({ deleteItemAsync: jest.fn(), getItemAsync: jest.fn(), setItemAsync: jest.fn() }));
jest.mock("expo/fetch", () => ({ fetch: jest.fn() }));
jest.mock("@/lib/state/mutation-outbox", () => ({ mutationOutbox: {} }));
jest.mock("@/lib/state/replica", () => ({ applyBootstrap: jest.fn(), upsertEvent: jest.fn() }));
const request = jest.mocked(fetch);
let token = "paired-token";
const response = (body: unknown) => ({ ok: true, status: 200, json: async () => body }) as never;
const options = { agentId: "agent_mac", operation: "test" as const, target: { kind: "swiftpm" as const } };
beforeEach(() => {
  jest.resetAllMocks(); token = "paired-token";
  jest.mocked(SecureStore.getItemAsync).mockImplementation(async (key) => key === "mongars.connection.v1" ? JSON.stringify({ baseUrl: "https://control.example", token }) : null);
});

describe("Swift revision transport", () => {
  it("binds consent to reviewed files and preserves the same request across uncertain retries", async () => {
    request.mockResolvedValueOnce(response(swiftProjectFixture)).mockRejectedValueOnce(new Error("lost")).mockResolvedValueOnce(response(swiftValidationFixture));
    const review = await reviewGoalProject("goal_1");
    const attempt = review.prepareSwiftValidation(options);
    expect(request).toHaveBeenCalledTimes(1);
    await expect(attempt.send()).rejects.toThrow("lost");
    expect(await attempt.send()).toEqual(swiftValidationFixture);
    expect(request.mock.calls[2]).toEqual(request.mock.calls[1]);
    expect(JSON.parse(request.mock.calls[1][1]?.body as string)).toEqual({ revision_id: swiftProjectFixture.revision_id,
      sha256: swiftProjectFixture.sha256, agent_id: "agent_mac", operation: "test", target: { kind: "swiftpm" }, execution_consent: true,
      idempotency_key: attempt.idempotencyKey });
    await attempt.send(); expect(request).toHaveBeenCalledTimes(3);
  });
  it("keeps the reviewed source identity immutable when the returned preview is modified", async () => {
    request.mockResolvedValueOnce(response(swiftProjectFixture)).mockResolvedValueOnce(response(swiftValidationFixture));
    const review = await reviewGoalProject("goal_1");
    review.project.revision_id = "different";
    review.project.sha256 = "0".repeat(64);
    const attempt = review.prepareSwiftValidation(options);
    await attempt.send();
    expect(JSON.parse(request.mock.calls[1][1]?.body as string)).toMatchObject({ revision_id: swiftProjectFixture.revision_id, sha256: swiftProjectFixture.sha256 });
  });
  it("does not make a legacy native revision writable merely because Python checks passed", async () => {
    request.mockResolvedValueOnce(response({ ...swiftProjectFixture, state: "ready" }));
    const review = await reviewGoalProject("goal_1");
    await expect(review.prepareApproval()).rejects.toThrow(/vérifications/);
    expect(request).toHaveBeenCalledTimes(1);
  });
  it("fences a prepared validation when pairing changes", async () => {
    request.mockResolvedValueOnce(response(swiftProjectFixture));
    const attempt = (await reviewGoalProject("goal_1")).prepareSwiftValidation(options);
    token = "changed";
    await expect(attempt.send()).rejects.toThrow(/connexion jumelée a changé/);
    expect(request).toHaveBeenCalledTimes(1);
  });
  it("rejects a successful response for other source or selected operation", async () => {
    request.mockResolvedValueOnce(response(swiftProjectFixture)).mockResolvedValueOnce(response({ ...swiftValidationFixture, operation: "build" }));
    const attempt = (await reviewGoalProject("goal_1")).prepareSwiftValidation(options);
    await expect(attempt.send()).rejects.toThrow(/ne correspond pas/);
  });
  it("reads absence as neutral and cancellation through the selected validation", async () => {
    request.mockResolvedValueOnce({ ok: false, status: 404, json: async () => ({ detail: "missing" }) } as never);
    expect(await getSwiftProjectValidation("goal_1")).toBeNull();
    request.mockResolvedValueOnce(response({ ...swiftValidationFixture, status: "cancelled" }));
    expect((await cancelSwiftProjectValidation("goal_1", "swift_1")).status).toBe("cancelled");
    expect(request.mock.calls[1][0]).toBe("https://control.example/goals/goal_1/project/swift-validation/swift_1/cancel");
  });
  it.each(["unknown_tool", "invalid_arguments", "policy_denied"] as const)("recognizes server diagnostic %s", async (diagnostic) => {
    request.mockResolvedValueOnce({ ok: false, status: 422, headers: { get: () => diagnostic }, json: async () => ({ detail: "raw server detail" }) } as never);
    await expect(getSwiftProjectValidation("goal_1")).rejects.toMatchObject({ diagnostic, message: expect.stringContaining("Aucune action") });
  });
  it("ignores an unknown diagnostic and preserves existing messages", async () => {
    request.mockResolvedValueOnce({ ok: false, status: 422, headers: { get: () => "invented" }, json: async () => ({ detail: "existing error" }) } as never);
    await expect(getSwiftProjectValidation("goal_1")).rejects.toMatchObject({ diagnostic: undefined, message: "existing error" });
  });
});

describe("native validation receipt parser", () => {
  const passed = { ...swiftValidationFixture, status: "passed", receipt: swiftReceiptFixture };
  it("accepts verified tests without exposing raw artifacts or extra private data", () => {
    const result = parseSwiftValidation(passed);
    expect(result.receipt).toEqual({ tests_executed: 1, test_failures: 0, duration_ms: 300, source_sha256: swiftValidationFixture.source_sha256 });
    expect(result.receipt).not.toHaveProperty("artifact_directory");
  });
  it.each([
    { tests_executed: 0 }, { source_unchanged: false }, { source_sha256: "0".repeat(64) }, { test_failures: 1 },
    { project_revision: { ...swiftReceiptFixture.project_revision, revision_id: "other" } }, { duration_ms: 120001 },
  ])("rejects invalid passed evidence %j", (changed) => expect(() => parseSwiftValidation({ ...passed, receipt: { ...swiftReceiptFixture, ...changed } })).toThrow());
  it("does not accept a malformed failed receipt", () => expect(() => parseSwiftValidation({ ...swiftValidationFixture, status: "failed", receipt: { files: [] } })).toThrow());
  it.each([{ kind: "swiftpm", command: "sh" }, { kind: "xcode", project: "../Hello.xcodeproj", scheme: "Hello", destination: "iPhone" },
    { kind: "xcode", project: "Hello.xcodeproj", scheme: "Hello", destination: "platform=iOS" },
  ])("rejects arbitrary target arguments", (target) => expect(() => parseSwiftTarget(target)).toThrow());
});
