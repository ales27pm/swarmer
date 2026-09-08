import {
  createContext,
  type ReactNode,
  useContext,
  useEffect,
  useRef,
} from "react";

import type { LiveSyncState } from "@/lib/sync/live-sync";

export type LiveSyncContextValue = {
  error: string | null;
  revision: number;
  state: LiveSyncState;
};

const DEFAULT_VALUE: LiveSyncContextValue = {
  error: null,
  revision: 0,
  state: "stopped",
};

const LiveSyncContext = createContext<LiveSyncContextValue>(DEFAULT_VALUE);

export function LiveSyncContextProvider({
  children,
  value,
}: {
  children: ReactNode;
  value: LiveSyncContextValue;
}) {
  return <LiveSyncContext.Provider value={value}>{children}</LiveSyncContext.Provider>;
}

export function useLiveSync(): LiveSyncContextValue {
  return useContext(LiveSyncContext);
}

export function useLiveRefresh(refresh: () => void | Promise<unknown>, delayMs = 150) {
  const { revision } = useLiveSync();
  const refreshRef = useRef(refresh);

  useEffect(() => {
    refreshRef.current = refresh;
  }, [refresh]);

  useEffect(() => {
    if (revision === 0) return;
    const handle = setTimeout(() => {
      void refreshRef.current();
    }, delayMs);
    return () => clearTimeout(handle);
  }, [delayMs, revision]);
}
