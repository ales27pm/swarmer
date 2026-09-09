import { type ReactNode, useEffect, useMemo, useState } from "react";
import { AppState } from "react-native";

import { bootstrapSync, drainMutationOutbox } from "@/lib/api/client";
import { subscribeConnectionChanges } from "@/lib/connection-events";
import { iphoneCapabilityTransport } from "@/lib/iphone-capabilities/runtime";
import { LiveSyncContextProvider } from "@/lib/sync/live-sync-context";
import {
  createLiveSyncController,
  type LiveSyncState,
} from "@/lib/sync/live-sync";

const RECONCILE_RETRY_DELAYS_MS = [250, 1_000, 4_000] as const;

export function LiveSyncProvider({ children }: { children: ReactNode }) {
  const [error, setError] = useState<string | null>(null);
  const [revision, setRevision] = useState(0);
  const [state, setState] = useState<LiveSyncState>("stopped");

  useEffect(() => {
    let disposed = false;
    let connectionGeneration = 0;
    let reconcileEpoch = 0;
    let pendingReconcileGeneration: number | null = null;
    let reconcileRunnerActive = false;
    let reconcileRunnerScheduled = false;
    let reconcileRetryCount = 0;
    let reconcileRetryHandle: ReturnType<typeof setTimeout> | null = null;

    const cancelReconcileRetry = () => {
      if (reconcileRetryHandle !== null) clearTimeout(reconcileRetryHandle);
      reconcileRetryHandle = null;
    };

    const reconcile = async (
      generation: number,
    ): Promise<"completed" | "failed" | "stale"> => {
      const epoch = ++reconcileEpoch;
      const isCurrent = () => !disposed
        && generation === connectionGeneration
        && epoch === reconcileEpoch;
      try {
        await bootstrapSync(isCurrent);
        if (!isCurrent()) return "stale";
        await drainMutationOutbox();
        if (!isCurrent()) return "stale";
        reconcileRetryCount = 0;
        setError(null);
        setRevision((value) => value + 1);
        return "completed";
      } catch (cause) {
        if (!isCurrent()) return "stale";
        setError(cause instanceof Error ? cause.message : String(cause));
        return "failed";
      }
    };

    const scheduleReconcileRunner = () => {
      if (
        disposed
        || reconcileRunnerActive
        || reconcileRunnerScheduled
        || reconcileRetryHandle !== null
      ) return;
      reconcileRunnerScheduled = true;
      void Promise.resolve().then(runReconcileQueue);
    };

    const scheduleReconcileRetry = (generation: number) => {
      if (
        disposed
        || generation !== connectionGeneration
        || reconcileRetryHandle !== null
        || reconcileRetryCount >= RECONCILE_RETRY_DELAYS_MS.length
      ) return false;
      const delayMs = RECONCILE_RETRY_DELAYS_MS[reconcileRetryCount];
      reconcileRetryCount += 1;
      pendingReconcileGeneration = generation;
      reconcileRetryHandle = setTimeout(() => {
        reconcileRetryHandle = null;
        if (disposed || generation !== connectionGeneration) return;
        scheduleReconcileRunner();
      }, delayMs);
      return true;
    };

    async function runReconcileQueue() {
      if (reconcileRunnerActive) return;
      reconcileRunnerScheduled = false;
      reconcileRunnerActive = true;
      try {
        while (!disposed && pendingReconcileGeneration !== null) {
          const generation = pendingReconcileGeneration;
          pendingReconcileGeneration = null;
          if (generation !== connectionGeneration) continue;
          const outcome = await reconcile(generation);
          if (outcome === "failed") {
            scheduleReconcileRetry(generation);
            break;
          }
        }
      } finally {
        reconcileRunnerActive = false;
        if (!disposed && pendingReconcileGeneration !== null) scheduleReconcileRunner();
      }
    }

    const requestReconcile = () => {
      if (disposed) return;
      pendingReconcileGeneration = connectionGeneration;
      scheduleReconcileRunner();
    };

    const createController = () => createLiveSyncController({
      onCapabilityRequest: (notification) => {
        iphoneCapabilityTransport.receiveNotification(notification);
      },
      onError: setError,
      onEvent: (event) => {
        setError(null);
        if (event.payload.refetch_required === true) requestReconcile();
        else setRevision((value) => value + 1);
      },
      onStateChange: (nextState) => {
        setState(nextState);
        if (nextState === "connected") requestReconcile();
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
      connectionGeneration += 1;
      reconcileEpoch += 1;
      cancelReconcileRetry();
      reconcileRetryCount = 0;
      pendingReconcileGeneration = null;
      controller.stop();
      iphoneCapabilityTransport.clear();
      setError(null);
      controller = createController();
      activate();
    });
    return () => {
      disposed = true;
      connectionGeneration += 1;
      reconcileEpoch += 1;
      cancelReconcileRetry();
      pendingReconcileGeneration = null;
      unsubscribeConnection();
      subscription.remove();
      controller.stop();
      iphoneCapabilityTransport.clear();
    };
  }, []);

  const value = useMemo(() => ({ error, revision, state }), [error, revision, state]);
  return <LiveSyncContextProvider value={value}>{children}</LiveSyncContextProvider>;
}
