import { beforeEach, describe, expect, it, jest } from "@jest/globals";
import { fetch } from "expo/fetch";
import * as SecureStore from "expo-secure-store";

import { getGoalConversation, reviewGoalProject } from "@/lib/api/client";
import { parseGoalConversation, parseProjectPreview } from "@/lib/api/project";
import { projectFixture, projectGoalFixture } from "@/testing/project-fixtures";

jest.mock("expo-secure-store", () => ({ deleteItemAsync: jest.fn(), getItemAsync: jest.fn(), setItemAsync: jest.fn() }));
jest.mock("expo/fetch", () => ({ fetch: jest.fn() }));
jest.mock("@/lib/state/mutation-outbox", () => ({ mutationOutbox: {} }));
jest.mock("@/lib/state/replica", () => ({ applyBootstrap: jest.fn(), upsertEvent: jest.fn() }));
const request = jest.mocked(fetch);
const getItem = jest.mocked(SecureStore.getItemAsync);
let connection: { baseUrl: string; token: string };
const conversation = {
  messages: [{ id: "message_question", goal_run_id: "goal_active", role: "assistant", content: "Une interface web?", created_at: "2030-01-01T00:00:00Z" }],
  active_goal_id: "goal_active", pending_question_id: "message_question",
};
const receipt = { task_id: "task_write", tool_call_id: "call_write", approval_id: "approval_write" };
function response(body: unknown) { return { ok: true, status: 200, json: async () => body } as never; }

beforeEach(() => {
  jest.resetAllMocks();
  connection = { baseUrl: "https://control.example", token: "paired-test-token" };
  getItem.mockImplementation(async (key) => key === "mongars.connection.v1" ? JSON.stringify(connection) : null);
});

describe("project conversation transport", () => {
  it("binds an explicit answer and uncertain retry to one message ID and the active linked goal", async () => {
    request.mockResolvedValueOnce(response(conversation)).mockRejectedValueOnce(new Error("reply response lost")).mockResolvedValueOnce(response(projectGoalFixture));
    const session = await getGoalConversation("goal_old");
    const attempt = session.prepareReply("Oui, une interface web et une base persistante.");
    await expect(attempt.send()).rejects.toThrow("reply response lost");
    await expect(attempt.send()).resolves.toEqual(projectGoalFixture);
    const [url, options] = request.mock.calls[1];
    expect(url).toBe("https://control.example/goals/goal_active/messages");
    expect(JSON.parse(options?.body as string)).toEqual({ message: "Oui, une interface web et une base persistante.", client_message_id: attempt.clientMessageId, reply_to_message_id: "message_question" });
    expect(request.mock.calls[2]).toEqual(request.mock.calls[1]);
    await attempt.send();
    expect(request).toHaveBeenCalledTimes(3);
  });
  it.each(["origin", "token"])("blocks a reply after the paired %s changes", async (changed) => {
    request.mockResolvedValueOnce(response(conversation));
    const attempt = (await getGoalConversation("goal_1")).prepareReply("Oui");
    connection = changed === "origin" ? { baseUrl: "https://other.example", token: "other" } : { ...connection, token: "other" };
    await expect(attempt.send()).rejects.toThrow(/connexion jumelée a changé/);
    expect(request).toHaveBeenCalledTimes(1);
  });
  it("rejects conversation data from an obsolete connection", async () => {
    request.mockImplementationOnce(async () => { connection.token = "changed"; return response(conversation); });
    await expect(getGoalConversation("goal_1")).rejects.toThrow(/connexion jumelée a changé/);
  });
  it("accepts a full 4000-character reply and rejects oversized replies before posting", async () => {
    request.mockResolvedValueOnce(response(conversation)).mockResolvedValueOnce(response(projectGoalFixture));
    const session = await getGoalConversation("goal_1");
    await session.prepareReply("é".repeat(4000)).send();
    expect(() => session.prepareReply("x".repeat(4001))).toThrow();
    expect(request).toHaveBeenCalledTimes(2);
  });
});

describe("project review transport", () => {
  it("fetches explicitly and binds one approval to the reviewed revision and digest", async () => {
    request.mockResolvedValueOnce(response(projectFixture)).mockResolvedValueOnce(response(receipt));
    const review = await reviewGoalProject("goal_1");
    expect(request).toHaveBeenCalledTimes(1);
    await expect(review.prepareApproval()).resolves.toEqual(receipt);
    expect(JSON.parse(request.mock.calls[1][1]?.body as string)).toEqual({ revision_id: "revision_2", sha256: projectFixture.sha256 });
    await expect(review.prepareApproval()).rejects.toThrow(/Actualisez/);
    expect(request).toHaveBeenCalledTimes(2);
  });
  it("blocks stale authority and never repeats an uncertain apply", async () => {
    request.mockResolvedValueOnce(response(projectFixture)).mockRejectedValueOnce(new Error("lost"));
    const review = await reviewGoalProject("goal_1");
    await expect(review.prepareApproval()).rejects.toThrow("lost");
    await expect(review.prepareApproval()).rejects.toThrow(/Actualisez/);
    request.mockResolvedValueOnce(response(projectFixture));
    const next = await reviewGoalProject("goal_1");
    connection.token = "changed";
    await expect(next.prepareApproval()).rejects.toThrow(/connexion jumelée a changé/);
    expect(request).toHaveBeenCalledTimes(3);
  });
  it("cannot prepare a failed-check revision even if labelled ready", async () => {
    request.mockResolvedValueOnce(response({ ...projectFixture, checks: [{ ...projectFixture.checks[0], status: "failed", exit_code: 1 }] }));
    const review = await reviewGoalProject("goal_1");
    await expect(review.prepareApproval()).rejects.toThrow(/vérifications/);
    expect(request).toHaveBeenCalledTimes(1);
  });
});

describe("private project validation", () => {
  it.each(["../escape", "/absolute.py", "src\\file.py", ".git/config", ".env", ".env.local", "config/.ENV.production", ".npmrc", ".pypirc", "config/auth.json", ".ssh/id_rsa", "private.key", "node_modules/file.js"])("rejects unsafe path %s", (path) => {
    expect(() => parseProjectPreview({ ...projectFixture, files: [{ path, content: "x" }] })).toThrow();
  });
  it.each([".env.example", ".env.sample", "config/.env.template"])("accepts configuration template %s", (path) => {
    expect(parseProjectPreview({ ...projectFixture, files: [{ path, content: "PORT=3000" }] }).files[0].path).toBe(path);
  });
  it.each([
    { label: "case collisions", files: [{ path: "app.py", content: "x" }, { path: "APP.py", content: "y" }] },
    { label: "file-directory collisions", files: [{ path: "src", content: "x" }, { path: "src/app.py", content: "y" }] },
    { label: "per-file bytes", files: [{ path: "app.py", content: "é".repeat(32001) }] },
    { label: "total bytes", files: Array.from({ length: 16 }, (_, i) => ({ path: `file${i}.py`, content: "x".repeat(64000) })) },
  ])("rejects $label", ({ files }) => expect(() => parseProjectPreview({ ...projectFixture, files })).toThrow());
  it("rejects invalid evidence, digest, revision and question binding", () => {
    expect(() => parseProjectPreview({ ...projectFixture, sha256: "invalid" })).toThrow();
    expect(() => parseProjectPreview({ ...projectFixture, revision: 0 })).toThrow();
    expect(() => parseProjectPreview({ ...projectFixture, checks: [{ ...projectFixture.checks[0], exit_code: 1 }] })).toThrow();
    expect(() => parseGoalConversation({ ...conversation, pending_question_id: "missing" })).toThrow();
  });
  it("preserves real failed checks and bounded UTF-8 source", () => {
    const parsed = parseProjectPreview({ ...projectFixture, state: "building", files: [{ path: "app.py", content: "é".repeat(32000) }], checks: [{ ...projectFixture.checks[0], status: "failed", exit_code: 1 }] });
    expect(parsed.files[0].content).toHaveLength(32000);
    expect(parsed.checks[0].status).toBe("failed");
  });
});
