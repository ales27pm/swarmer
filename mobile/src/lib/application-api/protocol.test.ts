import { createApplicationProtocol } from "./protocol";

const request = (command = "models.status", input = {}, key = "12345678-1234-1234-1234-123456789abc") => ({
  method: "POST", path: "/v1/commands", body: JSON.stringify({ command, input, idempotencyKey: key, instanceId: "testinstance" }),
});
const create = (dispatcher: Parameters<typeof createApplicationProtocol>[0]) => createApplicationProtocol(dispatcher, () => new Date(), "testinstance");
const getJob = (api: ReturnType<typeof createApplicationProtocol>, id = "job_testinstance_1") => api.handle({ method: "GET", path: `/v1/jobs/${id}`, body: "" }).body;

test("lost responses and concurrent retries execute a mutation once", async () => {
  let finish!: (value: unknown) => void;
  const execute = jest.fn(() => new Promise((resolve) => { finish = resolve; }));
  const api = create({ execute, catalog: () => ({}) });
  expect(api.handle(request()).status).toBe(202);
  expect(api.handle(request()).status).toBe(200);
  expect(execute).toHaveBeenCalledTimes(1);
  finish({ state: "ready" });
  await Promise.resolve();
  expect(getJob(api)).toMatchObject({ job: { state: "succeeded", result: { state: "ready" } } });
  expect(api.handle(request()).status).toBe(200);
  expect(execute).toHaveBeenCalledTimes(1);
});

test("same key with other arguments conflicts but object order is irrelevant", () => {
  const execute = jest.fn(async () => null);
  const api = create({ execute, catalog: () => ({}) });
  expect(api.handle(request("tasks.create", { a: 1, b: 2 })).status).toBe(202);
  expect(api.handle(request("tasks.create", { b: 2, a: 1 })).status).toBe(200);
  expect(api.handle(request("tasks.create", { a: 2 })).status).toBe(409);
  expect(execute).toHaveBeenCalledTimes(1);
});

test("background rejects new work and foreground preserves receipts", async () => {
  const execute = jest.fn(async () => "done");
  const api = create({ execute, catalog: () => ({}) });
  api.handle(request());
  api.setActive(false);
  expect(api.handle(request()).status).toBe(503);
  await Promise.resolve();
  api.setActive(true);
  expect(api.handle(request()).status).toBe(200);
  expect(execute).toHaveBeenCalledTimes(1);
});

test("errors never expose tokens or invite an automatic retry", async () => {
  const execute = jest.fn(async () => { throw new Error("Bearer secret https://example?ticket=credential"); });
  const api = create({ execute, catalog: () => ({}) });
  api.handle(request());
  await Promise.resolve();
  expect(getJob(api)).toMatchObject({ job: { state: "uncertain", error: { code: "outcome_unknown" } } });
  expect(JSON.stringify(getJob(api))).not.toContain("secret");
  api.handle(request());
  expect(execute).toHaveBeenCalledTimes(1);
});

test.each([
  ["invalid_plan", "vide ou ne respecte pas le contrat JSON"],
  ["invalid_context", "contexte du but"],
  ["context_unavailable", "contexte du plan"],
  ["generation_failed", "génération locale"],
  ["generation_truncated", "génération n’est pas complète"],
  ["model_not_ready", "modèle chargé"],
  ["stale_context", "ont changé depuis la génération"],
])("%s returns actionable static wording without forwarding exception contents", async (code, expected) => {
  const execute = jest.fn(async () => { throw Object.assign(new Error("Bearer secret private model text"), { code }); });
  const api = create({ execute, catalog: () => ({}) });
  api.handle(request("goals.plan.generate"));
  await Promise.resolve();
  expect(getJob(api)).toMatchObject({ job: { state: "failed", error: { code, message: expect.stringContaining(expected) } } });
  expect(JSON.stringify(getJob(api))).not.toContain("secret");
  expect(JSON.stringify(getJob(api))).not.toContain("private model text");
});

test("malformed envelopes and unknown routes never dispatch", () => {
  const execute = jest.fn(async () => null);
  const api = create({ execute, catalog: () => ({}) });
  expect(api.handle({ ...request(), body: "{}" }).status).toBe(400);
  expect(api.handle({ ...request(), body: "[" }).status).toBe(400);
  expect(api.handle({ ...request(), path: "/v1/eval" }).status).toBe(404);
  expect(api.handle({ method: "GET", path: "/v1/health", body: "secret" }).status).toBe(400);
  expect(execute).not.toHaveBeenCalled();
});

test("active capacity cannot be used to spawn unlimited generations", () => {
  const execute = jest.fn(() => new Promise(() => {}));
  const api = create({ execute, catalog: () => ({}) });
  for (let i = 0; i < 8; i++) expect(api.handle(request("inference.generate", {}, `request-key-0000${i}`)).status).toBe(202);
  expect(api.handle(request("inference.generate", {}, "request-key-00009")).status).toBe(429);
  expect(execute).toHaveBeenCalledTimes(8);
});

test("old mutation keys and job IDs cannot alias after a JavaScript reload", async () => {
  const execute = jest.fn(async () => null);
  const first = create({ execute, catalog: () => ({}) });
  first.handle(request());
  await Promise.resolve();
  const restarted = createApplicationProtocol({ execute, catalog: () => ({}) }, () => new Date(), "newinstance");
  expect(restarted.handle(request()).status).toBe(409);
  expect(restarted.handle({ method: "GET", path: "/v1/jobs/job_testinstance_1", body: "" }).status).toBe(404);
  expect(execute).toHaveBeenCalledTimes(1);
});

test("an oversized result does not mislabel a completed mutation as failure", async () => {
  const execute = jest.fn(async () => "漢".repeat(300_000));
  const api = create({ execute, catalog: () => ({}) });
  api.handle(request());
  await Promise.resolve();
  expect(getJob(api)).toMatchObject({ job: { state: "succeeded", resultOmitted: true } });
  expect(JSON.stringify(getJob(api)).length).toBeLessThan(1000);
});

test("an admitted background calculation permits observation, cancellation and exact receipt recovery only", async () => {
  const execute = jest.fn(async () => "done");
  const api = create({ execute, catalog: () => ({}) });
  const original = request("inference.generate");
  api.handle(original);
  api.setAccess("continuation");
  expect(api.handle({ method: "GET", path: "/v1/health", body: "" }).body).toMatchObject({ access: "continuation" });
  expect(api.handle(original)).toMatchObject({ status: 200, body: { replayed: true } });
  expect(execute).toHaveBeenCalledTimes(1);
  for (const command of ["inference.generate", "models.load", "goals.create", "iphone.photos.select"]) {
    expect(api.handle(request(command, {}, `background-${command}`))).toMatchObject({
      status: 409, body: { error: { code: "foreground_required" } },
    });
  }
  expect(execute).toHaveBeenCalledTimes(1);
  expect(api.handle(request("models.status", {}, "background-status" )).status).toBe(202);
  expect(api.handle(request("inference.cancel", {}, "background-cancel")).status).toBe(202);
  await Promise.resolve();
  expect(getJob(api)).toMatchObject({ job: { state: "succeeded" } });
  api.setAccess("inactive");
  expect(api.handle(original).status).toBe(503);
  api.setAccess("foreground");
  expect(api.handle(request("inference.generate", {}, "new-foreground-work")).status).toBe(202);
});
