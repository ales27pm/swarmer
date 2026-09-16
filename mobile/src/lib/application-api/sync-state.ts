import type { LiveSyncState } from "@/lib/sync/live-sync";

type SyncSnapshot = { state: LiveSyncState; revision: number; hasError: boolean };
let current: (SyncSnapshot & { observedAt: string; owner: symbol }) | null = null;

/** Publish the existing controller's state without exporting raw network errors or credentials. */
export function publishApplicationSyncState(snapshot: SyncSnapshot): () => void {
  const owner = Symbol("sync-provider");
  current = { ...snapshot, observedAt: new Date().toISOString(), owner };
  return () => { if (current?.owner === owner) current = null; };
}

export function readApplicationSyncState() {
  if (!current) return { available: false, state: "stopped", revision: 0, hasError: false, observedAt: null };
  const { owner: _owner, ...snapshot } = current;
  return { available: true, ...snapshot };
}
