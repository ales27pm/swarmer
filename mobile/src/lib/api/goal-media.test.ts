import { beforeEach, describe, expect, it, jest } from "@jest/globals";
import { fetch } from "expo/fetch";
import * as SecureStore from "expo-secure-store";
import { getGoalMediaArtifacts, getGoalMediaBytes, ConnectionChangedError } from "./client";
import { MAX_GOAL_MEDIA_BYTES, parseGoalMedia, verifyGoalMediaBytes } from "./goal-media";
import { mediaFixture, mediaGoalId } from "@/testing/goal-media-fixtures";

jest.mock("expo-secure-store", () => ({ getItemAsync: jest.fn(), setItemAsync: jest.fn(), deleteItemAsync: jest.fn() }));
jest.mock("expo/fetch", () => ({ fetch: jest.fn() }));
jest.mock("@/lib/state/mutation-outbox", () => ({ mutationOutbox: {} }));
jest.mock("@/lib/state/replica", () => ({ applyBootstrap: jest.fn(), upsertEvent: jest.fn() }));
const { artifact, bytes } = mediaFixture();
function binary(chunks = [bytes], headers: Record<string, string> = { "Content-Type": "image/png", "Content-Length": String(bytes.length) }) {
  let index = 0;
  const reader = { read: jest.fn(async () => index < chunks.length ? { done: false, value: chunks[index++] } : { done: true }),
    cancel: jest.fn(async () => undefined), releaseLock: jest.fn() };
  return { reader, response: { ok: true, headers: { get: (key: string) => headers[key] ?? null }, body: { getReader: () => reader } } as never };
}
let connection = { baseUrl: "https://control.example", token: "fixture-token" };
beforeEach(() => {
  jest.clearAllMocks(); connection = { baseUrl: "https://control.example", token: "fixture-token" };
  jest.mocked(SecureStore.getItemAsync).mockImplementation(async (key) => key === "mongars.connection.v1" ? JSON.stringify(connection) : null);
});
describe("goal media contract", () => {
  it("accepts only bounded PNG/WAV artifacts for the expected goal", () => {
    expect(parseGoalMedia({ artifacts: [artifact, mediaFixture(true).artifact] }, mediaGoalId)).toHaveLength(2);
    verifyGoalMediaBytes(bytes, artifact); verifyGoalMediaBytes(mediaFixture(true).bytes, mediaFixture(true).artifact);
  });
  it.each([
    { artifact_id: "https://other.example/private" }, { job_id: "../job" }, { goal_id: `goal_${"d".repeat(32)}` },
    { sha256: "bad" }, { size_bytes: MAX_GOAL_MEDIA_BYTES + 1 }, { size_bytes: 0 }, { media_type: "image/svg+xml" },
    { width: "512" }, { width: 100000 }, { duration_ms: 5 }, { url: "https://other.example/" },
  ])("rejects malformed reference %p", (patch) => expect(() => parseGoalMedia({ artifacts: [{ ...artifact, ...patch }] }, mediaGoalId)).toThrow());
  it("rejects duplicate identities, too many entries and missing fields", () => {
    expect(() => parseGoalMedia({ artifacts: [artifact, artifact] }, mediaGoalId)).toThrow();
    expect(() => parseGoalMedia({ artifacts: Array(101).fill(artifact) }, mediaGoalId)).toThrow();
    const { channels: _, ...missing } = artifact;
    expect(() => parseGoalMedia({ artifacts: [missing] }, mediaGoalId)).toThrow();
  });
  it("detects changed content and metadata even if the HTTP request succeeds", () => {
    expect(() => verifyGoalMediaBytes(bytes.slice(1), artifact)).toThrow();
    expect(() => verifyGoalMediaBytes(bytes, { ...artifact, sha256: "0".repeat(64) })).toThrow();
    expect(() => verifyGoalMediaBytes(bytes, { ...artifact, width: 768 })).toThrow();
  });
  it.each([{ channels: 2 }, { sample_rate: 44100 }, { duration_ms: 30001 }, { width: 512 }])("rejects invalid WAV metadata %p", (patch) => {
    expect(() => parseGoalMedia({ artifacts: [{ ...mediaFixture(true).artifact, ...patch }] }, mediaGoalId)).toThrow();
  });
});
describe("authenticated media transport", () => {
  it("authenticates metadata and bytes without including a token in either URL", async () => {
    jest.mocked(fetch).mockResolvedValueOnce({ ok: true, json: async () => ({ artifacts: [artifact] }) } as never);
    expect(await getGoalMediaArtifacts(mediaGoalId)).toEqual([artifact]);
    const stream = binary(); jest.mocked(fetch).mockResolvedValueOnce(stream.response);
    expect(await getGoalMediaBytes(artifact)).toEqual(bytes);
    for (const [url, options] of jest.mocked(fetch).mock.calls) {
      expect(String(url)).toMatch(/^https:\/\/control.example\/goals\/goal_[a-f0-9]+\/media/);
      expect(String(url)).not.toContain(connection.token);
      expect(options).toMatchObject({ redirect: "error", credentials: "omit", headers: { Authorization: `Bearer ${connection.token}` } });
    }
    expect(stream.reader.releaseLock).toHaveBeenCalled();
  });
  it("cancels reading an oversized stream even without Content-Length", async () => {
    const stream = binary([bytes, new Uint8Array([1])], { "Content-Type": "image/png" });
    jest.mocked(fetch).mockResolvedValue(stream.response);
    await expect(getGoalMediaBytes(artifact)).rejects.toThrow(/taille/);
    expect(stream.reader.cancel).toHaveBeenCalled();
  });
  it.each([
    { "Content-Type": "text/html", "Content-Length": "24" },
    { "Content-Type": "image/png", "Content-Length": "1000000000" },
  ])("rejects an unexpected representation before buffering %p", async (headers) => {
    const stream = binary([bytes], headers); jest.mocked(fetch).mockResolvedValue(stream.response);
    await expect(getGoalMediaBytes(artifact)).rejects.toThrow(); expect(stream.reader.read).not.toHaveBeenCalled();
    expect(jest.mocked(fetch).mock.calls[0][1]?.signal?.aborted).toBe(true);
  });
  it("rejects changed pairing after the binary download", async () => {
    jest.mocked(fetch).mockImplementation(async () => { connection = { ...connection, token: "new-token" }; return binary().response; });
    await expect(getGoalMediaBytes(artifact)).rejects.toBeInstanceOf(ConnectionChangedError);
  });
  it("rejects a cancelled or stale view before sending a request", async () => {
    const abort = new AbortController(); abort.abort();
    await expect(getGoalMediaBytes(artifact, () => true, abort.signal)).rejects.toThrow();
    await expect(getGoalMediaArtifacts(mediaGoalId, () => false)).rejects.toThrow();
    expect(fetch).not.toHaveBeenCalled();
  });
  it("rejects corrupted complete bytes and a partial stream", async () => {
    jest.mocked(fetch).mockResolvedValueOnce(binary([new Uint8Array(bytes.length)]).response);
    await expect(getGoalMediaBytes(artifact)).rejects.toThrow();
    jest.mocked(fetch).mockResolvedValueOnce(binary([bytes.slice(1)]).response);
    await expect(getGoalMediaBytes(artifact)).rejects.toThrow(/incomplet/);
  });
});
