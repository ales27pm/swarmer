type ConnectionListener = () => void;

const listeners = new Set<ConnectionListener>();

export function subscribeConnectionChanges(listener: ConnectionListener): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

export function notifyConnectionChanged(): void {
  for (const listener of listeners) listener();
}
