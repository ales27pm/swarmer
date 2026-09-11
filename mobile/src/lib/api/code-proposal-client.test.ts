import { beforeEach, describe, expect, it, jest } from "@jest/globals";
import { fetch } from "expo/fetch";
import * as SecureStore from "expo-secure-store";

import { reviewGoalCodeProposal } from "@/lib/api/client";
import { parseGoalCodeProposal } from "@/lib/api/code-proposal";

jest.mock("expo-secure-store", () => ({
  deleteItemAsync: jest.fn(),
  getItemAsync: jest.fn(),
  setItemAsync: jest.fn(),
}));
jest.mock("expo/fetch", () => ({ fetch: jest.fn() }));
jest.mock("@/lib/state/mutation-outbox", () => ({ mutationOutbox: {} }));
jest.mock("@/lib/state/replica", () => ({ applyBootstrap: jest.fn(), upsertEvent: jest.fn() }));

const request = jest.mocked(fetch);
const getItem = jest.mocked(SecureStore.getItemAsync);
const path = "/goals/goal_1/nodes/node_code/code-proposal";
const proposal = {
  node_id: "node_code",
  path: "generated/goal_1/node_code/app.py",
  content: "print('draft only')\n",
  sha256: "a".repeat(64),
  summary: "Proposition Python",
  status: "proposal",
  task_id: null,
};
let connection: { baseUrl: string; token: string };

function response(body: unknown) {
  return { ok: true, status: 200, json: async () => body } as never;
}

describe("code proposal transport", () => {
  beforeEach(() => {
    jest.resetAllMocks();
    connection = { baseUrl: "https://control.example", token: "paired-device-token" };
    getItem.mockImplementation(async (key) => key === "mongars.connection.v1" ? JSON.stringify(connection) : null);
    request.mockResolvedValue(response(proposal));
  });

  it("authenticates the review and binds a single apply attempt to its reviewed digest", async () => {
    request.mockResolvedValueOnce(response(proposal)).mockResolvedValueOnce(response({
      task_id: "tsk_write", tool_call_id: "call_write", approval_id: "apr_write",
    }));
    const review = await reviewGoalCodeProposal("goal_1", "node_code");
    expect(request).toHaveBeenCalledTimes(1);
    expect(request).toHaveBeenNthCalledWith(1, `https://control.example${path}`, {
      headers: { Authorization: "Bearer paired-device-token" },
    });
    await expect(review.prepareApproval()).resolves.toEqual({
      task_id: "tsk_write", tool_call_id: "call_write", approval_id: "apr_write",
    });
    expect(request).toHaveBeenNthCalledWith(2, `https://control.example${path}/apply`, {
      method: "POST",
      headers: { Authorization: "Bearer paired-device-token", "Content-Type": "application/json" },
      body: JSON.stringify({ sha256: proposal.sha256 }),
    });
    await expect(review.prepareApproval()).rejects.toThrow(/Actualisez la proposition/);
    expect(request).toHaveBeenCalledTimes(2);
  });

  it.each(["origin", "token"])("rejects apply after the paired %s changes", async (changed) => {
    const review = await reviewGoalCodeProposal("goal_1", "node_code");
    connection = changed === "origin"
      ? { baseUrl: "https://other.example", token: "other-token" }
      : { ...connection, token: "new-paired-token" };
    await expect(review.prepareApproval()).rejects.toThrow(/connexion jumelée a changé/);
    expect(request).toHaveBeenCalledTimes(1);
  });

  it("never replays an uncertain apply and permits a separate authoritative review", async () => {
    request.mockResolvedValueOnce(response(proposal)).mockRejectedValueOnce(new Error("Response lost"));
    const review = await reviewGoalCodeProposal("goal_1", "node_code");
    await expect(review.prepareApproval()).rejects.toThrow("Response lost");
    await expect(review.prepareApproval()).rejects.toThrow(/Actualisez la proposition/);
    expect(request).toHaveBeenCalledTimes(2);

    request.mockResolvedValueOnce(response({ ...proposal, status: "waiting_permission", task_id: "tsk_existing" }));
    const reconciled = await reviewGoalCodeProposal("goal_1", "node_code");
    expect(reconciled.proposal.task_id).toBe("tsk_existing");
    await expect(reconciled.prepareApproval()).rejects.toThrow(/Actualisez la proposition/);
    expect(request).toHaveBeenCalledTimes(3);
  });

  it("rejects preview data when the paired connection changes during its response", async () => {
    request.mockImplementationOnce(async () => {
      connection = { baseUrl: "https://other.example", token: "other-token" };
      return response(proposal);
    });
    await expect(reviewGoalCodeProposal("goal_1", "node_code")).rejects.toThrow(/connexion jumelée a changé/);
    expect(request).toHaveBeenCalledTimes(1);
  });

  it("rejects an incomplete apply receipt without replaying the request", async () => {
    request.mockResolvedValueOnce(response(proposal)).mockResolvedValueOnce(response({ task_id: "tsk_write" }));
    const review = await reviewGoalCodeProposal("goal_1", "node_code");
    await expect(review.prepareApproval()).rejects.toThrow(/demande d’autorisation attendue/);
    await expect(review.prepareApproval()).rejects.toThrow(/Actualisez la proposition/);
    expect(request).toHaveBeenCalledTimes(2);
  });
});

describe("code proposal review bounds", () => {
  it.each([
    { label: "another node", patch: { node_id: "node_other" } },
    { label: "a bare file path", patch: { path: "app.py" } },
    { label: "another goal path", patch: { path: "generated/goal_other/node_code/app.py" } },
    { label: "another node path", patch: { path: "generated/goal_1/node_other/app.py" } },
    { label: "an invalid digest", patch: { sha256: "not-a-digest" } },
    { label: "too many UTF-8 bytes", patch: { content: "é".repeat(32_001) } },
    { label: "empty content", patch: { content: "" } },
    { label: "an oversized summary", patch: { summary: "a".repeat(501) } },
    { label: "an empty summary", patch: { summary: " " } },
    { label: "an unsupported status", patch: { status: "executed" } },
    { label: "an approval without a task", patch: { status: "waiting_permission", task_id: null } },
  ])("rejects $label", ({ patch }) => {
    expect(() => parseGoalCodeProposal({ ...proposal, ...patch }, "goal_1", "node_code")).toThrow();
  });

  it("accepts exactly 64000 UTF-8 bytes without TextEncoder", () => {
    for (const content of ["a".repeat(64_000), "é".repeat(32_000), "🧪".repeat(16_000)]) {
      expect(parseGoalCodeProposal({ ...proposal, content }, "goal_1", "node_code").content).toBe(content);
    }
  });
});
