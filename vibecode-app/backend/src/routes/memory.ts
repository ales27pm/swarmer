import { Hono } from "hono";
import { z } from "zod";
import { db, id, now } from "../db";
import { appendAudit } from "../lib/audit";

// Memory Service — MVP uses SQL LIKE search; embeddings/FAISS arrive in Phase 5.
const memoryRouter = new Hono();

memoryRouter.get("/", (c) => {
  const rows = db
    .query("SELECT * FROM memory_items ORDER BY pinned DESC, updated_at DESC LIMIT 200")
    .all();
  return c.json({ data: rows });
});

memoryRouter.post("/search", async (c) => {
  const body = z
    .object({ query: z.string().min(1), scope: z.string().optional(), kind: z.string().optional() })
    .safeParse(await c.req.json().catch(() => ({})));
  if (!body.success) {
    return c.json({ error: { message: "query is required", code: "BAD_REQUEST" } }, 400);
  }
  const { query, scope, kind } = body.data;
  const terms = query.toLowerCase().split(/\s+/).filter(Boolean);
  const rows = db
    .query("SELECT * FROM memory_items ORDER BY pinned DESC, updated_at DESC LIMIT 500")
    .all()
    .filter((m: any) => (scope ? m.scope === scope : true) && (kind ? m.kind === kind : true))
    .map((m: any) => {
      const hay = `${m.content} ${m.summary ?? ""}`.toLowerCase();
      const score = terms.reduce((acc, t) => acc + (hay.includes(t) ? 1 : 0), 0) / Math.max(terms.length, 1);
      return { ...m, score };
    })
    .filter((m: any) => m.score > 0)
    .sort((a: any, b: any) => b.score - a.score)
    .slice(0, 50);
  return c.json({ data: rows });
});

memoryRouter.post("/remember", async (c) => {
  const body = z
    .object({
      content: z.string().min(1),
      summary: z.string().optional(),
      scope: z.string().default("général"),
      kind: z.string().default("fait"),
      pinned: z.boolean().default(false),
    })
    .safeParse(await c.req.json().catch(() => ({})));
  if (!body.success) {
    return c.json({ error: { message: "content is required", code: "BAD_REQUEST" } }, 400);
  }
  const memId = id("mem");
  const ts = now();
  db.prepare(
    `INSERT INTO memory_items (id, scope, kind, content, summary, pinned, created_at, updated_at)
     VALUES (?, ?, ?, ?, ?, ?, ?, ?)`
  ).run(memId, body.data.scope, body.data.kind, body.data.content, body.data.summary ?? null, body.data.pinned ? 1 : 0, ts, ts);
  appendAudit({
    trace_id: id("trc"),
    event_type: "memory.remembered",
    actor_type: "user",
    actor_id: "iphone",
    payload: { memory_id: memId, scope: body.data.scope },
  });
  return c.json({ data: db.query("SELECT * FROM memory_items WHERE id = ?").get(memId) }, 201);
});

memoryRouter.patch("/:id", async (c) => {
  const body = z
    .object({ pinned: z.boolean().optional(), content: z.string().optional() })
    .safeParse(await c.req.json().catch(() => ({})));
  if (!body.success) return c.json({ error: { message: "invalid body", code: "BAD_REQUEST" } }, 400);
  const existing = db.query("SELECT * FROM memory_items WHERE id = ?").get(c.req.param("id")) as any;
  if (!existing) return c.json({ error: { message: "Memory not found", code: "NOT_FOUND" } }, 404);
  db.prepare("UPDATE memory_items SET pinned = ?, content = ?, updated_at = ? WHERE id = ?").run(
    body.data.pinned === undefined ? existing.pinned : body.data.pinned ? 1 : 0,
    body.data.content ?? existing.content,
    now(),
    existing.id
  );
  return c.json({ data: db.query("SELECT * FROM memory_items WHERE id = ?").get(existing.id) });
});

memoryRouter.delete("/:id", (c) => {
  const existing = db.query("SELECT id FROM memory_items WHERE id = ?").get(c.req.param("id"));
  if (!existing) return c.json({ error: { message: "Memory not found", code: "NOT_FOUND" } }, 404);
  db.prepare("DELETE FROM memory_items WHERE id = ?").run(c.req.param("id"));
  return c.json({ data: { deleted: true } });
});

export { memoryRouter };
