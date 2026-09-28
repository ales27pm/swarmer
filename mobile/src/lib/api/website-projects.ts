/** Native website workflow contracts; captured content remains untrusted display text. */
export type WebsitePalette = { id: string; name: string; background: string; surface: string; text: string; muted: string; accent: string; accent_text: string };
export type WebsiteCapabilities = { schema_version: "1.0"; capture: boolean; browser_configured: boolean; browser_note: string; branding_configured: boolean; publication_configured: boolean; publication_target: string | null; palettes: WebsitePalette[] };
export type WebsiteProject = {
  schema_version: "1.0"; id: string; version: number; source_url: string; objective: string;
  status: "draft" | "capturing" | "captured" | "branding" | "awaiting_direction" | "building" | "preview_ready" | "publishing" | "published" | "failed" | "interrupted";
  error: string | null;
  capture: null | { pages: number; inventory_items: number; rendered_pages: number; render_sample_limit: number; assets: number; asset_status: string; coverage: { status: "partial" | "bounded_scope_exhausted" }; render_status: { url: string; status: string; reason: string | null }[]; screenshots: { url: string; viewport: string; sha256: string; path: string }[] };
  branding: null | { provider: string; source_digest: string; summary: string; review_required: boolean };
  build: null | { digest: string; palette: WebsitePalette; file_count: number; migration: { inventory_count: number; accounted_count: number; unassigned_count: number; counts: Record<string, number>; page_map: Record<string, string> }; strategy: { marketing_analysis: Record<string, unknown>; selected_direction?: { layout: "editorial" | "studio" | "catalog" } | null }; readiness: { status: string; blockers: string[] } };
  publication: null | { status: string; digest: string; url: string; file_count: number };
  events: { at: number; stage: string; message: string }[];
};
export type WebsiteCreate = { request_id: string; source_url: string; objective: string };
export type WebsiteCommand = { request_id: string; expected_version: number; action: "capture" | "branding" | "build"; palette_id?: string; direction_id?: "editorial" | "studio" | "catalog" };
export type WebsiteReview = { expected_version: number; build_digest: string };
export type WebsiteApproval = WebsiteReview & { approval_token: string; target: string; expires_in_seconds: number };
export type WebsitePublish = WebsiteReview & { approval_token: string; confirm_publication: true };

function invalid(): never { throw new Error("Le serveur a renvoyé un projet web incomplet. Actualise pour continuer."); }
function object(value: unknown): Record<string, unknown> {
  if (!value || typeof value !== "object" || Array.isArray(value)) return invalid();
  return value as Record<string, unknown>;
}
function str(value: unknown, max = 4000): string { if (typeof value !== "string" || value.length > max) return invalid(); return value; }
function count(value: unknown): number { if (typeof value !== "number" || !Number.isSafeInteger(value) || value < 0) return invalid(); return value; }
function list(value: unknown, max = 100): unknown[] { if (!Array.isArray(value) || value.length > max) return invalid(); return value; }
function bool(value: unknown): boolean { if (typeof value !== "boolean") return invalid(); return value; }
function sha(value: unknown): string { const v = str(value, 64); if (!/^[a-f0-9]{64}$/.test(v)) return invalid(); return v; }
function url(value: unknown): string { const v = str(value, 2048); const u = new URL(v); if (!["https:", "http:"].includes(u.protocol) || u.username || u.password) return invalid(); return v; }
function palette(value: unknown): WebsitePalette {
  const p = object(value);
  const colors: Record<string, string> = {};
  for (const k of ["background", "surface", "text", "muted", "accent", "accent_text"]) { const v = str(p[k], 7); if (!/^#[a-fA-F0-9]{6}$/.test(v)) return invalid(); colors[k] = v; }
  return { id: str(p.id, 40), name: str(p.name, 100), ...colors } as WebsitePalette;
}
export function parseWebsiteCapabilities(value: unknown): WebsiteCapabilities {
  const v = object(value); if (v.schema_version !== "1.0") return invalid();
  return { schema_version: "1.0", capture: bool(v.capture), browser_configured: bool(v.browser_configured), browser_note: str(v.browser_note), branding_configured: bool(v.branding_configured), publication_configured: bool(v.publication_configured), publication_target: v.publication_target === null ? null : url(v.publication_target), palettes: list(v.palettes, 12).map(palette) };
}
export function parseWebsiteProject(value: unknown, expectedId?: string): WebsiteProject {
  const v = object(value); const id = str(v.id, 100);
  if (v.schema_version !== "1.0" || !/^[A-Za-z0-9-]+$/.test(id) || (expectedId && id !== expectedId)) return invalid();
  const status = str(v.status);
  if (!["draft", "capturing", "captured", "branding", "awaiting_direction", "building", "preview_ready", "publishing", "published", "failed", "interrupted"].includes(status)) return invalid();
  let capture: WebsiteProject["capture"] = null;
  if (v.capture !== null) {
    const c = object(v.capture);
    const coverage = str(object(c.coverage).status);
    if (coverage !== "partial" && coverage !== "bounded_scope_exhausted") return invalid();
    capture = { pages: count(c.pages), inventory_items: count(c.inventory_items), rendered_pages: count(c.rendered_pages), render_sample_limit: count(c.render_sample_limit), assets: count(c.assets), asset_status: str(c.asset_status), coverage: { status: coverage },
      render_status: list(c.render_status, 30).map((r) => { const x = object(r); return { url: url(x.url), status: str(x.status), reason: x.reason === null ? null : str(x.reason) }; }),
      screenshots: list(c.screenshots, 60).map((s) => { const x = object(s); return { url: url(x.url), viewport: str(x.viewport), sha256: sha(x.sha256), path: str(x.path, 300) }; }) };
  }
  let build: WebsiteProject["build"] = null;
  if (v.build !== null) {
    const b = object(v.build), m = object(b.migration), r = object(b.readiness), s = object(b.strategy);
    const counts = Object.fromEntries(Object.entries(object(m.counts)).map(([k, val]) => [k, count(val)]));
    const pageMap = Object.fromEntries(Object.entries(object(m.page_map)).map(([k, val]) => [url(k), str(val, 240)]));
    build = { digest: sha(b.digest), palette: palette(b.palette), file_count: count(b.file_count), migration: { inventory_count: count(m.inventory_count), accounted_count: count(m.accounted_count), unassigned_count: count(m.unassigned_count), counts, page_map: pageMap }, strategy: { marketing_analysis: object(s.marketing_analysis) }, readiness: { status: str(r.status), blockers: list(r.blockers, 50).map((x) => str(x, 200)) } };
    if (s.selected_direction != null) {
      const layout = str(object(s.selected_direction).layout);
      if (!["editorial", "studio", "catalog"].includes(layout)) return invalid();
      build.strategy.selected_direction = { layout: layout as "editorial" | "studio" | "catalog" };
    }
  }
  const brand = v.branding === null ? null : object(v.branding), publication = v.publication === null ? null : object(v.publication);
  if (status === "published" && (!build || !publication || publication.status !== "published" || publication.digest !== build.digest)) return invalid();
  return { schema_version: "1.0", id, version: count(v.version), source_url: url(v.source_url), objective: str(v.objective), status: status as WebsiteProject["status"], error: v.error === null ? null : str(v.error), capture, build,
    branding: brand && { provider: str(brand.provider), source_digest: sha(brand.source_digest), summary: str(brand.summary, 8000), review_required: bool(brand.review_required) },
    publication: publication && { status: str(publication.status), digest: sha(publication.digest), url: url(publication.url), file_count: count(publication.file_count) },
    events: list(v.events).map((e) => { const x = object(e); if (typeof x.at !== "number" || !Number.isFinite(x.at)) return invalid(); return { at: x.at, stage: str(x.stage), message: str(x.message) }; }),
  };
}
export function parseWebsiteProjects(value: unknown): WebsiteProject[] { return list(value).map((v) => parseWebsiteProject(v)); }
export function parseWebsiteApproval(value: unknown): WebsiteApproval {
  const v = object(value), token = str(v.approval_token, 100);
  if (!/^[A-Za-z0-9_-]{32,100}$/.test(token)) return invalid();
  return { approval_token: token, build_digest: sha(v.build_digest), expected_version: count(v.expected_version), target: url(v.target), expires_in_seconds: count(v.expires_in_seconds) };
}
export function parseWebsitePreview(value: unknown): { path: string; build_digest: string; expires_in_seconds: number } {
  const v = object(value), path = str(v.path, 200);
  if (!/^\/website-previews\/[A-Za-z0-9_-]{32,100}\/index\.html$/.test(path)) return invalid();
  return { path, build_digest: sha(v.build_digest), expires_in_seconds: count(v.expires_in_seconds) };
}
