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

  const localConversation = { ...conversation, active_goal_id: "goal_1", pending_question_id: null, project_id: "project_1" };
  const localGoal = { ...projectGoalFixture, goal: { ...projectGoalFixture.goal, id: "goal_next", status: "planning",
    current_phase: "awaiting_local_plan", started_at: null, step_count: 0, replan_count: 0, model_call_count: 0 }, nodes: [], result: null };
  it("creates the explicit local continuation with an immutable mode and idempotency key across retry", async () => {
    request.mockResolvedValueOnce(response(localConversation)).mockRejectedValueOnce(new Error("reply lost")).mockResolvedValueOnce(response(localGoal));
    const session = await getGoalConversation("goal_1");
    const attempt = session.prepareReply("Ajoute une recherche aux contacts existants.", { planningMode: "iphone_local" });
    await expect(attempt.send()).rejects.toThrow("reply lost");
    await expect(attempt.send()).resolves.toEqual(localGoal);
    expect(request.mock.calls[1][0]).toBe("https://control.example/goals/goal_1/messages");
    expect(JSON.parse(request.mock.calls[1][1]?.body as string)).toEqual({
      message: "Ajoute une recherche aux contacts existants.", client_message_id: attempt.clientMessageId,
      reply_to_message_id: null, planning_mode: "iphone_local",
    });
    expect(request.mock.calls[2]).toEqual(request.mock.calls[1]);
    await attempt.send();
    expect(request).toHaveBeenCalledTimes(3);
  });

  it.each([null, undefined])("does not send a local continuation without a confirmed project (%s)", async (projectId) => {
    request.mockResolvedValueOnce(response({ ...localConversation, project_id: projectId }));
    const session = await getGoalConversation("goal_1");
    expect(() => session.prepareReply("Ajoute une recherche.", { planningMode: "iphone_local" })).toThrow("Aucun projet lié");
    expect(request).toHaveBeenCalledTimes(1);
  });

  it("does not redirect local continuation creation to a newer active goal", async () => {
    request.mockResolvedValueOnce(response({ ...localConversation, active_goal_id: "goal_newer" }));
    const session = await getGoalConversation("goal_1");
    expect(() => session.prepareReply("Ajoute une recherche.", { planningMode: "iphone_local" })).toThrow("travail le plus récent");
    expect(request).toHaveBeenCalledTimes(1);
  });

  it.each([
    { id: "goal_1" }, { started_at: "2030-01-01T00:01:00Z" }, { current_phase: "project_continue" }, { model_call_count: 1 },
  ])("does not accept an automatically started or wrong-goal local continuation %j", async (changed) => {
    request.mockResolvedValueOnce(response(localConversation)).mockResolvedValueOnce(response({ ...localGoal, goal: { ...localGoal.goal, ...changed } }));
    const session = await getGoalConversation("goal_1");
    await expect(session.prepareReply("Ajoute une recherche.", { planningMode: "iphone_local" }).send()).rejects.toThrow("n’a pas confirmé");
    expect(request).toHaveBeenCalledTimes(2);
  });

  it("fences a local continuation response when pairing changes in flight", async () => {
    request.mockResolvedValueOnce(response(localConversation));
    const attempt = (await getGoalConversation("goal_1")).prepareReply("Ajoute une recherche.", { planningMode: "iphone_local" });
    request.mockImplementationOnce(async () => { connection.token = "replacement"; return response(localGoal); });
    await expect(attempt.send()).rejects.toThrow("connexion jumelée a changé");
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
    expect(() => parseGoalConversation({ ...conversation, project_id: "../wrong" })).toThrow();
    expect(parseGoalConversation({ ...conversation, project_id: null }).project_id).toBeNull();
    expect(parseGoalConversation({ ...conversation, project_id: "project_1" }).project_id).toBe("project_1");
  });
  it("preserves real failed checks and bounded UTF-8 source", () => {
    const parsed = parseProjectPreview({ ...projectFixture, state: "building", files: [{ path: "app.py", content: "é".repeat(32000) }], checks: [{ ...projectFixture.checks[0], status: "failed", exit_code: 1 }] });
    expect(parsed.files[0].content).toHaveLength(32000);
    expect(parsed.checks[0].status).toBe("failed");
  });
});
