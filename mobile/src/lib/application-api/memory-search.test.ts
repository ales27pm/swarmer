import { beforeEach, describe, expect, it, jest } from "@jest/globals";
import * as SecureStore from "expo-secure-store";
import { fetch } from "expo/fetch";
import { applicationApi } from "./index";
import { searchMemory } from "./server";
import ordinary from "@/testing/symbolic-http-ordinary.json";

jest.mock("expo-secure-store", () => ({ getItemAsync: jest.fn(), setItemAsync: jest.fn(), deleteItemAsync: jest.fn() }));
jest.mock("expo/fetch", () => ({ fetch: jest.fn() }));
jest.mock("@/lib/state/mutation-outbox", () => ({ mutationOutbox: { pendingCount: jest.fn() } }));
jest.mock("@/lib/state/replica", () => ({ applyBootstrap: jest.fn(), upsertEvent: jest.fn(), localSwarmSnapshot: jest.fn() }));

const options = { scope: "project:symbolic-acceptance", kind: "fact", limit: 2,
  symbolic: { catalogs: [{ namespace: "software", scheme_id: "engineering" }] } };
const request = jest.mocked(fetch);

describe("memory search through the application API and real HTTP client", () => {
  beforeEach(() => {
    jest.clearAllMocks();
    jest.mocked(SecureStore.getItemAsync).mockImplementation(async (key) => key === "mongars.connection.v1"
      ? JSON.stringify({ baseUrl: "https://control.example", token: "test-device-token" }) : null);
    request.mockResolvedValue({ ok: true, status: 200, json: async () => ordinary } as never);
  });

  it("sends selected catalogs and scope, then preserves source-qualified evidence across both API layers", async () => {
    expect(await searchMemory("horloge silencieuse", options)).toEqual(ordinary);
    expect(request).toHaveBeenCalledTimes(1);
    const [url, init] = request.mock.calls[0];
    expect(url).toBe("https://control.example/memory/search");
    expect(init?.method).toBe("POST");
    expect(JSON.parse(init?.body as string)).toEqual({ query: "horloge silencieuse", ...options });
    const exposed = await applicationApi.execute("memory.search", { query: "horloge silencieuse", ...options });
    expect(exposed.data).toEqual(ordinary);
  });

  it("keeps old requests compatible and permits valid Unicode length through the registry", async () => {
    await searchMemory("🚀".repeat(1001));
    expect(JSON.parse(request.mock.calls[0][1]?.body as string)).toEqual({ query: "🚀".repeat(1001) });
  });

  it.each([
    { query: "cache", symbolic: options.symbolic },
    { query: "cache", ...options, scope: "global" },
    { query: "cache", ...options, symbolic: { catalogs: [] } },
    { query: "cache", ...options, symbolic: { catalogs: [...options.symbolic.catalogs, ...options.symbolic.catalogs] } },
    { query: "cache", ...options, grants_authority: true },
  ])("rejects invalid opt-in before the network: %j", async (input) => {
    await expect(applicationApi.execute("memory.search", input)).rejects.toThrow();
    expect(request).not.toHaveBeenCalled();
  });

  it("does not return a successful result with missing applicability after credential redaction", async () => {
    const rows = JSON.parse(JSON.stringify(ordinary));
    rows[0].symbolic_evidence[0].proposal.claim.applicability = { credentials: false };
    request.mockResolvedValue({ ok: true, status: 200, json: async () => rows } as never);
    await expect(applicationApi.execute("memory.search", { query: "silent clock", ...options })).rejects.toMatchObject({ code: "invalid_response" });
    expect(request).toHaveBeenCalledTimes(1);
  });
});
