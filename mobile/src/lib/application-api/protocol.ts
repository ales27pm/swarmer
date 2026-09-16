/** HTTP is only an adapter. The application registry owns commands and validation. */
export interface CommandDispatcher {
  catalog(): unknown;
  execute(command: string, input: unknown): Promise<unknown>;
}

export type ProtocolRequest = { method: string; path: string; body: string };
export type ProtocolResponse = { status: number; body: unknown };
type Job = {
  id: string;
  command: string;
  state: "running" | "succeeded" | "failed" | "uncertain";
  acceptedAt: string;
  finishedAt?: string;
  result?: unknown;
  resultOmitted?: boolean;
  error?: { code: string; message: string };
};
type Entry = { fingerprint: string; job: Job };

const MAX_BODY = 65_536;
// Worst-case UTF-8 expansion plus the envelope remains below the native 1 MiB cap.
const MAX_RESULT = 200_000;
const MAX_RETAINED_RESULT = 8_000_000;
const MAX_JOBS = 256;
const MAX_ACTIVE = 8;
const KEY = /^[A-Za-z0-9_.:-]{16,128}$/;
// Public wording is owned here; never forward model output or exception messages.
const LOCAL_PLAN_ERRORS: Readonly<Record<string, string>> = {
  invalid_plan: "La réponse du modèle est vide ou ne respecte pas le contrat JSON du plan local. Aucun plan n’a été accepté ni démarré.",
  invalid_context: "Le contexte du but ne permet pas de construire un plan local valide. Actualisez le but et ses capacités.",
  context_unavailable: "Le contexte du plan n’a pas pu être chargé. Aucun démarrage du but n’a été envoyé.",
  generation_failed: "La génération locale n’a pas produit de plan utilisable. Aucun démarrage du but n’a été envoyé.",
  generation_truncated: "La génération n’est pas complète ; aucun plan ne peut être soumis.",
  model_not_ready: "Le modèle chargé ne correspond pas au modèle demandé.",
  unsupported_runtime: "Ce runtime n’est pas disponible sur cet appareil.",
  stale_context: "Le but, la mémoire ou les capacités ont changé depuis la génération. Préparez un nouveau plan avant de démarrer.",
};

function object(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function canonical(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(canonical).join(",")}]`;
  if (object(value)) return `{${Object.keys(value).sort().map((key) => `${JSON.stringify(key)}:${canonical(value[key])}`).join(",")}}`;
  return JSON.stringify(value);
}

function failure(status: number, code: string, message: string): ProtocolResponse {
  return { status, body: { apiVersion: "1", error: { code, message } } };
}

/** A session never evicts idempotency keys: capacity fails closed instead of replaying work. */
export function createApplicationProtocol(
  dispatcher: CommandDispatcher,
  now = () => new Date(),
  // Correlation only, not an authentication secret. Prevent old keys/jobs aliasing after a JS reload.
  instanceId = `${Date.now().toString(36)}${Math.random().toString(36).slice(2)}${Math.random().toString(36).slice(2)}`,
) {
  const jobs = new Map<string, Entry>();
  let active = 0;
  let sequence = 0;
  let retainedResultBytes = 0;
  let accepting = true;
  let continuationOnly = false;

  async function run(entry: Entry, input: unknown) {
    try {
      const result = await dispatcher.execute(entry.job.command, input);
      const serialized = JSON.stringify(result ?? null);
      if (serialized.length > MAX_RESULT || retainedResultBytes + serialized.length > MAX_RETAINED_RESULT) {
        entry.job.resultOmitted = true;
        entry.job.state = "succeeded";
      } else {
        // Detach mutable state, closures, and non-JSON objects from retained receipts.
        entry.job.result = JSON.parse(serialized) as unknown;
        retainedResultBytes += serialized.length;
        entry.job.state = "succeeded";
      }
    } catch (error) {
      // Do not return arbitrary network/native exception text, URLs or server payloads.
      const code = object(error) && typeof error.code === "string"
        && /^[a-z_]{1,64}$/.test(error.code) ? error.code : "outcome_unknown";
      entry.job.error = { code, message: Object.hasOwn(LOCAL_PLAN_ERRORS, code) ? LOCAL_PLAN_ERRORS[code]
        : "La commande n’a pas confirmé sa réussite. Consulter son état avant toute nouvelle tentative." };
      entry.job.state = code === "outcome_unknown" ? "uncertain" : "failed";
    } finally {
      entry.job.finishedAt = now().toISOString();
      active -= 1;
    }
  }

  function handle(request: ProtocolRequest): ProtocolResponse {
    if (!accepting) return failure(503, "app_inactive", "L’application doit être au premier plan.");
    if (request.method === "GET" && request.body !== "") return failure(400, "invalid_request", "Corps GET interdit.");
    if (request.method === "GET" && request.path === "/v1/health") {
      return { status: 200, body: { apiVersion: "1", instanceId, state: "ready", access: continuationOnly ? "continuation" : "foreground", activeJobs: active, retainedJobs: jobs.size, capacity: MAX_JOBS, persistence: "javascript_session" } };
    }
    if (request.method === "GET" && request.path === "/v1/catalog") {
      return { status: 200, body: { apiVersion: "1", instanceId, catalog: dispatcher.catalog() } };
    }
    const jobMatch = /^\/v1\/jobs\/(job_[a-z0-9]+_[1-9][0-9]*)$/.exec(request.path);
    if (request.method === "GET" && jobMatch) {
      const entry = [...jobs.values()].find(({ job }) => job.id === jobMatch[1]);
      return entry ? { status: 200, body: { apiVersion: "1", instanceId, job: { ...entry.job } } }
        : failure(404, "unknown_job", "Traitement inconnu dans cette session ; ne pas déduire que l’action n’a pas eu lieu.");
    }
    if (request.path !== "/v1/commands" || request.method !== "POST") return failure(404, "unknown_route", "Route inconnue.");
    if (request.body.length > MAX_BODY) return failure(413, "request_too_large", "Requête trop volumineuse.");
    let value: unknown;
    try { value = JSON.parse(request.body) as unknown; } catch { return failure(400, "invalid_json", "JSON invalide."); }
    if (!object(value) || Object.keys(value).sort().join(",") !== "command,idempotencyKey,input,instanceId"
      || typeof value.idempotencyKey !== "string" || !KEY.test(value.idempotencyKey)
      || typeof value.command !== "string" || !/^[a-z][a-zA-Z0-9_.]{0,95}$/.test(value.command)
      || !object(value.input)) return failure(400, "invalid_request", "command, input, instanceId et idempotencyKey sont requis.");
    if (value.instanceId !== instanceId) return failure(409, "session_changed", "Le runtime a changé ; les anciennes commandes ne doivent pas être relancées automatiquement.");
    // Bound recursion before canonicalizing an untrusted JSON tree.
    const pending: { value: unknown; depth: number }[] = [{ value: value.input, depth: 0 }];
    while (pending.length) {
      const item = pending.pop()!;
      if (item.depth > 24) return failure(400, "invalid_request", "JSON trop imbriqué.");
      if (object(item.value) || Array.isArray(item.value)) {
        pending.push(...Object.values(item.value).map((child) => ({ value: child, depth: item.depth + 1 })));
      }
    }
    const fingerprint = canonical({ command: value.command, input: value.input });
    const existing = jobs.get(value.idempotencyKey);
    if (existing) {
      if (existing.fingerprint !== fingerprint) return failure(409, "idempotency_conflict", "Cette clé désigne déjà une autre commande.");
      return { status: 200, body: { apiVersion: "1", instanceId, replayed: true, job: { ...existing.job } } };
    }
    // An admitted native calculation permits observation and cancellation only.
    // A background listener is never authority to initiate unrelated work.
    if (continuationOnly && value.command !== "models.status" && value.command !== "inference.cancel") {
      return failure(409, "foreground_required", "Revenez dans l’application pour démarrer une nouvelle opération. Le suivi et l’annulation du calcul en cours restent disponibles.");
    }
    if (jobs.size >= MAX_JOBS || active >= MAX_ACTIVE) return failure(429, "session_capacity", "Capacité de session atteinte. Aucun nouveau traitement accepté.");
    const entry: Entry = { fingerprint, job: { id: `job_${instanceId}_${++sequence}`, command: value.command, state: "running", acceptedAt: now().toISOString() } };
    jobs.set(value.idempotencyKey, entry);
    active += 1;
    // Reserve key synchronously before dispatch. A second request cannot create another mutation.
    void run(entry, value.input);
    return { status: 202, body: { apiVersion: "1", instanceId, replayed: false, job: { ...entry.job } } };
  }
  return {
    handle,
    setActive: (value: boolean) => { accepting = value; continuationOnly = false; },
    setAccess: (value: "foreground" | "continuation" | "inactive") => {
      accepting = value !== "inactive";
      continuationOnly = value === "continuation";
    },
  };
}
