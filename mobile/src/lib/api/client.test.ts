import { beforeEach, describe, expect, it, jest } from "@jest/globals";
import { fetch } from "expo/fetch";
import * as SecureStore from "expo-secure-store";

import {
  authorizeIPhoneCapabilityRequest,
  bootstrapSync,
  consumeIPhoneCapabilityRequest,
  createIPhoneCapabilityApiSession,
  createEventStreamTicket,
  createTask,
  getIPhoneCapabilityRequest,
  listMessages,
  listIPhoneCapabilityRequests,
  pairDevice,
  submitIPhoneCapabilityResult,
  submitToolProposal,
  type Bootstrap,
} from "@/lib/api/client";
import type { CapabilityTransportSession } from "@/lib/iphone-capabilities/transport";
import type {
  CapabilityRequestDetail,
  CapabilityRequestEnvelope,
} from "@/lib/iphone-capabilities/types";
import { mutationOutbox } from "@/lib/state/mutation-outbox";
import { applyBootstrap } from "@/lib/state/replica";

type MailCapabilityDetail = Extract<
  CapabilityRequestDetail,
  { capability: "iphone.mail.compose" }
>;
type MailCapabilityEnvelope = Extract<
  CapabilityRequestEnvelope,
  { capability: "iphone.mail.compose" }
>;

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
const deleteItem = jest.mocked(SecureStore.deleteItemAsync);
const setItem = jest.mocked(SecureStore.setItemAsync);
const request = jest.mocked(fetch);
const mockAbandonPending = jest.mocked(mutationOutbox.abandonPending);
const mockApplyBootstrap = jest.mocked(applyBootstrap);

const CONNECTION_KEY = "mongars.connection.v1";
const PENDING_CONNECTION_KEY = "mongars.connection.pending.v1";
const PAIRING_ID = `pair_${"a".repeat(32)}`;
const CAPABILITY_REQUEST_ID = `iphreq_${"a".repeat(32)}`;
const CAPABILITY_GRANT_ID = `grt_${"c".repeat(64)}`;
const CAPABILITY_DIGEST = "sha256:928d3688233c2de096a0add2152d9747625307590618670406f332b0002a5461";
const CAPABILITY_CREATED_AT = new Date(Date.now() - 60_000).toISOString();
const CAPABILITY_EXPIRES_AT = new Date(Date.now() + 180_000).toISOString();
const verifiedBootstrap: Bootstrap = {
  server_time: "2026-09-05T12:00:00Z",
  tasks: [],
  approvals: [],
  tool_calls: [],
  conversations: [],
  agents: [],
  pinned_memory: [],
  counts: {
    tasks: 0,
    messages: 0,
    agents: 0,
    approvals_pending: 0,
    memory_items: 0,
    audit_events: 0,
  },
  cursor: "0",
};

function successfulJson(body: unknown, status = 200) {
  return {
    ok: true,
    status,
    json: async () => body,
  } as never;
}

function deferred<T>() {
  let reject!: (cause?: unknown) => void;
  let resolve!: (value: T | PromiseLike<T>) => void;
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, reject, resolve };
}

function candidateResponse(
  token = "new-device-token",
  deviceId = "iphone_test",
  pairingId = PAIRING_ID,
) {
  return {
    candidate_token: token,
    pairing_id: pairingId,
    device_id: deviceId,
    expires_in_seconds: 120,
  };
}

function readyResponse(deviceId = "iphone_test", pairingId = PAIRING_ID) {
  return {
    status: "ready",
    pairing_id: pairingId,
    device_id: deviceId,
    already_finalized: false,
  };
}

function storedConnection(baseUrl: string, token: string) {
  return JSON.stringify({ baseUrl, token });
}

function pendingConnection(
  baseUrl: string,
  token: string,
  deviceId = "iphone_test",
  replacedOrigins?: string[],
  pairingId = PAIRING_ID,
) {
  return JSON.stringify({
    baseUrl,
    token,
    pairingId,
    deviceId,
    ...(replacedOrigins?.length ? { replacedOrigins } : {}),
  });
}

function mutableConnectionStore(initial: Record<string, string | null>): Map<string, string> {
  const values = new Map(
    Object.entries(initial).filter(
      (entry): entry is [string, string] => entry[1] !== null,
    ),
  );
  getItem.mockImplementation(async (key: string) => values.get(key) ?? null);
  setItem.mockImplementation(async (key: string, value: string) => {
    values.set(key, value);
  });
  deleteItem.mockImplementation(async (key: string) => {
    values.delete(key);
  });
  return values;
}

function authenticatedRoute(endpoint: string, token: string): string {
  return `${endpoint}\nBearer ${token}`;
}

function mockRequestRoutes(entries: [string, () => unknown][]): void {
  const routes = new Map(entries);
  request.mockImplementation((url, init) => {
    const endpoint = String(url);
    const authorization = (init?.headers as Record<string, string> | undefined)?.Authorization;
    const handler = routes.get(`${endpoint}\n${authorization ?? ""}`) ?? routes.get(endpoint);
    if (!handler) throw new Error(`unexpected request: ${endpoint}`);
    return handler() as never;
  });
}

function capabilityDetail(): MailCapabilityDetail {
  return {
    schema_version: "0.9",
    request_id: CAPABILITY_REQUEST_ID,
    task_id: `tsk_${"b".repeat(32)}`,
    agent_id: "mail-worker",
    target_device_id: "iphone_test",
    capability: "iphone.mail.compose",
    status: "waiting_approval",
    created_at: CAPABILITY_CREATED_AT,
    expires_at: CAPABILITY_EXPIRES_AT,
    arguments: { recipients: ["a@example.com"], subject: "Hello", body: "Body" },
    action_digest: CAPABILITY_DIGEST,
    grant: null,
  };
}

function approvedCapability(): MailCapabilityEnvelope {
  const issuedAt = new Date(Date.now() - 1_000).toISOString();
  const expiresAt = new Date(Date.now() + 60_000).toISOString();
  return {
    ...capabilityDetail(),
    status: "approved",
    grant: {
      schema_version: "0.9",
      grant_id: CAPABILITY_GRANT_ID,
      request_id: CAPABILITY_REQUEST_ID,
      task_id: `tsk_${"b".repeat(32)}`,
      agent_id: "mail-worker",
      target_device_id: "iphone_test",
      approval_id: `icapr_${"d".repeat(32)}`,
      audit_id: 42,
      capability: "iphone.mail.compose",
      action_digest: CAPABILITY_DIGEST,
      issued_at: issuedAt,
      expires_at: expiresAt,
      use: "once",
    },
  };
}

function mockConnections(values: Record<string, string | null>) {
  mutableConnectionStore(values);
}

async function expectConnectionRaceBlocked(
  mutation: (session: CapabilityTransportSession) => Promise<unknown>,
  replacement = storedConnection("https://new.example", "new-device-token"),
): Promise<void> {
  const activeReadStarted = deferred<void>();
  const releaseActiveRead = deferred<string | null>();
  let captured = false;
  getItem.mockImplementation(async (key: string) => {
    if (key === PENDING_CONNECTION_KEY) return null;
    if (key !== CONNECTION_KEY) return null;
    if (!captured) {
      captured = true;
      return storedConnection("https://old.example", "old-device-token");
    }
    activeReadStarted.resolve();
    return releaseActiveRead.promise;
  });
  const session = await createIPhoneCapabilityApiSession();

  const pendingMutation = mutation(session);
  await activeReadStarted.promise;
  releaseActiveRead.resolve(replacement);

  await expect(pendingMutation).rejects.toThrow("bloquée avant envoi");
  expect(request).not.toHaveBeenCalled();
}

describe("control-plane connection storage", () => {
  beforeEach(() => {
    jest.clearAllMocks();
    mockConnections({});
    mockAbandonPending.mockResolvedValue(0);
  });

  it("rejects URLs that could conceal credentials before pairing", async () => {
    await expect(pairDevice("123456", "iphone", "iPhone", "https://user:secret@example.com")).rejects.toThrow(
      "ni identifiants",
    );
    expect(request).not.toHaveBeenCalled();
    expect(setItem).not.toHaveBeenCalled();
  });

  it("derives task ownership on the authenticated server", async () => {
    mockConnections({
      [CONNECTION_KEY]: storedConnection("https://control.example", "device-token"),
    });
    request.mockResolvedValue({
      ok: true,
      status: 201,
      json: async () => ({ id: "tsk_1" }),
    } as never);

    await createTask("inspect the workspace", "normal");

    expect(request).toHaveBeenCalledWith(
      "https://control.example/tasks",
      expect.objectContaining({
        body: JSON.stringify({ input: "inspect the workspace", mode: "normal" }),
        headers: expect.objectContaining({ Authorization: "Bearer device-token" }),
        method: "POST",
      }),
    );
  });

  it("submits a structured proposal only through the authenticated task route", async () => {
    mockConnections({
      [CONNECTION_KEY]: storedConnection("https://control.example", "device-token"),
    });
    request.mockResolvedValue(successfulJson({ id: "call_1" }));
    const proposal = {
      tool_name: "workspace.read_text",
      arguments: { path: "README.md" },
      summary: "Read the project overview",
    } as const;

    await submitToolProposal("tsk/one", proposal);

    expect(request).toHaveBeenCalledWith(
      "https://control.example/tasks/tsk%2Fone/tool-calls",
      expect.objectContaining({
        body: JSON.stringify({ ...proposal, planner_source: "iphone_local" }),
        headers: expect.objectContaining({ Authorization: "Bearer device-token" }),
        method: "POST",
      }),
    );
  });

  it("re-pairs an origin without disclosing the previous bearer token", async () => {
    mockConnections({
      [CONNECTION_KEY]: storedConnection("https://new.example", "old-device-token"),
    });
    request
      .mockResolvedValueOnce(successfulJson(candidateResponse()))
      .mockResolvedValueOnce(successfulJson(verifiedBootstrap))
      .mockResolvedValueOnce(successfulJson(readyResponse()))
      .mockResolvedValueOnce(successfulJson(verifiedBootstrap));

    await expect(pairDevice("123456", "iphone_test", "Test iPhone")).resolves.toEqual({
      bootstrap: verifiedBootstrap,
      serverUrl: "https://new.example",
    });

    expect(request).toHaveBeenCalledWith(
      "https://new.example/pairing/complete",
      expect.objectContaining({
        body: JSON.stringify({
          code: "123456",
          device_id: "iphone_test",
          name: "Test iPhone",
        }),
        headers: { "Content-Type": "application/json" },
        method: "POST",
      }),
    );
    expect(request).toHaveBeenNthCalledWith(
      2,
      "https://new.example/sync/bootstrap",
      expect.objectContaining({
        headers: { Authorization: "Bearer new-device-token" },
      }),
    );
    expect(request).toHaveBeenNthCalledWith(
      3,
      "https://new.example/pairing/finalize",
      expect.objectContaining({
        body: JSON.stringify({ pairing_id: PAIRING_ID, device_id: "iphone_test" }),
        headers: expect.objectContaining({ Authorization: "Bearer new-device-token" }),
        method: "POST",
      }),
    );
    expect(request).toHaveBeenNthCalledWith(
      4,
      "https://new.example/sync/bootstrap",
      expect.objectContaining({
        headers: { Authorization: "Bearer new-device-token" },
      }),
    );
    expect(setItem).toHaveBeenCalledTimes(2);
    expect(setItem).toHaveBeenNthCalledWith(
      1,
      PENDING_CONNECTION_KEY,
      pendingConnection(
        "https://new.example",
        "new-device-token",
        "iphone_test",
        ["https://new.example"],
      ),
    );
    expect(setItem).toHaveBeenCalledWith(
      CONNECTION_KEY,
      storedConnection("https://new.example", "new-device-token"),
    );
    expect(deleteItem).toHaveBeenCalledWith(PENDING_CONNECTION_KEY);
    expect(deleteItem).toHaveBeenCalledWith("mongars.server_url");
    expect(deleteItem).toHaveBeenCalledWith("mongars.device_token");
    expect(mockApplyBootstrap).toHaveBeenCalledWith(
      verifiedBootstrap,
      "https://new.example",
    );
    expect(mockAbandonPending).toHaveBeenCalledWith("https://new.example");
    expect(request.mock.invocationCallOrder[3]).toBeLessThan(
      mockAbandonPending.mock.invocationCallOrder[0],
    );
    expect(mockAbandonPending.mock.invocationCallOrder[0]).toBeLessThan(
      setItem.mock.invocationCallOrder[1],
    );
  });

  it("abandons only the prior origin after a changed-origin pairing is fully ready", async () => {
    mockConnections({
      [CONNECTION_KEY]: storedConnection("https://old.example", "old-device-token"),
    });
    request
      .mockResolvedValueOnce(successfulJson(candidateResponse()))
      .mockResolvedValueOnce(successfulJson(verifiedBootstrap))
      .mockResolvedValueOnce(successfulJson(readyResponse()))
      .mockResolvedValueOnce(successfulJson(verifiedBootstrap));

    await pairDevice("123456", "iphone_test", "Test iPhone", "https://new.example");

    expect(mockAbandonPending).toHaveBeenCalledTimes(1);
    expect(mockAbandonPending).toHaveBeenCalledWith("https://old.example");
    expect(mockAbandonPending).not.toHaveBeenCalledWith("https://new.example");
    expect(setItem).toHaveBeenCalledWith(
      PENDING_CONNECTION_KEY,
      pendingConnection(
        "https://new.example",
        "new-device-token",
        "iphone_test",
        ["https://old.example"],
      ),
    );
  });

  it("keeps the working origin and token when staged pairing fails", async () => {
    request.mockResolvedValue({
      ok: false,
      status: 400,
      json: async () => ({ detail: "invalid or expired pairing code" }),
    } as never);

    await expect(
      pairDevice("000000", "iphone_test", "Test iPhone", "https://new.example"),
    ).rejects.toThrow("invalid or expired pairing code");

    expect(deleteItem).not.toHaveBeenCalled();
    expect(setItem).not.toHaveBeenCalled();
  });

  it("rejects a malformed successful pairing response before changing the connection", async () => {
    request.mockResolvedValueOnce(successfulJson({ candidate_token: "" }));

    await expect(
      pairDevice("123456", "iphone_test", "Test iPhone", "https://new.example"),
    ).rejects.toThrow("n’a pas retourné de candidat valide");

    expect(request).toHaveBeenCalledTimes(1);
    expect(setItem).not.toHaveBeenCalled();
    expect(deleteItem).not.toHaveBeenCalled();
    expect(mockAbandonPending).not.toHaveBeenCalled();
  });

  it("keeps the prior connection when the new bearer fails authenticated bootstrap", async () => {
    request
      .mockResolvedValueOnce(successfulJson(candidateResponse()))
      .mockResolvedValueOnce({
        ok: false,
        status: 401,
        json: async () => ({ detail: "invalid device token" }),
      } as never);

    await expect(
      pairDevice("123456", "iphone_test", "Test iPhone", "https://new.example"),
    ).rejects.toThrow("invalid device token");

    expect(request).toHaveBeenNthCalledWith(
      2,
      "https://new.example/sync/bootstrap",
      expect.objectContaining({ headers: { Authorization: "Bearer new-device-token" } }),
    );
    expect(setItem).not.toHaveBeenCalled();
    expect(deleteItem).not.toHaveBeenCalled();
  });

  it("does not commit a connection for a malformed authenticated bootstrap", async () => {
    request
      .mockResolvedValueOnce(successfulJson(candidateResponse()))
      .mockResolvedValueOnce(successfulJson({ cursor: "0" }));

    await expect(
      pairDevice("123456", "iphone_test", "Test iPhone", "https://new.example"),
    ).rejects.toThrow("bootstrap authentifié valide");

    expect(setItem).not.toHaveBeenCalled();
    expect(deleteItem).not.toHaveBeenCalled();
  });

  it("rejects a finalization response not bound to the staged device", async () => {
    request
      .mockResolvedValueOnce(successfulJson(candidateResponse()))
      .mockResolvedValueOnce(successfulJson(verifiedBootstrap))
      .mockResolvedValueOnce(
        successfulJson({ ...readyResponse(), device_id: "different-device" }),
      );

    await expect(
      pairDevice("123456", "iphone_test", "Test iPhone", "https://new.example"),
    ).rejects.toThrow("n’a pas confirmé l’activation liée");

    expect(setItem).not.toHaveBeenCalled();
    expect(deleteItem).not.toHaveBeenCalled();
  });

  it("rejects remote plaintext pairing before fetch", async () => {
    await expect(
      pairDevice("123456", "iphone_test", "Test iPhone", "http://control.example"),
    ).rejects.toThrow("HTTPS est obligatoire");

    expect(request).not.toHaveBeenCalled();
    expect(setItem).not.toHaveBeenCalled();
  });

  it("rejects a stored remote plaintext connection before sending its bearer", async () => {
    mockConnections({
      [CONNECTION_KEY]: storedConnection("http://control.example", "must-not-leak"),
    });

    await expect(createTask("inspect", "normal")).rejects.toThrow(
      "connexion sécurisée enregistrée est invalide",
    );

    expect(request).not.toHaveBeenCalled();
  });

  it("fails closed instead of recombining a legacy origin and bearer", async () => {
    mockConnections({
      "mongars.server_url": "https://new.example",
      "mongars.device_token": "old-device-token",
    });

    await expect(createTask("inspect", "normal")).rejects.toThrow(
      "ancienne connexion non liée",
    );

    expect(request).not.toHaveBeenCalled();
  });

  it.each([
    JSON.stringify({
      baseUrl: "https://new.example",
      token: "pending-device-token",
      deviceId: "",
      pairingId: PAIRING_ID,
    }),
    pendingConnection(
      "https://new.example",
      "pending-device-token",
      "iphone_test",
      ["https://old.example", "https://old.example"],
    ),
    JSON.stringify({
      baseUrl: "https://new.example",
      token: "pending-device-token",
      deviceId: "iphone_test",
      pairingId: PAIRING_ID,
      replacedOrigins: ["https://old.example", 42],
    }),
  ])("discards malformed pending connection state before using the active bearer", async (pending) => {
    mockConnections({
      [PENDING_CONNECTION_KEY]: pending,
      [CONNECTION_KEY]: storedConnection("https://old.example", "active-device-token"),
    });
    request.mockResolvedValueOnce(successfulJson({ id: "tsk_active" }, 201));

    await createTask("use active connection", "normal");

    expect(deleteItem).toHaveBeenCalledWith(PENDING_CONNECTION_KEY);
    expect(request).toHaveBeenCalledWith(
      "https://old.example/tasks",
      expect.objectContaining({
        headers: expect.objectContaining({ Authorization: "Bearer active-device-token" }),
      }),
    );
  });

  it("keeps the prior atomic connection if committing a new pairing fails", async () => {
    request
      .mockResolvedValueOnce(successfulJson(candidateResponse()))
      .mockResolvedValueOnce(successfulJson(verifiedBootstrap))
      .mockResolvedValueOnce(successfulJson(readyResponse()));
    setItem.mockRejectedValueOnce(new Error("secure store unavailable"));

    await expect(
      pairDevice("123456", "iphone_test", "Test iPhone", "https://new.example"),
    ).rejects.toThrow("secure store unavailable");

    expect(deleteItem).not.toHaveBeenCalled();
    expect(setItem).not.toHaveBeenCalledWith(CONNECTION_KEY, expect.any(String));
    expect(request).toHaveBeenCalledTimes(3);
  });

  it("keeps a durable pending bearer when the activation response is lost", async () => {
    request
      .mockResolvedValueOnce(successfulJson(candidateResponse()))
      .mockResolvedValueOnce(successfulJson(verifiedBootstrap))
      .mockResolvedValueOnce(successfulJson(readyResponse()))
      .mockRejectedValueOnce(new Error("network interrupted"));

    await expect(
      pairDevice("123456", "iphone_test", "Test iPhone", "https://new.example"),
    ).rejects.toThrow("network interrupted");

    expect(setItem).toHaveBeenCalledWith(
      PENDING_CONNECTION_KEY,
      pendingConnection("https://new.example", "new-device-token"),
    );
    expect(setItem).not.toHaveBeenCalledWith(CONNECTION_KEY, expect.any(String));
    expect(deleteItem).not.toHaveBeenCalledWith(PENDING_CONNECTION_KEY);
    expect(mockAbandonPending).not.toHaveBeenCalled();
  });

  it("uses the durable pending origin for the replica when active storage fails", async () => {
    const store = mutableConnectionStore({});
    request
      .mockResolvedValueOnce(successfulJson(candidateResponse()))
      .mockResolvedValueOnce(successfulJson(verifiedBootstrap))
      .mockResolvedValueOnce(successfulJson(readyResponse()))
      .mockResolvedValueOnce(successfulJson(verifiedBootstrap));
    setItem.mockImplementation(async (key: string, value: string) => {
      if (key === CONNECTION_KEY) {
        throw new Error("active connection storage unavailable");
      }
      store.set(key, value);
    });

    await expect(
      pairDevice("123456", "iphone_test", "Test iPhone", "https://new.example"),
    ).resolves.toEqual({ bootstrap: verifiedBootstrap, serverUrl: "https://new.example" });

    expect(mockApplyBootstrap).toHaveBeenCalledWith(
      verifiedBootstrap,
      "https://new.example",
    );
    expect(deleteItem).not.toHaveBeenCalledWith(PENDING_CONNECTION_KEY);
  });

  it("promotes a durable pending bearer after a lost activation response", async () => {
    mockConnections({
      [PENDING_CONNECTION_KEY]: pendingConnection(
        "https://new.example",
        "new-device-token",
        "iphone_test",
        ["https://old.example"],
      ),
      [CONNECTION_KEY]: storedConnection("https://old.example", "old-device-token"),
    });
    request
      .mockResolvedValueOnce(successfulJson(verifiedBootstrap))
      .mockResolvedValueOnce(successfulJson({ id: "tsk_recovered" }, 201));

    await createTask("resume pending cutover", "normal");

    expect(request).toHaveBeenNthCalledWith(
      1,
      "https://new.example/sync/bootstrap",
      expect.objectContaining({
        headers: expect.objectContaining({ Authorization: "Bearer new-device-token" }),
      }),
    );
    expect(request).toHaveBeenNthCalledWith(
      2,
      "https://new.example/tasks",
      expect.objectContaining({
        headers: expect.objectContaining({ Authorization: "Bearer new-device-token" }),
      }),
    );
    expect(setItem).toHaveBeenCalledWith(
      CONNECTION_KEY,
      storedConnection("https://new.example", "new-device-token"),
    );
    expect(mockAbandonPending).toHaveBeenCalledWith("https://old.example");
    expect(mockAbandonPending.mock.invocationCallOrder[0]).toBeLessThan(
      setItem.mock.invocationCallOrder[0],
    );
    expect(mockAbandonPending.mock.invocationCallOrder[0]).toBeLessThan(
      request.mock.invocationCallOrder[1],
    );
    expect(deleteItem).toHaveBeenCalledWith(PENDING_CONNECTION_KEY);
  });

  it("selects the preserved active connection before sending a mutation when a candidate expires", async () => {
    mockConnections({
      [PENDING_CONNECTION_KEY]: pendingConnection(
        "https://new.example",
        "expired-device-token",
      ),
      [CONNECTION_KEY]: storedConnection("https://old.example", "old-device-token"),
    });
    request
      .mockResolvedValueOnce({
        ok: false,
        status: 401,
        json: async () => ({ detail: "invalid device token" }),
      } as never)
      .mockResolvedValueOnce(successfulJson({ id: "tsk_old" }, 201));

    await createTask("use preserved connection", "normal");

    expect(request).toHaveBeenNthCalledWith(
      1,
      "https://new.example/sync/bootstrap",
      expect.objectContaining({
        headers: expect.objectContaining({ Authorization: "Bearer expired-device-token" }),
      }),
    );
    expect(request).toHaveBeenNthCalledWith(
      2,
      "https://old.example/tasks",
      expect.objectContaining({
        headers: expect.objectContaining({ Authorization: "Bearer old-device-token" }),
      }),
    );
    expect(
      request.mock.calls.filter(([url, init]) =>
        url === "https://new.example/tasks" && init?.method === "POST"),
    ).toHaveLength(0);
    expect(deleteItem).toHaveBeenCalledWith(PENDING_CONNECTION_KEY);
  });

  it("does not let an older successful recovery overwrite a newer completed pairing", async () => {
    const pairingA = `pair_${"b".repeat(32)}`;
    const pairingB = `pair_${"c".repeat(32)}`;
    const tokenA = "pending-device-token-a";
    const tokenB = "pending-device-token-b";
    const store = mutableConnectionStore({
      [PENDING_CONNECTION_KEY]: pendingConnection(
        "https://a.example",
        tokenA,
        "iphone_test",
        ["https://old.example"],
        pairingA,
      ),
      [CONNECTION_KEY]: storedConnection("https://old.example", "old-device-token"),
    });
    const recoveryStarted = deferred<void>();
    const recoveryResponse = deferred<never>();
    mockRequestRoutes([
      [authenticatedRoute("https://a.example/sync/bootstrap", tokenA), () => {
        recoveryStarted.resolve();
        return recoveryResponse.promise;
      }],
      ["https://b.example/pairing/complete", () => (
        successfulJson(candidateResponse(tokenB, "iphone_test", pairingB))
      )],
      ["https://b.example/pairing/finalize", () => (
        successfulJson(readyResponse("iphone_test", pairingB))
      )],
      [authenticatedRoute("https://b.example/sync/bootstrap", tokenB), () => (
        successfulJson(verifiedBootstrap)
      )],
      [authenticatedRoute("https://a.example/tasks", tokenA), () => (
        successfulJson({ id: "tsk_race" }, 201)
      )],
    ]);

    const staleRecovery = createTask("must not use pairing A", "normal");
    await recoveryStarted.promise;
    await pairDevice("123456", "iphone_test", "Test iPhone", "https://b.example");

    recoveryResponse.resolve(successfulJson(verifiedBootstrap));
    await expect(staleRecovery).rejects.toThrow("connexion jumelée a changé");

    expect(store.get(CONNECTION_KEY)).toBe(storedConnection("https://b.example", tokenB));
    expect(store.has(PENDING_CONNECTION_KEY)).toBe(false);
    expect(
      request.mock.calls.filter(([url]) => String(url) === "https://a.example/tasks"),
    ).toHaveLength(0);
  });

  it("does not let an older pairing attempt stage after a newer pairing commits", async () => {
    const pairingA = `pair_${"f".repeat(32)}`;
    const pairingB = `pair_${"1".repeat(32)}`;
    const tokenA = "candidate-device-token-a";
    const tokenB = "candidate-device-token-b";
    const store = mutableConnectionStore({
      [CONNECTION_KEY]: storedConnection("https://old.example", "old-device-token"),
    });
    const finalizationAStarted = deferred<void>();
    const finalizationAResponse = deferred<never>();
    mockRequestRoutes([
      ["https://a.example/pairing/complete", () => (
        successfulJson(candidateResponse(tokenA, "iphone_test", pairingA))
      )],
      ["https://a.example/pairing/finalize", () => {
        finalizationAStarted.resolve();
        return finalizationAResponse.promise;
      }],
      [authenticatedRoute("https://a.example/sync/bootstrap", tokenA), () => (
        successfulJson(verifiedBootstrap)
      )],
      ["https://b.example/pairing/complete", () => (
        successfulJson(candidateResponse(tokenB, "iphone_test", pairingB))
      )],
      ["https://b.example/pairing/finalize", () => (
        successfulJson(readyResponse("iphone_test", pairingB))
      )],
      [authenticatedRoute("https://b.example/sync/bootstrap", tokenB), () => (
        successfulJson(verifiedBootstrap)
      )],
    ]);

    const olderPairing = pairDevice(
      "111111",
      "iphone_test",
      "Test iPhone",
      "https://a.example",
    );
    await finalizationAStarted.promise;
    await pairDevice("222222", "iphone_test", "Test iPhone", "https://b.example");

    finalizationAResponse.resolve(successfulJson(readyResponse("iphone_test", pairingA)));
    await expect(olderPairing).rejects.toThrow("connexion jumelée a changé");
    expect(store.get(CONNECTION_KEY)).toBe(storedConnection("https://b.example", tokenB));
    expect(store.has(PENDING_CONNECTION_KEY)).toBe(false);
    expect(
      setItem.mock.calls.filter(([, value]) => value.includes(tokenA)),
    ).toHaveLength(0);
  });

  it("does not let an older 401 recovery delete a newer pending pairing", async () => {
    const pairingA = `pair_${"d".repeat(32)}`;
    const pairingB = `pair_${"e".repeat(32)}`;
    const tokenA = "expired-device-token-a";
    const tokenB = "pending-device-token-b";
    const store = mutableConnectionStore({
      [PENDING_CONNECTION_KEY]: pendingConnection(
        "https://a.example",
        tokenA,
        "iphone_test",
        ["https://old.example"],
        pairingA,
      ),
      [CONNECTION_KEY]: storedConnection("https://old.example", "old-device-token"),
    });
    const recoveryStarted = deferred<void>();
    const recoveryResponse = deferred<never>();
    const cutoverStarted = deferred<void>();
    const cutoverResponse = deferred<never>();
    let bootstrapBCalls = 0;
    mockRequestRoutes([
      [authenticatedRoute("https://a.example/sync/bootstrap", tokenA), () => {
        recoveryStarted.resolve();
        return recoveryResponse.promise;
      }],
      ["https://b.example/pairing/complete", () => (
        successfulJson(candidateResponse(tokenB, "iphone_test", pairingB))
      )],
      ["https://b.example/pairing/finalize", () => (
        successfulJson(readyResponse("iphone_test", pairingB))
      )],
      [authenticatedRoute("https://b.example/sync/bootstrap", tokenB), () => {
        bootstrapBCalls += 1;
        if (bootstrapBCalls === 1) return successfulJson(verifiedBootstrap);
        cutoverStarted.resolve();
        return cutoverResponse.promise;
      }],
      [authenticatedRoute("https://old.example/tasks", "old-device-token"), () => (
        successfulJson({ id: "tsk_wrong_origin" }, 201)
      )],
    ]);

    const staleRecovery = createTask("must not fall back during re-pair", "normal");
    await recoveryStarted.promise;
    const newerPairing = pairDevice(
      "123456",
      "iphone_test",
      "Test iPhone",
      "https://b.example",
    );
    await cutoverStarted.promise;

    recoveryResponse.resolve({
      ok: false,
      status: 401,
      json: async () => ({ detail: "invalid device token" }),
    } as never);
    await expect(staleRecovery).rejects.toThrow("connexion jumelée a changé");
    expect(store.get(PENDING_CONNECTION_KEY)).toBe(
      pendingConnection(
        "https://b.example",
        tokenB,
        "iphone_test",
        ["https://a.example", "https://old.example"],
        pairingB,
      ),
    );
    expect(
      request.mock.calls.filter(([url]) => String(url).endsWith("/tasks")),
    ).toHaveLength(0);

    cutoverResponse.resolve(successfulJson(verifiedBootstrap));
    await expect(newerPairing).resolves.toEqual({
      bootstrap: verifiedBootstrap,
      serverUrl: "https://b.example",
    });
    expect(store.get(CONNECTION_KEY)).toBe(storedConnection("https://b.example", tokenB));
  });

  it("exchanges the bearer for a short websocket ticket without putting it in the URL", async () => {
    mockConnections({
      [CONNECTION_KEY]: storedConnection("https://control.example", "long-lived-device-token"),
    });
    const ticket = "short-lived-ticket-with-enough-entropy";
    request.mockResolvedValue(successfulJson({ ticket, expires_in_seconds: 30 }));

    await expect(createEventStreamTicket()).resolves.toEqual({
      expiresInSeconds: 30,
      serverUrl: "https://control.example",
      url: `wss://control.example/ws?ticket=${ticket}`,
    });

    expect(request).toHaveBeenCalledWith(
      "https://control.example/ws/ticket",
      expect.objectContaining({
        headers: expect.objectContaining({ Authorization: "Bearer long-lived-device-token" }),
        method: "POST",
      }),
    );
    expect(String(request.mock.calls[0][0])).not.toContain("long-lived-device-token");
  });

  it("refuses to create a websocket ticket while the device is not paired", async () => {
    mockConnections({});

    await expect(createEventStreamTicket()).rejects.toThrow("jumelé");
    expect(request).not.toHaveBeenCalled();
  });
});

describe("bootstrap replica commit ordering", () => {
  const activeConnection = storedConnection(
    "https://control.example",
    "device-token",
  );

  beforeEach(() => {
    jest.clearAllMocks();
    mockConnections({ [CONNECTION_KEY]: activeConnection });
    mockApplyBootstrap.mockResolvedValue();
  });

  it("does not apply an older response after a newer bootstrap begins", async () => {
    const olderBootstrap = { ...verifiedBootstrap, cursor: "older" };
    const newerBootstrap = { ...verifiedBootstrap, cursor: "newer" };
    const olderResponse = deferred<never>();
    const olderRequestStarted = deferred<void>();
    request.mockImplementationOnce(() => {
      olderRequestStarted.resolve();
      return olderResponse.promise;
    });

    const olderSync = bootstrapSync();
    await olderRequestStarted.promise;

    request.mockResolvedValueOnce(successfulJson(newerBootstrap));
    await expect(bootstrapSync()).resolves.toEqual(newerBootstrap);

    olderResponse.resolve(successfulJson(olderBootstrap));
    await expect(olderSync).resolves.toEqual(olderBootstrap);

    expect(mockApplyBootstrap).toHaveBeenCalledTimes(1);
    expect(mockApplyBootstrap).toHaveBeenCalledWith(
      newerBootstrap,
      "https://control.example",
    );
  });

  it("returns a blurred caller's response without committing it to the replica", async () => {
    const response = deferred<never>();
    const requestStarted = deferred<void>();
    let isCurrent = true;
    request.mockImplementationOnce(() => {
      requestStarted.resolve();
      return response.promise;
    });

    const sync = bootstrapSync(() => isCurrent);
    await requestStarted.promise;
    isCurrent = false;
    response.resolve(successfulJson(verifiedBootstrap));

    await expect(sync).resolves.toEqual(verifiedBootstrap);
    expect(mockApplyBootstrap).not.toHaveBeenCalled();
  });

  it("serializes replica commits so a newer response finishes last", async () => {
    const olderBootstrap = { ...verifiedBootstrap, cursor: "older-applying" };
    const newerBootstrap = { ...verifiedBootstrap, cursor: "newer-waiting" };
    const releaseOlderApply = deferred<void>();
    const olderApplyStarted = deferred<void>();
    const newerBodyRead = deferred<void>();
    let newerApplyStarted = false;
    request
      .mockResolvedValueOnce(successfulJson(olderBootstrap))
      .mockResolvedValueOnce({
        ok: true,
        status: 200,
        json: async () => {
          newerBodyRead.resolve();
          return newerBootstrap;
        },
      } as never);
    mockApplyBootstrap
      .mockImplementationOnce(async () => {
        olderApplyStarted.resolve();
        await releaseOlderApply.promise;
      })
      .mockImplementationOnce(async () => {
        newerApplyStarted = true;
      });

    const olderSync = bootstrapSync();
    await olderApplyStarted.promise;
    const newerSync = bootstrapSync();
    await newerBodyRead.promise;
    await new Promise<void>((resolve) => setTimeout(resolve, 0));

    expect(newerApplyStarted).toBe(false);
    expect(mockApplyBootstrap).toHaveBeenCalledTimes(1);

    releaseOlderApply.resolve();
    await expect(Promise.all([olderSync, newerSync])).resolves.toEqual([
      olderBootstrap,
      newerBootstrap,
    ]);
    expect(mockApplyBootstrap.mock.calls).toEqual([
      [olderBootstrap, "https://control.example"],
      [newerBootstrap, "https://control.example"],
    ]);
  });

  it("still rejects the current sync when its replica commit fails", async () => {
    const replicaError = new Error("replica unavailable");
    request.mockResolvedValueOnce(successfulJson(verifiedBootstrap));
    mockApplyBootstrap.mockRejectedValueOnce(replicaError);

    await expect(bootstrapSync()).rejects.toBe(replicaError);

    const recoveredBootstrap = { ...verifiedBootstrap, cursor: "recovered" };
    request.mockResolvedValueOnce(successfulJson(recoveredBootstrap));
    await expect(bootstrapSync()).resolves.toEqual(recoveredBootstrap);
    expect(mockApplyBootstrap).toHaveBeenLastCalledWith(
      recoveredBootstrap,
      "https://control.example",
    );
  });

  it("uses the strict authorize, consume, and result protocol without persisting the raw grant", async () => {
    mockConnections({
      [CONNECTION_KEY]: storedConnection("https://control.example", "device-token"),
    });
    const approved = approvedCapability();
    const result = {
      name: "iphone.mail.compose" as const,
      status: "completed" as const,
      value: { composed: true as const },
    };
    request
      .mockResolvedValueOnce(successfulJson(capabilityDetail()))
      .mockResolvedValueOnce(successfulJson(approved))
      .mockResolvedValueOnce(successfulJson({
        status: "consumed",
        request_id: CAPABILITY_REQUEST_ID,
        grant_id: CAPABILITY_GRANT_ID,
        action_digest: CAPABILITY_DIGEST,
        consumed_at: new Date().toISOString(),
      }))
      .mockResolvedValueOnce(successfulJson({
        status: "accepted",
        request_id: CAPABILITY_REQUEST_ID,
        grant_id: CAPABILITY_GRANT_ID,
      }));

    const detail = await getIPhoneCapabilityRequest(CAPABILITY_REQUEST_ID);
    const authorization = await authorizeIPhoneCapabilityRequest(detail, "approve");
    if (authorization.status !== "approved" || authorization.grant === null) {
      throw new Error("expected approved test grant");
    }
    await consumeIPhoneCapabilityRequest(authorization);
    await submitIPhoneCapabilityResult(authorization, result);

    expect(request).toHaveBeenNthCalledWith(
      2,
      `https://control.example/iphone/capabilities/requests/${CAPABILITY_REQUEST_ID}/authorize`,
      expect.objectContaining({
        body: JSON.stringify({ decision: "approve" }),
        headers: expect.objectContaining({ Authorization: "Bearer device-token" }),
        method: "POST",
      }),
    );
    expect(request).toHaveBeenNthCalledWith(
      3,
      `https://control.example/iphone/capabilities/requests/${CAPABILITY_REQUEST_ID}/execute`,
      expect.objectContaining({
        body: JSON.stringify({ grant_id: CAPABILITY_GRANT_ID, action_digest: CAPABILITY_DIGEST }),
        method: "POST",
      }),
    );
    expect(request).toHaveBeenNthCalledWith(
      4,
      `https://control.example/iphone/capabilities/requests/${CAPABILITY_REQUEST_ID}/result`,
      expect.objectContaining({
        body: JSON.stringify({
          grant_id: CAPABILITY_GRANT_ID,
          action_digest: CAPABILITY_DIGEST,
          result,
        }),
        method: "POST",
      }),
    );
    expect(setItem).not.toHaveBeenCalled();
  });

  it("allows only repeat approval for an approved request whose grant response was lost", async () => {
    mockConnections({
      [CONNECTION_KEY]: storedConnection("https://control.example", "device-token"),
    });
    const approvedWithoutGrant = {
      ...capabilityDetail(),
      status: "approved" as const,
      grant: null,
    };
    const recovered = approvedCapability();
    request.mockResolvedValueOnce(successfulJson(recovered));

    await expect(
      authorizeIPhoneCapabilityRequest(approvedWithoutGrant, "approve"),
    ).resolves.toEqual(recovered);
    await expect(
      authorizeIPhoneCapabilityRequest(approvedWithoutGrant, "deny"),
    ).rejects.toThrow("grant-recovery");
    expect(request).toHaveBeenCalledTimes(1);
  });

  it.each([
    [
      "authorization",
      (session: CapabilityTransportSession) => (
        session.authorizeRequest(capabilityDetail(), "approve")
      ),
    ],
    [
      "grant consumption",
      (session: CapabilityTransportSession) => session.consumeRequest(approvedCapability()),
    ],
    [
      "native result delivery",
      (session: CapabilityTransportSession) => session.submitResult(approvedCapability(), {
        name: "iphone.mail.compose",
        status: "completed",
        value: { composed: true },
      }),
    ],
  ])("blocks %s before fetch when the paired origin changes during the pre-send check", async (_label, mutation) => {
    await expectConnectionRaceBlocked(mutation);
  });

  it("blocks a same-origin device-token replacement before authorization fetch", async () => {
    await expectConnectionRaceBlocked(
      (session) => session.authorizeRequest(capabilityDetail(), "approve"),
      storedConnection("https://old.example", "different-device-token"),
    );
  });

  it("rejects a list response that exposes arguments or a grant", async () => {
    mockConnections({
      [CONNECTION_KEY]: storedConnection("https://control.example", "device-token"),
    });
    request.mockResolvedValueOnce(successfulJson([capabilityDetail()]));

    await expect(listIPhoneCapabilityRequests()).rejects.toThrow("invalid shape");
  });
});

describe("conversation read fencing", () => {
  beforeEach(() => {
    jest.clearAllMocks();
  });

  it("rejects a message response when the paired connection changes in flight", async () => {
    const response = deferred<never>();
    const requestStarted = deferred<void>();
    let activeConnection = storedConnection("https://old.example", "old-device-token");
    mockConnections({ [CONNECTION_KEY]: activeConnection });
    getItem.mockImplementation(async (key: string) => {
      if (key === PENDING_CONNECTION_KEY) return null;
      if (key === CONNECTION_KEY) return activeConnection;
      return null;
    });
    request.mockImplementationOnce(() => {
      requestStarted.resolve();
      return response.promise;
    });

    const pending = listMessages("conv_test");
    await requestStarted.promise;
    activeConnection = storedConnection("https://new.example", "new-device-token");
    response.resolve(successfulJson([]));

    await expect(pending).rejects.toThrow("connexion jumelée a changé");
  });

  it("rejects a message response after its caller is fenced", async () => {
    const response = deferred<never>();
    const requestStarted = deferred<void>();
    let isCurrent = true;
    mockConnections({
      [CONNECTION_KEY]: storedConnection("https://control.example", "device-token"),
    });
    request.mockImplementationOnce(() => {
      requestStarted.resolve();
      return response.promise;
    });

    const pending = listMessages("conv_test", () => isCurrent);
    await requestStarted.promise;
    isCurrent = false;
    response.resolve(successfulJson([]));

    await expect(pending).rejects.toThrow("connexion jumelée a changé");
  });
});
