import { beforeEach, describe, expect, it, jest } from "@jest/globals";
import { createHash } from "node:crypto";
import { fetch } from "expo/fetch";
import * as SecureStore from "expo-secure-store";

import { ConnectionChangedError, getGoalWritingDraft } from "@/lib/api/client";
import { parseGoalWritingDraft } from "@/lib/api/writing-draft";
import { sha256 } from "@/lib/iphone-capabilities/grant";

jest.mock("expo-secure-store", () => ({
  deleteItemAsync: jest.fn(), getItemAsync: jest.fn(), setItemAsync: jest.fn(),
}));
jest.mock("expo/fetch", () => ({ fetch: jest.fn() }));
jest.mock("@/lib/state/mutation-outbox", () => ({ mutationOutbox: {} }));
jest.mock("@/lib/state/replica", () => ({ applyBootstrap: jest.fn(), upsertEvent: jest.fn() }));

const request = jest.mocked(fetch);
const getItem = jest.mocked(SecureStore.getItemAsync);
const path = "/goals/goal_1/nodes/node_text/writing-draft";
const draft = {
  schema_version: "1.0",
  content_trust: "untrusted",
  text: "  Plan Swift 🧪\nÉquipe et calendrier.\n",
  summary: "Un plan conceptuel, sans exécution.",
  goal_run_id: "goal_1",
  node_id: "node_text",
  worker_job_id: "job_text",
  sha256: "499ee0a998030c9b8a48ccd971c91601a2eb3793e4c8c54f5ad43b621b408f4d",
};
let connection: { baseUrl: string; token: string };

function response(body: unknown) {
  return { ok: true, status: 200, json: async () => body } as never;
}

function deferred<T>() {
  let resolve!: (value: T | PromiseLike<T>) => void;
  const promise = new Promise<T>((resolvePromise) => { resolve = resolvePromise; });
  return { promise, resolve };
}

function parse(value: unknown) {
  return parseGoalWritingDraft(value, "goal_1", "node_text", "job_text");
}

describe("writing draft transport", () => {
  beforeEach(() => {
    jest.resetAllMocks();
    connection = { baseUrl: "https://control.example", token: "paired-device-token" };
    getItem.mockImplementation(async (key) => (
      key === "mongars.connection.v1" ? JSON.stringify(connection) : null
    ));
    request.mockResolvedValue(response(draft));
  });

  it("authenticates one read and returns exact text bound to the expected worker job", async () => {
    await expect(getGoalWritingDraft("goal_1", "node_text", "job_text")).resolves.toEqual(draft);
    expect(request).toHaveBeenCalledTimes(1);
    expect(request).toHaveBeenCalledWith(`https://control.example${path}`, {
      headers: { Authorization: "Bearer paired-device-token" },
    });
  });

  it.each(["origin", "token"])("rejects a delayed fetch after the paired %s changes", async (changed) => {
    const started = deferred<void>();
    const pendingResponse = deferred<never>();
    request.mockImplementationOnce(() => { started.resolve(); return pendingResponse.promise; });
    const pending = getGoalWritingDraft("goal_1", "node_text", "job_text");
    await started.promise;
    connection = changed === "origin"
      ? { ...connection, baseUrl: "https://other.example" }
      : { ...connection, token: "replacement-token" };
    pendingResponse.resolve(response(draft));
    await expect(pending).rejects.toBeInstanceOf(ConnectionChangedError);
    expect(request).toHaveBeenCalledTimes(1);
  });

  it("rejects a body that finishes decoding after its screen session was replaced", async () => {
    const started = deferred<void>();
    const pendingBody = deferred<unknown>();
    let session = 1;
    request.mockResolvedValueOnce({
      ok: true,
      status: 200,
      json: () => { started.resolve(); return pendingBody.promise; },
    } as never);
    const pending = getGoalWritingDraft("goal_1", "node_text", "job_text", () => session === 1);
    await started.promise;
    session = 2;
    pendingBody.resolve(draft);
    await expect(pending).rejects.toMatchObject({ name: "ConnectionChangedError", outcomeUnknown: false });
    expect(request).toHaveBeenCalledTimes(1);
  });

  it("does not fetch for an obsolete session or unsafe contextual identifier", async () => {
    await expect(getGoalWritingDraft("goal_1", "node_text", "job_text", () => false))
      .rejects.toBeInstanceOf(ConnectionChangedError);
    for (const ids of [
      ["../goal", "node_text", "job_text"],
      ["goal_1", "node/text", "job_text"],
      ["goal_1", "node_text", ""],
    ]) {
      await expect(getGoalWritingDraft(ids[0], ids[1], ids[2])).rejects.toThrow();
    }
    expect(request).not.toHaveBeenCalled();
  });

  it("rejects a result from a replaced job even on the same goal and node", async () => {
    request.mockResolvedValueOnce(response({ ...draft, worker_job_id: "job_replacement" }));
    await expect(getGoalWritingDraft("goal_1", "node_text", "job_text")).rejects.toThrow(/brouillon/);
    expect(request).toHaveBeenCalledTimes(1);
  });
});

describe("writing draft response contract", () => {
  it("verifies an independent Unicode UTF-8 digest while preserving whitespace", () => {
    expect(parse(draft)).toEqual(draft);
    const text = "é🧪\n";
    const sha256 = "f7db6e39fa80584660aea0c27f6e55cf55769990d3cb37792c43b9ad41f944ce";
    expect(parse({ ...draft, text, sha256 }).text).toBe(text);
  });

  it.each([
    { label: "another goal", patch: { goal_run_id: "goal_other" } },
    { label: "another node", patch: { node_id: "node_other" } },
    { label: "another job", patch: { worker_job_id: "job_other" } },
    { label: "coerced schema", patch: { schema_version: 1.0 } },
    { label: "trusted content", patch: { content_trust: "trusted" } },
    { label: "unknown field", patch: { executed: true } },
    { label: "uppercase digest", patch: { sha256: "A".repeat(64) } },
    { label: "malformed digest", patch: { sha256: "a".repeat(63) } },
    { label: "digest mismatch", patch: { sha256: "a".repeat(64) } },
    { label: "blank text", patch: { text: " \t\n" } },
    { label: "NUL in text", patch: { text: "draft\0text" } },
    { label: "unpaired high surrogate", patch: { text: "draft\ud800text" } },
    { label: "unpaired low surrogate", patch: { text: "draft\udffftext" } },
    { label: "nonstring text", patch: { text: 123 } },
    { label: "blank summary", patch: { summary: " \t\n" } },
    { label: "NUL in summary", patch: { summary: "draft\0summary" } },
    { label: "invalid Unicode summary", patch: { summary: "draft\ud800summary" } },
    { label: "nonstring summary", patch: { summary: ["summary"] } },
    { label: "oversized Unicode summary", patch: { summary: "🧪".repeat(1_201) } },
  ])("rejects $label", ({ patch }) => {
    // Give malformed text a matching digest so its rejection proves text validation.
    const digest = "text" in patch && typeof patch.text === "string"
      ? { sha256: sha256(patch.text) } : {};
    expect(() => parse({ ...draft, ...patch, ...digest })).toThrow(/brouillon/);
  });

  it("requires all and only the fields in an object response", () => {
    for (const missing of Object.keys(draft)) {
      const incomplete = Object.fromEntries(Object.entries(draft).filter(([key]) => key !== missing));
      expect(() => parse(incomplete)).toThrow();
    }
    for (const value of [null, [], "draft", 1]) expect(() => parse(value)).toThrow();
  });

  it("validates contextual IDs even if malformed response IDs match them", () => {
    for (const identifier of ["../escape", "node/child", "job?other", "a".repeat(201), ""]) {
      expect(() => parseGoalWritingDraft({ ...draft, node_id: identifier }, "goal_1", identifier, "job_text"))
        .toThrow();
    }
  });

  it.each(["a", "é", "🧪"])("bounds text by exact UTF-8 bytes for %s", (character) => {
    const bytes = Buffer.byteLength(character, "utf8");
    const text = character.repeat(24_000 / bytes);
    const sha256 = createHash("sha256").update(text, "utf8").digest("hex");
    expect(parse({ ...draft, text, sha256 }).text).toBe(text);
    const oversized = text + "x";
    expect(() => parse({
      ...draft, text: oversized, sha256: createHash("sha256").update(oversized, "utf8").digest("hex"),
    })).toThrow();
  });

  it("counts 1200 summary Unicode code points without truncating astral characters", () => {
    const summary = "🧪".repeat(1_200);
    expect(parse({ ...draft, summary }).summary).toBe(summary);
  });
});
