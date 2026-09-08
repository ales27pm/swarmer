import { type ReactNode, useEffect, useMemo, useState } from "react";
import { AppState } from "react-native";

import { bootstrapSync } from "@/lib/api/client";
import { subscribeConnectionChanges } from "@/lib/connection-events";
import { iphoneCapabilityTransport } from "@/lib/iphone-capabilities/runtime";
import { LiveSyncContextProvider } from "@/lib/sync/live-sync-context";
import {
  createLiveSyncController,
  type LiveSyncState,
} from "@/lib/sync/live-sync";

export function LiveSyncProvider({ children }: { children: ReactNode }) {
  const [error, setError] = useState<string | null>(null);
  const [revision, setRevision] = useState(0);
  const [state, setState] = useState<LiveSyncState>("stopped");

  useEffect(() => {
    let disposed = false;
    let reconcileEpoch = 0;

    const reconcile = async () => {
      const epoch = ++reconcileEpoch;
      try {
        await bootstrapSync(() => !disposed && epoch === reconcileEpoch);
        if (disposed || epoch !== reconcileEpoch) return;
        setError(null);
        setRevision((value) => value + 1);
      } catch (cause) {
        if (disposed || epoch !== reconcileEpoch) return;
        setError(cause instanceof Error ? cause.message : String(cause));
      }
    };

    const createController = () => createLiveSyncController({
      onCapabilityRequest: (notification) => {
        iphoneCapabilityTransport.receiveNotification(notification);
      },
      onError: setError,
      onEvent: () => {
        setError(null);
        setRevision((value) => value + 1);
      },
      onStateChange: (nextState) => {
        setState(nextState);
        if (nextState === "connected") void reconcile();
      },
    });

    let controller = createController();
    const activate = () => {
      if (AppState.currentState === "active") controller.start();
      else controller.pause();
    };
    activate();

    const subscription = AppState.addEventListener("change", (nextState) => {
      if (nextState === "active") controller.resume();
      else controller.pause();
    });
    const unsubscribeConnection = subscribeConnectionChanges(() => {
      reconcileEpoch += 1;
      controller.stop();
      iphoneCapabilityTransport.clear();
      setError(null);
      controller = createController();
      activate();
    });
    return () => {
      disposed = true;
      reconcileEpoch += 1;
      unsubscribeConnection();
      subscription.remove();
      controller.stop();
      iphoneCapabilityTransport.clear();
    };
  }, []);

  const value = useMemo(() => ({ error, revision, state }), [error, revision, state]);
  return <LiveSyncContextProvider value={value}>{children}</LiveSyncContextProvider>;
}
