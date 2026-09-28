import { act, fireEvent, render, screen, waitFor } from "@testing-library/react-native";
import { beforeEach, describe, expect, it, jest } from "@jest/globals";
import { AppState, Linking, type AppStateStatus } from "react-native";

import WebsiteScreen from "@/../app/website";
import {
  commandWebsiteProject,
  createWebsiteProject,
  getWebsiteCapabilities,
  listWebsiteProjects,
  prepareWebsitePublication,
  previewWebsiteProject,
  previewWebsiteScreenshot,
  publishWebsiteProject,
  type WebsiteCapabilities,
  type WebsiteProject,
} from "@/lib/application-api/server";
import { notifyConnectionChanged } from "@/lib/connection-events";

jest.mock("@/lib/application-api/server", () => ({
  commandWebsiteProject: jest.fn(), createWebsiteProject: jest.fn(),
  getWebsiteCapabilities: jest.fn(), listWebsiteProjects: jest.fn(),
  prepareWebsitePublication: jest.fn(), previewWebsiteProject: jest.fn(), previewWebsiteScreenshot: jest.fn(),
  publishWebsiteProject: jest.fn(),
}));

let mockAppStateListener: ((state: AppStateStatus) => void) | undefined;
jest.spyOn(AppState, "addEventListener").mockImplementation((_event, listener) => {
  mockAppStateListener = listener;
  return { remove: jest.fn() };
});
const openURL = jest.spyOn(Linking, "openURL");
const digest = "a".repeat(64);
const palette = { id: "ink", name: "Encre et ivoire", background: "#ffffff", surface: "#fafafa", text: "#111111", muted: "#555555", accent: "#004488", accent_text: "#ffffff" };
const capabilities: WebsiteCapabilities = {
  schema_version: "1.0", capture: true, browser_configured: true, browser_note: "Navigateur disponible",
  branding_configured: true, publication_configured: true, publication_target: "https://preview.example/site",
  palettes: [palette],
};
const draft: WebsiteProject = {
  schema_version: "1.0", id: "website-one", version: 1, source_url: "https://entreprise.example/",
  objective: "Moderniser notre site", status: "draft", error: null, capture: null, branding: null,
  build: null, publication: null, events: [],
};
const captured: WebsiteProject = {
  ...draft, version: 2, status: "captured",
  capture: {
    pages: 2, inventory_items: 4, rendered_pages: 2, render_sample_limit: 10,
    assets: 1, asset_status: "complete", coverage: { status: "bounded_scope_exhausted" }, render_status: [], screenshots: [],
  },
};
const built: WebsiteProject = {
  ...captured, version: 3, status: "preview_ready",
  build: {
    digest, palette, file_count: 3,
    migration: { inventory_count: 4, accounted_count: 4, unassigned_count: 0, counts: { migrated: 4 }, page_map: {} },
    strategy: { marketing_analysis: { positioning: "Des services lisibles" }, selected_direction: { layout: "editorial" } },
    readiness: { status: "ready", blockers: [] },
  },
};
const approval = {
  approval_token: "approval-token-" + "x".repeat(32), expected_version: 4, build_digest: digest,
  target: "https://preview.example/site", expires_in_seconds: 300,
};
const published: WebsiteProject = {
  ...built, version: 5, status: "published",
  publication: { status: "published", digest, url: "https://preview.example/site", file_count: 3 },
};

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (cause: Error) => void;
  const promise = new Promise<T>((settle, fail) => { resolve = settle; reject = fail; });
  return { promise, resolve, reject };
}
function press(label: string) { return fireEvent.press(screen.getByRole("button", { name: label })); }
async function selectProject(project: WebsiteProject = built) {
  jest.mocked(listWebsiteProjects).mockResolvedValue([project]);
  await render(<WebsiteScreen />);
  await waitFor(() => expect(screen.getByText(project.objective)).toBeTruthy());
  await fireEvent.press(screen.getByText(project.objective));
  await waitFor(() => expect(screen.getByRole("button", { name: "Tous les projets web" })).toBeTruthy());
}
async function state(value: AppStateStatus) {
  await act(async () => { mockAppStateListener?.(value); });
}
async function reviewPreview() {
  await press("Ouvrir l’aperçu du site");
  await waitFor(() => expect(openURL).toHaveBeenCalled());
  await waitFor(() => expect(screen.getByRole("checkbox")).not.toBeDisabled());
  await fireEvent.press(screen.getByRole("checkbox"));
  await waitFor(() => expect(screen.getByRole("button", { name: "Préparer la publication" })).not.toBeDisabled());
}

describe("WebsiteScreen workflow", () => {
  beforeEach(() => {
    jest.clearAllMocks();
    mockAppStateListener = undefined;
    AppState.currentState = "active";
    jest.mocked(listWebsiteProjects).mockReset().mockResolvedValue([]);
    jest.mocked(getWebsiteCapabilities).mockReset().mockResolvedValue(capabilities);
    jest.mocked(createWebsiteProject).mockReset().mockResolvedValue(draft);
    jest.mocked(commandWebsiteProject).mockReset().mockResolvedValue(captured);
    jest.mocked(previewWebsiteProject).mockReset().mockResolvedValue({
      path: "/website-previews/" + "x".repeat(32) + "/index.html", url: "https://control.example/website-previews/private/index.html", build_digest: digest, expires_in_seconds: 300,
    });
    jest.mocked(previewWebsiteScreenshot).mockReset().mockResolvedValue({
      url: "https://control.example/website-previews/private/screenshot.png", sha256: "c".repeat(64),
    });
    jest.mocked(prepareWebsitePublication).mockReset().mockResolvedValue(approval);
    jest.mocked(publishWebsiteProject).mockReset().mockResolvedValue(published);
    openURL.mockReset().mockResolvedValue(undefined);
  });

  it("creates, captures, builds, explicitly reviews and publishes the exact version", async () => {
    jest.mocked(commandWebsiteProject).mockResolvedValueOnce(captured).mockResolvedValueOnce(built);
    await render(<WebsiteScreen />);
    await fireEvent.changeText(screen.getByTestId("website-source"), " https://entreprise.example/ ");
    await fireEvent.changeText(screen.getByTestId("website-objective"), " Moderniser notre site ");
    await waitFor(() => expect(screen.getByRole("button", { name: "Créer le projet web" })).not.toBeDisabled());
    await press("Créer le projet web");
    await waitFor(() => expect(screen.getByText("1. Explorer le site d’origine")).toBeTruthy());
    expect(createWebsiteProject).toHaveBeenCalledWith(expect.objectContaining({ source_url: draft.source_url, objective: draft.objective, request_id: expect.any(String) }), expect.any(Function));
    await press("Explorer le site");
    await waitFor(() => expect(screen.getByRole("radio", { name: palette.name })).toBeTruthy());
    expect(commandWebsiteProject).toHaveBeenLastCalledWith(draft.id, expect.objectContaining({ action: "capture", expected_version: 1 }), expect.any(Function));
    expect(screen.getByRole("button", { name: "Construire l’aperçu" })).toBeDisabled();
    await fireEvent.press(screen.getByRole("radio", { name: palette.name }));
    await press("Construire l’aperçu");
    await waitFor(() => expect(screen.getByText("3. Vérifier la refonte")).toBeTruthy());
    expect(commandWebsiteProject).toHaveBeenLastCalledWith(draft.id, expect.objectContaining({ action: "build", expected_version: 2, palette_id: "ink", direction_id: "editorial" }), expect.any(Function));
    expect(screen.getByRole("button", { name: "Préparer la publication" })).toBeDisabled();
    await reviewPreview();
    expect(previewWebsiteProject).toHaveBeenCalledWith(draft.id, { expected_version: 3, build_digest: digest }, expect.any(Function));
    expect(prepareWebsitePublication).not.toHaveBeenCalled();
    await press("Préparer la publication");
    await waitFor(() => expect(screen.getByRole("button", { name: "Confirmer la publication" })).toBeTruthy());
    expect(prepareWebsitePublication).toHaveBeenCalledWith(draft.id, { expected_version: 3, build_digest: digest }, expect.any(Function));
    expect(publishWebsiteProject).not.toHaveBeenCalled();
    await press("Confirmer la publication");
    await waitFor(() => expect(screen.getByText("Dernière version publiée : https://preview.example/site")).toBeTruthy());
    expect(publishWebsiteProject).toHaveBeenCalledWith(draft.id, { expected_version: 4, build_digest: digest, approval_token: approval.approval_token, confirm_publication: true }, expect.any(Function));
  });

  it("keeps the user's draft and fences an old server's late list", async () => {
    const pending = deferred<WebsiteProject[]>();
    jest.mocked(listWebsiteProjects).mockReturnValueOnce(pending.promise);
    await render(<WebsiteScreen />);
    await fireEvent.changeText(screen.getByTestId("website-source"), "https://draft.example/");
    await fireEvent.changeText(screen.getByTestId("website-objective"), "Mon intention");
    await act(async () => { notifyConnectionChanged(); });
    const accept = jest.mocked(listWebsiteProjects).mock.calls[0][0]!;
    expect(accept()).toBe(false);
    await act(async () => { pending.resolve([built]); });
    expect(screen.queryByText(built.objective)).toBeNull();
    expect(screen.getByTestId("website-source")).toHaveProp("value", "https://draft.example/");
    expect(screen.getByTestId("website-objective")).toHaveProp("value", "Mon intention");
    expect(screen.getByRole("button", { name: "Créer le projet web" })).toBeDisabled();
  });

  it("does not resurrect a project when its create reply arrives after switching servers", async () => {
    const pending = deferred<WebsiteProject>();
    jest.mocked(createWebsiteProject).mockReturnValueOnce(pending.promise);
    await render(<WebsiteScreen />);
    await fireEvent.changeText(screen.getByTestId("website-source"), draft.source_url);
    await fireEvent.changeText(screen.getByTestId("website-objective"), draft.objective);
    await waitFor(() => expect(screen.getByRole("button", { name: "Créer le projet web" })).not.toBeDisabled());
    await press("Créer le projet web");
    await waitFor(() => expect(createWebsiteProject).toHaveBeenCalled());
    await act(async () => { notifyConnectionChanged(); pending.resolve(draft); });
    expect(screen.queryByText("1. Explorer le site d’origine")).toBeNull();
    expect(screen.getByTestId("website-objective")).toHaveProp("value", draft.objective);
    expect(jest.mocked(createWebsiteProject).mock.calls[0][1]!()).toBe(false);
  });

  it("retries an uncertain creation only on another press, with the same request identity", async () => {
    jest.mocked(createWebsiteProject).mockRejectedValueOnce(new Error("Réponse interrompue")).mockResolvedValueOnce(draft);
    await render(<WebsiteScreen />);
    await fireEvent.changeText(screen.getByTestId("website-source"), draft.source_url);
    await fireEvent.changeText(screen.getByTestId("website-objective"), draft.objective);
    await waitFor(() => expect(screen.getByRole("button", { name: "Créer le projet web" })).not.toBeDisabled());
    await press("Créer le projet web");
    await waitFor(() => expect(screen.getByText("Réponse interrompue")).toBeTruthy());
    expect(createWebsiteProject).toHaveBeenCalledTimes(1);
    await press("Créer le projet web");
    await waitFor(() => expect(screen.getByText("1. Explorer le site d’origine")).toBeTruthy());
    const requests = jest.mocked(createWebsiteProject).mock.calls;
    expect(requests[1][0].request_id).toEqual(requests[0][0].request_id);
  });

  it("discards a command reply after a connection change without automatic resubmission", async () => {
    const pending = deferred<WebsiteProject>();
    jest.mocked(commandWebsiteProject).mockReturnValueOnce(pending.promise);
    await selectProject(draft);
    await press("Explorer le site");
    await waitFor(() => expect(commandWebsiteProject).toHaveBeenCalledTimes(1));
    await act(async () => { notifyConnectionChanged(); pending.resolve(captured); });
    expect(screen.queryByText("2. Choisir une direction")).toBeNull();
    expect(commandWebsiteProject).toHaveBeenCalledTimes(1);
    expect(jest.mocked(commandWebsiteProject).mock.calls[0][2]!()).toBe(false);
  });

  it("keeps publication locked when opening the browser fails", async () => {
    openURL.mockRejectedValueOnce(new Error("Le navigateur ne s’est pas ouvert"));
    await selectProject();
    await press("Ouvrir l’aperçu du site");
    await waitFor(() => expect(screen.getByText("Le navigateur ne s’est pas ouvert")).toBeTruthy());
    expect(screen.getByRole("checkbox")).toBeDisabled();
    expect(screen.getByRole("button", { name: "Préparer la publication" })).toBeDisabled();
    expect(prepareWebsitePublication).not.toHaveBeenCalled();
    expect(publishWebsiteProject).not.toHaveBeenCalled();
  });

  it("does not unlock review when the browser fails after backgrounding", async () => {
    const opened = deferred<void>();
    openURL.mockImplementationOnce(() => {
      mockAppStateListener?.("background");
      return opened.promise;
    });
    await selectProject();
    await press("Ouvrir l’aperçu du site");
    await waitFor(() => expect(openURL).toHaveBeenCalled());
    await act(async () => { opened.reject(new Error("Browser unavailable")); });
    await state("active");
    expect(screen.getByRole("checkbox")).toBeDisabled();
    expect(screen.getByRole("button", { name: "Préparer la publication" })).toBeDisabled();
  });

  it("does not restore preview authority from an old server's delayed browser handoff", async () => {
    const opened = deferred<void>();
    openURL.mockReturnValueOnce(opened.promise);
    await selectProject();
    await press("Ouvrir l’aperçu du site");
    await waitFor(() => expect(openURL).toHaveBeenCalled());
    await act(async () => { notifyConnectionChanged(); });
    await act(async () => { screen.getByTestId("website-screen").props.refreshControl.props.onRefresh(); });
    await fireEvent.press(screen.getByText(built.objective));
    await act(async () => { opened.resolve(); });
    expect(screen.getByRole("checkbox")).toBeDisabled();
  });

  it("shows refresh progress until the current refresh settles", async () => {
    await selectProject();
    const pending = deferred<WebsiteProject[]>();
    jest.mocked(listWebsiteProjects).mockReturnValueOnce(pending.promise);
    await act(async () => { screen.getByTestId("website-screen").props.refreshControl.props.onRefresh(); });
    expect(screen.getByTestId("website-screen").props.refreshControl.props.refreshing).toBe(true);
    await act(async () => { pending.resolve([built]); });
    expect(screen.getByTestId("website-screen").props.refreshControl.props.refreshing).toBe(false);
  });

  it("expands branding recommendations and strategy independently", async () => {
    await selectProject({ ...built, branding: { provider: "infographic_artist", source_digest: digest, summary: "Une composition calme", review_required: true } });
    await press("Lire les recommandations");
    expect(screen.getByText("Une composition calme")).toBeTruthy();
    expect(screen.queryByText("Propositions à valider avec l’entreprise")).toBeNull();
    await press("Stratégie et traçabilité");
    expect(screen.getByText("Propositions à valider avec l’entreprise")).toBeTruthy();
    await press("Masquer les détails");
    expect(screen.getByText("Une composition calme")).toBeTruthy();
  });

  it("disables existing project rows while creation is pending", async () => {
    const pending = deferred<WebsiteProject>();
    jest.mocked(createWebsiteProject).mockReturnValueOnce(pending.promise);
    jest.mocked(listWebsiteProjects).mockResolvedValue([built]);
    await render(<WebsiteScreen />);
    await waitFor(() => expect(screen.getByText(built.objective)).toBeTruthy());
    await fireEvent.changeText(screen.getByTestId("website-source"), "https://new.example/");
    await fireEvent.changeText(screen.getByTestId("website-objective"), "Nouveau site");
    await press("Créer le projet web");
    expect(screen.getByRole("button", { name: new RegExp(built.objective) })).toBeDisabled();
    await fireEvent.press(screen.getByText(built.objective));
    expect(screen.getByTestId("website-objective")).toHaveProp("value", "Nouveau site");
    await act(async () => { pending.resolve({ ...draft, id: "new-project", objective: "Nouveau site" }); });
    expect(screen.getByText("1. Explorer le site d’origine")).toBeTruthy();
  });

  it("keeps the screenshot viewer open when refreshing returns unchanged evidence", async () => {
    const sha256 = "c".repeat(64);
    const shot = { url: draft.source_url, viewport: "mobile", sha256, path: `/website-projects/${draft.id}/screenshots/${sha256}` };
    const source = { ...captured, capture: { ...captured.capture!, screenshots: [shot] } };
    await selectProject(source);
    await press("Voir les captures d’écran");
    await press(`Téléphone · ${draft.source_url}`);
    await waitFor(() => expect(screen.getByLabelText("Capture du site source")).toBeTruthy());
    jest.mocked(listWebsiteProjects).mockResolvedValue(JSON.parse(JSON.stringify([{ ...source, version: 3 }])));
    await act(async () => { screen.getByTestId("website-screen").props.refreshControl.props.onRefresh(); });
    await waitFor(() => expect(listWebsiteProjects).toHaveBeenCalledTimes(2));
    expect(screen.getByRole("button", { name: "Masquer les captures" })).toBeTruthy();
    expect(screen.getByLabelText("Capture du site source")).toBeTruthy();
  });

  it("accepts an in-flight screenshot when only unrelated project state changes", async () => {
    const pending = deferred<{ url: string; sha256: string }>();
    jest.mocked(previewWebsiteScreenshot).mockReturnValueOnce(pending.promise);
    const sha256 = "c".repeat(64);
    const shot = { url: draft.source_url, viewport: "mobile", sha256, path: `/website-projects/${draft.id}/screenshots/${sha256}` };
    const source = { ...captured, capture: { ...captured.capture!, screenshots: [shot] } };
    await selectProject(source);
    await press("Voir les captures d’écran");
    await press(`Téléphone · ${draft.source_url}`);
    jest.mocked(listWebsiteProjects).mockResolvedValue([{ ...source, version: 4, status: "awaiting_direction" }]);
    await act(async () => { screen.getByTestId("website-screen").props.refreshControl.props.onRefresh(); });
    await act(async () => { pending.resolve({ url: "https://control.example/current.png", sha256 }); });
    expect(screen.getByLabelText("Capture du site source")).toHaveProp("source", { uri: "https://control.example/current.png" });
  });

  it("requires an explicit review after returning from the external browser", async () => {
    const opened = deferred<void>();
    openURL.mockImplementationOnce(() => {
      mockAppStateListener?.("background");
      return opened.promise;
    });
    await selectProject();
    await press("Ouvrir l’aperçu du site");
    await waitFor(() => expect(openURL).toHaveBeenCalled());
    await act(async () => { opened.resolve(); });
    await state("active");
    await waitFor(() => expect(listWebsiteProjects).toHaveBeenCalledTimes(2));
    expect(screen.getByRole("button", { name: "Préparer la publication" })).toBeDisabled();
    expect(screen.getByRole("checkbox")).not.toBeDisabled();
    await fireEvent.press(screen.getByRole("checkbox"));
    await waitFor(() => expect(screen.getByRole("button", { name: "Préparer la publication" })).not.toBeDisabled());
    expect(prepareWebsitePublication).not.toHaveBeenCalled();
  });

  it("removes a prepared publication token on backgrounding and never publishes automatically", async () => {
    await selectProject();
    await reviewPreview();
    await press("Préparer la publication");
    await waitFor(() => expect(screen.getByRole("button", { name: "Confirmer la publication" })).toBeTruthy());
    await state("background");
    expect(screen.queryByRole("button", { name: "Confirmer la publication" })).toBeNull();
    await state("active");
    expect(screen.queryByRole("button", { name: "Confirmer la publication" })).toBeNull();
    expect(publishWebsiteProject).not.toHaveBeenCalled();
  });

  it("removes a prepared token when refreshing discovers a different built version", async () => {
    await selectProject();
    await reviewPreview();
    await press("Préparer la publication");
    await waitFor(() => expect(screen.getByRole("button", { name: "Confirmer la publication" })).toBeTruthy());
    jest.mocked(listWebsiteProjects).mockResolvedValue([{ ...built, version: 7, build: { ...built.build!, digest: "b".repeat(64) } }]);
    const refreshControl = screen.getByTestId("website-screen").props.refreshControl;
    await act(async () => { refreshControl.props.onRefresh(); });
    await waitFor(() => expect(screen.queryByRole("button", { name: "Confirmer la publication" })).toBeNull());
    expect(screen.getByRole("button", { name: "Préparer la publication" })).toBeDisabled();
    expect(publishWebsiteProject).not.toHaveBeenCalled();
  });

  it("offers no publication while the server has no configured destination", async () => {
    jest.mocked(getWebsiteCapabilities).mockResolvedValue({ ...capabilities, publication_configured: false, publication_target: null });
    await selectProject();
    expect(screen.getByText("Une destination de publication doit être configurée sur le serveur.")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Préparer la publication" })).toBeDisabled();
    expect(publishWebsiteProject).not.toHaveBeenCalled();
  });

  it("never restores an older list snapshot over a command response", async () => {
    const commandReply = deferred<WebsiteProject>(); const staleRead = deferred<WebsiteProject[]>();
    jest.mocked(commandWebsiteProject).mockReturnValueOnce(commandReply.promise);
    await selectProject(draft);
    await press("Explorer le site");
    await waitFor(() => expect(commandWebsiteProject).toHaveBeenCalledTimes(1));
    jest.mocked(listWebsiteProjects).mockReturnValueOnce(staleRead.promise);
    const refreshControl = screen.getByTestId("website-screen").props.refreshControl;
    await act(async () => { refreshControl.props.onRefresh(); });
    await act(async () => { commandReply.resolve(captured); });
    await waitFor(() => expect(screen.getByText("2. Choisir une direction")).toBeTruthy());
    expect(screen.getByTestId("website-screen").props.refreshControl.props.refreshing).toBe(false);
    await act(async () => { staleRead.resolve([draft]); });
    expect(screen.getByText("2. Choisir une direction")).toBeTruthy();
    expect(screen.getByTestId("website-screen").props.refreshControl.props.refreshing).toBe(false);
  });

  it("discards a screenshot reply after pairing changes", async () => {
    const pending = deferred<{ url: string; sha256: string }>();
    jest.mocked(previewWebsiteScreenshot).mockReturnValueOnce(pending.promise);
    const sha256 = "c".repeat(64);
    const shot = { url: draft.source_url, viewport: "mobile", sha256, path: `/website-projects/${draft.id}/screenshots/${sha256}` };
    await selectProject({ ...captured, capture: { ...captured.capture!, screenshots: [shot] } });
    await press("Voir les captures d’écran");
    await press(`Téléphone · ${draft.source_url}`);
    await waitFor(() => expect(previewWebsiteScreenshot).toHaveBeenCalledTimes(1));
    await act(async () => { notifyConnectionChanged(); pending.resolve({ url: "https://control.example/old.png", sha256 }); });
    expect(jest.mocked(previewWebsiteScreenshot).mock.calls[0][2]!()).toBe(false);
    expect(screen.queryByLabelText("Capture du site source")).toBeNull();
  });

  it("discards a screenshot from an earlier capture when refresh replaces its source evidence", async () => {
    const pending = deferred<{ url: string; sha256: string }>();
    jest.mocked(previewWebsiteScreenshot).mockReturnValueOnce(pending.promise);
    const sha256 = "c".repeat(64);
    const shot = { url: draft.source_url, viewport: "mobile", sha256, path: `/website-projects/${draft.id}/screenshots/${sha256}` };
    await selectProject({ ...captured, capture: { ...captured.capture!, screenshots: [shot] } });
    await press("Voir les captures d’écran");
    await press(`Téléphone · ${draft.source_url}`);
    await waitFor(() => expect(previewWebsiteScreenshot).toHaveBeenCalledTimes(1));
    jest.mocked(listWebsiteProjects).mockResolvedValue([{ ...captured, version: 5,
      capture: { ...captured.capture!, screenshots: [{ ...shot, sha256: "d".repeat(64), path: `/website-projects/${draft.id}/screenshots/${"d".repeat(64)}` }] } }]);
    const refreshControl = screen.getByTestId("website-screen").props.refreshControl;
    await act(async () => { refreshControl.props.onRefresh(); });
    await waitFor(() => expect(screen.getByRole("button", { name: "Voir les captures d’écran" })).toBeTruthy());
    await act(async () => { pending.resolve({ url: "https://control.example/old.png", sha256 }); });
    await press("Voir les captures d’écran");
    expect(screen.queryByLabelText("Capture du site source")).toBeNull();
  });
});
