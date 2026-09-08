import { Hono } from "hono";
import { z } from "zod";
import { db, id, now } from "../db";
import { appendAudit } from "../lib/audit";
import { runTaskPipeline } from "../lib/orchestrator";
import type { Task } from "../types";

const tasksRouter = new Hono();

tasksRouter.get("/", (c) => {
  const status = c.req.query("status");
  const rows = status
    ? db.query("SELECT * FROM tasks WHERE status = ? ORDER BY created_at DESC LIMIT 100").all(status)
    : db.query("SELECT * FROM tasks ORDER BY created_at DESC LIMIT 100").all();
  return c.json({ data: rows });
});

tasksRouter.post("/", async (c) => {
  const body = z
    .object({
      input: z.string().min(1),
      mode: z.enum(["normal", "commandant", "review", "autonome"]).default("normal"),
      source: z.string().default("iphone"),
      conversation_id: z.string().optional(),
    })
    .safeParse(await c.req.json().catch(() => ({})));
  if (!body.success) {
    return c.json({ error: { message: "input is required", code: "BAD_REQUEST" } }, 400);
  }

  const taskId = id("tsk");
  const ts = now();
  const title = body.data.input.slice(0, 60);
  db.prepare(
    `INSERT INTO tasks (id, conversation_id, title, input, mode, status, created_at, updated_at)
     VALUES (?, ?, ?, ?, ?, 'queued', ?, ?)`
  ).run(taskId, body.data.conversation_id ?? null, title, body.data.input, body.data.mode, ts, ts);

  appendAudit({
    trace_id: taskId,
    event_type: "task.created",
    actor_type: "user",
    actor_id: body.data.source,
    task_id: taskId,
    payload: { mode: body.data.mode },
  });

  runTaskPipeline(taskId);

  return c.json({ data: db.query("SELECT * FROM tasks WHERE id = ?").get(taskId) as Task }, 201);
});

tasksRouter.get("/:id", (c) => {
  const task = db.query("SELECT * FROM tasks WHERE id = ?").get(c.req.param("id"));
  if (!task) return c.json({ error: { message: "Task not found", code: "NOT_FOUND" } }, 404);
  const messages = db.query("SELECT * FROM messages WHERE task_id = ? ORDER BY created_at ASC").all(c.req.param("id"));
  const approvals = db.query("SELECT * FROM approvals WHERE task_id = ? ORDER BY created_at ASC").all(c.req.param("id"));
  return c.json({ data: { task, messages, approvals } });
});

tasksRouter.post("/:id/cancel", (c) => {
  const task = db.query("SELECT * FROM tasks WHERE id = ?").get(c.req.param("id")) as Task | null;
  if (!task) return c.json({ error: { message: "Task not found", code: "NOT_FOUND" } }, 404);
  if (task.status === "completed" || task.status === "failed") {
    return c.json({ error: { message: "Task already finished", code: "TASK_FINISHED" } }, 409);
  }
  db.prepare("UPDATE tasks SET status = 'cancelled', updated_at = ?, completed_at = ? WHERE id = ?").run(
    now(),
    now(),
    task.id
  );
  appendAudit({
    trace_id: task.id,
    event_type: "task.cancelled",
    actor_type: "user",
    actor_id: "iphone",
    task_id: task.id,
  });
  return c.json({ data: db.query("SELECT * FROM tasks WHERE id = ?").get(task.id) });
});

export { tasksRouter };
