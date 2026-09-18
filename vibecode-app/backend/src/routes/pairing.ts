import { Hono } from "hono";
import { z } from "zod";
import { db, id, now } from "../db";
import { appendAudit } from "../lib/audit";

// Device pairing (API contracts doc §Auth). MVP: single-user local control plane.
const pairingRouter = new Hono();

const pendingCodes = new Map<string, number>(); // code -> expiry epoch ms

pairingRouter.post("/start", (c) => {
  const code = Math.floor(100000 + Math.random() * 900000).toString();
  const expiresAt = Date.now() + 5 * 60 * 1000;
  pendingCodes.set(code, expiresAt);
  appendAudit({
    trace_id: id("trc"),
    event_type: "pairing.started",
    actor_type: "system",
    actor_id: "control-plane",
  });
  return c.json({ data: { code, expires_at: new Date(expiresAt).toISOString() } });
});

pairingRouter.post("/confirm", async (c) => {
  const body = z
    .object({ code: z.string().length(6), device_id: z.string().min(1), device_name: z.string().min(1) })
    .safeParse(await c.req.json().catch(() => ({})));
  if (!body.success) {
    return c.json({ error: { message: "code, device_id and device_name are required", code: "BAD_REQUEST" } }, 400);
  }

  const expiry = pendingCodes.get(body.data.code);
  if (!expiry || expiry < Date.now()) {
    return c.json({ error: { message: "Invalid or expired pairing code", code: "PAIRING_INVALID" } }, 401);
  }
  pendingCodes.delete(body.data.code);

  const token = `mg_${crypto.randomUUID().replaceAll("-", "")}`;
  db.prepare(
    `INSERT INTO devices (id, name, token, created_at, last_seen_at) VALUES (?, ?, ?, ?, ?)
     ON CONFLICT(id) DO UPDATE SET token = excluded.token, last_seen_at = excluded.last_seen_at`
  ).run(body.data.device_id, body.data.device_name, token, now(), now());

  appendAudit({
    trace_id: id("trc"),
    event_type: "pairing.confirmed",
    actor_type: "user",
    actor_id: body.data.device_id,
  });

  return c.json({ data: { device_token: token, device_id: body.data.device_id } });
});

export { pairingRouter };
