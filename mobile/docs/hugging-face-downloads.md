# Downloading local models from Hugging Face

The local model screen accepts a public Hugging Face repository URL or an
`owner/repository` identifier. Select the runtime first, inspect the available
variants and sizes, then choose a download. Downloading and loading are separate
actions; recognizing a file layout does not establish device compatibility.

Supported artifacts:

- **MLX:** a directory with configuration, tokenizer and Safetensors weights.
- **GGUF:** one complete `.gguf` file, including a file within a repository folder.
- **Core ML:** one `.mlpackage` plus tokenizer sidecars found beside it or in a
  parent directory. Each package is a separate choice.

Links to repository folders (`tree`) and files (`blob` or `resolve`) can narrow
the choices. Private or gated repositories require authentication and are not
supported by this form. It never asks for or embeds an access token.

The resolver converts a branch or tag to an immutable commit before listing
files. Each file is pinned to its declared size and either its LFS SHA-256 or
Git blob SHA-1. The native downloader streams files to private staging, checks
storage capacity, verifies bytes and checksums, and uses the existing atomic
model import. A failed or cancelled download does not publish a partial model.
Model files are data; repository Python code and pickle weights are not run.

The UI reports bytes, file counts, verification and import. Keep the app in the
foreground until import finishes. Completed imports are listed with the other
local models and remain unloaded until the user selects **Charger le modèle**.
The download plan is local to this menu; the remote application API retains its
existing preset-only download command.

Qualification is layered: resolver and UI tests exercise metadata, selection,
failure and cancellation; native tests check file integrity and atomic import;
an iOS build verifies integration. A physical download and model load are
separate runtime checks and must be reported separately.
