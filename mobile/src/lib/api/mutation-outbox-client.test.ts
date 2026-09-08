import { beforeEach, describe, expect, it, jest } from "@jest/globals";
import { fetch } from "expo/fetch";
import * as SecureStore from "expo-secure-store";

import {
  createMutationOutboxApiSession,
  drainMutationOutbox,
} from "@/lib/api/client";
import { mutationOutbox, type MutationDelivery } from "@/lib/state/mutation-outbox";

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

const CONNECTION_KEY = "mongars.connection.v1";
const PENDING_CONNECTION_KEY = "mongars.connection.pending.v1";
const request = jest.mocked(fetch);
const getItem = jest.mocked(SecureStore.getItemAsync);
const mockDrain = jest.mocked(mutationOutbox.drain);

function storedConnection(baseUrl = "https://control.example", token = "device-token") {
  return JSON.stringify({ baseUrl, token });
}

function successfulJson(body: unknown) {
  return { ok: true, status: 200, json: async () => body } as never;
}

function delivery(
  overrides: Partial<MutationDelivery> = {},
): MutationDelivery {
  return {
    id: "mut_aaaaaaaaaaaaaaaaaaaa",
    origin: "https://control.example",
    operation: "feedback.create",
    resourceId: null,
    payload: { task_id: "tsk_1", score: 5, notes: "Useful" },
    idempotencyKey: "mut_feedback_12345678901234567890",
    createdAt: "2030-01-01T00:00:00.000Z",
    attempt: 1,
    ...overrides,
  };
}

describe("mutation outbox API binding", () => {
  let activeToken: string;

  beforeEach(() => {
    jest.clearAllMocks();
    activeToken = "device-token";
    getItem.mockImplementation(async (key: string) => {
      if (key === PENDING_CONNECTION_KEY) return null;
      if (key === CONNECTION_KEY) {
        return storedConnection("https://control.example", activeToken);
      }
      return null;
    });
  });

  it("maps a delivery to the authenticated idempotent sync endpoint", async () => {
    request.mockResolvedValueOnce(successfulJson({
      idempotent_replay: false,
      result: { id: "fb_1" },
    }));
    const session = await createMutationOutboxApiSession();

    await expect(session.send(delivery())).resolves.toEqual({
      idempotent_replay: false,
      result: { id: "fb_1" },
    });

    expect(session.origin).toBe("https://control.example");
    expect(JSON.stringify(session)).not.toContain("device-token");
    expect(request).toHaveBeenCalledWith(
      "https://control.example/sync/mutations",
      {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          Authorization: "Bearer device-token",
          "Idempotency-Key": "mut_feedback_12345678901234567890",
        },
        body: JSON.stringify({
          operation: "feedback.create",
          payload: { task_id: "tsk_1", score: 5, notes: "Useful" },
        }),
      },
    );
  });

  it("maps a resource-bound mutation without changing its payload", async () => {
    request.mockResolvedValueOnce(successfulJson({
      idempotent_replay: true,
      result: { id: "mem_1", pinned: true },
    }));
    const session = await createMutationOutboxApiSession();

    await session.send(delivery({
      operation: "memory.metadata.update",
      resourceId: "mem/1",
      payload: { pinned: true },
    }));

    expect(request).toHaveBeenCalledWith(
      "https://control.example/sync/mutations",
      expect.objectContaining({
        body: JSON.stringify({
          operation: "memory.metadata.update",
          resource_id: "mem/1",
          payload: { pinned: true },
        }),
      }),
    );
  });

  it("rejects another origin and a same-origin credential replacement before fetch", async () => {
    const session = await createMutationOutboxApiSession();

    await expect(session.send(delivery({ origin: "https://other.example" }))).rejects.toThrow(
      "autre control plane",
    );
    expect(request).not.toHaveBeenCalled();

    activeToken = "replacement-device-token";
    await expect(session.send(delivery())).rejects.toThrow("connexion jumelée a changé");
    expect(request).not.toHaveBeenCalled();
  });

  it("drains the active origin on reconnect through the bound sender", async () => {
    const expected = { attempted: 1, completed: 1, failed: 0, remaining: 0 };
    mockDrain.mockImplementationOnce(async (origin, sender, limit) => {
      expect(origin).toBe("https://control.example");
      expect(limit).toBe(12);
      request.mockResolvedValueOnce(successfulJson({
        idempotent_replay: true,
        result: { id: "fb_1" },
      }));
      await sender(delivery({ attempt: 2 }));
      return expected;
    });

    await expect(drainMutationOutbox(12)).resolves.toEqual(expected);

    expect(mockDrain).toHaveBeenCalledTimes(1);
    expect(request).toHaveBeenCalledWith(
      "https://control.example/sync/mutations",
      expect.objectContaining({
        headers: expect.objectContaining({
          Authorization: "Bearer device-token",
          "Idempotency-Key": "mut_feedback_12345678901234567890",
        }),
      }),
    );
  });

  it("keeps the row pending when the server response is invalid", async () => {
    request.mockResolvedValueOnce(successfulJson({ result: { id: "fb_1" } }));
    const session = await createMutationOutboxApiSession();

    await expect(session.send(delivery())).rejects.toThrow("reçu de mutation invalide");
    expect(mockDrain).not.toHaveBeenCalled();
  });
});
