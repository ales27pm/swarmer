import { Hono } from "hono";
import { z } from "zod";
import { db, id, now } from "../db";
import { appendAudit } from "../lib/audit";

// Sync bootstrap + audit ledger read + feedback events.
const syncRouter = new Hono();

syncRouter.get("/bootstrap", (c) => {
  const count = (table: string) =>
    (db.query(`SELECT COUNT(*) as n FROM ${table}`).get() as { n: number }).n;
  return c.json({
    data: {
      server_time: now(),
      counts: {
        tasks: count("tasks"),
        messages: count("messages"),
        agents: count("agents"),
        approvals_pending: (db.query("SELECT COUNT(*) as n FROM approvals WHERE status = 'pending'").get() as { n: number }).n,
        memory_items: count("memory_items"),
        audit_events: count("audit_events"),
      },
      agents: db.query("SELECT * FROM agents ORDER BY created_at ASC").all(),
      pinned_memory: db.query("SELECT * FROM memory_items WHERE pinned = 1 ORDER BY updated_at DESC").all(),
    },
  });
});

syncRouter.get("/audit", (c) => {
  const limit = Math.min(Number(c.req.query("limit")) || 50, 200);
  const rows = db.query("SELECT * FROM audit_events ORDER BY created_at DESC LIMIT ?").all(limit);
  return c.json({ data: rows });
});

syncRouter.post("/feedback", async (c) => {
  const body = z
    .object({
      task_id: z.string().optional(),
      agent_id: z.string().optional(),
      type: z.string().default("rating"),
      label: z.string().optional(),
      score: z.number().min(0).max(5).optional(),
      notes: z.string().optional(),
    })
    .safeParse(await c.req.json().catch(() => ({})));
  if (!body.success) return c.json({ error: { message: "invalid body", code: "BAD_REQUEST" } }, 400);

  const fbId = id("fbk");
  db.prepare(
    `INSERT INTO feedback_events (id, task_id, agent_id, type, label, score, notes, payload_json, created_at)
     VALUES (?, ?, ?, ?, ?, ?, ?, '{}', ?)`
  ).run(fbId, body.data.task_id ?? null, body.data.agent_id ?? null, body.data.type, body.data.label ?? null, body.data.score ?? null, body.data.notes ?? null, now());

  appendAudit({
    trace_id: body.data.task_id ?? id("trc"),
    event_type: "feedback.created",
    actor_type: "user",
    actor_id: "iphone",
    task_id: body.data.task_id ?? null,
    payload: { score: body.data.score ?? null },
  });

  return c.json({ data: db.query("SELECT * FROM feedback_events WHERE id = ?").get(fbId) }, 201);
});

export { syncRouter };
