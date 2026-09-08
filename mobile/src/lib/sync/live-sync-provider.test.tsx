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

  it("does not drain after a failed reconnect bootstrap", async () => {
    await render(<LiveSyncProvider><Text>child</Text></LiveSyncProvider>);
    const options = mockCreateController.mock.calls[0][0];
    mockBootstrap.mockRejectedValueOnce(new Error("offline"));

    await act(async () => options?.onStateChange?.("connected"));

    await waitFor(() => expect(mockBootstrap).toHaveBeenCalledTimes(1));
    expect(mockDrainMutationOutbox).not.toHaveBeenCalled();
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
