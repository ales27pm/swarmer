import { beforeEach, describe, expect, it, jest } from "@jest/globals";
import { fetch } from "expo/fetch";
import * as SecureStore from "expo-secure-store";

import { parseActivityPage } from "@/lib/api/activity";
import { ConnectionChangedError, getActivity } from "@/lib/api/client";
import { activityItem, activityPage } from "@/testing/activity-fixtures";

jest.mock("expo-secure-store", () => ({ deleteItemAsync: jest.fn(), getItemAsync: jest.fn(), setItemAsync: jest.fn() }));
jest.mock("expo/fetch", () => ({ fetch: jest.fn() }));
jest.mock("@/lib/state/mutation-outbox", () => ({ mutationOutbox: {} }));
jest.mock("@/lib/state/replica", () => ({ applyBootstrap: jest.fn(), upsertEvent: jest.fn() }));
const request = jest.mocked(fetch);
const parse = (value: unknown) => parseActivityPage(value, "goal", "goal_1");
const response = (value: unknown) => ({ ok: true, status: 200, json: async () => value }) as never;
function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((next) => { resolve = next; });
  return { resolve, promise };
}

describe("activity response contract", () => {
  it("preserves nullable evidence and zero values without inventing live operations", () => {
    const value = activityPage([activityItem("project_check:revision_1:00", {
      kind: "project_check", duration_ms: 0, started_at: null, completed_at: null,
      detail: { revision_id: "revision_1", check_index: 0, file_count: 0, exit_code: 0, command: ["python", "-m", "pytest"] },
    })]);
    expect(parse(value)).toEqual(value);
    expect(parse(activityPage([])).items).toEqual([]);
  });

  it.each([
    { schema_version: "2.0" }, { schema_version: 1 }, { has_more: "false" }, { has_more: true },
    { next_cursor: "../unsafe" }, { next_cursor: "a".repeat(4097), has_more: true },
    { next_cursor: "safe_cursor", has_more: false }, { next_cursor: "safe_cursor", has_more: true, items: [] },
    { items: Array(101).fill(activityItem()) }, { items: [activityItem(), activityItem()] },
    { payload: { secret: "must not appear" } }, { scope: { type: "task", id: "goal_1", goal_run_id: null, root_task_id: null } },
    { scope: { type: "goal", id: "goal_other", goal_run_id: null, root_task_id: null } },
    { coverage: { mode: "persisted_records", live_operations: true, notice: "live" } },
  ])("rejects invalid or misleading page metadata %p", (patch) => {
    expect(() => parse({ ...activityPage(), ...patch })).toThrow(/preuves d’activité/);
  });

  it.each([
    { id: "../source" }, { kind: "raw_log" }, { status: "success" }, { duration_ms: -1 },
    { duration_ms: 1.2 }, { duration_ms: Number.MAX_SAFE_INTEGER + 1 }, { duration_ms: "100" },
    { recorded_at: "not a date" }, { completed_at: "x".repeat(65) }, { title: "" }, { title: "🧪".repeat(161) }, { title: "bad\ud800text" }, { recorded_at: "" },
    { agent_id: "a/b" }, { model_id: "x".repeat(201) }, { role: "system" }, { raw_payload: {} },
  ])("rejects invalid item metadata %p", (patch) => {
    expect(() => parse({ ...activityPage(), items: [{ ...activityItem(), ...patch }] })).toThrow();
  });

  it.each([
    { check_index: 12 }, { exit_code: -256 }, { file_count: 81 }, { command: Array(9).fill("arg") },
    { command: ["x".repeat(257)] }, { command: [1] }, { command: ["bad\0arg"] }, { stdout: "raw log" },
  ])("rejects invalid detail metadata %p", (patch) => {
    expect(() => parse(activityPage([activityItem("item", { detail: { ...activityItem().detail, ...patch } } as never)]))).toThrow();
  });

  it("requires the complete versioned metadata envelope", () => {
    const page = activityPage();
    for (const missing of Object.keys(page)) {
      expect(() => parse(Object.fromEntries(Object.entries(page).filter(([key]) => key !== missing)))).toThrow();
    }
    expect(() => parse(null)).toThrow();
    expect(() => parse([])).toThrow();
    expect(parse(activityPage([activityItem("id:1", { title: "🧪".repeat(160) })])).items[0].title).toHaveLength(320);
  });
});

describe("activity authenticated transport", () => {
  let connection: { baseUrl: string; token: string };
  beforeEach(() => {
    jest.resetAllMocks();
    connection = { baseUrl: "https://control.example", token: "paired-device-token" };
    jest.mocked(SecureStore.getItemAsync).mockImplementation(async (key) => key === "mongars.connection.v1" ? JSON.stringify(connection) : null);
    request.mockResolvedValue(response(activityPage()));
  });

  it.each(["task", "goal"] as const)("reads only the requested %s and its cursor", async (scope) => {
    const page = activityPage([], { scope: { type: scope, id: "id:1", goal_run_id: null, root_task_id: null } });
    request.mockResolvedValue(response(page));
    await expect(getActivity(scope, "id:1", "cursor_safe")).resolves.toEqual(page);
    expect(request).toHaveBeenCalledWith(`https://control.example/${scope}s/id%3A1/activity?cursor=cursor_safe`, {
      headers: { Authorization: "Bearer paired-device-token" },
    });
  });

  it.each(["origin", "token", "screen"] as const)("discards a late response after %s changes", async (reason) => {
    const pending = deferred<unknown>(); const started = deferred<void>(); let active = true;
    request.mockResolvedValue({ ok: true, status: 200, json: () => { started.resolve(); return pending.promise; } } as never);
    const promise = getActivity("goal", "goal_1", undefined, () => active);
    await started.promise;
    if (reason === "origin") connection.baseUrl = "https://other.example";
    if (reason === "token") connection.token = "replacement-token";
    if (reason === "screen") active = false;
    pending.resolve(activityPage());
    await expect(promise).rejects.toBeInstanceOf(ConnectionChangedError);
  });

  it("rejects an inactive caller and invalid identities/cursors before transport", async () => {
    await expect(getActivity("goal", "goal_1", undefined, () => false)).rejects.toBeInstanceOf(ConnectionChangedError);
    await expect(getActivity("goal", "../other")).rejects.toThrow();
    await expect(getActivity("goal", "goal_1", "x&limit=100")).rejects.toThrow();
    expect(request).not.toHaveBeenCalled();
  });

  it("checks response scope, rather than trusting a successful HTTP status", async () => {
    request.mockResolvedValue(response(activityPage([], { scope: { type: "goal", id: "goal_other", goal_run_id: null, root_task_id: null } })));
    await expect(getActivity("goal", "goal_1")).rejects.toThrow(/preuves d’activité/);
  });
});
