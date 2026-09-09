import { act, render, waitFor } from "@testing-library/react-native";
import { beforeEach, describe, expect, it, jest } from "@jest/globals";
import { Text } from "react-native";

import { bootstrapSync, drainMutationOutbox } from "@/lib/api/client";
import { subscribeConnectionChanges } from "@/lib/connection-events";
import { iphoneCapabilityTransport } from "@/lib/iphone-capabilities/runtime";
import {
  createLiveSyncController,
  type LiveSyncController,
} from "@/lib/sync/live-sync";
import { LiveSyncProvider } from "@/lib/sync/live-sync-provider";

function deferred<T>() {
  let reject!: (cause?: unknown) => void;
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((settle, fail) => {
    resolve = settle;
    reject = fail;
  });
  return { promise, reject, resolve };
}

jest.mock("@/lib/api/client", () => ({
  bootstrapSync: jest.fn(),
  drainMutationOutbox: jest.fn(),
}));
jest.mock("@/lib/connection-events", () => ({ subscribeConnectionChanges: jest.fn() }));
jest.mock("@/lib/iphone-capabilities/runtime", () => ({
  iphoneCapabilityTransport: {
    clear: jest.fn(),
    receiveNotification: jest.fn(),
  },
}));
jest.mock("@/lib/sync/live-sync", () => ({ createLiveSyncController: jest.fn() }));

const mockBootstrap = jest.mocked(bootstrapSync);
const mockDrainMutationOutbox = jest.mocked(drainMutationOutbox);
const mockCreateController = jest.mocked(createLiveSyncController);
const mockSubscribe = jest.mocked(subscribeConnectionChanges);
const mockCapabilityTransport = jest.mocked(iphoneCapabilityTransport);

describe("LiveSyncProvider", () => {
  let notifyConnectionChanged: (() => void) | undefined;
  const controllers: (LiveSyncController & { start: jest.Mock; stop: jest.Mock })[] = [];

  beforeEach(() => {
    jest.clearAllMocks();
    controllers.length = 0;
    notifyConnectionChanged = undefined;
    mockBootstrap.mockResolvedValue({} as never);
    mockDrainMutationOutbox.mockResolvedValue({
      attempted: 0,
      completed: 0,
      failed: 0,
      remaining: 0,
    });
    mockSubscribe.mockImplementation((listener) => {
      notifyConnectionChanged = listener;
      return () => undefined;
    });
    mockCreateController.mockImplementation(() => {
      const controller = {
        pause: jest.fn(),
        resume: jest.fn(),
        start: jest.fn(),
        stop: jest.fn(),
      };
      controllers.push(controller);
      return controller;
    });
  });

  it("restarts the socket controller when pairing changes the server identity", async () => {
    await render(<LiveSyncProvider><Text>child</Text></LiveSyncProvider>);
    expect(controllers).toHaveLength(1);

    await act(async () => notifyConnectionChanged?.());

    expect(controllers).toHaveLength(2);
    expect(controllers[0].stop).toHaveBeenCalledTimes(1);
    expect(
      controllers[1].start.mock.calls.length
        + (controllers[1].pause as jest.Mock).mock.calls.length,
    ).toBe(1);
  });

  it("reconciles the authoritative bootstrap whenever a socket connects", async () => {
    await render(<LiveSyncProvider><Text>child</Text></LiveSyncProvider>);
    const options = mockCreateController.mock.calls[0][0];

    await act(async () => options?.onStateChange?.("connected"));

    await waitFor(() => expect(mockBootstrap).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(mockDrainMutationOutbox).toHaveBeenCalledTimes(1));
  });

  it("drains safe offline mutations only after reconnect bootstrap succeeds", async () => {
    await render(<LiveSyncProvider><Text>child</Text></LiveSyncProvider>);
    const options = mockCreateController.mock.calls[0][0];
    const order: string[] = [];
    mockBootstrap.mockImplementationOnce(async () => {
      order.push("bootstrap");
      return {} as never;
    });
    mockDrainMutationOutbox.mockImplementationOnce(async () => {
      order.push("drain");
      return { attempted: 1, completed: 1, failed: 0, remaining: 0 };
    });

    await act(async () => options?.onStateChange?.("connected"));

    await waitFor(() => expect(mockDrainMutationOutbox).toHaveBeenCalledTimes(1));
    expect(order).toEqual(["bootstrap", "drain"]);
  });

  it("coalesces a burst of metadata-only invalidations into one authoritative refresh", async () => {
    await render(<LiveSyncProvider><Text>child</Text></LiveSyncProvider>);
    const options = mockCreateController.mock.calls[0][0];

    await act(async () => {
      options?.onEvent?.({
        type: "task.updated",
        payload: { id: "tsk_1", status: "running", refetch_required: true },
      });
      options?.onEvent?.({
        type: "message.created",
        payload: { id: "msg_1", conversation_id: "cnv_1", refetch_required: true },
      });
      options?.onEvent?.({
        type: "orchestrator.proposed",
        payload: {
          task_id: "tsk_1",
          planner_source: "ubuntu_local",
          refetch_required: true,
        },
      });
    });

    await waitFor(() => expect(mockBootstrap).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(mockDrainMutationOutbox).toHaveBeenCalledTimes(1));
  });

  it("never overlaps refreshes and runs one follow-up for invalidations received in flight", async () => {
    const first = deferred<never>();
    const second = deferred<never>();
    let active = 0;
    let maxActive = 0;
    mockBootstrap
      .mockImplementationOnce(async () => {
        active += 1;
        maxActive = Math.max(maxActive, active);
        const value = await first.promise;
        active -= 1;
        return value;
      })
      .mockImplementationOnce(async () => {
        active += 1;
        maxActive = Math.max(maxActive, active);
        const value = await second.promise;
        active -= 1;
        return value;
      });
    await render(<LiveSyncProvider><Text>child</Text></LiveSyncProvider>);
    const options = mockCreateController.mock.calls[0][0];

    await act(async () => options?.onEvent?.({
      type: "task.updated",
      payload: { id: "tsk_1", refetch_required: true },
    }));
    await waitFor(() => expect(mockBootstrap).toHaveBeenCalledTimes(1));

    await act(async () => {
      options?.onEvent?.({
        type: "message.created",
        payload: { id: "msg_1", refetch_required: true },
      });
      options?.onEvent?.({
        type: "message.created",
        payload: { id: "msg_2", refetch_required: true },
      });
    });
    expect(mockBootstrap).toHaveBeenCalledTimes(1);

    await act(async () => first.resolve({} as never));
    await waitFor(() => expect(mockBootstrap).toHaveBeenCalledTimes(2));
    await act(async () => second.resolve({} as never));
    await waitFor(() => expect(mockDrainMutationOutbox).toHaveBeenCalledTimes(2));
    expect(maxActive).toBe(1);
  });

  it("fences an old-origin refresh before reconciling the replacement connection", async () => {
    const oldRefresh = deferred<never>();
    let oldShouldApply: (() => boolean) | undefined;
    mockBootstrap.mockImplementationOnce(async (shouldApply) => {
      oldShouldApply = shouldApply;
      return oldRefresh.promise;
    });
    await render(<LiveSyncProvider><Text>child</Text></LiveSyncProvider>);
    const oldOptions = mockCreateController.mock.calls[0][0];

    await act(async () => oldOptions?.onEvent?.({
      type: "task.updated",
      payload: { id: "tsk_old", refetch_required: true },
    }));
    await waitFor(() => expect(mockBootstrap).toHaveBeenCalledTimes(1));

    await act(async () => notifyConnectionChanged?.());
    expect(oldShouldApply?.()).toBe(false);
    const newOptions = mockCreateController.mock.calls[1][0];
    await act(async () => newOptions?.onStateChange?.("connected"));
    expect(mockBootstrap).toHaveBeenCalledTimes(1);

    await act(async () => oldRefresh.resolve({} as never));
    await waitFor(() => expect(mockBootstrap).toHaveBeenCalledTimes(2));
  });

  it("does not drain after a failed reconnect bootstrap", async () => {
    await render(<LiveSyncProvider><Text>child</Text></LiveSyncProvider>);
    const options = mockCreateController.mock.calls[0][0];
    mockBootstrap.mockRejectedValueOnce(new Error("offline"));

    await act(async () => options?.onStateChange?.("connected"));

    await waitFor(() => expect(mockBootstrap).toHaveBeenCalledTimes(1));
    expect(mockDrainMutationOutbox).not.toHaveBeenCalled();
  });

  it("retries a transient invalidation failure without requiring another socket event", async () => {
    jest.useFakeTimers();
    let rendered: Awaited<ReturnType<typeof render>> | undefined;
    try {
      mockBootstrap
        .mockRejectedValueOnce(new Error("offline"))
        .mockResolvedValueOnce({} as never);
      rendered = await render(<LiveSyncProvider><Text>child</Text></LiveSyncProvider>);
      const options = mockCreateController.mock.calls[0][0];

      await act(async () => options?.onEvent?.({
        type: "task.updated",
        payload: { id: "tsk_retry", refetch_required: true },
      }));
      expect(mockBootstrap).toHaveBeenCalledTimes(1);
      expect(mockDrainMutationOutbox).not.toHaveBeenCalled();

      await act(async () => {
        jest.advanceTimersByTime(250);
        await Promise.resolve();
      });

      expect(mockBootstrap).toHaveBeenCalledTimes(2);
      expect(mockDrainMutationOutbox).toHaveBeenCalledTimes(1);
    } finally {
      if (rendered) await rendered.unmount();
      jest.useRealTimers();
    }
  });

  it("coalesces invalidations during backoff and bounds automatic retries", async () => {
    jest.useFakeTimers();
    let rendered: Awaited<ReturnType<typeof render>> | undefined;
    try {
      mockBootstrap.mockRejectedValue(new Error("offline"));
      rendered = await render(<LiveSyncProvider><Text>child</Text></LiveSyncProvider>);
      const options = mockCreateController.mock.calls[0][0];

      await act(async () => options?.onEvent?.({
        type: "task.updated",
        payload: { id: "tsk_retry", refetch_required: true },
      }));
      await act(async () => {
        options?.onEvent?.({
          type: "message.created",
          payload: { id: "msg_1", refetch_required: true },
        });
        options?.onEvent?.({
          type: "message.created",
          payload: { id: "msg_2", refetch_required: true },
        });
      });
      expect(mockBootstrap).toHaveBeenCalledTimes(1);

      for (const delayMs of [250, 1_000, 4_000]) {
        await act(async () => {
          jest.advanceTimersByTime(delayMs);
          await Promise.resolve();
        });
      }
      expect(mockBootstrap).toHaveBeenCalledTimes(4);

      await act(async () => {
        jest.advanceTimersByTime(60_000);
        await Promise.resolve();
      });
      expect(mockBootstrap).toHaveBeenCalledTimes(4);
      expect(mockDrainMutationOutbox).not.toHaveBeenCalled();
    } finally {
      if (rendered) await rendered.unmount();
      jest.useRealTimers();
    }
  });

  it("cancels an invalidation retry when the paired origin changes", async () => {
    jest.useFakeTimers();
    let rendered: Awaited<ReturnType<typeof render>> | undefined;
    try {
      mockBootstrap.mockRejectedValueOnce(new Error("offline"));
      rendered = await render(<LiveSyncProvider><Text>child</Text></LiveSyncProvider>);
      const options = mockCreateController.mock.calls[0][0];

      await act(async () => options?.onEvent?.({
        type: "task.updated",
        payload: { id: "tsk_old", refetch_required: true },
      }));
      expect(mockBootstrap).toHaveBeenCalledTimes(1);

      await act(async () => notifyConnectionChanged?.());
      await act(async () => {
        jest.advanceTimersByTime(60_000);
        await Promise.resolve();
      });

      expect(mockBootstrap).toHaveBeenCalledTimes(1);
    } finally {
      if (rendered) await rendered.unmount();
      jest.useRealTimers();
    }
  });

  it("cancels an invalidation retry when the provider is disposed", async () => {
    jest.useFakeTimers();
    let rendered: Awaited<ReturnType<typeof render>> | undefined;
    try {
      mockBootstrap.mockRejectedValueOnce(new Error("offline"));
      rendered = await render(<LiveSyncProvider><Text>child</Text></LiveSyncProvider>);
      const options = mockCreateController.mock.calls[0][0];

      await act(async () => options?.onEvent?.({
        type: "task.updated",
        payload: { id: "tsk_dispose", refetch_required: true },
      }));
      expect(mockBootstrap).toHaveBeenCalledTimes(1);

      await rendered.unmount();
      rendered = undefined;
      await act(async () => {
        jest.advanceTimersByTime(60_000);
        await Promise.resolve();
      });

      expect(mockBootstrap).toHaveBeenCalledTimes(1);
    } finally {
      if (rendered) await rendered.unmount();
      jest.useRealTimers();
    }
  });

  it("queues capability notifications without executing them", async () => {
    await render(<LiveSyncProvider><Text>child</Text></LiveSyncProvider>);
    const options = mockCreateController.mock.calls[0][0];
    const notification = {
      request_id: `iphreq_${"a".repeat(32)}`,
      capability_name: "iphone.location.current" as const,
      expires_at: new Date(Date.now() + 60_000).toISOString(),
      preview: { arguments_redacted: true as const },
    };

    await act(async () => options?.onCapabilityRequest?.(notification));

    expect(mockCapabilityTransport.receiveNotification).toHaveBeenCalledWith(notification);
  });
});
