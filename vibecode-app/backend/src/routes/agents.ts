import { Hono } from "hono";
import { z } from "zod";
import { db, id, now } from "../db";

// Agent Registry — agents never touch the DB directly; they register here.
const agentsRouter = new Hono();

agentsRouter.get("/", (c) => {
  const rows = db.query("SELECT * FROM agents ORDER BY created_at ASC").all();
  return c.json({ data: rows });
});

agentsRouter.get("/:id", (c) => {
  const agent = db.query("SELECT * FROM agents WHERE id = ?").get(c.req.param("id"));
  if (!agent) return c.json({ error: { message: "Agent not found", code: "NOT_FOUND" } }, 404);
  const recentTasks = db
    .query("SELECT * FROM tasks WHERE id IN (SELECT task_id FROM audit_events WHERE actor_id = ?) ORDER BY created_at DESC LIMIT 10")
    .all(c.req.param("id"));
  return c.json({ data: { agent, recent_tasks: recentTasks } });
});

agentsRouter.post("/register", async (c) => {
  const body = z
    .object({
      name: z.string().min(1),
      version: z.string().default("0.1.0"),
      endpoint: z.string().min(1),
      model_id: z.string().optional(),
      skills: z.array(z.string()).default([]),
    })
    .safeParse(await c.req.json().catch(() => ({})));
  if (!body.success) {
    return c.json({ error: { message: "name and endpoint are required", code: "BAD_REQUEST" } }, 400);
  }
  const agentId = id("agt");
  const ts = now();
  db.prepare(
    `INSERT INTO agents (id, name, version, endpoint, model_id, status, skills_json, last_heartbeat_at, created_at, updated_at)
     VALUES (?, ?, ?, ?, ?, 'online', ?, ?, ?, ?)`
  ).run(agentId, body.data.name, body.data.version, body.data.endpoint, body.data.model_id ?? null, JSON.stringify(body.data.skills), ts, ts, ts);
  return c.json({ data: db.query("SELECT * FROM agents WHERE id = ?").get(agentId) }, 201);
});

agentsRouter.post("/:id/heartbeat", (c) => {
  const existing = db.query("SELECT id FROM agents WHERE id = ?").get(c.req.param("id"));
  if (!existing) return c.json({ error: { message: "Agent not found", code: "NOT_FOUND" } }, 404);
  db.prepare("UPDATE agents SET last_heartbeat_at = ?, status = 'online', updated_at = ? WHERE id = ?").run(
    now(),
    now(),
    c.req.param("id")
  );
  return c.json({ data: { ok: true } });
});

export { agentsRouter };
