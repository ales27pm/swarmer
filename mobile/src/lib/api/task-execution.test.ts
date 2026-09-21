import { beforeEach, describe, expect, it, jest } from "@jest/globals";
import { fetch } from "expo/fetch";
import * as SecureStore from "expo-secure-store";

import { getTask } from "@/lib/api/client";
import { parseTaskGoalExecution, type TaskGoalExecution } from "@/lib/api/task-execution";

jest.mock("expo-secure-store", () => ({ deleteItemAsync: jest.fn(), getItemAsync: jest.fn(), setItemAsync: jest.fn() }));
jest.mock("expo/fetch", () => ({ fetch: jest.fn() }));
jest.mock("@/lib/state/mutation-outbox", () => ({ mutationOutbox: {} }));
jest.mock("@/lib/state/replica", () => ({ applyBootstrap: jest.fn(), upsertEvent: jest.fn() }));

const execution: TaskGoalExecution = {
  schema_version: "1.0", task_id: "tsk_root", root_task_id: "tsk_root", goal_run_id: "goal_crm",
  status: "running", truncated: false,
  nodes: [{ node_id: "node_1", node_type: "worker", status: "running", title: "Lire le projet",
    output_summary: null, error_summary: null,
    provenance: { node_id: "node_1", worker_job_id: "job_1", agent_id: "agt_1", required_skill: "workspace.list_dir", result_digest: null } }],
};

describe("task execution evidence contract", () => {
  beforeEach(() => {
    jest.resetAllMocks();
    jest.mocked(SecureStore.getItemAsync).mockImplementation(async (key) => key === "mongars.connection.v1"
      ? JSON.stringify({ baseUrl: "https://control.example", token: "paired-device-token" }) : null);
  });

  it("preserves bounded worker metadata without classifying it as a direct tool call", async () => {
    const data = { task: { id: "tsk_root" }, tool_calls: [], approvals: [], messages: [], goal_execution: execution };
    jest.mocked(fetch).mockResolvedValue({ ok: true, status: 200, json: async () => data } as never);
    const result = await getTask("tsk_root");
    expect(result.goal_execution).toEqual(execution);
    expect(result.tool_calls).toEqual([]);
    expect(fetch).toHaveBeenCalledTimes(1);
    expect(fetch).toHaveBeenCalledWith("https://control.example/tasks/tsk_root", { headers: { Authorization: "Bearer paired-device-token" } });
  });

  it("accepts old-server responses that omit the optional projection", () => {
    expect(parseTaskGoalExecution(undefined, "tsk_root")).toBeNull();
    expect(parseTaskGoalExecution(null, "tsk_root")).toBeNull();
  });

  it.each([
    { ...execution, task_id: "tsk_other" },
    { ...execution, goal_run_id: "../goal" },
    { ...execution, result_json: { secret: "not-public" } },
    { ...execution, nodes: Array.from({ length: 21 }, () => execution.nodes[0]) },
    { ...execution, nodes: [execution.nodes[0], execution.nodes[0]] },
    { ...execution, nodes: [{ ...execution.nodes[0], payload: "not-public" }] },
    { ...execution, nodes: [{ ...execution.nodes[0], title: "x".repeat(1201) }] },
    { ...execution, nodes: [{ ...execution.nodes[0], status: "imaginary" }] },
    { ...execution, nodes: [{ ...execution.nodes[0], provenance: { ...execution.nodes[0].provenance, node_id: "node_other" } }] },
  ])("rejects unsafe or unrelated evidence", (value) => {
    expect(() => parseTaskGoalExecution(value, "tsk_root")).toThrow(/preuves/);
  });

  it("rejects malformed evidence at the HTTP boundary", async () => {
    jest.mocked(fetch).mockResolvedValue({ ok: true, status: 200,
      json: async () => ({ task: { id: "tsk_root" }, goal_execution: { ...execution, task_id: "tsk_other" } }),
    } as never);
    await expect(getTask("tsk_root")).rejects.toThrow(/preuves/);
  });
});
