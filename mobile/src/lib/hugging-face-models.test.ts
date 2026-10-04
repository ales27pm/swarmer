import { afterEach, beforeEach, describe, expect, it, jest } from "@jest/globals";
import { resolveHuggingFaceModels } from "./hugging-face-models";

const revision = "a".repeat(40);
const blob = "b".repeat(40);
const sha256 = "c".repeat(64);
const origin = "https://huggingface.co";
const infoURL = `${origin}/api/models/owner/model/revision/main`;
const treeURL = `${origin}/api/models/owner/model/tree/${revision}?recursive=true&expand=false`;
const info = { sha: revision, private: false, gated: false };
const file = (path: string, size = 10) => ({ type: "file", path, size, oid: blob });
const weight = (path: string, size = 100) => ({ ...file(path, size), lfs: { oid: sha256, size } });
const directory = (path: string) => ({ type: "directory", path });
const mlx = (prefix = "") => [file(`${prefix}config.json`), file(`${prefix}tokenizer.json`),
  file(`${prefix}tokenizer_config.json`), weight(`${prefix}model.safetensors`)];
const coreml = (prefix = "model.mlpackage") => [directory(prefix), file(`${prefix}/Manifest.json`),
  file(`${prefix}/Data/com.apple.CoreML/model.mlmodel`), weight(`${prefix}/Data/com.apple.CoreML/weights/weight.bin`)];
const tokenizers = (prefix = "") => [file(`${prefix}tokenizer.json`), file(`${prefix}tokenizer_config.json`)];
const anemllRepo = "anemll/anemll-Llama-3.2-1B-FAST-iOS_0.3.0";
const anemllRevision = "c6461a77a6f803424ec347f9537aadac37094879";
const anemllComponents = ["llama_embeddings_lut8.mlmodelc", "llama_lm_head_lut8.mlmodelc",
  "llama_FFN_PF_lut4_chunk_01of01.mlmodelc"];
const compiledFiles = ["analytics/coremldata.bin", "coremldata.bin", "metadata.json", "model.mil", "weights/weight.bin"];
const anemll = (prefix = "") => [
  ...anemllComponents.flatMap((component) => [directory(`${prefix}${component}`),
    ...compiledFiles.map((name) => name.endsWith(".bin")
      ? weight(`${prefix}${component}/${name}`) : file(`${prefix}${component}/${name}`))]),
  file(`${prefix}config.json`), ...tokenizers(prefix),
];

function response(value: unknown, options: { status?: number; link?: string; url?: string; redirected?: boolean; length?: string } = {}): Response {
  const status = options.status ?? 200;
  return { status, ok: status >= 200 && status < 300, url: options.url ?? "", redirected: options.redirected ?? false,
    headers: { get: (name: string) => name === "link" ? options.link ?? null : name === "content-length" ? options.length ?? null : null },
    text: async () => JSON.stringify(value),
  } as unknown as Response;
}
const originalFetch = global.fetch;
const mockedFetch = jest.fn<typeof fetch>();
function repository(rows: unknown[], metadata = info) {
  mockedFetch.mockResolvedValueOnce(response(metadata)).mockResolvedValueOnce(response(rows));
}
beforeEach(() => { mockedFetch.mockReset(); global.fetch = mockedFetch; });
afterEach(() => { global.fetch = originalFetch; jest.useRealTimers(); });

describe("public Hugging Face download resolution", () => {
  it("pins MLX metadata and selects only model files, with distinct LFS and Git digests", async () => {
    repository([...mlx(), file("README.md"), file("model.py"), file(".gitattributes"), file("chat_template.jinja")]);
    const [choice] = await resolveHuggingFaceModels("owner/model", "mlx");
    expect(mockedFetch.mock.calls.map(([url]) => url)).toEqual([infoURL, treeURL]);
    expect(choice.plan).toMatchObject({ runtime: "mlx", repoId: "owner/model", revision, displayName: "model" });
    expect(choice.sizeBytes).toBe(140);
    expect(choice.plan.files.map((f) => f.path)).toEqual(["chat_template.jinja", "config.json", "model.safetensors", "tokenizer_config.json", "tokenizer.json"]);
    expect(choice.plan.files.find((f) => f.path === "model.safetensors")).toEqual({ path: "model.safetensors", sizeBytes: 100, sha256 });
    expect(choice.plan.files.find((f) => f.path === "config.json")).toEqual({ path: "config.json", sizeBytes: 10, gitBlobSha1: blob });
    expect(mockedFetch.mock.calls[0][1]).toMatchObject({ credentials: "omit", redirect: "error", headers: { Accept: "application/json" } });
    expect(Object.keys(mockedFetch.mock.calls[0][1]?.headers ?? {})).toEqual(["Accept"]);
  });

  it("keeps GGUF variants separate and never chooses a quantization automatically", async () => {
    repository([weight("model-Q4_K_M.gguf", 400), weight("model-Q8_0.gguf", 800), weight("split-00001-of-00002.gguf")]);
    const choices = await resolveHuggingFaceModels("https://huggingface.co/owner/model", "llama.cpp");
    expect(choices.map((c) => c.label)).toEqual(["model-Q4_K_M.gguf", "model-Q8_0.gguf"]);
    expect(choices.map((c) => c.sizeBytes)).toEqual([400, 800]);
    expect(choices.every((c) => c.plan.files.length === 1)).toBe(true);
  });

  it.each(["blob", "resolve"])("uses an explicit %s file and its requested ref", async (action) => {
    repository([weight("model-Q4.gguf"), weight("model-Q8.gguf")]);
    const result = await resolveHuggingFaceModels(`https://huggingface.co/owner/model/${action}/v1/model-Q8.gguf?download=true`, "llama.cpp");
    expect(result.map((c) => c.label)).toEqual(["model-Q8.gguf"]);
    expect(mockedFetch.mock.calls[0][0]).toBe(`${origin}/api/models/owner/model/revision/v1`);
    expect(mockedFetch.mock.calls[1][0]).toBe(treeURL);
  });

  it("accepts encoded slash refs and scopes a selected MLX folder", async () => {
    repository([...mlx(), directory("int4"), ...mlx("int4/")]);
    const choices = await resolveHuggingFaceModels("https://huggingface.co/owner/model/tree/refs%2Fpr%2F1/int4", "mlx");
    expect(mockedFetch.mock.calls[0][0]).toBe(`${origin}/api/models/owner/model/revision/refs%2Fpr%2F1`);
    expect(choices).toHaveLength(1);
    expect(choices[0].plan.files.every((f) => f.path.startsWith("int4/"))).toBe(true);
  });

  it("uses the closest complete Core ML tokenizer pair and isolates each package", async () => {
    repository([...tokenizers(), file("config.json"), directory("q4"), ...tokenizers("q4/"),
      ...coreml("q4/small.mlpackage"), ...coreml("large.mlpackage"), file("convert.py")]);
    const choices = await resolveHuggingFaceModels("owner/model", "coreml");
    expect(choices).toHaveLength(2);
    const small = choices.find((c) => c.label === "small.mlpackage")!;
    expect(small.plan.files.map((f) => f.path)).toContain("q4/tokenizer.json");
    expect(small.plan.files.some((f) => f.path === "tokenizer.json" || f.path === "config.json" || f.path.startsWith("large."))).toBe(false);
    expect(choices.find((c) => c.label === "large.mlpackage")!.plan.files.map((f) => f.path)).toContain("config.json");
  });

  it("selects a Core ML package folder or a file inside it", async () => {
    repository([...tokenizers(), ...coreml(), ...coreml("other.mlpackage")]);
    const result = await resolveHuggingFaceModels("https://huggingface.co/owner/model/blob/main/model.mlpackage/Manifest.json", "coreml");
    expect(result).toHaveLength(1);
    expect(result[0].label).toBe("model.mlpackage");
  });

  it("does not propose Core ML packages whose contents the native importer rejects", async () => {
    repository([...tokenizers(), ...coreml(), file("model.mlpackage/Data/custom.py")]);
    await expect(resolveHuggingFaceModels("owner/model", "coreml")).rejects.toThrow("Aucun modèle");
  });

  it("selects the pinned ANEMLL three-component reference as one immutable plan", async () => {
    repository([...anemll(), file("README.md"), file("meta.yaml"), file("chat.py"), file("chat_full.py")],
      { ...info, sha: anemllRevision });
    const [choice] = await resolveHuggingFaceModels(anemllRepo, "coreml");
    expect(mockedFetch.mock.calls.map(([url]) => url)).toEqual([
      `${origin}/api/models/${anemllRepo}/revision/main`,
      `${origin}/api/models/${anemllRepo}/tree/${anemllRevision}?recursive=true&expand=false`,
    ]);
    expect(choice.plan).toMatchObject({ runtime: "coreml", repoId: anemllRepo, revision: anemllRevision });
    expect(choice.label).toBe("ANEMLL Llama 3.2 1B · Core ML (512 tokens)");
    expect(Object.keys(choice.plan).sort()).toEqual(["displayName", "files", "repoId", "revision", "runtime"]);
    expect(choice.plan.files).toHaveLength(18);
    expect(choice.plan.files.map((f) => f.path).sort()).toEqual(anemll().filter((r) => r.type === "file").map((r) => r.path).sort());
    expect(choice.plan.files.find((f) => f.path.endsWith("weights/weight.bin"))).toMatchObject({ sizeBytes: 100, sha256 });
    expect(choice.plan.files.find((f) => f.path.endsWith("model.mil"))).toMatchObject({ sizeBytes: 10, gitBlobSha1: blob });
    expect(choice.sizeBytes).toBe(990);
  });

  it.each(["tree/main/export", "tree/main/export/llama_FFN_PF_lut4_chunk_01of01.mlmodelc",
    "blob/main/export/llama_FFN_PF_lut4_chunk_01of01.mlmodelc/model.mil"])(
    "retains one optional ANEMLL parent and all siblings for %s", async (path) => {
      repository([directory("export"), ...anemll("export/"), ...tokenizers()], { ...info, sha: anemllRevision });
      const choices = await resolveHuggingFaceModels(`${origin}/${anemllRepo}/${path}`, "coreml");
      expect(choices).toHaveLength(1);
      expect(choices[0].id).toBe(`${anemllRevision}:export`);
      expect(choices[0].plan.files).toHaveLength(18);
      expect(choices[0].plan.files.every((f) => f.path.startsWith("export/"))).toBe(true);
    },
  );

  it.each([
    ["owner/model", anemllRevision], [anemllRepo, revision],
    ["anemll/anemll-Llama-3.2-1B-FAST-iOS_0.3.5", anemllRevision],
  ])("does not authorize compiled layouts for another repository or revision: %s %s", async (repo, sha) => {
    repository(anemll(), { ...info, sha });
    await expect(resolveHuggingFaceModels(repo, "coreml")).rejects.toThrow();
  });

  it.each(anemll().filter((r) => r.type === "file").map((r) => r.path))(
    "rejects the ANEMLL plan if required file %s is missing", async (path) => {
      repository(anemll().filter((r) => r.path !== path), { ...info, sha: anemllRevision });
      await expect(resolveHuggingFaceModels(anemllRepo, "coreml")).rejects.toThrow();
    },
  );

  it.each([
    [...anemll(), ...coreml()],
    [...anemll(), file("other.mlmodelc/model.mil")],
    [...anemll(), file(`${anemllComponents[2]}/extra.py`)],
    [...anemll(), file(`${anemllComponents[0]}/Metadata.json`)],
    [...anemll(), file("Tokenizer.json")],
    [...anemll(), directory("export"), ...anemll("export/")],
    anemll().map((r) => r.path.startsWith(anemllComponents[2]) ? { ...r, path: `other/${r.path}` } : r),
  ].map((rows) => ({ rows })))("rejects mixed, ambiguous, or extra ANEMLL model contents %#", async ({ rows }) => {
    repository(rows, { ...info, sha: anemllRevision });
    await expect(resolveHuggingFaceModels(anemllRepo, "coreml")).rejects.toThrow();
  });

  it("does not borrow ANEMLL configuration or tokenizers from a parent directory", async () => {
    repository([directory("export"), ...anemll("export/").filter((r) => r.path !== "export/tokenizer.json"),
      ...tokenizers()], { ...info, sha: anemllRevision });
    await expect(resolveHuggingFaceModels(`${origin}/${anemllRepo}/tree/main/export`, "coreml")).rejects.toThrow();
  });

  it.each(["llama_embeddings_lut8.mlmodelc/weights/weight.bin", "config.json"])(
    "rejects an unverifiable ANEMLL file: %s", async (path) => {
      repository(anemll().map((r) => r.path === path ? { ...r, oid: undefined, lfs: undefined } : r),
        { ...info, sha: anemllRevision });
      await expect(resolveHuggingFaceModels(anemllRepo, "coreml")).rejects.toThrow();
    },
  );

  it("assembles all ANEMLL components across immutable pagination without selecting a script", async () => {
    const tree = `${origin}/api/models/${anemllRepo}/tree/${anemllRevision}?recursive=true&expand=false`;
    mockedFetch.mockResolvedValueOnce(response({ ...info, sha: anemllRevision }))
      .mockResolvedValueOnce(response(anemll().slice(0, 9), { link: `<${tree}&cursor=next>; rel="next"` }))
      .mockResolvedValueOnce(response([...anemll().slice(9), file("chat.py")]));
    const choices = await resolveHuggingFaceModels(anemllRepo, "coreml");
    expect(choices).toHaveLength(1);
    expect(choices[0].plan.files).toHaveLength(18);
    expect(mockedFetch).toHaveBeenCalledTimes(3);
  });

  it("preserves a long choice label while bounding the native display name", async () => {
    const name = "m".repeat(150) + ".gguf";
    repository([weight(name)]);
    const [result] = await resolveHuggingFaceModels("owner/model", "llama.cpp");
    expect(result.label).toBe(name);
    expect(result.plan.displayName).toHaveLength(120);
  });

  it("follows bounded pagination only on the immutable revision", async () => {
    const next = `${treeURL}&cursor=next`;
    mockedFetch.mockResolvedValueOnce(response(info))
      .mockResolvedValueOnce(response(mlx().slice(0, 2), { link: `<${next}>; rel="next"` }))
      .mockResolvedValueOnce(response(mlx().slice(2)));
    expect(await resolveHuggingFaceModels("owner/model", "mlx")).toHaveLength(1);
    expect(mockedFetch.mock.calls.map(([url]) => url)).toEqual([infoURL, treeURL, next]);
  });

  it.each([
    "https://example.com/x?recursive=true&expand=false&cursor=x",
    `${origin}/api/models/other/repo/tree/${revision}?recursive=true&expand=false&cursor=x`,
    `${origin}/api/models/owner/model/tree/main?recursive=true&expand=false&cursor=x`,
    `${treeURL}&cursor=x&token=secret`, `${treeURL}&cursor=x&cursor=y`,
    `${treeURL}&cursor=x#fragment`, `${treeURL}&limit=10`,
  ])("rejects hostile pagination without following it: %s", async (next) => {
    mockedFetch.mockResolvedValueOnce(response(info)).mockResolvedValueOnce(response(mlx(), { link: `<${next}>; rel="next"` }));
    await expect(resolveHuggingFaceModels("owner/model", "mlx")).rejects.toThrow("Pagination");
    expect(mockedFetch).toHaveBeenCalledTimes(2);
  });

  it("rejects a pagination loop and duplicate tree paths", async () => {
    const next = `${treeURL}&cursor=x`;
    mockedFetch.mockResolvedValueOnce(response(info)).mockResolvedValueOnce(response([], { link: `<${next}>; rel=next` }))
      .mockResolvedValueOnce(response(mlx(), { link: `<${next}>; rel=next` }));
    await expect(resolveHuggingFaceModels("owner/model", "mlx")).rejects.toThrow("pagination");
    mockedFetch.mockReset();
    repository([...mlx(), file("config.json")]);
    await expect(resolveHuggingFaceModels("owner/model", "mlx")).rejects.toThrow("dupliqué");
  });

  it.each([
    "http://huggingface.co/owner/model", "https://evil.test/owner/model", "https://huggingface.co.evil.test/owner/model",
    "https://user:secret@huggingface.co/owner/model", "https://huggingface.co/owner/model?token=secret",
    "https://huggingface.co/owner/model#secret", "https://huggingface.co/owner/model/tree/main/../file",
    "https://huggingface.co/owner/model/tree/main/%2e%2e/file", "https://huggingface.co/owner/model/tree/main/%252e%252e/file",
    "https://huggingface.co/owner/model/tree/main/a%2Fb", "https://huggingface.co/owner/model/tree/main/a\\b",
    "owner/model/extra", "owner/../model", "https://huggingface.co/owner/model/blob/main",
    "https://huggingface.co/owner/model/tree/main/%ZZ",
  ])("rejects unsafe or unsupported input before networking: %s", async (input) => {
    await expect(resolveHuggingFaceModels(input, "mlx")).rejects.toThrow();
    expect(mockedFetch).not.toHaveBeenCalled();
  });

  it.each([401, 403, 404])("explains inaccessible metadata HTTP %s without leaking response bodies", async (status) => {
    mockedFetch.mockResolvedValueOnce(response({ secret: "not-for-the-error" }, { status }));
    await expect(resolveHuggingFaceModels("owner/model", "mlx")).rejects.toThrow("public accessible sans connexion");
    expect(mockedFetch).toHaveBeenCalledTimes(1);
  });

  it.each([{ ...info, private: true }, { ...info, gated: "auto" }, { ...info, gated: "manual" }, { ...info, sha: "main" }])("rejects non-public or unpinned repositories", async (metadata) => {
    mockedFetch.mockResolvedValueOnce(response(metadata));
    await expect(resolveHuggingFaceModels("owner/model", "mlx")).rejects.toThrow();
    expect(mockedFetch).toHaveBeenCalledTimes(1);
  });

  it.each([
    { ...weight("m.gguf"), lfs: { oid: sha256, size: 99 } },
    { ...weight("m.gguf"), lfs: { oid: blob, size: 100 } },
    { ...file("m.gguf"), oid: "not-a-hash" },
    { ...file("m.gguf"), size: 0 }, { ...file("m.gguf"), size: 1.5 },
    file("space model.gguf"), file("é.gguf"), file("../m.gguf"),
  ])("rejects unverifiable or native-unsupported selected files", async (badFile) => {
    repository([badFile]);
    await expect(resolveHuggingFaceModels("owner/model", "llama.cpp")).rejects.toThrow();
  });

  it("rejects case-colliding selected files on iOS", async () => {
    repository([...mlx(), file("Model.safetensors")]);
    await expect(resolveHuggingFaceModels("owner/model", "mlx")).rejects.toThrow("noms ambigus");
  });

  it.each([
    { runtime: "mlx" as const, rows: mlx().filter((r) => r.path !== "tokenizer_config.json") },
    { runtime: "mlx" as const, rows: [...tokenizers(), file("config.json"), weight("adapter_model.safetensors")] },
    { runtime: "coreml" as const, rows: [...coreml(), file("tokenizer.json")] },
    { runtime: "llama.cpp" as const, rows: [weight("split-00001-of-00002.gguf")] },
  ])("rejects incomplete or unsupported $runtime layouts", async ({ runtime, rows }) => {
    repository(rows);
    await expect(resolveHuggingFaceModels("owner/model", runtime)).rejects.toThrow();
  });

  it("enforces per-choice file and byte limits without silently trimming the model", async () => {
    repository([weight("large.gguf", 16 * 1024 ** 3 + 1)]);
    await expect(resolveHuggingFaceModels("owner/model", "llama.cpp")).rejects.toThrow("512 fichiers ou 16 Gio");
    mockedFetch.mockReset();
    repository([...mlx(), ...Array.from({ length: 509 }, (_, i) => weight(`weights-${i}.safetensors`))]);
    await expect(resolveHuggingFaceModels("owner/model", "mlx")).rejects.toThrow("512 fichiers ou 16 Gio");
  });

  it("rejects missing selected paths and redirected metadata", async () => {
    repository([weight("a.gguf")]);
    await expect(resolveHuggingFaceModels("https://huggingface.co/owner/model/blob/main/missing.gguf", "llama.cpp")).rejects.toThrow("n’existe pas");
    mockedFetch.mockReset().mockResolvedValueOnce(response(info, { redirected: true, url: "https://evil.test" }));
    await expect(resolveHuggingFaceModels("owner/model", "mlx")).rejects.toThrow("redirigé");
  });

  it("rejects already aborted requests without networking", async () => {
    const controller = new AbortController(); controller.abort();
    await expect(resolveHuggingFaceModels("owner/model", "mlx", controller.signal)).rejects.toMatchObject({ name: "AbortError" });
    expect(mockedFetch).not.toHaveBeenCalled();
  });

  it("forwards in-flight cancellation and bounds a stalled request", async () => {
    jest.useFakeTimers();
    mockedFetch.mockImplementation((_url, options) => new Promise((_resolve, reject) => {
      options?.signal?.addEventListener("abort", () => { const error = new Error("cancelled"); error.name = "AbortError"; reject(error); });
    }));
    const controller = new AbortController();
    const cancelled = resolveHuggingFaceModels("owner/model", "mlx", controller.signal);
    const rejected = expect(cancelled).rejects.toMatchObject({ name: "AbortError" });
    controller.abort(); await rejected;
    const stalled = resolveHuggingFaceModels("owner/model", "mlx");
    const timedOut = expect(stalled).rejects.toThrow("trop de temps");
    await jest.advanceTimersByTimeAsync(15_000); await timedOut;
  });
});
