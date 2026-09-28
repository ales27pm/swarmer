import { beforeEach, describe, expect, it, jest } from "@jest/globals";
import { fetch } from "expo/fetch";
import * as SecureStore from "expo-secure-store";
import {
  ConnectionChangedError, commandWebsiteProject, createWebsiteProject,
  getWebsiteProject, listWebsiteProjects, prepareWebsitePublication,
  previewWebsiteProject, publishWebsiteProject,
} from "./client";
import {
  parseWebsiteApproval, parseWebsiteCapabilities, parseWebsitePreview,
  parseWebsiteProject,
} from "./website-projects";

jest.mock("expo-secure-store", () => ({ deleteItemAsync: jest.fn(), getItemAsync: jest.fn(), setItemAsync: jest.fn() }));
jest.mock("expo/fetch", () => ({ fetch: jest.fn() }));
jest.mock("@/lib/state/mutation-outbox", () => ({ mutationOutbox: {} }));
jest.mock("@/lib/state/replica", () => ({ applyBootstrap: jest.fn(), upsertEvent: jest.fn() }));

const digest = "a".repeat(64);
const token = "review-token_".repeat(4);
const palette = { id: "paper-ink", name: "Papier", background: "#F6F3EC", surface: "#FFFFFF", text: "#20242C", muted: "#535E70", accent: "#3449A0", accent_text: "#FFFFFF" };
const review = { expected_version: 5, build_digest: digest };
const preview = { path: `/website-previews/${token}/index.html`, build_digest: digest, expires_in_seconds: 300 };
const approval = { expected_version: 6, build_digest: digest, approval_token: token, target: "https://published.example/releases", expires_in_seconds: 300 };
const publish = { expected_version: 6, build_digest: digest, approval_token: token, confirm_publication: true as const };
const response = (value: unknown) => ({ ok: true, status: 200, json: async () => value }) as never;

function project() {
  return {
    schema_version: "1.0", id: "web-project-1", version: 5,
    source_url: "https://company.example/", objective: "Présenter les services clairement.",
    status: "preview_ready", error: null, capture: null, branding: null, publication: null,
    build: {
      digest, palette, file_count: 5,
      migration: { inventory_count: 3, accounted_count: 3, unassigned_count: 0, counts: { retained: 3 }, page_map: { "https://company.example/": "index.html" } },
      strategy: { marketing_analysis: { audience: { status: "unknown" } } },
      readiness: { status: "needs_review", blockers: ["source_capture_partial"] },
    },
    events: [{ at: 1800000000, stage: "preview_ready", message: "Aperçu à vérifier." }],
  };
}

function published() {
  return { ...project(), version: 8, status: "published", publication: { status: "published", digest, url: "https://published.example/releases/r1/index.html", file_count: 2 } };
}

describe("website workflow response contracts", () => {
  it("preserves partial-capture blockers without relabeling them publication readiness", () => {
    const parsed = parseWebsiteProject(project(), "web-project-1");
    expect(parsed.status).toBe("preview_ready");
    expect(parsed.build?.readiness.blockers).toEqual(["source_capture_partial"]);
    expect(parsed.build?.strategy.marketing_analysis.audience).toEqual({ status: "unknown" });
  });

  it("strips unrelated internal payloads and accepts a missing optional direction", () => {
    const parsed = parseWebsiteProject({ ...project(), owner: "private-owner", capture_dir: "/private/capture", approval: { token: "private-approval" } });
    expect(JSON.stringify(parsed)).not.toContain("private-");
    expect(parsed.build?.palette.id).toBe("paper-ink");
  });

  it.each([
    "https://attacker.example/index.html", "//attacker.example/index.html",
    `/website-previews/${token}/../private.json`, `/website-previews/${token}/%2e%2e/private.json`,
    `/website-previews/${token}/index.html?redirect=https://attacker.example`,
    `/website-previews/${token}/index.html#other`, "/website-previews/short/index.html",
    `/other/${token}/index.html`, `/website-previews/${token}/index.html/extra`,
  ])("rejects forged preview path %s", (path) => {
    expect(() => parseWebsitePreview({ ...preview, path })).toThrow();
  });

  it.each([
    { approval_token: "short" }, { approval_token: `${token}/escape` },
    { build_digest: "not-a-digest" }, { build_digest: "a".repeat(63) },
    { expected_version: -1 }, { expected_version: 1.5 }, { expires_in_seconds: Infinity },
    { target: "javascript:alert(1)" }, { target: "https://user:secret@example.org/" },
  ])("rejects invalid publication authority %j", (patch) => {
    expect(() => parseWebsiteApproval({ ...approval, ...patch })).toThrow();
  });

  it("rejects a foreign project, unknown phase and malformed publication digest", () => {
    expect(() => parseWebsiteProject(project(), "other-project")).toThrow();
    expect(() => parseWebsiteProject({ ...project(), status: "automatically_approved" })).toThrow();
    expect(() => parseWebsiteProject({ ...published(), publication: { ...published().publication, digest: "forged" } })).toThrow();
    expect(() => parseWebsiteProject({ ...project(), version: -1 })).toThrow();
  });

  it("distinguishes unavailable integrations and rejects non-color palette input", () => {
    const capabilities = { schema_version: "1.0", capture: true, browser_configured: false, browser_note: "Navigateur absent.", branding_configured: false, publication_configured: false, publication_target: null, palettes: [palette] };
    expect(parseWebsiteCapabilities(capabilities)).toMatchObject({ branding_configured: false, publication_configured: false });
    expect(() => parseWebsiteCapabilities({ ...capabilities, palettes: [{ ...palette, accent: "url(x)" }] })).toThrow();
  });
});

describe("website transport authority and connection fencing", () => {
  const request = jest.mocked(fetch);
  let connection: { baseUrl: string; token: string };
  beforeEach(() => {
    jest.resetAllMocks();
    connection = { baseUrl: "https://control.example", token: "paired-fixture-token" };
    jest.mocked(SecureStore.getItemAsync).mockImplementation(async (key) => key === "mongars.connection.v1" ? JSON.stringify(connection) : null);
  });

  it("loads project lists read-only and rejects a foreign detail response", async () => {
    request.mockResolvedValueOnce(response([project()])).mockResolvedValueOnce(response({ ...project(), id: "foreign-project" }));
    await expect(listWebsiteProjects()).resolves.toHaveLength(1);
    expect(request.mock.calls[0][0]).toBe("https://control.example/website-projects");
    expect(request.mock.calls[0][1]?.method ?? "GET").toBe("GET");
    await expect(getWebsiteProject("web-project-1")).rejects.toThrow();
  });

  it("constructs previews only on the paired origin and binds their build digest", async () => {
    request.mockResolvedValueOnce(response(preview));
    await expect(previewWebsiteProject("web-project-1", review)).resolves.toMatchObject({ url: "https://control.example" + preview.path, build_digest: digest });
    expect(JSON.parse(request.mock.calls[0][1]?.body as string)).toEqual(review);
    request.mockResolvedValueOnce(response({ ...preview, build_digest: "b".repeat(64) }));
    await expect(previewWebsiteProject("web-project-1", review)).rejects.toThrow(/changé/);
  });

  it("rejects a forged preview target before returning a URL to open", async () => {
    request.mockResolvedValueOnce(response({ ...preview, path: "https://attacker.example" }));
    await expect(previewWebsiteProject("web-project-1", review)).rejects.toThrow();
    expect(request).toHaveBeenCalledTimes(1);
  });

  it.each([
    { build_digest: "b".repeat(64) }, { expected_version: 5 }, { expected_version: 7 },
  ])("rejects an approval that is not bound to the reviewed digest and next version %j", async (patch) => {
    request.mockResolvedValueOnce(response({ ...approval, ...patch }));
    await expect(prepareWebsitePublication("web-project-1", review)).rejects.toThrow(/version/);
    expect(request).toHaveBeenCalledTimes(1);
  });

  it("keeps approval and publication as separate explicit requests", async () => {
    request.mockResolvedValueOnce(response(approval));
    await expect(prepareWebsitePublication("web-project-1", review)).resolves.toMatchObject(approval);
    expect(request).toHaveBeenCalledTimes(1);
    expect(request.mock.calls[0][0]).toBe("https://control.example/website-projects/web-project-1/publication-review");
    request.mockResolvedValueOnce(response(published()));
    await expect(publishWebsiteProject("web-project-1", publish)).resolves.toMatchObject({ status: "published" });
    expect(request.mock.calls[1][0]).toBe("https://control.example/website-projects/web-project-1/publish");
    expect(JSON.parse(request.mock.calls[1][1]?.body as string)).toEqual(publish);
  });

  it("rejects a well-formed receipt for a different published build", async () => {
    request.mockResolvedValueOnce(response({ ...published(), build: { ...project().build, digest: "b".repeat(64) }, publication: { ...published().publication, digest: "b".repeat(64) } }));
    await expect(publishWebsiteProject("web-project-1", publish)).rejects.toThrow(/version|publication/);
    expect(request).toHaveBeenCalledTimes(1);
  });

  it.each(["origin", "token"])("rejects stale read results after paired %s changes", async (kind) => {
    request.mockImplementationOnce(async () => {
      connection = kind === "origin" ? { ...connection, baseUrl: "https://other.example" } : { ...connection, token: "changed" };
      return response(project());
    });
    await expect(getWebsiteProject("web-project-1")).rejects.toBeInstanceOf(ConnectionChangedError);
    expect(request).toHaveBeenCalledTimes(1);
  });

  it("marks an in-flight publication uncertain when pairing changes and never repeats it", async () => {
    request.mockImplementationOnce(async () => { connection.token = "changed"; return response(published()); });
    await expect(publishWebsiteProject("web-project-1", publish)).rejects.toMatchObject({ name: "ConnectionChangedError", outcomeUnknown: true });
    expect(request).toHaveBeenCalledTimes(1);
  });

  it("does not dispatch any mutation when the screen fence is already stale", async () => {
    await expect(commandWebsiteProject("web-project-1", { request_id: "command-fixture", expected_version: 5, action: "build", palette_id: "paper-ink", direction_id: "editorial" }, () => false)).rejects.toBeInstanceOf(ConnectionChangedError);
    expect(request).not.toHaveBeenCalled();
  });

  it.each(["create", "command", "publish"])("never retries an uncertain %s implicitly", async (operation) => {
    request.mockRejectedValueOnce(new Error("response lost")).mockResolvedValueOnce(response(published()));
    const promise = operation === "create" ? createWebsiteProject({ request_id: "create-fixture", source_url: "https://company.example", objective: "Refonte" })
      : operation === "command" ? commandWebsiteProject("web-project-1", { request_id: "command-fixture", expected_version: 5, action: "capture" })
        : publishWebsiteProject("web-project-1", publish);
    await expect(promise).rejects.toThrow("response lost");
    expect(request).toHaveBeenCalledTimes(1);
  });
});
