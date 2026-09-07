import { beforeAll, describe, expect, it, jest } from "@jest/globals";
import * as SQLite from "expo-sqlite";

import { applyBootstrap, localApprovals } from "@/lib/state/replica";

jest.mock("expo-sqlite", () => ({ openDatabaseAsync: jest.fn() }));

describe("SQLite bootstrap replica", () => {
  const task = {
    id: "tsk_1",
    created_at: "2030-01-01T10:00:00Z",
    updated_at: "2030-01-01T10:01:00Z",
  };
  const approval = {
    id: "apr_1",
    created_at: "2030-01-01T10:02:00Z",
    decided_at: null,
  };
  const execAsync = jest.fn(async () => undefined);
  const runAsync = jest.fn(async () => ({ changes: 1, lastInsertRowId: 0 }));
  const getAllAsync = jest.fn(async () => [{ payload: JSON.stringify(approval) }]);
  const withTransactionAsync = jest.fn(async (operation: () => Promise<void>) => {
    await operation();
  });
  const database = {
    execAsync,
    getAllAsync,
    runAsync,
    withTransactionAsync,
  } as unknown as SQLite.SQLiteDatabase;

  beforeAll(() => {
    jest.mocked(SQLite.openDatabaseAsync).mockResolvedValue(database);
  });

  it("atomically stores bootstrap tasks, approvals, and cursor, then reads cached approvals", async () => {
    await applyBootstrap({ tasks: [task], approvals: [approval], cursor: "audit:42" });

    expect(SQLite.openDatabaseAsync).toHaveBeenCalledWith("mongars-replica.db");
    expect(execAsync).toHaveBeenCalledWith(expect.stringContaining("PRAGMA journal_mode = WAL"));
    expect(withTransactionAsync).toHaveBeenCalledTimes(1);
    expect(runAsync).toHaveBeenNthCalledWith(
      1,
      "INSERT OR REPLACE INTO tasks(id,payload,updated_at) VALUES(?,?,?)",
      task.id,
      JSON.stringify(task),
      task.updated_at,
    );
    expect(runAsync).toHaveBeenNthCalledWith(
      2,
      "INSERT OR REPLACE INTO approvals(id,payload,updated_at) VALUES(?,?,?)",
      approval.id,
      JSON.stringify(approval),
      approval.created_at,
    );
    expect(runAsync).toHaveBeenNthCalledWith(
      3,
      "INSERT OR REPLACE INTO sync_meta(key,value) VALUES('cursor',?)",
      "audit:42",
    );

    await expect(localApprovals()).resolves.toEqual([approval]);
    expect(getAllAsync).toHaveBeenCalledWith(
      "SELECT payload FROM approvals ORDER BY updated_at DESC",
    );
  });
});
