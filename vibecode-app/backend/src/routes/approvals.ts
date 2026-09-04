import { Hono } from "hono";
import { z } from "zod";
import { db } from "../db";
import { resolveApproval } from "../lib/orchestrator";

const approvalsRouter = new Hono();

approvalsRouter.get("/", (c) => {
  const status = c.req.query("status") ?? "pending";
  const rows = db
    .query("SELECT * FROM approvals WHERE status = ? ORDER BY created_at DESC LIMIT 100")
    .all(status);
  return c.json({ data: rows });
});

approvalsRouter.post("/:id/decision", async (c) => {
  const body = z
    .object({
      decision: z.enum(["allow_once", "allow_rule", "deny"]),
      user_note: z.string().optional(),
    })
    .safeParse(await c.req.json().catch(() => ({})));
  if (!body.success) {
    return c.json({ error: { message: "decision must be allow_once | allow_rule | deny", code: "BAD_REQUEST" } }, 400);
  }

  const updated = resolveApproval(c.req.param("id"), body.data.decision, body.data.user_note);
  if (!updated) {
    return c.json({ error: { message: "Approval not found or already decided", code: "NOT_FOUND" } }, 404);
  }
  return c.json({ data: updated });
});

export { approvalsRouter };
