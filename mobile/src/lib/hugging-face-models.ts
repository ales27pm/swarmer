/** Public Hub metadata only. Layout compatibility does not establish device execution. */
export type HuggingFaceDownloadPlan = {
  runtime: "mlx" | "coreml" | "llama.cpp";
  repoId: string;
  revision: string;
  displayName: string;
  files: { path: string; sizeBytes: number; sha256?: string; gitBlobSha1?: string }[];
};

export type HuggingFaceModelChoice = {
  id: string;
  label: string;
  sizeBytes: number;
  plan: HuggingFaceDownloadPlan;
};

const ORIGIN = "https://huggingface.co";
const MAX_FILES = 512;
const MAX_BYTES = 16 * 1024 ** 3;
const MAX_ENTRIES = 16_384;
const MAX_PAGES = 32;
const MAX_METADATA_CHARS = 2_000_000;
const SIDECARS = new Set([
  "config.json", "generation_config.json", "tokenizer.json", "tokenizer_config.json",
  "special_tokens_map.json", "added_tokens.json", "tokenizer.model", "merges.txt", "vocab.json",
]);
const MLX_SIDECARS = new Set([...SIDECARS, "vocab.txt", "tokenizer.tiktoken", "chat_template.jinja"]);
// Only this reviewed compiled pipeline is supported; filenames alone do not identify a contract.
const ANEMLL_REPO = "anemll/anemll-Llama-3.2-1B-FAST-iOS_0.3.0";
const ANEMLL_REVISION = "c6461a77a6f803424ec347f9537aadac37094879";
const ANEMLL_COMPONENTS = ["llama_embeddings_lut8.mlmodelc", "llama_lm_head_lut8.mlmodelc",
  "llama_FFN_PF_lut4_chunk_01of01.mlmodelc"];
const ANEMLL_COMPONENT_FILES = ["analytics/coremldata.bin", "coremldata.bin", "metadata.json", "model.mil", "weights/weight.bin"];
type Runtime = HuggingFaceDownloadPlan["runtime"];
type Row = Record<string, unknown>;
type Source = { repoId: string; ref: string; path: string; fileLink: boolean };
type Candidate = { path: string; files: Row[]; label?: string };

function fail(message: string): never { throw new Error(message); }
function object(value: unknown): value is Row {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}
function relativePath(path: string): boolean {
  return path.length > 0 && path.length <= 4096 && path.normalize("NFC") === path
    && path.split("/").every((p) => p.length > 0 && p.length <= 240 && p !== "." && p !== ".."
      && !/[\\:%?#\u0000-\u001f\u007f]/.test(p));
}
function decode(value: string): string {
  try { return decodeURIComponent(value); } catch { return fail("Le lien Hugging Face contient un encodage invalide."); }
}
function parseSource(input: string): Source {
  const value = input.trim();
  if (!value || value.length > 2048 || /[\\\u0000-\u001f\u007f]/.test(value)) {
    fail("Colle un lien HTTPS Hugging Face ou un identifiant auteur/modèle.");
  }
  let segments: string[];
  if (/^[a-z][a-z0-9+.-]*:/i.test(value)) {
    let url: URL;
    try { url = new URL(value); } catch { return fail("Le lien Hugging Face est invalide."); }
    if (url.origin !== ORIGIN || url.username || url.password || url.hash
      || [...url.searchParams].some(([k, v]) => k !== "download" || !["true", "1"].includes(v))) {
      fail("Utilise un lien public https://huggingface.co, sans identifiant ni jeton d’accès.");
    }
    // Inspect the original path before URL can normalize literal or encoded '..'.
    const rawPath = value.match(/^https:\/\/[^/?#]+([^?#]*)/i)?.[1];
    if (rawPath === undefined) fail("Seuls les liens HTTPS Hugging Face sont acceptés.");
    segments = rawPath.replace(/^\//, "").replace(/\/$/, "").split("/").map(decode);
  } else {
    if (/[?#%]/.test(value)) fail("L’identifiant doit être de la forme auteur/modèle.");
    segments = value.split("/");
  }
  const [owner, repo, action, ref, ...paths] = segments;
  const validRepoPart = (v: string | undefined) => v !== undefined
    && /^[A-Za-z0-9][A-Za-z0-9._-]{0,95}$/.test(v) && !v.includes("..") && !v.endsWith(".");
  if (!validRepoPart(owner) || !validRepoPart(repo)) fail("L’identifiant doit être de la forme auteur/modèle.");
  if (segments.length === 2) return { repoId: `${owner}/${repo}`, ref: "main", path: "", fileLink: false };
  if (!value.includes("://") || !["tree", "blob", "resolve"].includes(action)
    || !ref || !relativePath(ref) || paths.some((p) => p.includes("/") || !relativePath(p))) {
    fail("Lien non pris en charge : utilise le dépôt, un dossier tree ou un fichier blob/resolve.");
  }
  const path = paths.join("/");
  if ((path && !relativePath(path)) || (action !== "tree" && !path)) fail("Le chemin du modèle est invalide.");
  return { repoId: `${owner}/${repo}`, ref, path, fileLink: action !== "tree" };
}

function abortError(): Error {
  const error = new Error("Recherche Hugging Face annulée.");
  error.name = "AbortError";
  return error;
}

async function metadata(url: string, signal?: AbortSignal): Promise<{ value: unknown; link: string | null }> {
  if (signal?.aborted) throw abortError();
  const controller = new AbortController();
  const abort = () => controller.abort();
  signal?.addEventListener("abort", abort, { once: true });
  let timedOut = false;
  const timer = setTimeout(() => { timedOut = true; controller.abort(); }, 15_000);
  try {
    const response = await fetch(url, {
      method: "GET", credentials: "omit", redirect: "error", signal: controller.signal,
      headers: { Accept: "application/json" },
    });
    if (response.redirected || (response.url && response.url !== url)) {
      fail("Le dépôt a été redirigé. Utilise son adresse Hugging Face actuelle.");
    }
    if ([401, 403, 404].includes(response.status)) fail("Dépôt ou révision introuvable, privé ou soumis à autorisation. Choisis un dépôt public accessible sans connexion.");
    if (response.status === 429) fail("Hugging Face limite les requêtes. Réessaie dans un instant.");
    if (!response.ok) fail("Hugging Face est momentanément indisponible.");
    const length = response.headers.get("content-length");
    if (length && (!/^\d+$/.test(length) || Number(length) > MAX_METADATA_CHARS * 2)) {
      fail("Les métadonnées du dépôt sont trop volumineuses. Choisis un dossier plus précis.");
    }
    const text = await response.text();
    if (signal?.aborted) throw abortError();
    if (text.length > MAX_METADATA_CHARS) fail("Les métadonnées du dépôt sont trop volumineuses.");
    let value: unknown;
    try { value = JSON.parse(text); } catch { return fail("Hugging Face a retourné des métadonnées invalides."); }
    return { value, link: response.headers.get("link") };
  } catch (error) {
    if (signal?.aborted) throw abortError();
    if (timedOut) fail("Hugging Face met trop de temps à répondre. Réessaie.");
    if (error instanceof TypeError || (error instanceof Error && error.name === "AbortError")) {
      fail("Impossible de joindre Hugging Face. Vérifie la connexion Internet.");
    }
    throw error;
  } finally {
    clearTimeout(timer);
    signal?.removeEventListener("abort", abort);
  }
}

function nextPage(link: string | null, expectedPath: string): string | undefined {
  if (!link) return undefined;
  if (link.length > 16_384) fail("Pagination Hugging Face invalide.");
  const next = link.split(/,(?=\s*<)/).filter((part) => /;\s*rel\s*=\s*"?next"?(?:\s|;|$)/i.test(part));
  if (next.length > 1) fail("Pagination Hugging Face ambiguë.");
  if (!next.length) return undefined;
  const target = next[0].match(/^\s*<([^>]+)>/);
  if (!target) fail("Pagination Hugging Face invalide.");
  let url: URL;
  try { url = new URL(target[1], ORIGIN); } catch { return fail("Pagination Hugging Face invalide."); }
  if (url.origin !== ORIGIN || url.username || url.password || url.hash || url.pathname !== expectedPath
    || url.searchParams.get("recursive") !== "true" || url.searchParams.get("expand") !== "false"
    || [...url.searchParams.keys()].some((k) => !["recursive", "expand", "cursor", "limit"].includes(k))
    || [...new Set(url.searchParams.keys())].some((k) => url.searchParams.getAll(k).length !== 1)
    || !url.searchParams.get("cursor") || (url.searchParams.get("cursor")?.length ?? 0) > 8192) {
    fail("Pagination Hugging Face hors du dépôt ou de la révision choisis.");
  }
  return url.href;
}

function downloadFile(row: Row): HuggingFaceDownloadPlan["files"][number] {
  const sizeBytes = row.size;
  if (typeof row.path !== "string" || !relativePath(row.path)
    || row.path.length > 1024 || row.path.split("/").length > 16
    || !row.path.split("/").every((part) => /^[A-Za-z0-9][A-Za-z0-9._-]{0,239}$/.test(part))
    || typeof sizeBytes !== "number" || !Number.isSafeInteger(sizeBytes) || sizeBytes <= 0) {
    fail("Un fichier du modèle n’a pas de taille ou de chemin vérifiable.");
  }
  if (row.lfs !== undefined && row.lfs !== null) {
    if (!object(row.lfs) || row.lfs.size !== sizeBytes || typeof row.lfs.oid !== "string") {
      fail("Les métadonnées LFS du modèle sont incomplètes.");
    }
    const sha256 = row.lfs.oid.replace(/^sha256:/, "");
    if (!/^[a-f0-9]{64}$/.test(sha256)) fail("L’empreinte SHA-256 du modèle est absente.");
    return { path: row.path, sizeBytes, sha256 };
  }
  if (typeof row.oid !== "string" || !/^[a-f0-9]{40}$/.test(row.oid)) fail("L’empreinte Git du fichier est absente.");
  return { path: row.path, sizeBytes, gitBlobSha1: row.oid };
}

function parent(path: string): string { return path.slice(0, Math.max(0, path.lastIndexOf("/"))); }
function leaf(path: string): string { return path.slice(path.lastIndexOf("/") + 1); }
function under(path: string, directory: string): boolean { return !directory || path === directory || path.startsWith(directory + "/"); }

function anemllCandidate(source: Source, revision: string, rows: Map<string, Row>, files: Row[]): Candidate {
  if (revision !== ANEMLL_REVISION) fail("Cette révision ANEMLL ne correspond pas au profil Core ML pris en charge.");
  const models = new Set([...rows.keys()].flatMap((path) => {
    const segments = path.split("/");
    const index = segments.findIndex((part) => /\.(mlmodelc|mlpackage|mlmodel)$/i.test(part));
    return index < 0 ? [] : [segments.slice(0, index + 1).join("/")];
  }));
  const roots = new Set([...models].map(parent));
  if (models.size !== ANEMLL_COMPONENTS.length || roots.size !== 1) {
    fail("Le profil ANEMLL exige exactement trois composants Core ML dans un même dossier.");
  }
  const root = [...roots][0];
  const rooted = (path: string) => root ? `${root}/${path}` : path;
  const components = ANEMLL_COMPONENTS.map(rooted);
  if (components.some((path) => !models.has(path))) fail("Les composants ANEMLL sont incomplets ou incompatibles.");
  const expected = new Set(components.flatMap((component) => ANEMLL_COMPONENT_FILES.map((path) => `${component}/${path}`)));
  const compiled = files.filter((row) => components.some((component) => under(row.path as string, component)));
  if (compiled.length !== expected.size || compiled.some((row) => !expected.has(row.path as string))) {
    fail("Les fichiers compilés ANEMLL sont incomplets ou contiennent des éléments non pris en charge.");
  }
  const sidecars = files.filter((row) => parent(row.path as string) === root
    && SIDECARS.has(leaf(row.path as string).toLowerCase()));
  const names = new Set(sidecars.map((row) => leaf(row.path as string)));
  if (["config.json", "tokenizer.json", "tokenizer_config.json"].some((name) => !names.has(name))) {
    fail("Le profil ANEMLL exige config.json et les deux fichiers tokenizer dans le même dossier.");
  }
  const selected = [...compiled, ...sidecars];
  if (source.path && !(source.fileLink
    ? selected.some((row) => row.path === source.path)
    : under(root, source.path) || components.some((component) => under(source.path, component)))) {
    fail("Le chemin demandé ne correspond pas au profil ANEMLL complet.");
  }
  return { path: root, files: selected, label: "ANEMLL Llama 3.2 1B · Core ML (512 tokens)" };
}

/** Resolve public metadata to explicit choices; never downloads or executes model weights. */
export async function resolveHuggingFaceModels(
  input: string, runtime: Runtime, signal?: AbortSignal,
): Promise<HuggingFaceModelChoice[]> {
  if (!["mlx", "coreml", "llama.cpp"].includes(runtime)) fail("Moteur local non pris en charge.");
  const source = parseSource(input);
  const prefix = `/api/models/${source.repoId}`;
  const info = (await metadata(`${ORIGIN}${prefix}/revision/${encodeURIComponent(source.ref)}`, signal)).value;
  if (!object(info) || typeof info.sha !== "string" || !/^[a-f0-9]{40}$/.test(info.sha)) {
    fail("Impossible de figer la révision de ce modèle.");
  }
  if (info.private !== false || (info.gated !== false && info.gated !== undefined)) {
    fail("Ce dépôt est privé ou soumis à autorisation. Choisis un modèle public sans connexion.");
  }
  const revision = info.sha;
  const treePath = `${prefix}/tree/${revision}`;
  let url: string | undefined = `${ORIGIN}${treePath}?recursive=true&expand=false`;
  const visited = new Set<string>();
  const rows = new Map<string, Row>();
  let entries = 0;
  while (url) {
    if (visited.has(url) || visited.size >= MAX_PAGES) fail("Le dépôt dépasse la limite de pagination. Choisis un dépôt plus petit.");
    visited.add(url);
    const result = await metadata(url, signal);
    if (!Array.isArray(result.value)) fail("La liste des fichiers Hugging Face est invalide.");
    entries += result.value.length;
    if (entries > MAX_ENTRIES) fail("Le dépôt contient trop de fichiers pour cet import.");
    for (const value of result.value) {
      if (!object(value) || typeof value.path !== "string" || !relativePath(value.path)
        || !["file", "directory"].includes(String(value.type)) || rows.has(value.path)) {
        fail("La liste contient un chemin de fichier invalide ou dupliqué.");
      }
      rows.set(value.path, value);
    }
    url = nextPage(result.link, treePath);
  }
  if (source.path && (!rows.has(source.path) || (source.fileLink && rows.get(source.path)?.type !== "file"))) {
    fail("Le fichier ou dossier demandé n’existe pas dans cette révision.");
  }
  const files = [...rows.values()].filter((r) => r.type === "file");
  const candidates: Candidate[] = [];
  if (runtime === "llama.cpp") {
    const ggufs = files.filter((r) => typeof r.path === "string" && r.path.toLowerCase().endsWith(".gguf")
      && (source.fileLink ? r.path === source.path : under(r.path, source.path)));
    for (const row of ggufs) {
      const path = row.path as string;
      if (/-\d{5}-of-\d{5}\.gguf$/i.test(path)) continue;
      candidates.push({ path, files: [row] });
    }
    if (!candidates.length && ggufs.length) fail("Ce GGUF est découpé en plusieurs fichiers. Choisis une variante GGUF complète en un seul fichier.");
  } else if (runtime === "mlx") {
    for (const config of files.filter((r) => leaf(r.path as string) === "config.json")) {
      const directory = parent(config.path as string);
      const direct = files.filter((r) => parent(r.path as string) === directory);
      const names = new Set(direct.map((r) => leaf(r.path as string)));
      if (!names.has("tokenizer.json") || !names.has("tokenizer_config.json")
        || !direct.some((r) => (r.path as string).endsWith(".safetensors") && leaf(r.path as string) !== "adapter_model.safetensors")) continue;
      if (source.path && !(source.fileLink ? parent(source.path) === directory : under(directory, source.path))) continue;
      candidates.push({ path: directory, files: direct.filter((r) => {
        const name = leaf(r.path as string);
        return MLX_SIDECARS.has(name) || (name.endsWith(".safetensors") && name !== "adapter_model.safetensors") || name.endsWith(".safetensors.index.json");
      }) });
    }
  } else if (source.repoId === ANEMLL_REPO) {
    candidates.push(anemllCandidate(source, revision, rows, files));
  } else {
    const packages = new Set(files.flatMap((r) => {
      const segments = (r.path as string).split("/");
      const index = segments.findIndex((p) => p.endsWith(".mlpackage"));
      return index < 0 ? [] : [segments.slice(0, index + 1).join("/")];
    }));
    for (const path of packages) {
      if (source.path && !(source.fileLink ? under(source.path, path) : under(path, source.path))) continue;
      const packageFiles = files.filter((r) => under(r.path as string, path));
      if (!rows.has(path + "/Manifest.json") || !packageFiles.some((r) => (r.path as string).endsWith(".mlmodel"))) continue;
      if (packageFiles.some((r) => {
        const relative = (r.path as string).slice(path.length + 1);
        return relative !== "Manifest.json" && !(relative.startsWith("Data/") && /\.(mlmodel|bin)$/.test(relative));
      })) continue;
      let directory = parent(path);
      let sidecars: Row[] = [];
      while (true) {
        const direct = files.filter((r) => parent(r.path as string) === directory && SIDECARS.has(leaf(r.path as string)));
        if (direct.some((r) => leaf(r.path as string) === "tokenizer.json")
          && direct.some((r) => leaf(r.path as string) === "tokenizer_config.json")) { sidecars = direct; break; }
        if (!directory) break;
        directory = parent(directory);
      }
      if (sidecars.length) candidates.push({ path, files: [...packageFiles, ...sidecars] });
    }
  }
  if (!candidates.length) fail(`Aucun modèle ${runtime} complet trouvé. Vérifie le format et les fichiers tokenizer requis.`);
  const choices: HuggingFaceModelChoice[] = [];
  for (const candidate of candidates) {
    if (candidate.files.length > MAX_FILES) continue;
    const selected = candidate.files.map(downloadFile).sort((a, b) => a.path.localeCompare(b.path));
    const sizeBytes = selected.reduce((n, f) => n + f.sizeBytes, 0);
    if (!Number.isSafeInteger(sizeBytes) || sizeBytes > MAX_BYTES) continue;
    if (new Set(selected.map((f) => f.path.toLowerCase())).size !== selected.length) fail("Des fichiers du modèle ont des noms ambigus sur cet appareil.");
    const label = candidate.label ?? (candidate.path ? leaf(candidate.path) : leaf(source.repoId));
    const displayName = label.slice(0, 120).replace(/[\uD800-\uDBFF]$/, "");
    choices.push({ id: `${revision}:${candidate.path || "."}`, label, sizeBytes,
      plan: { runtime, repoId: source.repoId, revision, displayName, files: selected } });
  }
  if (!choices.length) fail("Ce modèle dépasse la limite de 512 fichiers ou 16 Gio par téléchargement.");
  return choices.sort((a, b) => a.label.localeCompare(b.label) || a.id.localeCompare(b.id));
}
