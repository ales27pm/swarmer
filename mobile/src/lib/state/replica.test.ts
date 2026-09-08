import { beforeAll, beforeEach, describe, expect, it, jest } from "@jest/globals";
import * as SQLite from "expo-sqlite";

import type { Approval, Task } from "@/lib/api/types";
import {
  applyBootstrap,
  localApprovals,
  localTask,
  localTasks,
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
  const execAsync = jest.fn(async () => undefined);
  const runAsync = jest.fn(async () => ({ changes: 1, lastInsertRowId: 0 }));
  const getAllAsync = jest.fn(async () => [{ payload: JSON.stringify(approval) }]);
  const getFirstAsync = jest.fn(async (query: string) =>
    query.includes("sync_meta")
      ? { value: "https://control.example" }
      : { payload: JSON.stringify(task) },
  );
  const withTransactionAsync = jest.fn(async (operation: () => Promise<void>) => {
    await operation();
  });
  const database = {
    execAsync,
    getAllAsync,
    getFirstAsync,
    runAsync,
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
    expect(runAsync).toHaveBeenNthCalledWith(
      1,
      "DELETE FROM tasks",
    );
    expect(runAsync).toHaveBeenNthCalledWith(
      2,
      "DELETE FROM approvals",
    );
    expect(runAsync).toHaveBeenNthCalledWith(
      3,
      "INSERT OR REPLACE INTO sync_meta(key,value) VALUES('scope',?)",
      "https://control.example",
    );
    expect(runAsync).toHaveBeenNthCalledWith(
      4,
      "INSERT OR REPLACE INTO tasks(id,payload,updated_at) VALUES(?,?,?)",
      task.id,
      JSON.stringify(task),
      task.updated_at,
    );
    expect(runAsync).toHaveBeenNthCalledWith(
      5,
      "INSERT OR REPLACE INTO approvals(id,payload,updated_at) VALUES(?,?,?)",
      approval.id,
      JSON.stringify(approval),
      approval.created_at,
    );
    expect(runAsync).toHaveBeenNthCalledWith(
      6,
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
    expect(runAsync).toHaveBeenCalledTimes(2);
  });
});
