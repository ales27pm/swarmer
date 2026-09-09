import * as SQLite from "expo-sqlite";

const DATABASE_NAME = "mongars-replica.db";
const DELIVERY_ERROR = "delivery failed; retry requires the same idempotency key";
const DELIVERY_TIMEOUT_ERROR = "delivery timed out; retry requires the same idempotency key";
const INVALID_LOCAL_ERROR = "invalid local mutation; delivery blocked";
const PERMANENT_DELIVERY_ERROR =
  "server rejected mutation permanently; automatic replay disabled";
const ABANDONED_ERROR = "abandoned after authentication context changed";
const DEFAULT_SEND_TIMEOUT_MS = 15_000;
const MAX_SEND_TIMEOUT_MS = 120_000;
const PERMANENT_DELIVERY_STATUSES = new Set([400, 404, 409, 410, 413, 415, 422]);
const drainTailsByOrigin = new Map<string, Promise<unknown>>();
const uncertainDeliveries = new Map<string, Promise<void>>();
const CHAT_MODES = new Set<NonNullable<ChatMessageMutationPayload["mode"]>>([
  "normal",
  "commandant",
  "review",
  "autonome",
]);

const SAFE_MUTATION_OPERATIONS = [
  "feedback.create",
  "memory.metadata.update",
  "chat.message.create",
] as const;

export type SafeMutationOperation = (typeof SAFE_MUTATION_OPERATIONS)[number];

type FeedbackCreateMutationPayload = {
  task_id?: string | null;
  agent_id?: string | null;
  type?: string;
  label?: string | null;
  score?: number | null;
  notes?: string | null;
};

type MemoryMetadataMutationPayload = {
  pinned: boolean;
};

type ChatMessageMutationPayload = {
  content: string;
  conversation_id?: string | null;
  mode?: "normal" | "commandant" | "review" | "autonome";
  start_task?: false;
};

type MutationPayloadByOperation = {
  "feedback.create": FeedbackCreateMutationPayload;
  "memory.metadata.update": MemoryMetadataMutationPayload;
  "chat.message.create": ChatMessageMutationPayload;
};

type MutationInputFor<TOperation extends SafeMutationOperation> = {
  origin: string;
  operation: TOperation;
  payload: MutationPayloadByOperation[TOperation];
  resourceId?: string;
  idempotencyKey?: string;
};

export type MutationOutboxInput = {
  [TOperation in SafeMutationOperation]: MutationInputFor<TOperation>;
}[SafeMutationOperation];

export type MutationOutboxRow = {
  id: string;
  origin: string;
  operation: SafeMutationOperation;
  resource_id: string | null;
  payload_json: string;
  idempotency_key: string;
  created_at: string;
  attempts: number;
  last_attempt_at: string | null;
  last_error: string | null;
  completed_at: string | null;
};

export type MutationDelivery = {
  id: string;
  origin: string;
  operation: SafeMutationOperation;
  resourceId: string | null;
  payload: MutationPayloadByOperation[SafeMutationOperation];
  idempotencyKey: string;
  createdAt: string;
  attempt: number;
};

export type MutationSender = (
  mutation: MutationDelivery,
  signal: AbortSignal,
) => Promise<unknown>;

export type MutationDrainResult = {
  attempted: number;
  completed: number;
  failed: number;
  remaining: number;
};

type CountRow = { count: number };
type OpenDatabase = () => Promise<SQLite.SQLiteDatabase>;

export class MutationOutboxValidationError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "MutationOutboxValidationError";
  }
}

type MutationOutboxOptions = {
  openDatabase?: OpenDatabase;
  createId?: () => string;
  now?: () => string;
  sendTimeoutMs?: number;
};

type CanonicalMutation = {
  origin: string;
  operation: SafeMutationOperation;
  resourceId: string | null;
  payloadJson: string;
  idempotencyKey: string;
};

type MutationRowOutcome = {
  attempted: number;
  completed: number;
  failed: number;
  stop: boolean;
};

function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

class MutationDeliveryTimeoutError extends Error {}

function isPermanentDeliveryError(value: unknown): boolean {
  if (!isRecord(value)) return false;
  return (
    typeof value.status === "number" &&
    Number.isSafeInteger(value.status) &&
    PERMANENT_DELIVERY_STATUSES.has(value.status)
  );
}

function deliveryKey(origin: string, id: string): string {
  return `${origin}\u0000${id}`;
}

function serializeOriginDrain<T>(origin: string, operation: () => Promise<T>): Promise<T> {
  const previous = drainTailsByOrigin.get(origin) ?? Promise.resolve();
  const running = previous.catch(() => undefined).then(operation);
  drainTailsByOrigin.set(origin, running);
  const cleanup = () => {
    if (drainTailsByOrigin.get(origin) === running) drainTailsByOrigin.delete(origin);
  };
  void running.then(cleanup, cleanup);
  return running;
}

function assertExactKeys(value: Record<string, unknown>, keys: readonly string[]): void {
  const allowed = new Set(keys);
  if (Object.keys(value).some((key) => !allowed.has(key))) {
    throw new MutationOutboxValidationError("mutation payload contains a forbidden field");
  }
}

function optionalBoundedString(
  value: unknown,
  field: string,
  maximum: number,
): string | null | undefined {
  if (value === undefined || value === null) return value;
  if (typeof value !== "string" || value.length > maximum) {
    throw new MutationOutboxValidationError(`${field} is invalid`);
  }
  return value;
}

function requiredBoundedString(value: unknown, field: string, maximum: number): string {
  if (typeof value !== "string" || value.length < 1 || value.length > maximum) {
    throw new MutationOutboxValidationError(`${field} is invalid`);
  }
  return value;
}

function optionalResourceId(value: unknown): string | null {
  if (value === undefined) return null;
  return requiredBoundedString(value, "resourceId", 500);
}

function parseOrigin(value: unknown): URL {
  if (typeof value !== "string") {
    throw new MutationOutboxValidationError("origin is invalid");
  }
  try {
    return new URL(value.trim());
  } catch {
    throw new MutationOutboxValidationError("origin is invalid");
  }
}

function originHasForbiddenComponents(parsed: URL): boolean {
  return Boolean(
    parsed.username ||
      parsed.password ||
      parsed.search ||
      parsed.hash ||
      parsed.pathname !== "/",
  );
}

function assertBareHttpOrigin(parsed: URL): void {
  if (
    !["http:", "https:"].includes(parsed.protocol) ||
    originHasForbiddenComponents(parsed)
  ) {
    throw new MutationOutboxValidationError("origin must be a bare HTTP(S) origin");
  }
}

function isLoopbackOriginHostname(hostname: string): boolean {
  const normalized = hostname.toLowerCase().replace(/^\[|\]$/g, "");
  return (
    ["localhost", "::1"].includes(normalized) ||
    normalized.endsWith(".localhost") ||
    /^127(?:\.\d{1,3}){3}$/.test(normalized)
  );
}

function assertSecureOrigin(parsed: URL): void {
  if (parsed.protocol !== "https:" && !isLoopbackOriginHostname(parsed.hostname)) {
    throw new MutationOutboxValidationError("HTTPS is required outside loopback");
  }
}

function normalizeOrigin(value: unknown): string {
  const parsed = parseOrigin(value);
  assertBareHttpOrigin(parsed);
  assertSecureOrigin(parsed);
  return parsed.origin;
}

function assertIdempotencyKey(value: unknown): string {
  if (
    typeof value !== "string" ||
    value.length < 20 ||
    value.length > 200 ||
    !/^[A-Za-z0-9._:-]+$/.test(value)
  ) {
    throw new MutationOutboxValidationError("idempotencyKey is invalid");
  }
  return value;
}

function assignIfDefined<T extends object, TKey extends keyof T>(
  target: T,
  key: TKey,
  value: T[TKey] | undefined,
): void {
  if (value !== undefined) target[key] = value;
}

function optionalFeedbackType(value: unknown): string | undefined {
  const type = optionalBoundedString(value, "type", 100);
  if (type === null) {
    throw new MutationOutboxValidationError("type is invalid");
  }
  return type;
}

function optionalFeedbackScore(value: unknown): number | null | undefined {
  if (value === undefined || value === null) return value;
  if (
    typeof value !== "number" ||
    !Number.isFinite(value) ||
    value < 0 ||
    value > 5
  ) {
    throw new MutationOutboxValidationError("score is invalid");
  }
  return value;
}

function canonicalFeedbackPayload(payload: Record<string, unknown>): FeedbackCreateMutationPayload {
  assertExactKeys(payload, ["task_id", "agent_id", "type", "label", "score", "notes"]);
  const result: FeedbackCreateMutationPayload = {};
  assignIfDefined(result, "task_id", optionalBoundedString(payload.task_id, "task_id", 500));
  assignIfDefined(result, "agent_id", optionalBoundedString(payload.agent_id, "agent_id", 500));
  assignIfDefined(result, "type", optionalFeedbackType(payload.type));
  assignIfDefined(result, "label", optionalBoundedString(payload.label, "label", 200));
  assignIfDefined(result, "score", optionalFeedbackScore(payload.score));
  assignIfDefined(result, "notes", optionalBoundedString(payload.notes, "notes", 4_000));
  return result;
}

function canonicalMemoryPayload(
  payload: Record<string, unknown>,
  resourceId: string | null,
): MemoryMetadataMutationPayload {
  assertExactKeys(payload, ["pinned"]);
  if (!resourceId) {
    throw new MutationOutboxValidationError("memory.metadata.update requires resourceId");
  }
  if (typeof payload.pinned !== "boolean") {
    throw new MutationOutboxValidationError("pinned is invalid");
  }
  return { pinned: payload.pinned };
}

function canonicalChatPayload(
  payload: Record<string, unknown>,
  resourceId: string | null,
): ChatMessageMutationPayload & { start_task: false } {
  assertExactKeys(payload, ["content", "conversation_id", "mode", "start_task"]);
  assertChatIsMessageOnly(payload.start_task);
  const result: ChatMessageMutationPayload & { start_task: false } = {
    content: requiredBoundedString(payload.content, "content", 32_000),
    start_task: false,
  };
  assignIfDefined(
    result,
    "conversation_id",
    resolveConversationId(payload.conversation_id, resourceId),
  );
  assignIfDefined(result, "mode", optionalChatMode(payload.mode));
  return result;
}

function assertChatIsMessageOnly(startTask: unknown): void {
  if (startTask !== undefined && startTask !== false) {
    throw new MutationOutboxValidationError("chat task creation cannot be queued offline");
  }
}

function resolveConversationId(
  value: unknown,
  resourceId: string | null,
): string | null | undefined {
  const payloadConversationId = optionalBoundedString(
    value,
    "conversation_id",
    500,
  );
  if (resourceId && payloadConversationId && resourceId !== payloadConversationId) {
    throw new MutationOutboxValidationError("conversation_id does not match resourceId");
  }
  return resourceId ?? payloadConversationId;
}

function optionalChatMode(value: unknown): ChatMessageMutationPayload["mode"] {
  if (value === undefined) return undefined;
  if (typeof value !== "string" || !CHAT_MODES.has(value as NonNullable<ChatMessageMutationPayload["mode"]>)) {
    throw new MutationOutboxValidationError("mode is invalid");
  }
  return value as NonNullable<ChatMessageMutationPayload["mode"]>;
}

function canonicalPayload(
  operation: SafeMutationOperation,
  payload: unknown,
  resourceId: string | null,
): MutationPayloadByOperation[SafeMutationOperation] {
  if (!isRecord(payload)) {
    throw new MutationOutboxValidationError("mutation payload must be an object");
  }
  if (operation === "feedback.create") return canonicalFeedbackPayload(payload);
  if (operation === "memory.metadata.update") {
    return canonicalMemoryPayload(payload, resourceId);
  }
  return canonicalChatPayload(payload, resourceId);
}

function assertSafeOperation(value: unknown): SafeMutationOperation {
  if (!SAFE_MUTATION_OPERATIONS.includes(value as SafeMutationOperation)) {
    throw new MutationOutboxValidationError("operation is not safe for offline replay");
  }
  return value as SafeMutationOperation;
}

function randomIdentifier(): string {
  const values = new Uint8Array(16);
  if (globalThis.crypto?.getRandomValues) {
    globalThis.crypto.getRandomValues(values);
  } else {
    for (let index = 0; index < values.length; index += 1) {
      values[index] = Math.floor(Math.random() * 256);
    }
  }
  return `mut_${[...values].map((value) => value.toString(16).padStart(2, "0")).join("")}`;
}

function validateRow(row: MutationOutboxRow): MutationDelivery {
  const origin = normalizeOrigin(row.origin);
  const operation = assertSafeOperation(row.operation);
  const resourceId = optionalResourceId(row.resource_id ?? undefined);
  let parsed: unknown;
  try {
    parsed = JSON.parse(row.payload_json);
  } catch {
    throw new MutationOutboxValidationError("stored mutation payload is invalid");
  }
  const payload = canonicalPayload(operation, parsed, resourceId);
  const payloadJson = JSON.stringify(payload);
  if (origin !== row.origin || payloadJson !== row.payload_json) {
    throw new MutationOutboxValidationError("stored mutation binding is invalid");
  }
  return {
    id: assertIdempotencyKey(row.id),
    origin,
    operation,
    resourceId,
    payload,
    idempotencyKey: assertIdempotencyKey(row.idempotency_key),
    createdAt: row.created_at,
    attempt: row.attempts + 1,
  };
}

function sameBinding(row: MutationOutboxRow, mutation: CanonicalMutation): boolean {
  return (
    row.origin === mutation.origin &&
    row.operation === mutation.operation &&
    row.resource_id === mutation.resourceId &&
    row.payload_json === mutation.payloadJson &&
    row.idempotency_key === mutation.idempotencyKey
  );
}

export class MutationOutbox {
  private readonly openDatabase: OpenDatabase;
  private readonly createId: () => string;
  private readonly now: () => string;
  private readonly sendTimeoutMs: number;
  private databasePromise: Promise<SQLite.SQLiteDatabase> | null = null;

  constructor(options: MutationOutboxOptions = {}) {
    this.openDatabase = options.openDatabase ?? (() => SQLite.openDatabaseAsync(DATABASE_NAME));
    this.createId = options.createId ?? randomIdentifier;
    this.now = options.now ?? (() => new Date().toISOString());
    this.sendTimeoutMs = options.sendTimeoutMs ?? DEFAULT_SEND_TIMEOUT_MS;
    if (
      !Number.isSafeInteger(this.sendTimeoutMs) ||
      this.sendTimeoutMs < 1 ||
      this.sendTimeoutMs > MAX_SEND_TIMEOUT_MS
    ) {
      throw new MutationOutboxValidationError("send timeout is invalid");
    }
  }

  private async database(): Promise<SQLite.SQLiteDatabase> {
    if (!this.databasePromise) {
      this.databasePromise = this.openDatabase().then(async (database) => {
        await database.execAsync(`
          PRAGMA journal_mode = WAL;
          CREATE TABLE IF NOT EXISTS mutation_outbox (
            id TEXT PRIMARY KEY,
            origin TEXT NOT NULL,
            operation TEXT NOT NULL,
            resource_id TEXT,
            payload_json TEXT NOT NULL,
            idempotency_key TEXT NOT NULL,
            created_at TEXT NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts >= 0),
            last_attempt_at TEXT,
            last_error TEXT,
            completed_at TEXT,
            UNIQUE(origin, idempotency_key),
            CHECK(operation IN ('feedback.create','memory.metadata.update','chat.message.create'))
          );
          CREATE INDEX IF NOT EXISTS idx_mutation_outbox_pending
          ON mutation_outbox(origin, completed_at, created_at);
        `);
        return database;
      });
    }
    return this.databasePromise;
  }

  async enqueue(input: MutationOutboxInput): Promise<MutationOutboxRow> {
    const operation = assertSafeOperation(input.operation);
    const origin = normalizeOrigin(input.origin);
    const resourceId = optionalResourceId(input.resourceId);
    const payloadJson = JSON.stringify(canonicalPayload(operation, input.payload, resourceId));
    const id = assertIdempotencyKey(this.createId());
    const idempotencyKey = assertIdempotencyKey(input.idempotencyKey ?? id);
    const createdAt = this.now();
    const database = await this.database();

    await database.runAsync(
      `INSERT OR IGNORE INTO mutation_outbox(
        id,origin,operation,resource_id,payload_json,idempotency_key,created_at,
        attempts,last_attempt_at,last_error,completed_at
      ) VALUES(?,?,?,?,?,?,?,0,NULL,NULL,NULL)`,
      id,
      origin,
      operation,
      resourceId,
      payloadJson,
      idempotencyKey,
      createdAt,
    );
    const row = await database.getFirstAsync<MutationOutboxRow>(
      "SELECT * FROM mutation_outbox WHERE origin=? AND idempotency_key=?",
      origin,
      idempotencyKey,
    );
    if (!row) throw new Error("mutation outbox insert failed");
    const canonical = { origin, operation, resourceId, payloadJson, idempotencyKey };
    if (!sameBinding(row, canonical)) {
      throw new MutationOutboxValidationError(
        "idempotency key is already bound to a different mutation",
      );
    }
    return row;
  }

  async pendingCount(originValue: string): Promise<number> {
    const origin = normalizeOrigin(originValue);
    const database = await this.database();
    const row = await database.getFirstAsync<CountRow>(
      "SELECT COUNT(*) AS count FROM mutation_outbox WHERE origin=? AND completed_at IS NULL",
      origin,
    );
    return row?.count ?? 0;
  }

  async abandonPending(originValue: string): Promise<number> {
    const origin = normalizeOrigin(originValue);
    const database = await this.database();
    const result = await database.runAsync(
      `UPDATE mutation_outbox SET completed_at=?, last_error=?
       WHERE origin=? AND completed_at IS NULL`,
      this.now(),
      ABANDONED_ERROR,
      origin,
    );
    return result.changes;
  }

  async drain(
    originValue: string,
    sender: MutationSender,
    limit = 50,
  ): Promise<MutationDrainResult> {
    const origin = normalizeOrigin(originValue);
    if (!Number.isSafeInteger(limit) || limit < 1 || limit > 100) {
      throw new MutationOutboxValidationError("drain limit is invalid");
    }
    return serializeOriginDrain(origin, () => this.drainOnce(origin, sender, limit));
  }

  private async waitForDelivery(
    pending: Promise<unknown>,
    controller: AbortController,
  ): Promise<void> {
    let timeout: ReturnType<typeof setTimeout> | undefined;
    const deadline = new Promise<never>((_resolve, reject) => {
      timeout = setTimeout(() => {
        // Reject the deadline first so this attempt is classified as uncertain,
        // even when aborting the transport synchronously rejects `pending`.
        reject(new MutationDeliveryTimeoutError());
        controller.abort();
      }, this.sendTimeoutMs);
    });
    try {
      await Promise.race([pending, deadline]);
    } finally {
      if (timeout !== undefined) clearTimeout(timeout);
    }
  }

  private rememberUncertainDelivery(
    database: SQLite.SQLiteDatabase,
    row: MutationOutboxRow,
    origin: string,
    pending: Promise<unknown>,
  ): void {
    const key = deliveryKey(origin, row.id);
    let tracked: Promise<void>;
    tracked = pending.then(
      async () => {
        await database.runAsync(
          `UPDATE mutation_outbox SET completed_at=?,last_error=NULL
           WHERE id=? AND origin=? AND completed_at IS NULL`,
          this.now(),
          row.id,
          origin,
        );
      },
      () => undefined,
    ).catch(() => undefined).finally(() => {
      if (uncertainDeliveries.get(key) === tracked) uncertainDeliveries.delete(key);
    });
    uncertainDeliveries.set(key, tracked);
  }

  private async handleDeliveryFailure(
    database: SQLite.SQLiteDatabase,
    row: MutationOutboxRow,
    origin: string,
    cause: unknown,
    pending: Promise<unknown>,
  ): Promise<MutationRowOutcome> {
    if (cause instanceof MutationDeliveryTimeoutError) {
      this.rememberUncertainDelivery(database, row, origin, pending);
      await database.runAsync(
        `UPDATE mutation_outbox SET last_error=?
         WHERE id=? AND origin=? AND completed_at IS NULL`,
        DELIVERY_TIMEOUT_ERROR,
        row.id,
        origin,
      );
      return { attempted: 1, completed: 0, failed: 1, stop: true };
    }
    if (isPermanentDeliveryError(cause)) {
      await database.runAsync(
        `UPDATE mutation_outbox SET completed_at=?,last_error=?
         WHERE id=? AND origin=? AND completed_at IS NULL`,
        this.now(),
        PERMANENT_DELIVERY_ERROR,
        row.id,
        origin,
      );
      return { attempted: 1, completed: 0, failed: 1, stop: false };
    }
    await database.runAsync(
      `UPDATE mutation_outbox SET last_error=?
       WHERE id=? AND origin=? AND completed_at IS NULL`,
      DELIVERY_ERROR,
      row.id,
      origin,
    );
    return { attempted: 1, completed: 0, failed: 1, stop: true };
  }

  private async processRow(
    database: SQLite.SQLiteDatabase,
    row: MutationOutboxRow,
    origin: string,
    sender: MutationSender,
  ): Promise<MutationRowOutcome> {
    if (uncertainDeliveries.has(deliveryKey(origin, row.id))) {
      return { attempted: 0, completed: 0, failed: 0, stop: true };
    }
    let delivery: MutationDelivery;
    try {
      delivery = validateRow(row);
    } catch {
      await database.runAsync(
        `UPDATE mutation_outbox SET completed_at=?,last_error=?
         WHERE id=? AND origin=? AND completed_at IS NULL`,
        this.now(),
        INVALID_LOCAL_ERROR,
        row.id,
        origin,
      );
      return { attempted: 0, completed: 0, failed: 1, stop: false };
    }

    const attemptStarted = await database.runAsync(
      `UPDATE mutation_outbox SET attempts=attempts+1,last_attempt_at=?,last_error=NULL
       WHERE id=? AND origin=? AND completed_at IS NULL`,
      this.now(),
      row.id,
      origin,
    );
    if (attemptStarted.changes !== 1) {
      return { attempted: 0, completed: 0, failed: 0, stop: false };
    }
    const controller = new AbortController();
    const pending = Promise.resolve().then(() => sender(delivery, controller.signal));
    try {
      await this.waitForDelivery(pending, controller);
    } catch (cause) {
      return this.handleDeliveryFailure(database, row, origin, cause, pending);
    }
    const marked = await database.runAsync(
      `UPDATE mutation_outbox SET completed_at=?,last_error=NULL
       WHERE id=? AND origin=? AND completed_at IS NULL`,
      this.now(),
      row.id,
      origin,
    );
    return {
      attempted: 1,
      completed: marked.changes === 1 ? 1 : 0,
      failed: 0,
      stop: false,
    };
  }

  private async drainOnce(
    origin: string,
    sender: MutationSender,
    limit: number,
  ): Promise<MutationDrainResult> {
    const database = await this.database();
    const rows = await database.getAllAsync<MutationOutboxRow>(
      `SELECT * FROM mutation_outbox
       WHERE completed_at IS NULL AND origin=?
       ORDER BY created_at ASC, id ASC LIMIT ?`,
      origin,
      limit,
    );
    let attempted = 0;
    let completed = 0;
    let failed = 0;

    for (const row of rows) {
      const outcome = await this.processRow(database, row, origin, sender);
      attempted += outcome.attempted;
      completed += outcome.completed;
      failed += outcome.failed;
      if (outcome.stop) break;
    }

    return {
      attempted,
      completed,
      failed,
      remaining: await this.pendingCount(origin),
    };
  }
}

export const mutationOutbox = new MutationOutbox();
