import { beforeEach, describe, expect, it, jest } from "@jest/globals";
import { fetch } from "expo/fetch";
import * as SecureStore from "expo-secure-store";
import { parseMemoryUsagePage } from "@/lib/api/memory-usage";
import { ConnectionChangedError, getGoalMemoryUsage } from "@/lib/api/client";
import { memoryUsageEntry, memoryUsageItem, memoryUsagePage } from "@/testing/memory-usage-fixtures";
jest.mock("expo-secure-store", () => ({ deleteItemAsync: jest.fn(), getItemAsync: jest.fn(), setItemAsync: jest.fn() }));
jest.mock("expo/fetch", () => ({ fetch: jest.fn() }));
jest.mock("@/lib/state/mutation-outbox", () => ({ mutationOutbox: {} }));
jest.mock("@/lib/state/replica", () => ({ applyBootstrap: jest.fn(), upsertEvent: jest.fn() }));
const request = jest.mocked(fetch);
const parse = (value: unknown) => parseMemoryUsagePage(value, "goal_1");
const response = (value: unknown) => ({ ok: true, status: 200, json: async () => value }) as never;
function deferred<T>() { let resolve!: (value: T) => void; const promise = new Promise<T>(next => { resolve = next; }); return { resolve, promise }; }

describe("passive memory receipt contract", () => {
  it("preserves unknown historical source versions and distinct stages without inventing evidence", () => {
    const entries = [memoryUsageEntry(), memoryUsageEntry({ id: "retrieval:q_1", evidence_stage: "retrieved", model_call_id: null, context_id: null,
      retrieval: { mode: "lexical", reason: "fallback", provider_fingerprint: null } }),
    memoryUsageEntry({ id: "worker_job:job_1", evidence_stage: "included_in_worker_job", worker_job_id: "job_1", node_id: "node_1", model_call_id: null, context_id: null, model_id: null })];
    expect(parse(memoryUsagePage(entries))).toEqual(memoryUsagePage(entries));
    expect(parse(memoryUsagePage([], { project_id: null }))).toEqual(memoryUsagePage([], { project_id: null }));
  });
  it.each([
    { goal_id: "goal_other" }, { schema_version: "2.0" }, { project_id: "../other" }, { history_coverage: "all_memory" },
    { availability: "no_records" }, { next_cursor: "unsafe?" }, { next_cursor: "x".repeat(513) }, { raw_context: {} },
    { entries: Array(51).fill(memoryUsageEntry()) }, { entries: [memoryUsageEntry(), memoryUsageEntry()] },
    { observed_at: "2026-09-28T22:00:00" }, { current_conversation_revision: -1 }, { current_conversation_revision: Number.MAX_SAFE_INTEGER + 1 },
  ])("rejects invalid page %p", patch => expect(() => parse({ ...memoryUsagePage(), ...patch })).toThrow());
  it.each([
    { context_id: null }, { model_call_id: "gmc_other" }, { worker_job_id: "job_1" }, { evidence_stage: "assimilated" },
    { retrieval: { mode: "vector", reason: null, provider_fingerprint: null } }, { status: "x".repeat(51) },
    { items: [memoryUsageItem({ source_state: "redacted" })] }, { items: [memoryUsageItem({ source_state: "missing" })] },
    { items: [memoryUsageItem({ summary: "🦉".repeat(801) })] }, { items: [memoryUsageItem({ summary: "bad\ud800" })] },
    { items: [memoryUsageItem(), memoryUsageItem()] }, { items: Array(101).fill(memoryUsageItem()) },
    { omitted_item_count: -1 }, { raw_query: "private" },
  ])("rejects invalid or misleading entry %p", patch => expect(() => parse(memoryUsagePage([{ ...memoryUsageEntry(), ...patch } as never]))).toThrow());
  it("requires complete nullable fields and rejects excess UTF-8 bytes", () => {
    const page = memoryUsagePage();
    for (const missing of Object.keys(page)) expect(() => parse(Object.fromEntries(Object.entries(page).filter(([key]) => key !== missing)))).toThrow();
    const items = Array.from({ length: 100 }, (_, i) => memoryUsageItem({ id: `mem_${i}`, summary: "🦉".repeat(800) }));
    expect(() => parse(memoryUsagePage([memoryUsageEntry({ items })]))).toThrow();
    expect(parse(memoryUsagePage([memoryUsageEntry({ items: [memoryUsageItem({ summary: "🦉".repeat(800) })] })])).entries[0].items[0].summary).toHaveLength(1600);
  });
});

describe("passive authenticated memory transport", () => {
  let connection: { baseUrl: string; token: string };
  beforeEach(() => {
    jest.resetAllMocks(); connection = { baseUrl: "https://control.example", token: "paired-token" };
    jest.mocked(SecureStore.getItemAsync).mockImplementation(async key => key === "mongars.connection.v1" ? JSON.stringify(connection) : null);
    request.mockResolvedValue(response(memoryUsagePage()));
  });
  it("only GETs the requested goal, without the active memory-context POST", async () => {
    await expect(getGoalMemoryUsage("goal_1", "opaque_cursor")).resolves.toEqual(memoryUsagePage());
    expect(request).toHaveBeenCalledTimes(1);
    expect(request).toHaveBeenCalledWith("https://control.example/goals/goal_1/memory-usage?cursor=opaque_cursor", { headers: { Authorization: "Bearer paired-token" } });
  });
  it.each(["origin", "token", "screen"] as const)("discards late receipts after %s changes", async reason => {
    const pending = deferred<unknown>(), started = deferred<void>(); let active = true;
    request.mockResolvedValue({ ok: true, status: 200, json: () => { started.resolve(); return pending.promise; } } as never);
    const promise = getGoalMemoryUsage("goal_1", undefined, () => active); await started.promise;
    if (reason === "origin") connection.baseUrl = "https://other.example";
    if (reason === "token") connection.token = "other-token";
    if (reason === "screen") active = false;
    pending.resolve(memoryUsagePage()); await expect(promise).rejects.toBeInstanceOf(ConnectionChangedError);
  });
  it("rejects wrong project responses and malformed arguments", async () => {
    request.mockResolvedValue(response(memoryUsagePage([], { goal_id: "goal_other" })));
    await expect(getGoalMemoryUsage("goal_1")).rejects.toThrow(); request.mockClear();
    await expect(getGoalMemoryUsage("../other")).rejects.toThrow();
    await expect(getGoalMemoryUsage("goal_1", "x&limit=50")).rejects.toThrow();
    await expect(getGoalMemoryUsage("goal_1", undefined, () => false)).rejects.toBeInstanceOf(ConnectionChangedError);
    expect(request).not.toHaveBeenCalled();
  });
});
