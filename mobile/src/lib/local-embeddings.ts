import { requireOptionalNativeModule } from "expo";
import type { LocalEmbeddingInput, LocalEmbeddingLoadInput, LocalEmbeddingResult, LocalEmbeddingStatus } from "../../modules/swarmer-local-inference/src/SwarmerLocalInference.types";
export type { LocalEmbeddingInput, LocalEmbeddingLoadInput, LocalEmbeddingResult, LocalEmbeddingStatus } from "../../modules/swarmer-local-inference/src/SwarmerLocalInference.types";

const REPOSITORY = "intfloat/multilingual-e5-small";
const PIPELINE = "e5-prefixes-mean-l2-specialtokens-v1";
const REVISION = /^[0-9a-f]{40}$/;
const native = requireOptionalNativeModule<{
  embeddingStatus?(): Promise<unknown>;
  loadEmbedder?(input: LocalEmbeddingLoadInput): Promise<unknown>;
  embed?(input: LocalEmbeddingInput): Promise<unknown>;
  unloadEmbedder?(): Promise<void>;
}>("SwarmerLocalInference");

function record(value: unknown): Record<string, unknown> {
  if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error("Réponse embeddings native invalide.");
  return value as Record<string, unknown>;
}
function parseStatus(value: unknown): LocalEmbeddingStatus {
  const result = record(value);
  if (!["disabled", "loading", "ready", "embedding", "unloading", "failed"].includes(String(result.state)) ||
    result.dimensions !== 384 || result.pipeline !== PIPELINE ||
    (result.modelId !== null && result.modelId !== REPOSITORY) ||
    (result.revision !== null && (typeof result.revision !== "string" || !REVISION.test(result.revision))) ||
    ((result.state === "ready" || result.state === "embedding") && (result.modelId !== REPOSITORY || result.revision === null))) {
    throw new Error("État embeddings natif invalide.");
  }
  return result as LocalEmbeddingStatus;
}
export function isLocalEmbeddingAvailable(): boolean {
  return !!native?.embeddingStatus && !!native?.loadEmbedder && !!native?.embed && !!native?.unloadEmbedder;
}
export async function getLocalEmbeddingStatus(): Promise<LocalEmbeddingStatus> {
  if (!native?.embeddingStatus) throw new Error("Mets à jour l’app iOS pour utiliser les embeddings expérimentaux.");
  return parseStatus(await native.embeddingStatus());
}
export async function loadLocalEmbedder(input: LocalEmbeddingLoadInput): Promise<LocalEmbeddingStatus> {
  if (input.experimental !== true) throw new Error("Active explicitement les embeddings expérimentaux avant le chargement.");
  if (input.modelId !== REPOSITORY || !REVISION.test(input.revision)) throw new Error("Le modèle E5 et une révision immuable sont requis.");
  if (!native?.loadEmbedder) throw new Error("Les embeddings locaux ne sont pas disponibles dans cette version.");
  const status = parseStatus(await native.loadEmbedder(input));
  if (status.state !== "ready" || status.modelId !== input.modelId || status.revision !== input.revision) {
    throw new Error("Le chargement n’a pas confirmé le modèle et sa révision.");
  }
  return status;
}
export async function embedLocalTexts(input: LocalEmbeddingInput): Promise<LocalEmbeddingResult> {
  if (!Array.isArray(input.texts) || input.texts.length < 1 || input.texts.length > 8 ||
    !["query", "document"].includes(input.kind) || input.texts.some((text) => typeof text !== "string" || !text.trim() || text.length > 16_384)) {
    throw new Error("Fournis de 1 à 8 textes non vides, avec le type query ou document.");
  }
  if (!native?.embed) throw new Error("Les embeddings locaux ne sont pas disponibles dans cette version.");
  const value = record(await native.embed(input));
  if (value.modelId !== REPOSITORY || typeof value.revision !== "string" || !REVISION.test(value.revision) ||
    value.pipeline !== PIPELINE || value.dimensions !== 384 || value.kind !== input.kind ||
    !Array.isArray(value.vectors) || value.vectors.length !== input.texts.length ||
    !value.vectors.every((vector) => Array.isArray(vector) && vector.length === 384 && vector.every((n) => typeof n === "number" && Number.isFinite(n)) && Math.abs(Math.hypot(...vector) - 1) < 0.01) ||
    !Array.isArray(value.tokenCounts) || value.tokenCounts.length !== input.texts.length ||
    !value.tokenCounts.every((count) => Number.isInteger(count) && count > 0 && count <= 512)) {
    throw new Error("Les vecteurs, leur identité ou leurs dimensions sont invalides.");
  }
  return value as LocalEmbeddingResult;
}
export async function unloadLocalEmbedder(): Promise<void> {
  if (!native?.unloadEmbedder) throw new Error("Les embeddings locaux ne sont pas disponibles dans cette version.");
  await native.unloadEmbedder();
}
/** Persist this entire identity with an index; embeddings of different revisions/pipelines are never interchangeable. */
export function localEmbeddingIndexIdentity(result: Pick<LocalEmbeddingResult, "modelId" | "revision" | "pipeline" | "dimensions">): string {
  return `${result.modelId}@${result.revision}:${result.pipeline}:${result.dimensions}`;
}
