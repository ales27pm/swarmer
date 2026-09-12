import type { LocalInferenceCapabilities, LocalInferenceRuntime } from "@/lib/local-inference";

export type GgufDownload = {
  repoId: string;
  revision: string;
  filename: string;
  sha256: string;
  sizeBytes: number;
  displayName: string;
};

export type LocalModelPreset = {
  runtime: LocalInferenceRuntime;
  name: string;
  repoId: string;
  revision: string;
  quantization: string;
  downloadSize: string;
  detail: string;
  filename?: string;
  download?: GgufDownload;
};

// Verified against the Hub API and model cards on 2026-09-12. A conversion's
// update date is not a new Dolphin release or an on-device quality benchmark.
export const LOCAL_MODEL_PRESETS: Record<LocalInferenceRuntime, LocalModelPreset> = {
  mlx: {
    runtime: "mlx",
    name: "Dolphin 3.0 · Llama 3.2 3B",
    repoId: "mlx-community/dolphin3.0-llama3.2-3B-4Bit",
    revision: "cdc777b578ff86a69f1b05c9bc00df0cdc2f52d1",
    quantization: "MLX 4 bits",
    downloadSize: "environ 1,82 Go",
    detail: "Préréglage conseillé pour les itérations sur iPhone. Le premier chargement télécharge les poids et le tokenizer depuis Hugging Face.",
  },
  "llama.cpp": {
    runtime: "llama.cpp",
    name: "Dolphin 3.0 · Llama 3.2 3B",
    repoId: "bartowski/Dolphin3.0-Llama3.2-3B-GGUF",
    revision: "ac6b1ee98e3864ebd5998216f800a07d74b166b5",
    quantization: "GGUF Q4_K_M",
    downloadSize: "2,02 Go",
    detail: "Télécharge le fichier dans l’app, vérifie son intégrité, puis charge-le. Un fichier GGUF déjà téléchargé peut aussi être importé.",
    filename: "Dolphin3.0-Llama3.2-3B-Q4_K_M.gguf",
    download: {
      repoId: "bartowski/Dolphin3.0-Llama3.2-3B-GGUF",
      revision: "ac6b1ee98e3864ebd5998216f800a07d74b166b5",
      filename: "Dolphin3.0-Llama3.2-3B-Q4_K_M.gguf",
      sha256: "5d6d02eeefa1ab5dbf23f97afdf5c2c95ad3d946dc3b6e9ab72e6c1637d54177",
      sizeBytes: 2_019_382_400,
      displayName: "Dolphin 3.0 Llama 3.2 3B · Q4_K_M",
    },
  },
  coreml: {
    runtime: "coreml",
    name: "Dolphin 3.0 · Llama 3.2 3B",
    repoId: "ales27pm/Dolphin3.0-CoreML",
    revision: "c786a7060b183baa9ce8f8b11dded70f6d88e021",
    quantization: "Core ML INT4 avec cache KV",
    downloadSize: "environ 1,82 Go",
    filename: "Dolphin3.0-Llama3.2-3B-stateful-int4.mlpackage",
    detail: "Importe un dossier contenant ce package et ses fichiers tokenizer.json, tokenizer_config.json, config.json et generation_config.json. Contexte de 2 048 jetons; iOS 18 minimum.",
  },
};

export function presetSourceUrl(preset: LocalModelPreset): string {
  return `https://huggingface.co/${preset.repoId}/tree/${preset.revision}`;
}

export function preferredLocalRuntime(capabilities: LocalInferenceCapabilities): LocalInferenceRuntime | null {
  if (capabilities.mlx) return "mlx";
  if (capabilities.llamaCpp) return "llama.cpp";
  if (capabilities.coreml) return "coreml";
  return null;
}
