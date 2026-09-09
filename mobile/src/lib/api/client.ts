import { fetch } from "expo/fetch";
import * as SecureStore from "expo-secure-store";

import { notifyConnectionChanged } from "@/lib/connection-events";
import {
  assertCapabilityRequestFresh,
  CapabilityProtocolError,
  parseCapabilityAuthorizationResponse,
  parseCapabilityConsumeReceipt,
  parseCapabilityRequestDetail,
  parseCapabilityRequestEnvelope,
  parseCapabilityRequestList,
  parseCapabilityRequestLookup,
  parseCapabilityResult,
  parseCapabilityResultReceipt,
} from "@/lib/iphone-capabilities/grant";
import type {
  CapabilityAuthorizationDecision,
  CapabilityAuthorizationResponse,
  CapabilityRequestDetail,
  CapabilityRequestEnvelope,
  CapabilityRequestPreview,
  CapabilityResult,
  CapabilityResultReceipt,
  ConsumedCapabilityGrant,
} from "@/lib/iphone-capabilities/types";
import type { CapabilityTransportSession } from "@/lib/iphone-capabilities/transport";
import {
  mutationOutbox,
  type MutationDelivery,
  type MutationDrainResult,
} from "@/lib/state/mutation-outbox";
import { applyBootstrap, upsertEvent } from "@/lib/state/replica";
import type {
  Agent,
  Approval,
  ApprovalDecisionResult,
  AuditEvent,
  Bootstrap,
  GoalCreateInput,
  GoalDetail,
  GoalFeedbackInput,
  GoalRecord,
  GoalResult,
  MemoryItem,
  Message,
  PlanNode,
  Task,
  TaskDetail,
  TaskMode,
  TaskStatus,
  ToolCall,
  ToolProposalInput,
} from "@/lib/api/types";

export type {
  Agent,
  Approval,
  ApprovalDecisionResult,
  AuditEvent,
  Bootstrap,
  GoalAutonomyProfile,
  GoalCreateInput,
  GoalDetail,
  GoalFeedbackInput,
  GoalNodeStatus,
  GoalRecord,
  GoalResult,
  GoalStatus,
  MemoryItem,
  Message,
  PlanNode,
  Task,
  TaskDetail,
  TaskMode,
  TaskStatus,
  ToolCall,
  ToolProposalInput,
} from "@/lib/api/types";

const CONNECTION_KEY = "mongars.connection.v1";
const PENDING_CONNECTION_KEY = "mongars.connection.pending.v1";
const LEGACY_SERVER_URL_KEY = "mongars.server_url";
const LEGACY_TOKEN_KEY = "mongars.device_token";
const DEFAULT_SERVER_URL = "http://127.0.0.1:8710";

let latestBootstrapGeneration = 0;
let bootstrapReplicaApplyTail: Promise<void> = Promise.resolve();
let latestPairingAttemptGeneration = 0;
let connectionStorageGeneration = 0;
let connectionStorageTail: Promise<void> = Promise.resolve();

type StoredConnection = {
  baseUrl: string;
  token: string;
};

type PendingConnection = StoredConnection & {
  deviceId: string;
  pairingId: string;
  replacedOrigins?: string[];
};

type PendingConnectionSnapshot = PendingConnection & {
  serialized: string;
  storageGeneration: number;
};

type ResolvedConnection =
  | (StoredConnection & { source: "active" })
  | (PendingConnectionSnapshot & { source: "pending" })
  | { baseUrl: string; source: "default"; token: null };

type RequestConnectionFence = {
  baseUrl: string;
  token: string | null;
  storageGeneration: number;
};

export type PairingResult = {
  bootstrap: Bootstrap;
  serverUrl: string;
};

export type EventStreamTicket = {
  expiresInSeconds: number;
  serverUrl: string;
  url: string;
};

export type MutationSyncReceipt = {
  idempotent_replay: boolean;
  result: unknown;
};

export type MutationOutboxApiSession = {
  origin: string;
  drain: (limit?: number) => Promise<MutationDrainResult>;
  send: (
    mutation: MutationDelivery,
    signal?: AbortSignal,
  ) => Promise<MutationSyncReceipt>;
};

export class ApiError extends Error {
  constructor(
    readonly status: number,
    message: string,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

async function withConnectionStorageLock<T>(
  operation: () => Promise<T>,
): Promise<T> {
  const previous = connectionStorageTail;
  let release = (): void => undefined;
  connectionStorageTail = new Promise<void>((resolve) => {
    release = resolve;
  });
  await previous;
  try {
    return await operation();
  } finally {
    release();
  }
}

function connectionChangedDuringRecovery(): Error {
  return new Error("La connexion jumelée a changé pendant la reprise; réessaie l’action.");
}

function isLoopbackHostname(hostname: string): boolean {
  const normalized = hostname.toLowerCase().replace(/^\[|\]$/g, "");
  if (normalized === "localhost" || normalized.endsWith(".localhost") || normalized === "::1") {
    return true;
  }
  const octets = normalized.split(".");
  return (
    octets.length === 4 &&
    octets.every((octet) => /^\d{1,3}$/.test(octet) && Number(octet) <= 255) &&
    Number(octets[0]) === 127
  );
}

function parseServerUrl(value: string): URL {
  try {
    return new URL(value.trim().replace(/\/+$/, ""));
  } catch {
    throw new Error("L’URL du control plane est invalide.");
  }
}

function assertSupportedServerUrl(parsed: URL): void {
  if (!["http:", "https:"].includes(parsed.protocol) || !parsed.hostname) {
    throw new Error("Utilise une URL http ou https valide.");
  }
}

function assertSecureServerTransport(parsed: URL): void {
  if (parsed.protocol === "http:" && !isLoopbackHostname(parsed.hostname)) {
    throw new Error("HTTPS est obligatoire hors de la boucle locale.");
  }
}

function assertBareServerOrigin(parsed: URL): void {
  if (parsed.username || parsed.password || parsed.search || parsed.hash) {
    throw new Error("L’URL ne doit contenir ni identifiants, ni paramètres.");
  }
  if (parsed.pathname !== "/") {
    throw new Error("L’URL du control plane ne doit pas contenir de chemin.");
  }
}

function normalizeServerUrl(value: string): string {
  const parsed = parseServerUrl(value);
  assertSupportedServerUrl(parsed);
  assertSecureServerTransport(parsed);
  assertBareServerOrigin(parsed);
  return parsed.origin;
}

function parseStoredConnection(value: string): StoredConnection {
  const parsed = JSON.parse(value) as Partial<StoredConnection>;
  if (typeof parsed.baseUrl !== "string" || !isDeviceToken(parsed.token)) {
    throw new Error("invalid shape");
  }
  return { baseUrl: normalizeServerUrl(parsed.baseUrl), token: parsed.token };
}

function requiredPendingDeviceId(value: unknown): string {
  if (typeof value !== "string" || !value) {
    throw new Error("invalid pending shape");
  }
  return value;
}

function requiredPendingPairingId(value: unknown): string {
  if (typeof value !== "string" || !/^pair_[0-9a-f]{32}$/.test(value)) {
    throw new Error("invalid pending shape");
  }
  return value;
}

function normalizeReplacedOrigin(value: unknown): string {
  if (typeof value !== "string") {
    throw new Error("invalid pending shape");
  }
  return normalizeServerUrl(value);
}

function parseReplacedOrigins(value: unknown): string[] | undefined {
  if (value === undefined) return undefined;
  if (!Array.isArray(value) || value.length > 2) {
    throw new Error("invalid pending shape");
  }
  const origins = value.map(normalizeReplacedOrigin);
  if (new Set(origins).size !== origins.length) {
    throw new Error("invalid pending shape");
  }
  return origins;
}

function parsePendingConnection(value: string): PendingConnection {
  const parsed = JSON.parse(value) as Partial<PendingConnection>;
  const connection = parseStoredConnection(value);
  const deviceId = requiredPendingDeviceId(parsed.deviceId);
  const pairingId = requiredPendingPairingId(parsed.pairingId);
  const replacedOrigins = parseReplacedOrigins(parsed.replacedOrigins);
  return {
    ...connection,
    deviceId,
    pairingId,
    ...(replacedOrigins?.length ? { replacedOrigins } : {}),
  };
}

async function readActiveConnectionLocked(): Promise<StoredConnection | null> {
  const stored = await SecureStore.getItemAsync(CONNECTION_KEY);
  if (!stored) return null;
  try {
    return parseStoredConnection(stored);
  } catch {
    throw new Error("La connexion sécurisée enregistrée est invalide. Recommence le jumelage.");
  }
}

async function readPendingConnectionLocked(): Promise<PendingConnectionSnapshot | null> {
  const stored = await SecureStore.getItemAsync(PENDING_CONNECTION_KEY);
  if (!stored) return null;
  try {
    return {
      ...parsePendingConnection(stored),
      serialized: stored,
      storageGeneration: connectionStorageGeneration,
    };
  } catch {
    const deleted = await SecureStore.deleteItemAsync(PENDING_CONNECTION_KEY).then(
      () => true,
      () => false,
    );
    if (deleted) connectionStorageGeneration += 1;
    return null;
  }
}

async function getConnection(): Promise<ResolvedConnection> {
  return withConnectionStorageLock(async () => {
    const pending = await readPendingConnectionLocked();
    if (pending) return { ...pending, source: "pending" };

    const active = await readActiveConnectionLocked();
    if (active) return { ...active, source: "active" };

    // Legacy releases stored the origin and token independently, so a failed
    // re-pair could leave a new origin beside an old bearer. Never recombine
    // those values: the only safe migration is a fresh pairing that creates the
    // atomically bound connection record above.
    const [legacyUrl, legacyToken] = await Promise.all([
      SecureStore.getItemAsync(LEGACY_SERVER_URL_KEY),
      SecureStore.getItemAsync(LEGACY_TOKEN_KEY),
    ]);
    if (legacyUrl || legacyToken) {
      throw new Error(
        "Une ancienne connexion non liée a été détectée. Recommence le jumelage.",
      );
    }
    return {
      baseUrl: DEFAULT_SERVER_URL,
      source: "default",
      token: null,
    };
  });
}

async function resolveRequestConnection(): Promise<{ baseUrl: string; token: string | null }> {
  const stored = await getConnection();
  return stored.source === "pending" ? resolvePendingConnection(stored) : stored;
}

async function readEffectiveConnectionLocked(): Promise<{
  baseUrl: string;
  token: string | null;
}> {
  const pending = await readPendingConnectionLocked();
  if (pending) return pending;
  const active = await readActiveConnectionLocked();
  return active ?? { baseUrl: DEFAULT_SERVER_URL, token: null };
}

function connectionRequestChanged(): Error {
  return new Error("La connexion jumelée a changé pendant la requête; réessaie l’action.");
}

async function captureRequestConnectionFence(): Promise<RequestConnectionFence> {
  const resolved = await resolveRequestConnection();
  return withConnectionStorageLock(async () => {
    const current = await readEffectiveConnectionLocked();
    if (current.baseUrl !== resolved.baseUrl || current.token !== resolved.token) {
      throw connectionRequestChanged();
    }
    return {
      ...resolved,
      storageGeneration: connectionStorageGeneration,
    };
  });
}

async function assertRequestConnectionCurrent(
  connection: RequestConnectionFence,
): Promise<void> {
  await withConnectionStorageLock(async () => {
    if (connection.storageGeneration !== connectionStorageGeneration) {
      throw connectionRequestChanged();
    }
    const current = await readEffectiveConnectionLocked();
    if (current.baseUrl !== connection.baseUrl || current.token !== connection.token) {
      throw connectionRequestChanged();
    }
  });
}

async function fencedRequest<T>(
  path: string,
  init?: RequestInit,
  shouldAccept: () => boolean = () => true,
): Promise<T> {
  const connection = await captureRequestConnectionFence();
  if (!shouldAccept()) throw connectionRequestChanged();
  const value = await requestAt<T>(connection.baseUrl, path, init, connection.token);
  await assertRequestConnectionCurrent(connection);
  if (!shouldAccept()) throw connectionRequestChanged();
  return value;
}

export async function getServerUrl(): Promise<string> {
  return (await getConnection()).baseUrl;
}

export async function hasDeviceToken(): Promise<boolean> {
  return Boolean((await getConnection()).token);
}

export async function createEventStreamTicket(): Promise<EventStreamTicket> {
  const stored = await getConnection();
  const connection = stored.source === "pending"
    ? await resolvePendingConnection(stored)
    : stored;
  if (!connection.token) {
    throw new ApiError(401, "Cet iPhone n’est pas jumelé au control plane.");
  }

  const value = await requestAt<unknown>(
    connection.baseUrl,
    "/ws/ticket",
    { method: "POST" },
    connection.token,
  );
  if (
    !isRecord(value) ||
    Object.keys(value).some((key) => !["ticket", "expires_in_seconds"].includes(key)) ||
    typeof value.ticket !== "string" ||
    !/^[A-Za-z0-9_-]{20,256}$/.test(value.ticket) ||
    !Number.isSafeInteger(value.expires_in_seconds) ||
    Number(value.expires_in_seconds) < 1 ||
    Number(value.expires_in_seconds) > 300
  ) {
    throw new Error("Le control plane a retourné un ticket temps réel invalide.");
  }

  const url = new URL(connection.baseUrl);
  url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
  url.pathname = "/ws";
  url.search = "";
  url.searchParams.set("ticket", value.ticket);
  return {
    expiresInSeconds: Number(value.expires_in_seconds),
    serverUrl: connection.baseUrl,
    url: url.toString(),
  };
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const connection = await resolveRequestConnection();
  return requestAt<T>(connection.baseUrl, path, init, connection.token);
}

async function resolvePendingConnection(
  pending: PendingConnectionSnapshot,
): Promise<StoredConnection> {
  try {
    const bootstrap = await requestAt<unknown>(
      pending.baseUrl,
      "/sync/bootstrap",
      undefined,
      pending.token,
    );
    if (!isBootstrapEnvelope(bootstrap)) {
      throw new Error("Le candidat n’a pas retourné un bootstrap de reprise valide.");
    }
    const outcome = await promotePendingConnection(pending);
    if (outcome === "stale") throw connectionChangedDuringRecovery();
    if (outcome === "promoted") notifyConnectionChanged();
    return pending;
  } catch (cause) {
    if (!(cause instanceof ApiError) || cause.status !== 401) throw cause;
    const recovery = await discardPendingConnection(pending);
    if (recovery.stale) throw connectionChangedDuringRecovery();
    const active = recovery.active;
    if (!active) throw cause;
    return active;
  }
}

async function isPendingConnectionCurrentLocked(
  pending: PendingConnectionSnapshot,
): Promise<boolean> {
  if (pending.storageGeneration !== connectionStorageGeneration) return false;
  return (await SecureStore.getItemAsync(PENDING_CONNECTION_KEY)) === pending.serialized;
}

async function discardPendingConnection(
  pending: PendingConnectionSnapshot,
): Promise<{ active: StoredConnection | null; stale: boolean }> {
  return withConnectionStorageLock(async () => {
    if (!(await isPendingConnectionCurrentLocked(pending))) {
      return { active: null, stale: true };
    }
    await SecureStore.deleteItemAsync(PENDING_CONNECTION_KEY);
    connectionStorageGeneration += 1;
    return { active: await readActiveConnectionLocked(), stale: false };
  });
}

async function abandonReplacedMutationOriginsLocked(
  pending: PendingConnection,
): Promise<void> {
  let origins = pending.replacedOrigins;
  if (origins === undefined) {
    const active = await readActiveConnectionLocked();
    origins = active && !sameConnection(active, pending) ? [active.baseUrl] : [];
  }
  for (const origin of origins) {
    await mutationOutbox.abandonPending(origin);
  }
}

type PendingPromotionOutcome = "promoted" | "retained" | "stale";

async function promotePendingConnection(
  pending: PendingConnectionSnapshot,
  beforePromotion?: () => Promise<void>,
): Promise<PendingPromotionOutcome> {
  return withConnectionStorageLock(async () => {
    if (!(await isPendingConnectionCurrentLocked(pending))) return "stale";
    if (beforePromotion) await beforePromotion();
    await abandonReplacedMutationOriginsLocked(pending);
    try {
      await SecureStore.setItemAsync(
        CONNECTION_KEY,
        JSON.stringify({
          baseUrl: pending.baseUrl,
          token: pending.token,
        } satisfies StoredConnection),
      );
    } catch {
      // The pending record remains a durable, origin-bound recovery credential.
      return "retained";
    }
    connectionStorageGeneration += 1;
    await Promise.allSettled([
      SecureStore.deleteItemAsync(PENDING_CONNECTION_KEY),
      SecureStore.deleteItemAsync(LEGACY_SERVER_URL_KEY),
      SecureStore.deleteItemAsync(LEGACY_TOKEN_KEY),
    ]);
    return "promoted";
  });
}

async function requestAt<T>(
  baseUrl: string,
  path: string,
  init?: RequestInit,
  token?: string | null,
): Promise<T> {
  const response = await fetch(`${baseUrl}${path}`, {
    ...init,
    headers: {
      ...(init?.body ? { "Content-Type": "application/json" } : {}),
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
      ...init?.headers,
    },
  });
  if (!response.ok) throw await responseError(response);
  return readResponse<T>(response);
}

async function responseError(response: Response): Promise<ApiError> {
  let detail = "";
  try {
    const body = (await response.json()) as { detail?: string };
    detail = typeof body.detail === "string" ? body.detail : "";
  } catch {
    detail = await response.text().catch(() => "");
  }
  return new ApiError(
    response.status,
    detail || `Le control plane a répondu HTTP ${response.status}.`,
  );
}

function readResponse<T>(response: Response): Promise<T> | T {
  if (response.status === 204) return undefined as T;
  return response.json() as Promise<T>;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function isDeviceToken(value: unknown): value is string {
  return (
    typeof value === "string" &&
    value.length > 0 &&
    value.length <= 4096 &&
    value.trim() === value &&
    !/\s/.test(value)
  );
}

function isBootstrapEnvelope(value: unknown): value is Bootstrap {
  if (!isRecord(value) || !isRecord(value.counts)) return false;
  const arrays = [
    value.tasks,
    value.approvals,
    value.tool_calls,
    value.conversations,
    value.agents,
    value.pinned_memory,
  ];
  const optionalArrays = [value.messages, value.goals, value.plan_nodes, value.goal_results];
  const counts = [
    value.counts.tasks,
    value.counts.messages,
    value.counts.agents,
    value.counts.approvals_pending,
    value.counts.memory_items,
    value.counts.audit_events,
  ];
  return (
    typeof value.server_time === "string" &&
    typeof value.cursor === "string" &&
    arrays.every(Array.isArray) &&
    optionalArrays.every((item) => item === undefined || Array.isArray(item)) &&
    counts.every((count) => Number.isSafeInteger(count) && Number(count) >= 0)
  );
}

function parseMutationSyncReceipt(value: unknown): MutationSyncReceipt {
  if (
    !isRecord(value) ||
    Object.keys(value).some((key) => !["idempotent_replay", "result"].includes(key)) ||
    typeof value.idempotent_replay !== "boolean" ||
    !("result" in value)
  ) {
    throw new Error("Le control plane a retourné un reçu de mutation invalide.");
  }
  return { idempotent_replay: value.idempotent_replay, result: value.result };
}

function resourceId(value: string): string {
  return encodeURIComponent(value);
}

type PairingCandidate = {
  candidateToken: string;
  deviceId: string;
  pairingId: string;
};

function parsePairingCandidate(value: unknown, expectedDeviceId: string): PairingCandidate {
  if (
    !isRecord(value) ||
    !isDeviceToken(value.candidate_token) ||
    typeof value.pairing_id !== "string" ||
    !/^pair_[0-9a-f]{32}$/.test(value.pairing_id) ||
    value.device_id !== expectedDeviceId ||
    !Number.isSafeInteger(value.expires_in_seconds) ||
    Number(value.expires_in_seconds) <= 0
  ) {
    throw new Error("Le serveur de jumelage n’a pas retourné de candidat valide.");
  }
  return {
    candidateToken: value.candidate_token,
    deviceId: expectedDeviceId,
    pairingId: value.pairing_id,
  };
}

function assertPairingFinalization(value: unknown, candidate: PairingCandidate): void {
  if (
    !isRecord(value) ||
    !["ready", "active"].includes(String(value.status)) ||
    value.device_id !== candidate.deviceId ||
    value.pairing_id !== candidate.pairingId ||
    typeof value.already_finalized !== "boolean"
  ) {
    throw new Error("Le serveur n’a pas confirmé l’activation liée à ce jumelage.");
  }
}

async function stagePendingConnection(
  target: string,
  candidate: PairingCandidate,
  pairingAttemptGeneration: number,
): Promise<PendingConnectionSnapshot> {
  return withConnectionStorageLock(async () => {
    if (pairingAttemptGeneration !== latestPairingAttemptGeneration) {
      throw connectionChangedDuringRecovery();
    }
    const priorPending = await readPendingConnectionLocked();
    const priorActive = await readActiveConnectionLocked();
    const replacedOrigins = [...new Set(
      [priorPending, priorActive]
        .filter((connection): connection is StoredConnection => (
          connection !== null
          && (connection.baseUrl !== target || connection.token !== candidate.candidateToken)
        ))
        .map((connection) => connection.baseUrl),
    )];
    const pending = {
      baseUrl: target,
      token: candidate.candidateToken,
      pairingId: candidate.pairingId,
      deviceId: candidate.deviceId,
      ...(replacedOrigins.length ? { replacedOrigins } : {}),
    } satisfies PendingConnection;
    const serialized = JSON.stringify(pending);
    await SecureStore.setItemAsync(PENDING_CONNECTION_KEY, serialized);
    connectionStorageGeneration += 1;
    return {
      ...pending,
      serialized,
      storageGeneration: connectionStorageGeneration,
    };
  });
}

export async function pairDevice(
  code: string,
  deviceId: string,
  name = "iPhone",
  serverUrl?: string,
): Promise<PairingResult> {
  const pairingAttemptGeneration = ++latestPairingAttemptGeneration;
  const target = normalizeServerUrl(serverUrl ?? (await getServerUrl()));
  const response = await requestAt<unknown>(
    target,
    "/pairing/complete",
    {
      method: "POST",
      body: JSON.stringify({ code, device_id: deviceId, name }),
    },
    null,
  );
  const candidate = parsePairingCandidate(response, deviceId);
  const stagedBootstrap = await requestAt<unknown>(
    target,
    "/sync/bootstrap",
    undefined,
    candidate.candidateToken,
  );
  if (!isBootstrapEnvelope(stagedBootstrap)) {
    throw new Error("Le serveur jumelé n’a pas retourné un bootstrap authentifié valide.");
  }
  const finalized = await requestAt<unknown>(
    target,
    "/pairing/finalize",
    {
      method: "POST",
      body: JSON.stringify({ pairing_id: candidate.pairingId, device_id: candidate.deviceId }),
    },
    candidate.candidateToken,
  );
  assertPairingFinalization(finalized, candidate);
  const pending = await stagePendingConnection(target, candidate, pairingAttemptGeneration);

  // This second authenticated read is the protocol cutover, not a duplicate
  // verification. The server promotes the ready bearer only after its recovery
  // record is durable. A lost response leaves that record available for retry.
  const activeBootstrap = await requestAt<unknown>(
    target,
    "/sync/bootstrap",
    undefined,
    candidate.candidateToken,
  );
  if (!isBootstrapEnvelope(activeBootstrap)) {
    throw new Error("Le serveur activé n’a pas retourné un bootstrap authentifié valide.");
  }
  const outcome = await promotePendingConnection(
    pending,
    () => applyBootstrap(activeBootstrap, target),
  );
  if (outcome === "stale") throw connectionChangedDuringRecovery();
  notifyConnectionChanged();
  return { bootstrap: activeBootstrap, serverUrl: target };
}

async function requireMutationConnection(): Promise<StoredConnection> {
  const connection = await resolveRequestConnection();
  if (!connection.token) {
    throw new ApiError(401, "Cet iPhone n’est pas jumelé au control plane.");
  }
  return { baseUrl: connection.baseUrl, token: connection.token };
}

export async function createMutationOutboxApiSession(): Promise<MutationOutboxApiSession> {
  const connection = await requireMutationConnection();
  const send = async (
    mutation: MutationDelivery,
    signal?: AbortSignal,
  ): Promise<MutationSyncReceipt> => {
    if (mutation.origin !== connection.baseUrl) {
      throw new Error("La mutation locale appartient à un autre control plane.");
    }
    const current = await requireMutationConnection();
    if (!sameConnection(current, connection)) {
      throw new Error("La connexion jumelée a changé; la mutation locale a été bloquée.");
    }
    const value = await requestAt<unknown>(
      connection.baseUrl,
      "/sync/mutations",
      {
        method: "POST",
        ...(signal ? { signal } : {}),
        headers: { "Idempotency-Key": mutation.idempotencyKey },
        body: JSON.stringify({
          operation: mutation.operation,
          ...(mutation.resourceId ? { resource_id: mutation.resourceId } : {}),
          payload: mutation.payload,
        }),
      },
      connection.token,
    );
    return parseMutationSyncReceipt(value);
  };
  return {
    origin: connection.baseUrl,
    send,
    drain: (limit) => mutationOutbox.drain(connection.baseUrl, send, limit),
  };
}

export async function drainMutationOutbox(limit?: number): Promise<MutationDrainResult> {
  const session = await createMutationOutboxApiSession();
  return session.drain(limit);
}

export function createTask(input: string, mode: TaskMode = "normal"): Promise<Task> {
  return request<Task>("/tasks", {
    method: "POST",
    body: JSON.stringify({ input, mode }),
  });
}

export function listTasks(status?: TaskStatus): Promise<Task[]> {
  const query = status ? `?status=${encodeURIComponent(status)}` : "";
  return request<Task[]>(`/tasks${query}`);
}

export function getTask(taskId: string): Promise<TaskDetail> {
  return request<TaskDetail>(`/tasks/${resourceId(taskId)}`);
}

export function listGoals(
  shouldAccept: () => boolean = () => true,
): Promise<GoalRecord[]> {
  return fencedRequest<GoalRecord[]>("/goals", undefined, shouldAccept);
}

export function createGoal(input: GoalCreateInput): Promise<GoalDetail> {
  return fencedRequest<GoalDetail>("/goals", {
    method: "POST",
    body: JSON.stringify(input),
  });
}

export function getGoal(
  goalId: string,
  shouldAccept: () => boolean = () => true,
): Promise<GoalDetail> {
  return fencedRequest<GoalDetail>(
    `/goals/${resourceId(goalId)}`,
    undefined,
    shouldAccept,
  );
}

export function startGoal(goalId: string): Promise<GoalDetail> {
  return fencedRequest<GoalDetail>(`/goals/${resourceId(goalId)}/start`, {
    method: "POST",
    body: JSON.stringify({}),
  });
}

export function cancelGoal(goalId: string): Promise<GoalDetail> {
  return fencedRequest<GoalDetail>(`/goals/${resourceId(goalId)}/cancel`, {
    method: "POST",
    body: JSON.stringify({}),
  });
}

export function replanGoal(goalId: string, reason?: string): Promise<GoalDetail> {
  return fencedRequest<GoalDetail>(`/goals/${resourceId(goalId)}/replan`, {
    method: "POST",
    body: JSON.stringify(reason?.trim() ? { reason: reason.trim() } : {}),
  });
}

export function listGoalNodes(
  goalId: string,
  shouldAccept: () => boolean = () => true,
): Promise<PlanNode[]> {
  return fencedRequest<PlanNode[]>(
    `/goals/${resourceId(goalId)}/nodes`,
    undefined,
    shouldAccept,
  );
}

export function getGoalResult(
  goalId: string,
  shouldAccept: () => boolean = () => true,
): Promise<GoalResult | null> {
  return fencedRequest<GoalResult | null>(
    `/goals/${resourceId(goalId)}/result`,
    undefined,
    shouldAccept,
  );
}

export function createGoalFeedback(
  goalId: string,
  input: GoalFeedbackInput,
): Promise<unknown> {
  return fencedRequest<unknown>(`/goals/${resourceId(goalId)}/feedback`, {
    method: "POST",
    body: JSON.stringify(input),
  });
}

export function cancelTask(taskId: string): Promise<Task> {
  return request<Task>(`/tasks/${resourceId(taskId)}/cancel`, { method: "POST" });
}

export function planTask(
  taskId: string,
): Promise<ToolCall | { task_id: string; proposal: unknown; task: Task | null }> {
  return request<ToolCall | { task_id: string; proposal: unknown; task: Task | null }>(
    `/tasks/${resourceId(taskId)}/plan`,
    { method: "POST" },
  );
}

export function submitToolProposal(
  taskId: string,
  proposal: ToolProposalInput,
): Promise<ToolCall> {
  return request<ToolCall>(`/tasks/${resourceId(taskId)}/tool-calls`, {
    method: "POST",
    body: JSON.stringify({ ...proposal, planner_source: "iphone_local" }),
  });
}

export function sendChat(
  content: string,
  conversationId?: string,
  mode: TaskMode = "normal",
  startTask = false,
): Promise<{ conversation_id: string; task: Task | null; message?: Message }> {
  return request<{ conversation_id: string; task: Task | null; message?: Message }>("/chat", {
    method: "POST",
    body: JSON.stringify({ content, conversation_id: conversationId, mode, start_task: startTask }),
  });
}

export async function listMessages(
  conversationId: string,
  shouldAccept: () => boolean = () => true,
): Promise<Message[]> {
  const connection = await resolveRequestConnection();
  if (!shouldAccept()) throw new Error("La lecture de conversation n’est plus actuelle.");
  const messages = await requestAt<Message[]>(
    connection.baseUrl,
    `/conversations/${resourceId(conversationId)}/messages`,
    undefined,
    connection.token,
  );
  const current = await resolveRequestConnection();
  if (
    !shouldAccept()
    || current.baseUrl !== connection.baseUrl
    || current.token !== connection.token
  ) {
    throw new Error("La connexion jumelée a changé pendant la lecture de conversation.");
  }
  return messages;
}

export async function bootstrapSync(
  shouldApply: () => boolean = () => true,
): Promise<Bootstrap> {
  const generation = ++latestBootstrapGeneration;
  const connection = await captureRequestConnectionFence();
  const data = await requestAt<Bootstrap>(
    connection.baseUrl,
    "/sync/bootstrap",
    undefined,
    connection.token,
  );
  await assertRequestConnectionCurrent(connection);
  const pendingApply = bootstrapReplicaApplyTail
    .catch(() => undefined)
    .then(async () => {
      if (generation !== latestBootstrapGeneration || !shouldApply()) return;
      await assertRequestConnectionCurrent(connection);
      await applyBootstrap(data, connection.baseUrl);
    });
  bootstrapReplicaApplyTail = pendingApply;
  await pendingApply;
  await assertRequestConnectionCurrent(connection);
  return data;
}

export function listApprovals(
  status: Approval["status"] = "pending",
): Promise<Approval[]> {
  return request<Approval[]>(`/approvals?status=${encodeURIComponent(status)}`);
}

export type ApprovalDecisionReceipt = {
  authoritativeResult: ApprovalDecisionResult;
  localReplicaError: string | null;
};

export async function decideApproval(
  id: string,
  decision: "approve" | "deny",
): Promise<ApprovalDecisionReceipt> {
  const stored = await getConnection();
  const connection = stored.source === "pending"
    ? await resolvePendingConnection(stored)
    : stored;
  const result = await requestAt<ApprovalDecisionResult>(
    connection.baseUrl,
    `/approvals/${resourceId(id)}/decision`,
    { method: "POST", body: JSON.stringify({ decision }) },
    connection.token,
  );
  const approval = "approval" in result ? result.approval : result;
  try {
    await upsertEvent(connection.baseUrl, "approval.decided", approval);
    return { authoritativeResult: result, localReplicaError: null };
  } catch (cause) {
    return {
      authoritativeResult: result,
      localReplicaError: cause instanceof Error ? cause.message : String(cause),
    };
  }
}

export function listMemory(): Promise<MemoryItem[]> {
  return request<MemoryItem[]>("/memory");
}

export function searchMemory(query: string): Promise<MemoryItem[]> {
  return request<MemoryItem[]>("/memory/search", {
    method: "POST",
    body: JSON.stringify({ query }),
  });
}

export function rememberMemory(input: {
  content: string;
  summary?: string;
  scope?: string;
  kind?: string;
  pinned?: boolean;
}): Promise<MemoryItem> {
  return request<MemoryItem>("/memory", { method: "POST", body: JSON.stringify(input) });
}

export function updateMemory(
  id: string,
  input: { content?: string; summary?: string; pinned?: boolean },
): Promise<MemoryItem> {
  return request<MemoryItem>(`/memory/${resourceId(id)}`, {
    method: "PATCH",
    body: JSON.stringify(input),
  });
}

export function deleteMemory(id: string): Promise<void> {
  return request<void>(`/memory/${resourceId(id)}`, { method: "DELETE" });
}

export function listAgents(): Promise<Agent[]> {
  return request<Agent[]>("/agents");
}

export function listAudit(limit = 30): Promise<AuditEvent[]> {
  const bounded = Math.min(Math.max(limit, 1), 200);
  return request<AuditEvent[]>(`/audit?limit=${bounded}`);
}

type CapabilityRequestFunction = <T>(path: string, init?: RequestInit) => Promise<T>;

async function listIPhoneCapabilityRequestsWith(
  send: CapabilityRequestFunction,
): Promise<CapabilityRequestPreview[]> {
  return parseCapabilityRequestList(await send<unknown>("/iphone/capabilities/requests"));
}

async function getIPhoneCapabilityRequestWith(
  send: CapabilityRequestFunction,
  requestId: string,
): Promise<CapabilityRequestDetail> {
  return parseCapabilityRequestLookup(
    await send<unknown>(`/iphone/capabilities/requests/${resourceId(requestId)}`),
  );
}

async function authorizeIPhoneCapabilityRequestWith(
  send: CapabilityRequestFunction,
  capabilityRequest: CapabilityRequestDetail,
  decision: CapabilityAuthorizationDecision,
): Promise<CapabilityAuthorizationResponse> {
  if (decision !== "approve" && decision !== "deny") {
    throw new CapabilityProtocolError("Capability authorization decision is invalid.");
  }
  const strictRequest = parseCapabilityRequestDetail(capabilityRequest);
  assertCapabilityRequestFresh(strictRequest);
  const canDecide = strictRequest.status === "waiting_approval";
  const canRecover = (
    decision === "approve" &&
    strictRequest.status === "approved" &&
    strictRequest.grant === null
  );
  if ((!canDecide && !canRecover) || strictRequest.grant !== null) {
    throw new CapabilityProtocolError(
      "Only a waiting request or an approved grant-recovery request may be authorized.",
    );
  }
  const value = await send<unknown>(
    `/iphone/capabilities/requests/${resourceId(strictRequest.request_id)}/authorize`,
    { method: "POST", body: JSON.stringify({ decision }) },
  );
  return parseCapabilityAuthorizationResponse(value, decision, strictRequest);
}

async function consumeIPhoneCapabilityRequestWith(
  send: CapabilityRequestFunction,
  capabilityRequest: CapabilityRequestEnvelope,
): Promise<ConsumedCapabilityGrant> {
  const strictRequest = parseCapabilityRequestEnvelope(capabilityRequest);
  const value = await send<unknown>(
    `/iphone/capabilities/requests/${resourceId(strictRequest.request_id)}/execute`,
    {
      method: "POST",
      body: JSON.stringify({
        grant_id: strictRequest.grant.grant_id,
        action_digest: strictRequest.action_digest,
      }),
    },
  );
  return parseCapabilityConsumeReceipt(value, strictRequest);
}

async function submitIPhoneCapabilityResultWith(
  send: CapabilityRequestFunction,
  capabilityRequest: CapabilityRequestEnvelope,
  result: CapabilityResult,
): Promise<CapabilityResultReceipt> {
  const strictResult = parseCapabilityResult(result, capabilityRequest.capability);
  const value = await send<unknown>(
    `/iphone/capabilities/requests/${resourceId(capabilityRequest.request_id)}/result`,
    {
      method: "POST",
      body: JSON.stringify({
        grant_id: capabilityRequest.grant.grant_id,
        action_digest: capabilityRequest.action_digest,
        result: strictResult,
      }),
    },
  );
  return parseCapabilityResultReceipt(value, capabilityRequest);
}

async function requireCapabilityConnection(): Promise<StoredConnection> {
  const connection = await resolveRequestConnection();
  if (!connection.token) {
    throw new ApiError(401, "Cet iPhone n’est pas jumelé au control plane.");
  }
  return { baseUrl: connection.baseUrl, token: connection.token };
}

function sameConnection(left: StoredConnection, right: StoredConnection): boolean {
  return left.baseUrl === right.baseUrl && left.token === right.token;
}

async function requestOnBoundCapabilityConnection<T>(
  connection: StoredConnection,
  path: string,
  init?: RequestInit,
): Promise<T> {
  const current = await requireCapabilityConnection();
  if (!sameConnection(current, connection)) {
    throw new CapabilityProtocolError(
      "La connexion jumelée a changé; la capacité iPhone a été bloquée avant envoi.",
    );
  }
  return requestAt<T>(connection.baseUrl, path, init, connection.token);
}

export async function createIPhoneCapabilityApiSession(): Promise<CapabilityTransportSession> {
  const connection = await requireCapabilityConnection();
  const send: CapabilityRequestFunction = (path, init) => (
    requestOnBoundCapabilityConnection(connection, path, init)
  );
  return {
    origin: connection.baseUrl,
    authorizeRequest: (capabilityRequest, decision) => (
      authorizeIPhoneCapabilityRequestWith(send, capabilityRequest, decision)
    ),
    consumeRequest: (capabilityRequest) => (
      consumeIPhoneCapabilityRequestWith(send, capabilityRequest)
    ),
    getRequest: (requestId) => getIPhoneCapabilityRequestWith(send, requestId),
    listRequests: () => listIPhoneCapabilityRequestsWith(send),
    submitResult: (capabilityRequest, result) => (
      submitIPhoneCapabilityResultWith(send, capabilityRequest, result)
    ),
  };
}

export async function listIPhoneCapabilityRequests(): Promise<CapabilityRequestPreview[]> {
  return listIPhoneCapabilityRequestsWith(request);
}

export async function getIPhoneCapabilityRequest(
  requestId: string,
): Promise<CapabilityRequestDetail> {
  return getIPhoneCapabilityRequestWith(request, requestId);
}

export async function authorizeIPhoneCapabilityRequest(
  capabilityRequest: CapabilityRequestDetail,
  decision: CapabilityAuthorizationDecision,
): Promise<CapabilityAuthorizationResponse> {
  return authorizeIPhoneCapabilityRequestWith(request, capabilityRequest, decision);
}

export async function consumeIPhoneCapabilityRequest(
  capabilityRequest: CapabilityRequestEnvelope,
): Promise<ConsumedCapabilityGrant> {
  return consumeIPhoneCapabilityRequestWith(request, capabilityRequest);
}

export async function submitIPhoneCapabilityResult(
  capabilityRequest: CapabilityRequestEnvelope,
  result: CapabilityResult,
): Promise<CapabilityResultReceipt> {
  return submitIPhoneCapabilityResultWith(request, capabilityRequest, result);
}

export function createFeedback(input: {
  task_id?: string;
  agent_id?: string;
  score?: number;
  notes?: string;
  label?: string;
}): Promise<{ id: string }> {
  return request<{ id: string }>("/feedback", {
    method: "POST",
    body: JSON.stringify(input),
  });
}
