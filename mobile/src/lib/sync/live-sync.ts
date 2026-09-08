import { createEventStreamTicket } from "@/lib/api/client";
import { upsertEvent } from "@/lib/state/replica";

type ControlPlaneEvent = {
  type: string;
  payload: Record<string, unknown>;
};

export type LiveSyncState = "connecting" | "connected" | "disconnected" | "paused" | "stopped";

type SocketLike = {
  readyState: number;
  onclose: ((event: unknown) => void) | null;
  onerror: ((event: unknown) => void) | null;
  onmessage: ((event: { data: unknown }) => void) | null;
  onopen: ((event: unknown) => void) | null;
  close: () => void;
  send: (value: string) => void;
};

type LiveSyncOptions = {
  onError?: (message: string) => void;
  onEvent?: (event: ControlPlaneEvent) => void;
  onStateChange?: (state: LiveSyncState) => void;
};

type LiveSyncDependencies = {
  cancelScheduled?: (handle: unknown) => void;
  createSocket?: (url: string) => SocketLike;
  createTicket?: typeof createEventStreamTicket;
  persistEvent?: typeof upsertEvent;
  schedule?: (callback: () => void, delayMs: number) => unknown;
};

export type LiveSyncController = {
  pause: () => void;
  resume: () => void;
  start: () => void;
  stop: () => void;
};

function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function parseEvent(raw: string): ControlPlaneEvent | null {
  try {
    const value: unknown = JSON.parse(raw);
    if (
      !isRecord(value) ||
      Object.keys(value).some((key) => !["type", "payload"].includes(key)) ||
      typeof value.type !== "string" ||
      !value.type ||
      !isRecord(value.payload)
    ) {
      return null;
    }
    return { type: value.type, payload: value.payload };
  } catch {
    return null;
  }
}

export function createLiveSyncController(
  options: LiveSyncOptions = {},
  dependencies: LiveSyncDependencies = {},
): LiveSyncController {
  const createTicket = dependencies.createTicket ?? createEventStreamTicket;
  const createSocket = dependencies.createSocket
    ?? ((url) => new WebSocket(url) as unknown as SocketLike);
  const persistEvent = dependencies.persistEvent ?? upsertEvent;
  const schedule = dependencies.schedule ?? ((callback, delayMs) => setTimeout(callback, delayMs));
  const cancelScheduled = dependencies.cancelScheduled
    ?? ((handle) => clearTimeout(handle as ReturnType<typeof setTimeout>));
  let socket: SocketLike | null = null;
  let reconnectHandle: unknown | null = null;
  let enabled = false;
  let stopped = false;
  let connecting = false;
  let attempt = 0;
  let epoch = 0;
  let persistTail = Promise.resolve();

  const setState = (state: LiveSyncState) => options.onStateChange?.(state);

  function clearReconnect() {
    if (reconnectHandle !== null) cancelScheduled(reconnectHandle);
    reconnectHandle = null;
  }

  function closeSocket() {
    const current = socket;
    socket = null;
    if (current) {
      current.onclose = null;
      current.onerror = null;
      current.onmessage = null;
      current.onopen = null;
      current.close();
    }
  }

  function queueReconnect() {
    if (!enabled || stopped || reconnectHandle !== null) return;
    const delay = Math.min(30_000, 1_000 * 2 ** Math.min(attempt, 5));
    attempt += 1;
    reconnectHandle = schedule(() => {
      reconnectHandle = null;
      void connect();
    }, delay);
  }

  async function connect() {
    if (!enabled || stopped || connecting || socket) return;
    connecting = true;
    const connectionEpoch = epoch;
    setState("connecting");
    try {
      const ticket = await createTicket();
      if (!enabled || stopped || connectionEpoch !== epoch) return;
      const current = createSocket(ticket.url);
      socket = current;
      current.onopen = () => {
        if (socket !== current || !enabled || stopped) return;
        attempt = 0;
        setState("connected");
      };
      current.onmessage = (message: { data: unknown }) => {
        if (socket !== current || !enabled || stopped) return;
        const event = typeof message.data === "string" ? parseEvent(message.data) : null;
        if (!event) {
          options.onError?.("Événement temps réel invalide.");
          return;
        }
        persistTail = persistTail
          .catch(() => undefined)
          .then(async () => {
            try {
              await persistEvent(ticket.serverUrl, event.type, event.payload);
            } catch (cause) {
              options.onError?.(
                cause instanceof Error ? cause.message : "Échec de la réplique locale.",
              );
            }
            options.onEvent?.(event);
          });
      };
      current.onerror = () => {
        if (socket !== current) return;
        options.onError?.("Connexion temps réel interrompue.");
        current.close();
      };
      current.onclose = () => {
        if (socket !== current) return;
        socket = null;
        setState(enabled && !stopped ? "disconnected" : stopped ? "stopped" : "paused");
        queueReconnect();
      };
    } catch (cause) {
      if (!enabled || stopped || connectionEpoch !== epoch) return;
      options.onError?.(cause instanceof Error ? cause.message : String(cause));
      setState("disconnected");
      queueReconnect();
    } finally {
      connecting = false;
    }
  }

  function pause() {
    if (stopped) return;
    enabled = false;
    epoch += 1;
    clearReconnect();
    closeSocket();
    setState("paused");
  }

  function resume() {
    if (stopped || enabled) return;
    enabled = true;
    epoch += 1;
    void connect();
  }

  function stop() {
    stopped = true;
    enabled = false;
    epoch += 1;
    clearReconnect();
    closeSocket();
    setState("stopped");
  }

  return { pause, resume, start: resume, stop };
}
