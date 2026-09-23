import { Hono } from "hono";
import { z } from "zod";
import { db, id, now } from "../db";
import { appendAudit } from "../lib/audit";
import { runTaskPipeline } from "../lib/orchestrator";
import type { Task } from "../types";

// Chat — a user message spawns a task handled by the orchestrator.
const chatRouter = new Hono();

chatRouter.get("/conversations", (c) => {
  const rows = db
    .query(
      `SELECT c.*, (SELECT content FROM messages m WHERE m.conversation_id = c.id ORDER BY m.created_at DESC LIMIT 1) AS last_message
       FROM conversations c ORDER BY c.updated_at DESC LIMIT 50`
    )
    .all();
  return c.json({ data: rows });
});

chatRouter.get("/conversations/:id/messages", (c) => {
  const rows = db
    .query("SELECT * FROM messages WHERE conversation_id = ? ORDER BY created_at ASC LIMIT 500")
    .all(c.req.param("id"));
  return c.json({ data: rows });
});

chatRouter.post("/chat", async (c) => {
  const body = z
    .object({ content: z.string().min(1), conversation_id: z.string().optional() })
    .safeParse(await c.req.json().catch(() => ({})));
  if (!body.success) {
    return c.json({ error: { message: "content is required", code: "BAD_REQUEST" } }, 400);
  }

  const ts = now();
  let conversationId = body.data.conversation_id;
  if (!conversationId) {
    conversationId = id("cnv");
    db.prepare("INSERT INTO conversations (id, title, created_at, updated_at) VALUES (?, ?, ?, ?)").run(
      conversationId,
      body.data.content.slice(0, 48),
      ts,
      ts
    );
  }

  db.prepare(
    `INSERT INTO messages (id, conversation_id, role, content, created_at) VALUES (?, ?, 'user', ?, ?)`
  ).run(id("msg"), conversationId, body.data.content, ts);

  // Every chat message becomes a task for the orchestrator.
  const taskId = id("tsk");
  db.prepare(
    `INSERT INTO tasks (id, conversation_id, title, input, mode, status, created_at, updated_at)
     VALUES (?, ?, ?, ?, 'normal', 'queued', ?, ?)`
  ).run(taskId, conversationId, body.data.content.slice(0, 60), body.data.content, ts, ts);

  appendAudit({
    trace_id: taskId,
    event_type: "task.created",
    actor_type: "user",
    actor_id: "iphone",
    task_id: taskId,
    payload: { source: "chat" },
  });

  runTaskPipeline(taskId);

  return c.json(
    {
      data: {
        conversation_id: conversationId,
        task: db.query("SELECT * FROM tasks WHERE id = ?").get(taskId) as Task,
      },
    },
    201
  );
});

export { chatRouter };
