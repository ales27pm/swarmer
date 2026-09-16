import { publishApplicationSyncState, readApplicationSyncState } from "./sync-state";

test("publishes current provider state without stale cleanup clearing its replacement", () => {
  const old = publishApplicationSyncState({ state: "connected", revision: 1, hasError: false });
  const current = publishApplicationSyncState({ state: "paused", revision: 2, hasError: true });
  old();
  expect(readApplicationSyncState()).toMatchObject({ available: true, state: "paused", revision: 2, hasError: true });
  current();
  expect(readApplicationSyncState()).toMatchObject({ available: false, state: "stopped" });
});
