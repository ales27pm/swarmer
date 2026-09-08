import { describe, expect, it, jest } from "@jest/globals";

import { createLiveSyncController } from "@/lib/sync/live-sync";

jest.mock("@/lib/api/client", () => ({ createEventStreamTicket: jest.fn() }));
jest.mock("@/lib/state/replica", () => ({ upsertEvent: jest.fn() }));

type Handler<T> = ((event: T) => void) | null;

class FakeSocket {
  readyState = 0;
  onclose: Handler<unknown> = null;
  onerror: Handler<unknown> = null;
  onmessage: Handler<{ data: unknown }> = null;
  onopen: Handler<unknown> = null;
  sent: string[] = [];

  close() {
    this.readyState = 3;
  }

  open() {
    this.readyState = 1;
    this.onopen?.({});
  }

  receive(value: unknown) {
    this.onmessage?.({ data: JSON.stringify(value) });
  }

  send(value: string) {
    this.sent.push(value);
  }
}

function flush() {
  return new Promise<void>((resolve) => setTimeout(resolve, 0));
}

describe("live sync controller", () => {
  it("persists valid events in arrival order and reports one live revision per event", async () => {
    const socket = new FakeSocket();
    const persisted: string[] = [];
    const observed: string[] = [];
    let releaseFirst!: () => void;
    const firstBlocked = new Promise<void>((resolve) => {
      releaseFirst = resolve;
    });
    const controller = createLiveSyncController(
      {
        onEvent: (event) => observed.push(event.type),
      },
      {
        createSocket: () => socket,
        createTicket: async () => ({ expiresInSeconds: 30, serverUrl: "https://example", url: "wss://example/ws?ticket=one" }),
        persistEvent: async (_scope, type) => {
          if (type === "task.updated") await firstBlocked;
          persisted.push(type);
        },
        schedule: () => 1,
        cancelScheduled: () => undefined,
      },
    );

    controller.start();
    await flush();
    socket.open();
    socket.receive({ type: "task.updated", payload: { id: "tsk_1" } });
    socket.receive({ type: "approval.requested", payload: { id: "apr_1" } });
    await flush();
    expect(persisted).toEqual([]);
    releaseFirst();
    await flush();
    await flush();

    expect(persisted).toEqual(["task.updated", "approval.requested"]);
    expect(observed).toEqual(persisted);
  });

  it("ignores malformed envelopes instead of poisoning the local replica", async () => {
    const socket = new FakeSocket();
    const persistEvent = jest.fn(async () => undefined);
    const onError = jest.fn();
    const controller = createLiveSyncController(
      { onError },
      {
        createSocket: () => socket,
        createTicket: async () => ({ expiresInSeconds: 30, serverUrl: "https://example", url: "wss://example/ws?ticket=one" }),
        persistEvent,
        schedule: () => 1,
        cancelScheduled: () => undefined,
      },
    );

    controller.start();
    await flush();
    socket.open();
    socket.receive({ type: "task.updated", payload: [] });
    await flush();

    expect(persistEvent).not.toHaveBeenCalled();
    expect(onError).toHaveBeenCalledWith("Événement temps réel invalide.");
  });

  it("delivers only an ID-only capability notification without persisting or executing it", async () => {
    const socket = new FakeSocket();
    const persistEvent = jest.fn(async () => undefined);
    const onCapabilityRequest = jest.fn();
    const controller = createLiveSyncController(
      { onCapabilityRequest },
      {
        createSocket: () => socket,
        createTicket: async () => ({ expiresInSeconds: 30, serverUrl: "https://example", url: "wss://example/ws?ticket=one" }),
        persistEvent,
        schedule: () => 1,
        cancelScheduled: () => undefined,
      },
    );

    controller.start();
    await flush();
    socket.open();
    socket.receive({
      type: "iphone.capability.requested",
      payload: {
        request_id: `iphreq_${"a".repeat(32)}`,
        capability_name: "iphone.location.current",
        expires_at: new Date(Date.now() + 60_000).toISOString(),
        preview: { arguments_redacted: true },
      },
    });
    await flush();

    expect(onCapabilityRequest).toHaveBeenCalledWith(expect.objectContaining({
      request_id: `iphreq_${"a".repeat(32)}`,
      capability_name: "iphone.location.current",
      preview: { arguments_redacted: true },
    }));
    expect(persistEvent).not.toHaveBeenCalled();
  });

  it("uses a fresh one-use ticket after a disconnect and never reconnects after stop", async () => {
    const sockets: FakeSocket[] = [];
    const tickets = jest
      .fn<() => Promise<{ expiresInSeconds: number; serverUrl: string; url: string }>>()
      .mockResolvedValueOnce({ expiresInSeconds: 30, serverUrl: "https://example", url: "wss://example/ws?ticket=one" })
      .mockResolvedValueOnce({ expiresInSeconds: 30, serverUrl: "https://example", url: "wss://example/ws?ticket=two" });
    let reconnect: (() => void) | null = null;
    const controller = createLiveSyncController(
      {},
      {
        createSocket: () => {
          const socket = new FakeSocket();
          sockets.push(socket);
          return socket;
        },
        createTicket: tickets,
        persistEvent: async () => undefined,
        schedule: (callback) => {
          reconnect = () => {
            reconnect = null;
            callback();
          };
          return 1;
        },
        cancelScheduled: () => {
          reconnect = null;
        },
      },
    );

    controller.start();
    await flush();
    sockets[0].open();
    sockets[0].onclose?.({ code: 1006 });
    expect(reconnect).not.toBeNull();
    const runReconnect = reconnect as (() => void) | null;
    runReconnect?.();
    await flush();

    expect(tickets).toHaveBeenCalledTimes(2);
    expect(sockets).toHaveLength(2);

    controller.stop();
    sockets[1].onclose?.({ code: 1006 });
    expect(reconnect).toBeNull();
  });
});
