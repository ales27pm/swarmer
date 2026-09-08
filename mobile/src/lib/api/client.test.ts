import { beforeEach, describe, expect, it, jest } from "@jest/globals";
import { fetch } from "expo/fetch";
import * as SecureStore from "expo-secure-store";

import {
  bootstrapSync,
  createEventStreamTicket,
  createTask,
  pairDevice,
  submitToolProposal,
  type Bootstrap,
} from "@/lib/api/client";
import { applyBootstrap } from "@/lib/state/replica";

jest.mock("expo-secure-store", () => ({
  deleteItemAsync: jest.fn(),
  getItemAsync: jest.fn(),
  setItemAsync: jest.fn(),
}));
jest.mock("expo/fetch", () => ({ fetch: jest.fn() }));
jest.mock("@/lib/state/replica", () => ({
  applyBootstrap: jest.fn(),
  upsertEvent: jest.fn(),
}));

const getItem = jest.mocked(SecureStore.getItemAsync);
const deleteItem = jest.mocked(SecureStore.deleteItemAsync);
const setItem = jest.mocked(SecureStore.setItemAsync);
const request = jest.mocked(fetch);
const mockApplyBootstrap = jest.mocked(applyBootstrap);

const CONNECTION_KEY = "mongars.connection.v1";
const PENDING_CONNECTION_KEY = "mongars.connection.pending.v1";
const PAIRING_ID = `pair_${"a".repeat(32)}`;
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

function candidateResponse(token = "new-device-token", deviceId = "iphone_test") {
  return {
    candidate_token: token,
    pairing_id: PAIRING_ID,
    device_id: deviceId,
    expires_in_seconds: 120,
  };
}

function readyResponse(deviceId = "iphone_test") {
  return {
    status: "ready",
    pairing_id: PAIRING_ID,
    device_id: deviceId,
    already_finalized: false,
  };
}

function storedConnection(baseUrl: string, token: string) {
  return JSON.stringify({ baseUrl, token });
}

function pendingConnection(baseUrl: string, token: string, deviceId = "iphone_test") {
  return JSON.stringify({ baseUrl, token, pairingId: PAIRING_ID, deviceId });
}

function mockConnections(values: Record<string, string | null>) {
  getItem.mockImplementation(async (key: string) => values[key] ?? null);
}

describe("control-plane connection storage", () => {
  beforeEach(() => {
    jest.clearAllMocks();
    getItem.mockResolvedValue(null);
    deleteItem.mockResolvedValue();
    setItem.mockResolvedValue();
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
      pendingConnection("https://new.example", "new-device-token"),
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
  });

  it("uses the durable pending origin for the replica when active storage fails", async () => {
    request
      .mockResolvedValueOnce(successfulJson(candidateResponse()))
      .mockResolvedValueOnce(successfulJson(verifiedBootstrap))
      .mockResolvedValueOnce(successfulJson(readyResponse()))
      .mockResolvedValueOnce(successfulJson(verifiedBootstrap));
    setItem
      .mockResolvedValueOnce()
      .mockRejectedValueOnce(new Error("active connection storage unavailable"));

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
});
