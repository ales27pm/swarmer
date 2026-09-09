import { beforeAll, beforeEach, describe, expect, it, jest } from "@jest/globals";
import * as SQLite from "expo-sqlite";

import type { Approval, GoalRecord, GoalResult, PlanNode, Task } from "@/lib/api/types";
import {
  applyBootstrap,
  localAgents,
  localAuditMeta,
  localConversations,
  localDataAuthorizesSensitiveAction,
  localGoalDetail,
  localGoals,
  localMemory,
  localApprovals,
  localSwarmSnapshot,
  localTask,
  localTasks,
  localToolCalls,
  upsertEvent,
} from "@/lib/state/replica";

jest.mock("expo-sqlite", () => ({ openDatabaseAsync: jest.fn() }));

describe("SQLite bootstrap replica", () => {
  const task = {
    id: "tsk_1",
    created_at: "2030-01-01T10:00:00Z",
    updated_at: "2030-01-01T10:01:00Z",
  } as Task;
  const approval = {
    id: "apr_1",
    created_at: "2030-01-01T10:02:00Z",
    decided_at: null,
  } as Approval;
  const goal: GoalRecord = {
    id: "goal_1",
    root_task_id: "tsk_1",
    objective: "Qualifier le swarm",
    status: "running",
    autonomy_profile: "assisted",
    planner_source: "ubuntu_local",
    max_steps: 8,
    max_parallelism: 2,
    max_replans: 1,
    max_runtime_seconds: 600,
    max_model_calls: 10,
    step_count: 1,
    replan_count: 0,
    model_call_count: 1,
    completion_criteria: ["Preuves présentes"],
    current_phase: "execution",
    created_at: "2030-01-01T10:00:00Z",
    updated_at: "2030-01-01T10:03:00Z",
  };
  const node: PlanNode = {
    id: "node_1",
    goal_run_id: goal.id,
    node_type: "worker",
    title: "Inspecter",
    objective: "Inspecter les preuves",
    status: "running",
    priority: 0,
    depends_on: [],
    assigned_agent_id: "agent_1",
    created_at: "2030-01-01T10:00:00Z",
    updated_at: "2030-01-01T10:04:00Z",
  };
  const result: GoalResult = {
    goal_run_id: goal.id,
    root_task_id: goal.root_task_id,
    status: "completed",
    answer: "Qualification terminée",
    completed_nodes: [node.id],
    failed_nodes: [],
    agents_used: ["agent_1"],
    memory_ids: [],
    episode_ids: [],
    started_at: "2030-01-01T10:00:00Z",
    completed_at: "2030-01-01T10:05:00Z",
    limitations: [],
  };
  const execAsync = jest.fn(async () => undefined);
  const runAsync = jest.fn(async () => ({ changes: 1, lastInsertRowId: 0 }));
  const getAllAsync = jest.fn(async (_query?: string) => [{ payload: JSON.stringify(approval) }]);
  const getFirstAsync = jest.fn(async (query: string) =>
    query.includes("sync_meta")
      ? { value: "https://control.example" }
      : { payload: JSON.stringify(task) },
  );
  const withTransactionAsync = jest.fn(async (operation: () => Promise<void>) => {
    await operation();
  });
  const withExclusiveTransactionAsync = jest.fn(async (
    operation: (transaction: SQLite.SQLiteDatabase) => Promise<void>,
  ) => {
    await operation(database);
  });
  const database = {
    execAsync,
    getAllAsync,
    getFirstAsync,
    runAsync,
    withExclusiveTransactionAsync,
    withTransactionAsync,
  } as unknown as SQLite.SQLiteDatabase;

  beforeAll(() => {
    jest.mocked(SQLite.openDatabaseAsync).mockResolvedValue(database);
  });

  beforeEach(() => {
    jest.clearAllMocks();
    getAllAsync.mockResolvedValue([{ payload: JSON.stringify(approval) }]);
    getFirstAsync.mockImplementation(async (query: string) =>
      query.includes("sync_meta")
        ? { value: "https://control.example" }
        : { payload: JSON.stringify(task) },
    );
  });

  it("atomically stores bootstrap tasks, approvals, and cursor, then reads cached approvals", async () => {
    await applyBootstrap(
      { tasks: [task], approvals: [approval], cursor: "audit:42" },
      "https://control.example",
    );

    expect(SQLite.openDatabaseAsync).toHaveBeenCalledWith("mongars-replica.db");
    expect(execAsync).toHaveBeenCalledWith(expect.stringContaining("PRAGMA journal_mode = WAL"));
    expect(withTransactionAsync).toHaveBeenCalledTimes(1);
    expect(runAsync).toHaveBeenCalledWith("DELETE FROM tasks");
    expect(runAsync).toHaveBeenCalledWith("DELETE FROM approvals");
    expect(runAsync).toHaveBeenCalledWith(
      "INSERT OR REPLACE INTO tasks(id,payload,updated_at) VALUES(?,?,?)",
      task.id,
      JSON.stringify(task),
      task.updated_at,
    );
    expect(runAsync).toHaveBeenCalledWith(
      "INSERT OR REPLACE INTO approvals(id,payload,updated_at) VALUES(?,?,?)",
      approval.id,
      JSON.stringify(approval),
      approval.created_at,
    );
    expect(runAsync).toHaveBeenCalledWith(
      "INSERT OR REPLACE INTO sync_meta(key,value) VALUES('cursor',?)",
      "audit:42",
    );

    await expect(localApprovals("https://control.example")).resolves.toEqual([approval]);
    expect(getAllAsync).toHaveBeenCalledWith(
      "SELECT payload FROM approvals WHERE json_extract(payload, '$.status') = ? ORDER BY updated_at DESC",
      "pending",
    );
  });

  it("reads cached tasks by status and a single cached task", async () => {
    getAllAsync.mockResolvedValueOnce([{ payload: JSON.stringify(task) }]);

    await expect(localTasks("https://control.example", "completed")).resolves.toEqual([task]);
    expect(getAllAsync).toHaveBeenLastCalledWith(
      "SELECT payload FROM tasks WHERE json_extract(payload, '$.status') = ? ORDER BY updated_at DESC",
      "completed",
    );

    await expect(localTask("https://control.example", "tsk_1")).resolves.toEqual(task);
    expect(getFirstAsync).toHaveBeenCalledWith(
      "SELECT payload FROM tasks WHERE id=?",
      "tsk_1",
    );
  });

  it("never reads records belonging to a different control plane", async () => {
    getFirstAsync.mockResolvedValueOnce({ value: "https://old.example" });

    await expect(localTasks("https://new.example")).resolves.toEqual([]);
    expect(getAllAsync).not.toHaveBeenCalled();
  });

  it("persists only supported authoritative websocket event payloads", async () => {
    await upsertEvent("https://control.example", "task.updated", task);
    await upsertEvent("https://control.example", "approval.decided", approval);
    await upsertEvent("https://control.example", "tool.completed", { id: "call_1" });

    expect(runAsync).toHaveBeenCalledWith(
      "INSERT OR REPLACE INTO tasks(id,payload,updated_at) VALUES(?,?,?)",
      task.id,
      JSON.stringify(task),
      task.updated_at,
    );
    expect(runAsync).toHaveBeenCalledWith(
      "INSERT OR REPLACE INTO approvals(id,payload,updated_at) VALUES(?,?,?)",
      approval.id,
      JSON.stringify(approval),
      approval.created_at,
    );
    expect(runAsync).toHaveBeenCalledTimes(3);
  });

  it("does not replace full cached content with metadata-only refetch notifications", async () => {
    await upsertEvent("https://control.example", "task.updated", {
      id: "tsk_1",
      status: "running",
      updated_at: "2030-01-01T00:00:00.000Z",
      refetch_required: true,
    });
    await upsertEvent("https://control.example", "message.created", {
      id: "msg_1",
      conversation_id: "cnv_1",
      created_at: "2030-01-01T00:00:00.000Z",
      refetch_required: true,
    });

    expect(runAsync).not.toHaveBeenCalled();
  });

  it.each(["approval.requested", "approval.decided"])(
    "preserves a full cached approval when %s is only an invalidation",
    async (eventType) => {
      await upsertEvent("https://control.example", eventType, {
        id: "apr_1",
        status: eventType === "approval.decided" ? "approved" : "pending",
        updated_at: "2030-01-01T00:00:00.000Z",
        refetch_required: true,
      });

      expect(runAsync).not.toHaveBeenCalled();
    },
  );

  it("exposes scoped queries and never authorizes sensitive actions from cached state", async () => {
    await localToolCalls("https://control.example");
    await localAgents("https://control.example");
    await localMemory("https://control.example");
    await localConversations("https://control.example");
    expect(getAllAsync).toHaveBeenCalledWith("SELECT payload FROM tool_calls ORDER BY updated_at DESC");
    expect(getAllAsync).toHaveBeenCalledWith("SELECT payload FROM agents ORDER BY updated_at DESC");
    expect(getAllAsync).toHaveBeenCalledWith("SELECT payload FROM pinned_memory ORDER BY updated_at DESC");
    expect(getAllAsync).toHaveBeenCalledWith("SELECT payload FROM conversations ORDER BY updated_at DESC");
    expect(localDataAuthorizesSensitiveAction()).toBe(false);
  });

  it("persists audit cursor and counts metadata", async () => {
    getFirstAsync
      .mockResolvedValueOnce({ value: "https://control.example" })
      .mockResolvedValueOnce({ value: "42" })
      .mockResolvedValueOnce({ value: '{"tasks":1}' });
    await expect(localAuditMeta("https://control.example")).resolves.toEqual({
      cursor: "42",
      counts: { tasks: 1 },
    });
  });

  it("hydrates every authoritative bootstrap collection", async () => {
    await applyBootstrap({
      tasks: [], approvals: [], cursor: "7",
      tool_calls: [{ id: "call_1", created_at: "2030-01-01" }] as never[],
      conversations: [{ id: "cnv_1", created_at: "2030-01-01" }] as never[],
      messages: [{ id: "msg_1", created_at: "2030-01-01" }] as never[],
      agents: [{ id: "agt_1", created_at: "2030-01-01" }] as never[],
      pinned_memory: [{ id: "mem_1", created_at: "2030-01-01" }] as never[],
      goals: [goal],
      plan_nodes: [node],
      goal_results: [result],
    }, "https://control.example");
    for (const [table, id, storedAt] of [
      ["tool_calls", "call_1", "2030-01-01"],
      ["conversations", "cnv_1", "2030-01-01"],
      ["messages", "msg_1", "2030-01-01"],
      ["agents", "agt_1", "2030-01-01"],
      ["pinned_memory", "mem_1", "2030-01-01"],
      ["goals", goal.id, goal.updated_at],
      ["plan_nodes", node.id, node.updated_at],
      ["goal_results", result.goal_run_id, result.completed_at],
    ]) {
      expect(runAsync).toHaveBeenCalledWith(
        `INSERT OR REPLACE INTO ${table}(id,payload,updated_at) VALUES(?,?,?)`,
        id, expect.any(String), storedAt,
      );
    }
  });

  it.each([
    ["goal.updated", { id: goal.id, status: "running", objective: "partial private goal" }],
    ["plan.node.updated", {
      id: node.id,
      goal_run_id: goal.id,
      status: "completed",
      expected_output: "partial private output",
      result_summary: "partial private result",
    }],
    ["goal.result.updated", {
      goal_run_id: goal.id,
      status: "completed",
      answer: "partial private answer",
    }],
  ])(
    "does not replace full goal state when %s is only an invalidation",
    async (eventType, payload) => {
      await upsertEvent("https://control.example", eventType, {
        ...payload,
        refetch_required: true,
      });

      expect(runAsync).not.toHaveBeenCalled();
    },
  );

  it("stores safe full goal event payloads with stable row identities", async () => {
    await upsertEvent("https://control.example", "goal.updated", goal);
    await upsertEvent("https://control.example", "plan.node.updated", node);
    await upsertEvent("https://control.example", "goal.result.updated", result);

    expect(runAsync).toHaveBeenCalledWith(
      "INSERT OR REPLACE INTO goals(id,payload,updated_at) VALUES(?,?,?)",
      goal.id,
      JSON.stringify(goal),
      goal.updated_at,
    );
    expect(runAsync).toHaveBeenCalledWith(
      "INSERT OR REPLACE INTO goal_results(id,payload,updated_at) VALUES(?,?,?)",
      result.goal_run_id,
      JSON.stringify(result),
      result.completed_at,
    );
  });

  it("reads a goal detail atomically from only the active origin", async () => {
    getFirstAsync.mockImplementation(async (query: string) => {
      if (query.includes("sync_meta")) return { value: "https://control.example" };
      if (query.includes("FROM goals")) return { payload: JSON.stringify(goal) };
      if (query.includes("FROM goal_results")) return { payload: JSON.stringify(result) };
      return { payload: JSON.stringify(task) };
    });
    getAllAsync.mockResolvedValue([{ payload: JSON.stringify(node) }]);

    await expect(localGoalDetail("https://control.example", goal.id)).resolves.toEqual({
      goal,
      nodes: [node],
      result,
    });
    expect(withExclusiveTransactionAsync).toHaveBeenCalledTimes(1);
  });

  it("discards an atomic goal snapshot if its origin changes before completion", async () => {
    let scopeReads = 0;
    getFirstAsync.mockImplementation(async (query: string) => {
      if (query.includes("key='scope'")) {
        scopeReads += 1;
        return { value: scopeReads === 1 ? "https://control.example" : "https://replacement.example" };
      }
      if (query.includes("FROM goals")) return { payload: JSON.stringify(goal) };
      if (query.includes("FROM goal_results")) return { payload: JSON.stringify(result) };
      return { payload: JSON.stringify(task) };
    });
    getAllAsync.mockResolvedValue([{ payload: JSON.stringify(node) }]);

    await expect(localGoalDetail("https://control.example", goal.id)).resolves.toBeNull();
  });

  it("exposes an origin-scoped swarm snapshot without granting authority", async () => {
    getFirstAsync.mockImplementation(async (query: string) => {
      if (query.includes("key='scope'")) return { value: "https://control.example" };
      if (query.includes("key='cursor'")) return { value: "audit:99" };
      if (query.includes("key='counts'")) return { value: JSON.stringify({ tasks: 1 }) };
      return { payload: JSON.stringify(task) };
    });
    getAllAsync.mockImplementation(async (query?: string) => {
      if (query?.includes("FROM goals")) return [{ payload: JSON.stringify(goal) }];
      if (query?.includes("FROM plan_nodes")) return [{ payload: JSON.stringify(node) }];
      if (query?.includes("FROM goal_results")) return [{ payload: JSON.stringify(result) }];
      return [];
    });

    const snapshot = await localSwarmSnapshot("https://control.example");
    expect(snapshot).toEqual(expect.objectContaining({
      cursor: "audit:99",
      goals: [goal],
      origin: "https://control.example",
      plan_nodes: [node],
      goal_results: [result],
    }));
    await expect(localGoals("https://control.example")).resolves.toEqual([goal]);
    expect(localDataAuthorizesSensitiveAction()).toBe(false);
  });
});
