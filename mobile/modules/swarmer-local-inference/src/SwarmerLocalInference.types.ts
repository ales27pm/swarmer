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

export type LocalModelDownloadInput = {
  repoId: string;
  revision: string;
  filename: string;
  sha256: string;
  sizeBytes: number;
  displayName: string;
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

export type LocalEmbeddingStatus = {
  state: "disabled" | "loading" | "ready" | "embedding" | "unloading" | "failed";
  modelId: string | null;
  revision: string | null;
  dimensions: 384;
  pipeline: "e5-prefixes-mean-l2-specialtokens-v1";
  message?: string | null;
};
export type LocalEmbeddingLoadInput = { modelId: "intfloat/multilingual-e5-small"; revision: string; experimental: boolean };
export type LocalEmbeddingInput = { texts: string[]; kind: "query" | "document" };
export type LocalEmbeddingResult = {
  modelId: "intfloat/multilingual-e5-small";
  revision: string;
  dimensions: 384;
  pipeline: "e5-prefixes-mean-l2-specialtokens-v1";
  kind: "query" | "document";
  vectors: number[][];
  tokenCounts: number[];
};
