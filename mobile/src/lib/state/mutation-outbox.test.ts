import { describe, expect, it, jest } from "@jest/globals";
import type { SQLiteDatabase, SQLiteRunResult } from "expo-sqlite";

import {
  MutationOutbox,
  MutationOutboxValidationError,
  type MutationOutboxRow,
} from "@/lib/state/mutation-outbox";

jest.mock("expo-sqlite", () => ({ openDatabaseAsync: jest.fn() }));

type StoredRow = MutationOutboxRow;

class MemoryDatabase {
  readonly rows = new Map<string, StoredRow>();
  initialized = false;

  async execAsync(sql: string): Promise<void> {
    if (sql.includes("CREATE TABLE IF NOT EXISTS mutation_outbox")) {
      this.initialized = true;
    }
  }

  async runAsync(sql: string, ...params: unknown[]): Promise<SQLiteRunResult> {
    if (sql.startsWith("INSERT OR IGNORE INTO mutation_outbox")) {
      return this.insertMutation(params);
    }
    if (sql.startsWith("UPDATE mutation_outbox SET attempts=attempts+1")) {
      return this.startAttempt(params);
    }
    if (sql.startsWith("UPDATE mutation_outbox SET completed_at=?, last_error=?")) {
      return this.abandonOrigin(params);
    }
    if (sql.startsWith("UPDATE mutation_outbox SET completed_at=?")) {
      return this.completeMutation(params);
    }
    if (sql.startsWith("UPDATE mutation_outbox SET last_error=?")) {
      return this.failMutation(params);
    }
    throw new Error(`Unexpected runAsync SQL: ${sql}`);
  }

  private insertMutation(params: unknown[]): SQLiteRunResult {
    const [id, origin, operation, resourceId, payloadJson, idempotencyKey, createdAt] = params as [
      string,
      string,
      StoredRow["operation"],
      string | null,
      string,
      string,
      string,
    ];
    const duplicate = [...this.rows.values()].find(
      (row) => row.origin === origin && row.idempotency_key === idempotencyKey,
    );
    if (duplicate) return { changes: 0, lastInsertRowId: 0 };
    this.rows.set(id, {
      id,
      origin,
      operation,
      resource_id: resourceId,
      payload_json: payloadJson,
      idempotency_key: idempotencyKey,
      created_at: createdAt,
      attempts: 0,
      last_attempt_at: null,
      last_error: null,
      completed_at: null,
    });
    return { changes: 1, lastInsertRowId: this.rows.size };
  }

  private pendingRow(id: string, origin: string): StoredRow | undefined {
    const row = this.rows.get(id);
    return row?.origin === origin && row.completed_at === null ? row : undefined;
  }

  private startAttempt(params: unknown[]): SQLiteRunResult {
    const [lastAttemptAt, id, origin] = params as [string, string, string];
    const row = this.pendingRow(id, origin);
    if (!row) return { changes: 0, lastInsertRowId: 0 };
    row.attempts += 1;
    row.last_attempt_at = lastAttemptAt;
    row.last_error = null;
    return { changes: 1, lastInsertRowId: 0 };
  }

  private abandonOrigin(params: unknown[]): SQLiteRunResult {
    const [completedAt, reason, origin] = params as [string, string, string];
    let changes = 0;
    for (const row of this.rows.values()) {
      if (row.origin !== origin || row.completed_at !== null) continue;
      row.completed_at = completedAt;
      row.last_error = reason;
      changes += 1;
    }
    return { changes, lastInsertRowId: 0 };
  }

  private completeMutation(params: unknown[]): SQLiteRunResult {
    const [completedAt, id, origin] = params as [string, string, string];
    const row = this.pendingRow(id, origin);
    if (!row) return { changes: 0, lastInsertRowId: 0 };
    row.completed_at = completedAt;
    row.last_error = null;
    return { changes: 1, lastInsertRowId: 0 };
  }

  private failMutation(params: unknown[]): SQLiteRunResult {
    const [lastError, id, origin] = params as [string, string, string];
    const row = this.pendingRow(id, origin);
    if (!row) return { changes: 0, lastInsertRowId: 0 };
    row.last_error = lastError;
    return { changes: 1, lastInsertRowId: 0 };
  }

  async getFirstAsync<T>(sql: string, ...params: unknown[]): Promise<T | null> {
    if (sql.includes("WHERE origin=? AND idempotency_key=?")) {
      const [origin, idempotencyKey] = params as [string, string];
      return ([...this.rows.values()].find(
        (row) => row.origin === origin && row.idempotency_key === idempotencyKey,
      ) ?? null) as T | null;
    }
    if (sql.includes("COUNT(*) AS count")) {
      const [origin] = params as [string];
      const count = [...this.rows.values()].filter(
        (row) => row.origin === origin && row.completed_at === null,
      ).length;
      return { count } as T;
    }
    throw new Error(`Unexpected getFirstAsync SQL: ${sql}`);
  }

  async getAllAsync<T>(sql: string, ...params: unknown[]): Promise<T[]> {
    if (sql.includes("completed_at IS NULL AND origin=?")) {
      const [origin, limit] = params as [string, number];
      return [...this.rows.values()]
        .filter((row) => row.origin === origin && row.completed_at === null)
        .sort((left, right) =>
          left.created_at.localeCompare(right.created_at) || left.id.localeCompare(right.id),
        )
        .slice(0, limit) as T[];
    }
    throw new Error(`Unexpected getAllAsync SQL: ${sql}`);
  }
}

function makeOutbox(database: MemoryDatabase, ids = ["mut_aaaaaaaaaaaaaaaaaaaa"]): MutationOutbox {
  let index = 0;
  return new MutationOutbox({
    openDatabase: async () => database as unknown as SQLiteDatabase,
    createId: () => ids[index++] ?? `mut_fallback_${index}`,
    now: () => "2030-01-01T12:00:00.000Z",
  });
}

describe("mobile mutation outbox", () => {
  it("persists an offline feedback mutation without persisting authentication", async () => {
    const database = new MemoryDatabase();
    const outbox = makeOutbox(database);

    const mutation = await outbox.enqueue({
      origin: "https://CONTROL.example/",
      operation: "feedback.create",
      payload: { task_id: "tsk_1", score: 5, notes: "Helpful" },
      idempotencyKey: "mut_feedback_12345678901234567890",
    });

    expect(database.initialized).toBe(true);
    expect(mutation.origin).toBe("https://control.example");
    expect(mutation.attempts).toBe(0);
    expect(mutation.completed_at).toBeNull();
    expect(JSON.stringify([...database.rows.values()])).not.toMatch(
      /authorization|bearer|device_token|grant_token|lease_token/i,
    );
    await expect(outbox.pendingCount("https://control.example")).resolves.toBe(1);
  });

  it("keeps the same idempotency key after a lost response and completes on reconnect", async () => {
    const database = new MemoryDatabase();
    const firstRuntime = makeOutbox(database);
    await firstRuntime.enqueue({
      origin: "https://control.example",
      operation: "feedback.create",
      payload: { score: 4 },
      idempotencyKey: "mut_feedback_12345678901234567890",
    });

    const deliveries: string[] = [];
    const responseLost = jest.fn(async (mutation: { idempotencyKey: string }) => {
      deliveries.push(mutation.idempotencyKey);
      throw new Error("response lost after authoritative commit: bearer secret must not persist");
    });
    await expect(firstRuntime.drain("https://control.example", responseLost)).resolves.toEqual({
      attempted: 1,
      completed: 0,
      failed: 1,
      remaining: 1,
    });

    const secondRuntime = makeOutbox(database, ["mut_bbbbbbbbbbbbbbbbbbbb"]);
    const duplicateReceipt = jest.fn(async (mutation: { idempotencyKey: string }) => {
      deliveries.push(mutation.idempotencyKey);
    });
    await expect(secondRuntime.drain("https://control.example", duplicateReceipt)).resolves.toEqual({
      attempted: 1,
      completed: 1,
      failed: 0,
      remaining: 0,
    });

    expect(deliveries).toEqual([
      "mut_feedback_12345678901234567890",
      "mut_feedback_12345678901234567890",
    ]);
    const stored = [...database.rows.values()][0];
    expect(stored.attempts).toBe(2);
    expect(stored.last_error).toBeNull();
    expect(stored.completed_at).not.toBeNull();
    expect(JSON.stringify(stored)).not.toContain("bearer secret");
  });

  it("deduplicates enqueue by origin and rejects idempotency-key payload drift", async () => {
    const database = new MemoryDatabase();
    const outbox = makeOutbox(database, [
      "mut_aaaaaaaaaaaaaaaaaaaa",
      "mut_bbbbbbbbbbbbbbbbbbbb",
      "mut_cccccccccccccccccccc",
    ]);
    const input = {
      origin: "https://control.example",
      operation: "feedback.create" as const,
      payload: { score: 5 },
      idempotencyKey: "mut_feedback_12345678901234567890",
    };

    const first = await outbox.enqueue(input);
    const duplicate = await outbox.enqueue(input);
    expect(duplicate.id).toBe(first.id);
    expect(database.rows.size).toBe(1);

    await expect(outbox.enqueue({ ...input, payload: { score: 1 } })).rejects.toThrow(
      "idempotency key is already bound to a different mutation",
    );
  });

  it("never drains a mutation to a different server origin", async () => {
    const database = new MemoryDatabase();
    const outbox = makeOutbox(database);
    await outbox.enqueue({
      origin: "https://old.example",
      operation: "feedback.create",
      payload: { score: 5 },
      idempotencyKey: "mut_feedback_12345678901234567890",
    });
    const sender = jest.fn(async () => undefined);

    await expect(outbox.drain("https://new.example", sender)).resolves.toEqual({
      attempted: 0,
      completed: 0,
      failed: 0,
      remaining: 0,
    });
    expect(sender).not.toHaveBeenCalled();
    await expect(outbox.pendingCount("https://old.example")).resolves.toBe(1);
  });

  it("can explicitly abandon old pending work during logout or re-pair", async () => {
    const database = new MemoryDatabase();
    const outbox = makeOutbox(database);
    await outbox.enqueue({
      origin: "https://control.example",
      operation: "memory.metadata.update",
      resourceId: "mem_1",
      payload: { pinned: true },
      idempotencyKey: "mut_memory_123456789012345678901",
    });

    await expect(outbox.abandonPending("https://control.example")).resolves.toBe(1);
    await expect(outbox.pendingCount("https://control.example")).resolves.toBe(0);
    const sender = jest.fn(async () => undefined);
    await outbox.drain("https://control.example", sender);
    expect(sender).not.toHaveBeenCalled();
  });

  it("stores an ordinary chat message with start_task forced false", async () => {
    const database = new MemoryDatabase();
    const outbox = makeOutbox(database);
    const mutation = await outbox.enqueue({
      origin: "https://control.example",
      operation: "chat.message.create",
      resourceId: "cnv_1",
      payload: { content: "Continue our discussion", mode: "normal" },
      idempotencyKey: "mut_chat_12345678901234567890123",
    });

    expect(JSON.parse(mutation.payload_json)).toEqual({
      content: "Continue our discussion",
      conversation_id: "cnv_1",
      mode: "normal",
      start_task: false,
    });
  });

  it("preserves the complete canonical feedback shape including explicit nulls", async () => {
    const database = new MemoryDatabase();
    const outbox = makeOutbox(database);

    const mutation = await outbox.enqueue({
      origin: "https://control.example",
      operation: "feedback.create",
      payload: {
        task_id: null,
        agent_id: "agt_1",
        type: "rating",
        label: null,
        score: null,
        notes: "Useful",
      },
      idempotencyKey: "mut_feedback_nulls_123456789012345",
    });

    expect(JSON.parse(mutation.payload_json)).toEqual({
      task_id: null,
      agent_id: "agt_1",
      type: "rating",
      label: null,
      score: null,
      notes: "Useful",
    });
  });

  it.each([
    "http://control.example",
    "https://user:secret@control.example",
    "https://control.example/private",
    "ftp://control.example",
  ])("rejects the unsafe outbox origin %s before opening SQLite", async (origin) => {
    const database = new MemoryDatabase();
    const outbox = makeOutbox(database);

    await expect(
      outbox.enqueue({
        origin,
        operation: "feedback.create",
        payload: { score: 5 },
        idempotencyKey: "mut_feedback_origin_123456789012345",
      }),
    ).rejects.toBeInstanceOf(MutationOutboxValidationError);
    expect(database.initialized).toBe(false);
  });

  it.each([
    { resourceId: "cnv_1", payload: { content: "hello", conversation_id: "cnv_2" } },
    { resourceId: undefined, payload: { content: "hello", mode: "unsafe" } },
  ])("rejects an invalid chat binding before persistence", async ({ resourceId, payload }) => {
    const database = new MemoryDatabase();
    const outbox = makeOutbox(database);

    await expect(
      outbox.enqueue({
        origin: "https://control.example",
        operation: "chat.message.create",
        ...(resourceId ? { resourceId } : {}),
        payload,
        idempotencyKey: "mut_chat_binding_12345678901234567",
      } as never),
    ).rejects.toBeInstanceOf(MutationOutboxValidationError);
    expect(database.initialized).toBe(false);
  });

  it.each([
    "approval.decide",
    "iphone.capability.execute",
    "iphone.capability.result",
    "process.run",
    "sms.compose",
    "mail.compose",
  ])("rejects the sensitive operation %s before opening SQLite", async (operation) => {
    const database = new MemoryDatabase();
    const outbox = makeOutbox(database);

    await expect(outbox.enqueue({
      origin: "https://control.example",
      operation,
      payload: {},
      idempotencyKey: "mut_sensitive_1234567890123456789",
    } as never)).rejects.toBeInstanceOf(MutationOutboxValidationError);
    expect(database.initialized).toBe(false);
    expect(database.rows.size).toBe(0);
  });

  it("rejects sensitive fields and chat task execution before persistence", async () => {
    const database = new MemoryDatabase();
    const outbox = makeOutbox(database);

    await expect(outbox.enqueue({
      origin: "https://control.example",
      operation: "feedback.create",
      payload: { score: 5, token: "device-token" },
      idempotencyKey: "mut_sensitive_1234567890123456789",
    } as never)).rejects.toBeInstanceOf(MutationOutboxValidationError);
    await expect(outbox.enqueue({
      origin: "https://control.example",
      operation: "chat.message.create",
      payload: { content: "run it", start_task: true },
      idempotencyKey: "mut_sensitive_2234567890123456789",
    } as never)).rejects.toBeInstanceOf(MutationOutboxValidationError);
    expect(database.initialized).toBe(false);
  });
});
