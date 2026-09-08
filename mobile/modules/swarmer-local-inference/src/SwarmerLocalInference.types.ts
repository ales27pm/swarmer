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
