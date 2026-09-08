import { beforeEach, describe, expect, it, jest } from "@jest/globals";
import { fetch } from "expo/fetch";
import * as SecureStore from "expo-secure-store";

import { decideApproval, type Approval } from "@/lib/api/client";
import { upsertEvent } from "@/lib/state/replica";

jest.mock("expo-secure-store", () => ({
  deleteItemAsync: jest.fn(),
  getItemAsync: jest.fn(),
  setItemAsync: jest.fn(),
}));
jest.mock("expo/fetch", () => ({ fetch: jest.fn() }));
jest.mock("@/lib/state/mutation-outbox", () => ({
  mutationOutbox: {
    abandonPending: jest.fn(),
    drain: jest.fn(),
  },
}));
jest.mock("@/lib/state/replica", () => ({
  applyBootstrap: jest.fn(),
  upsertEvent: jest.fn(),
}));

const getItem = jest.mocked(SecureStore.getItemAsync);
const request = jest.mocked(fetch);
const writeReplica = jest.mocked(upsertEvent);

const decidedApproval: Approval = {
  id: "apr_server_decided",
  task_id: "tsk_server_decided",
  tool_call_id: "call_server_decided",
  action_digest: `sha256:${"c".repeat(64)}`,
  action: "workspace.read_text",
  action_preview: {
    operation: "Read workspace text",
    target: "notes/result.txt",
    details: ["Read one workspace file"],
    arguments_redacted: false,
  },
  binding_valid: true,
  requester: { type: "device", id: "iphone-test", name: "Test iPhone" },
  policy: {
    rule_id: "ask-workspace-read",
    decision: "ask",
    reason: "Reading this workspace file requires approval.",
  },
  affected_data_summary: "Reads notes/result.txt.",
  audit_id: 91,
  consent_context_valid: true,
  summary: "Read a workspace file",
  risk: "medium",
  status: "approved",
  created_at: "2026-09-05T12:00:00Z",
  expires_at: "2026-09-05T12:05:00Z",
  decided_at: "2026-09-05T12:01:00Z",
  decision: { decision: "approve", actor_id: "iphone-test", user_note: null },
};

describe("decideApproval", () => {
  beforeEach(() => {
    jest.clearAllMocks();
    getItem.mockImplementation(async (key: string) =>
      key === "mongars.connection.v1"
        ? JSON.stringify({ baseUrl: "https://control.example", token: "device-token" })
        : null,
    );
    request.mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => decidedApproval,
    } as never);
  });

  it("preserves authoritative success when the local SQLite replica write fails", async () => {
    writeReplica.mockRejectedValue(new Error("SQLite unavailable"));

    await expect(decideApproval(decidedApproval.id, "approve")).resolves.toEqual({
      authoritativeResult: decidedApproval,
      localReplicaError: "SQLite unavailable",
    });

    expect(request).toHaveBeenCalledWith(
      `https://control.example/approvals/${decidedApproval.id}/decision`,
      expect.objectContaining({
        body: JSON.stringify({ decision: "approve" }),
        headers: expect.objectContaining({ Authorization: "Bearer device-token" }),
        method: "POST",
      }),
    );
    expect(writeReplica).toHaveBeenCalledWith(
      "https://control.example",
      "approval.decided",
      decidedApproval,
    );
  });
});
