import { requireOptionalNativeModule } from "expo";

import type { ToolProposalInput } from "@/lib/api/types";

export const LOCAL_INFERENCE_MODULE_NAME = "SwarmerLocalInference";

export type LocalInferenceRuntime = "coreml" | "mlx" | "llama.cpp";

export type LocalInferenceCapabilities = {
  coreml: boolean;
  mlx: boolean;
  llamaCpp: boolean;
  platform: string;
  reasons?: Record<string, string>;
};

export type LocalModel = {
  modelId: string;
  runtime: LocalInferenceRuntime;
  displayName: string;
  source: string;
  sizeBytes: number;
  importedAt: string;
};

export type LocalInferenceStatus = {
  state: "idle" | "loading" | "ready" | "generating" | "cancelling" | "failed";
  runtime: LocalInferenceRuntime | null;
  modelId: string | null;
  revision: string | null;
  message?: string;
};

export type LocalGenerationResult = {
  text: string;
  finishReason: "stop" | "length" | "cancelled";
  tokenCount: number;
};

export type NoToolProposal = {
  tool_name: "none";
  arguments: Record<string, never>;
  summary: string;
};

export type LocalToolProposal = ToolProposalInput | NoToolProposal;

type NativeLocalInferenceModule = {
  capabilities(): Promise<unknown>;
  importModel(input: {
    runtime: LocalInferenceRuntime;
    uri: string;
    displayName?: string;
  }): Promise<unknown>;
  pickAndImportDirectory(runtime: "coreml" | "mlx"): Promise<unknown>;
  listModels(): Promise<unknown>;
  loadModel(input: {
    runtime: LocalInferenceRuntime;
    modelId: string;
    revision?: string;
  }): Promise<unknown>;
  status(): Promise<unknown>;
  generate(input: {
    prompt: string;
    maxTokens?: number;
    temperature?: number;
  }): Promise<unknown>;
  cancel(): Promise<unknown>;
  unload(): Promise<unknown>;
};

const nativeModule = requireOptionalNativeModule<NativeLocalInferenceModule>(
  LOCAL_INFERENCE_MODULE_NAME,
);

const RUNTIMES = new Set<unknown>(["coreml", "mlx", "llama.cpp"]);
const TOP_LEVEL_PROPOSAL_KEYS = ["arguments", "summary", "tool_name"] as const;
const MAX_MODEL_TEXT_LENGTH = 65_536;
const MAX_PATH_LENGTH = 4_096;
const MAX_ARGUMENT_LENGTH = 16_384;
const MAX_PROCESS_ARGUMENTS = 64;

function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function hasExactKeys(record: Record<string, unknown>, expected: readonly string[]): boolean {
  const actual = Object.keys(record).sort();
  const sortedExpected = [...expected].sort();
  return (
    actual.length === sortedExpected.length &&
    actual.every((key, index) => key === sortedExpected[index])
  );
}

function isRuntime(value: unknown): value is LocalInferenceRuntime {
  return RUNTIMES.has(value);
}

function isNonBlankString(value: unknown, maxLength = Number.MAX_SAFE_INTEGER): value is string {
  return typeof value === "string" && value.trim().length > 0 && value.length <= maxLength;
}

function assertSummary(value: unknown): string {
  if (!isNonBlankString(value, 2_000)) {
    throw new Error("La proposition locale contient un résumé invalide.");
  }
  return value.trim();
}

function assertPath(value: unknown): string {
  if (!isNonBlankString(value, MAX_PATH_LENGTH) || value.includes("\0")) {
    throw new Error("La proposition locale contient un chemin invalide.");
  }
  return value;
}

function assertExactArguments(
  value: unknown,
  expected: readonly string[],
): Record<string, unknown> {
  if (!isRecord(value) || !hasExactKeys(value, expected)) {
    throw new Error("La proposition locale contient des arguments inattendus.");
  }
  return value;
}

function parseWorkspaceProposal(
  toolName: "workspace.list_dir" | "workspace.read_text",
  value: unknown,
  summary: string,
): ToolProposalInput {
  const argumentsRecord = assertExactArguments(value, ["path"]);
  return {
    tool_name: toolName,
    arguments: { path: assertPath(argumentsRecord.path) },
    summary,
  };
}

function parseWriteProposal(value: unknown, summary: string): ToolProposalInput {
  const argumentsRecord = assertExactArguments(value, ["content", "path"]);
  if (typeof argumentsRecord.content !== "string" || argumentsRecord.content.length > 1_000_000) {
    throw new Error("La proposition locale contient un contenu d’écriture invalide.");
  }
  return {
    tool_name: "workspace.write_text",
    arguments: {
      path: assertPath(argumentsRecord.path),
      content: argumentsRecord.content,
    },
    summary,
  };
}

function processArgumentKeys(value: Record<string, unknown>): readonly string[] {
  const expected = ["argv"];
  if (Object.hasOwn(value, "cwd")) expected.push("cwd");
  if (Object.hasOwn(value, "timeout_seconds")) expected.push("timeout_seconds");
  return expected;
}

function parseProcessProposal(value: unknown, summary: string): ToolProposalInput {
  if (!isRecord(value) || !hasExactKeys(value, processArgumentKeys(value))) {
    throw new Error("La proposition locale contient des arguments de processus inattendus.");
  }
  const argv = value.argv;
  if (
    !Array.isArray(argv) ||
    argv.length === 0 ||
    argv.length > MAX_PROCESS_ARGUMENTS ||
    !argv.every((argument) => isNonBlankString(argument, MAX_ARGUMENT_LENGTH) && !argument.includes("\0"))
  ) {
    throw new Error("La proposition locale contient une commande invalide.");
  }
  const cwd = Object.hasOwn(value, "cwd") ? assertPath(value.cwd) : undefined;
  const timeout = value.timeout_seconds;
  if (
    timeout !== undefined &&
    (typeof timeout !== "number" || !Number.isFinite(timeout) || timeout <= 0)
  ) {
    throw new Error("La proposition locale contient un délai invalide.");
  }
  return {
    tool_name: "process.run",
    arguments: {
      argv,
      ...(cwd === undefined ? {} : { cwd }),
      ...(timeout === undefined ? {} : { timeout_seconds: timeout }),
    },
    summary,
  };
}

function assertUnambiguousJson(text: string): void {
  let offset = 0;

  function skipWhitespace() {
    while (/\s/.test(text[offset] ?? "")) offset += 1;
  }

  function parseString(): string {
    const start = offset;
    if (text[offset] !== '"') throw new Error("expected string");
    offset += 1;
    while (offset < text.length) {
      const character = text[offset];
      if (character === '"') {
        offset += 1;
        return JSON.parse(text.slice(start, offset)) as string;
      }
      if (character === "\\") {
        offset += 1;
        const escape = text[offset];
        if (escape === "u") {
          const codePoint = text.slice(offset + 1, offset + 5);
          if (!/^[0-9a-fA-F]{4}$/.test(codePoint)) throw new Error("invalid escape");
          offset += 5;
          continue;
        }
        if (!['"', "\\", "/", "b", "f", "n", "r", "t"].includes(escape ?? "")) {
          throw new Error("invalid escape");
        }
        offset += 1;
        continue;
      }
      if (!character || character.charCodeAt(0) < 0x20) throw new Error("invalid string");
      offset += 1;
    }
    throw new Error("unterminated string");
  }

  function parseObject(depth: number) {
    offset += 1;
    skipWhitespace();
    const keys = new Set<string>();
    if (text[offset] === "}") {
      offset += 1;
      return;
    }
    while (offset < text.length) {
      const key = parseString();
      if (keys.has(key)) throw new Error("duplicate key");
      keys.add(key);
      skipWhitespace();
      if (text[offset] !== ":") throw new Error("expected colon");
      offset += 1;
      parseValue(depth + 1);
      skipWhitespace();
      if (text[offset] === "}") {
        offset += 1;
        return;
      }
      if (text[offset] !== ",") throw new Error("expected comma");
      offset += 1;
      skipWhitespace();
    }
    throw new Error("unterminated object");
  }

  function parseArray(depth: number) {
    offset += 1;
    skipWhitespace();
    if (text[offset] === "]") {
      offset += 1;
      return;
    }
    while (offset < text.length) {
      parseValue(depth + 1);
      skipWhitespace();
      if (text[offset] === "]") {
        offset += 1;
        return;
      }
      if (text[offset] !== ",") throw new Error("expected comma");
      offset += 1;
      skipWhitespace();
    }
    throw new Error("unterminated array");
  }

  function parseValue(depth: number) {
    if (depth > 32) throw new Error("json nesting too deep");
    skipWhitespace();
    const character = text[offset];
    if (character === "{") return parseObject(depth);
    if (character === "[") return parseArray(depth);
    if (character === '"') {
      parseString();
      return;
    }
    for (const literal of ["true", "false", "null"]) {
      if (text.startsWith(literal, offset)) {
        offset += literal.length;
        return;
      }
    }
    const number = text.slice(offset).match(/^-?(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][+-]?\d+)?/);
    if (!number) throw new Error("invalid value");
    offset += number[0].length;
  }

  parseValue(0);
  skipWhitespace();
  if (offset !== text.length) throw new Error("trailing input");
}

export function parseLocalToolProposal(text: string): LocalToolProposal {
  if (!isNonBlankString(text, MAX_MODEL_TEXT_LENGTH)) {
    throw new Error("Le modèle local n’a retourné aucune proposition exploitable.");
  }
  let decoded: unknown;
  try {
    assertUnambiguousJson(text.trim());
    decoded = JSON.parse(text.trim()) as unknown;
  } catch {
    throw new Error(
      "Le modèle local doit retourner un objet JSON exact, sans prose ni clés dupliquées.",
    );
  }
  if (!isRecord(decoded) || !hasExactKeys(decoded, TOP_LEVEL_PROPOSAL_KEYS)) {
    throw new Error("La proposition locale ne respecte pas l’enveloppe attendue.");
  }
  const summary = assertSummary(decoded.summary);
  switch (decoded.tool_name) {
    case "none":
      assertExactArguments(decoded.arguments, []);
      return { tool_name: "none", arguments: {}, summary };
    case "workspace.list_dir":
    case "workspace.read_text":
      return parseWorkspaceProposal(decoded.tool_name, decoded.arguments, summary);
    case "workspace.write_text":
      return parseWriteProposal(decoded.arguments, summary);
    case "process.run":
      return parseProcessProposal(decoded.arguments, summary);
    default:
      throw new Error("La proposition locale utilise un outil non pris en charge.");
  }
}

export function isActionableToolProposal(
  proposal: LocalToolProposal,
): proposal is ToolProposalInput {
  return proposal.tool_name !== "none";
}

export function buildLocalProposalPrompt(intent: string): string {
  const normalized = intent.trim();
  if (!normalized) throw new Error("Saisis une intention avant de lancer le modèle local.");
  if (normalized.length > 32_000) {
    throw new Error("L’intention locale dépasse la limite de 32 000 caractères.");
  }
  return [
    "Tu es le planificateur local de Swarmer.",
    "Retourne exactement un objet JSON, sans Markdown ni prose autour.",
    "Schéma: {\"tool_name\":string,\"arguments\":object,\"summary\":string}.",
    "Outils autorisés:",
    "- workspace.list_dir: {\"path\":string}",
    "- workspace.read_text: {\"path\":string}",
    "- workspace.write_text: {\"path\":string,\"content\":string}",
    "- process.run: {\"argv\":string[],\"cwd\"?:string,\"timeout_seconds\"?:number}",
    "- none: {}",
    "Tu proposes seulement la prochaine action. Ne prétends jamais qu’elle a été exécutée.",
    `Intention: ${normalized}`,
  ].join("\n");
}

export function isImmutableHuggingFaceRevision(value: string): boolean {
  return /^[0-9a-fA-F]{40}$/.test(value.trim());
}

export function isHuggingFaceModelId(value: string): boolean {
  return /^[a-z0-9][a-z0-9._-]{0,95}\/[a-z0-9][a-z0-9._-]{0,95}$/i.test(
    value.trim(),
  );
}

function requireModule(): NativeLocalInferenceModule {
  if (!nativeModule) {
    throw new Error(
      "L’inférence locale native exige une version de développement iOS compatible.",
    );
  }
  return nativeModule;
}

function parseCapabilities(value: unknown): LocalInferenceCapabilities {
  if (!isRecord(value)) throw new Error("Capacités natives invalides.");
  const allowedKeys = new Set(["coreml", "mlx", "llamaCpp", "platform", "reasons"]);
  if (Object.keys(value).some((key) => !allowedKeys.has(key))) {
    throw new Error("Capacités natives invalides.");
  }
  if (
    typeof value.coreml !== "boolean" ||
    typeof value.mlx !== "boolean" ||
    typeof value.llamaCpp !== "boolean" ||
    !isNonBlankString(value.platform)
  ) {
    throw new Error("Capacités natives invalides.");
  }
  let reasons: Record<string, string> | undefined;
  if (value.reasons !== undefined && value.reasons !== null) {
    if (
      !isRecord(value.reasons) ||
      Object.values(value.reasons).some((reason) => typeof reason !== "string")
    ) {
      throw new Error("Capacités natives invalides.");
    }
    reasons = value.reasons as Record<string, string>;
  }
  return {
    coreml: value.coreml,
    mlx: value.mlx,
    llamaCpp: value.llamaCpp,
    platform: value.platform,
    ...(reasons ? { reasons } : {}),
  };
}

function parseLocalModel(value: unknown): LocalModel {
  if (
    !isRecord(value) ||
    !hasExactKeys(value, [
      "displayName",
      "importedAt",
      "modelId",
      "runtime",
      "sizeBytes",
      "source",
    ]) ||
    !isNonBlankString(value.modelId) ||
    !isRuntime(value.runtime) ||
    !isNonBlankString(value.displayName) ||
    !isNonBlankString(value.source) ||
    !Number.isSafeInteger(value.sizeBytes) ||
    Number(value.sizeBytes) < 0 ||
    !isNonBlankString(value.importedAt)
  ) {
    throw new Error("Le modèle natif retourné est invalide.");
  }
  return {
    modelId: value.modelId,
    runtime: value.runtime,
    displayName: value.displayName,
    source: value.source,
    sizeBytes: Number(value.sizeBytes),
    importedAt: value.importedAt,
  };
}

function parseStatus(value: unknown): LocalInferenceStatus {
  if (!isRecord(value)) throw new Error("État natif invalide.");
  const states = new Set<unknown>([
    "idle",
    "loading",
    "ready",
    "generating",
    "cancelling",
    "failed",
  ]);
  const runtime = value.runtime;
  const modelId = value.modelId;
  const revision = value.revision;
  if (
    !states.has(value.state) ||
    !(runtime === null || isRuntime(runtime)) ||
    !(modelId === null || isNonBlankString(modelId)) ||
    !(revision === null || (typeof revision === "string" && isImmutableHuggingFaceRevision(revision))) ||
    !(value.message === undefined || value.message === null || typeof value.message === "string")
  ) {
    throw new Error("État natif invalide.");
  }
  return {
    state: value.state as LocalInferenceStatus["state"],
    runtime,
    modelId,
    revision,
    ...(typeof value.message === "string" ? { message: value.message } : {}),
  };
}

function parseGeneration(value: unknown): LocalGenerationResult {
  if (
    !isRecord(value) ||
    !hasExactKeys(value, ["finishReason", "text", "tokenCount"]) ||
    typeof value.text !== "string" ||
    value.text.length > MAX_MODEL_TEXT_LENGTH ||
    typeof value.finishReason !== "string" ||
    !["stop", "length", "cancelled"].includes(value.finishReason) ||
    !Number.isSafeInteger(value.tokenCount) ||
    Number(value.tokenCount) < 0 ||
    Number(value.tokenCount) > 512
  ) {
    throw new Error("La génération native a retourné un résultat invalide.");
  }
  return {
    text: value.text,
    finishReason: value.finishReason as LocalGenerationResult["finishReason"],
    tokenCount: Number(value.tokenCount),
  };
}

export function isLocalInferenceAvailable(): boolean {
  return nativeModule !== null;
}

export async function getLocalInferenceCapabilities(): Promise<LocalInferenceCapabilities> {
  return parseCapabilities(await requireModule().capabilities());
}

export async function importLocalModel(input: {
  runtime: LocalInferenceRuntime;
  uri: string;
  displayName?: string;
}): Promise<LocalModel> {
  return parseLocalModel(await requireModule().importModel(input));
}

export async function pickAndImportLocalModelDirectory(
  runtime: "coreml" | "mlx",
): Promise<LocalModel> {
  return parseLocalModel(await requireModule().pickAndImportDirectory(runtime));
}

export async function listLocalModels(): Promise<LocalModel[]> {
  const value = await requireModule().listModels();
  if (!Array.isArray(value)) throw new Error("La liste native des modèles est invalide.");
  return value.map(parseLocalModel);
}

export async function loadLocalModel(input: {
  runtime: LocalInferenceRuntime;
  modelId: string;
  revision?: string;
}): Promise<LocalInferenceStatus> {
  return parseStatus(await requireModule().loadModel(input));
}

export async function getLocalInferenceStatus(): Promise<LocalInferenceStatus> {
  return parseStatus(await requireModule().status());
}

export async function generateLocalProposal(input: {
  prompt: string;
  maxTokens?: number;
  temperature?: number;
}): Promise<LocalGenerationResult> {
  return parseGeneration(await requireModule().generate(input));
}

export async function cancelLocalGeneration(): Promise<void> {
  await requireModule().cancel();
}

export async function unloadLocalModel(): Promise<void> {
  await requireModule().unload();
}
