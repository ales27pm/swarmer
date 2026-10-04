import { createHash } from "node:crypto";
import { db, id, now } from "../db";

// Audit ledger — append-only, hash-chained (MASTER_SPEC: "Strict où ça agit").
export function appendAudit(event: {
  trace_id: string;
  event_type: string;
  actor_type: "user" | "agent" | "system";
  actor_id: string;
  task_id?: string | null;
  payload?: unknown;
}) {
  const last = db
    .query("SELECT hash FROM audit_events ORDER BY created_at DESC, rowid DESC LIMIT 1")
    .get() as { hash: string | null } | null;
  const prev_hash = last?.hash ?? null;
  const payload_json = JSON.stringify(event.payload ?? {});
  const hash = createHash("sha256")
    .update(`${prev_hash ?? "genesis"}|${event.trace_id}|${event.event_type}|${payload_json}`)
    .digest("hex");

  db.prepare(
    `INSERT INTO audit_events (id, trace_id, event_type, actor_type, actor_id, task_id, payload_json, prev_hash, hash, created_at)
     VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`
  ).run(
    id("aud"),
    event.trace_id,
    event.event_type,
    event.actor_type,
    event.actor_id,
    event.task_id ?? null,
    payload_json,
    prev_hash,
    hash,
    now()
  );
}
