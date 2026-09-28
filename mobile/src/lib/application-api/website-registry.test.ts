import { beforeEach, describe, expect, it, jest } from "@jest/globals";
import { AppState } from "react-native";
import * as server from "@/lib/api/client";
import { applicationApi } from "./index";
import { applicationSessions } from "./sessions";

jest.mock("expo/fetch", () => ({ fetch: jest.fn() }));
jest.mock("expo-document-picker", () => ({ getDocumentAsync: jest.fn() }));
jest.mock("@/lib/state/mutation-outbox", () => ({ mutationOutbox: { pendingCount: jest.fn() } }));
jest.mock("@/lib/state/replica", () => ({ applyBootstrap: jest.fn(), upsertEvent: jest.fn(), localSwarmSnapshot: jest.fn() }));
jest.mock("@/lib/api/client", () => ({
  ...jest.requireActual<typeof import("@/lib/api/client")>("@/lib/api/client"),
  getWebsiteCapabilities: jest.fn(), prepareWebsitePublication: jest.fn(), publishWebsiteProject: jest.fn(),
}));

describe("website commands in the application API", () => {
  beforeEach(() => { jest.clearAllMocks(); applicationSessions.clear(); });

  it("exposes real typed commands and requires foreground for publication authority", () => {
    const commands = applicationApi.catalog().commands;
    const expected = {
      "websites.capabilities": "WebsiteCapabilities", "websites.list": "WebsiteProject[]",
      "websites.get": "WebsiteProject", "websites.create": "WebsiteProject",
      "websites.command": "WebsiteProject", "websites.preview": "WebsitePreview",
      "websites.prepare-publication": "WebsiteApproval", "websites.publish": "WebsiteProject",
    };
    for (const [name, type] of Object.entries(expected)) {
      expect(commands.find((command) => command.name === name)).toMatchObject({
        available: true, inputSchema: { type: "object", additionalProperties: false },
        output: { dataType: type, contractVersion: "1.0", envelope: "ApplicationResult" },
      });
    }
    for (const name of ["websites.prepare-publication", "websites.publish"]) {
      expect(commands.find((command) => command.name === name)).toMatchObject({ effect: "mutation", requiresForeground: true });
    }
    expect(commands.find((command) => command.name === "websites.preview")).toMatchObject({ effect: "read" });
  });

  it("returns capability data through the same registered server function used by UI", async () => {
    const capabilities = { schema_version: "1.0" as const, capture: true, browser_configured: false, browser_note: "Navigateur absent.", branding_configured: false, publication_configured: false, publication_target: null, palettes: [] };
    jest.mocked(server.getWebsiteCapabilities).mockResolvedValue(capabilities);
    const result = await applicationApi.execute("websites.capabilities", {});
    expect(result.data).toEqual(capabilities);
    expect(server.getWebsiteCapabilities).toHaveBeenCalledTimes(1);
    expect(server.publishWebsiteProject).not.toHaveBeenCalled();
  });

  it.each([
    { expected_version: 0, build_digest: "a".repeat(64), approval_token: "t".repeat(40), confirm_publication: true },
    { expected_version: 6, build_digest: "bad", approval_token: "t".repeat(40), confirm_publication: true },
    { expected_version: 6, build_digest: "a".repeat(64), approval_token: "short", confirm_publication: true },
    { expected_version: 6, build_digest: "a".repeat(64), approval_token: "t".repeat(40), confirm_publication: false },
    { expected_version: 6, build_digest: "a".repeat(64), approval_token: "t".repeat(40), confirm_publication: true, destination: "/tmp/override" },
  ])("rejects invalid or expanded publication authority before transport %j", async (input) => {
    const prior = AppState.currentState;
    AppState.currentState = "active";
    try {
      await expect(applicationApi.execute("websites.publish", { id: "web-project-1", input })).rejects.toMatchObject({ code: "invalid_arguments" });
      expect(server.publishWebsiteProject).not.toHaveBeenCalled();
    } finally { AppState.currentState = prior; }
  });
});
