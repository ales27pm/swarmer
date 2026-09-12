# Local LLM settings

The default control-plane model is **Hermes-3-Llama-3.2-3B-abliterated**.
The goal planner and evaluator inherit that model, so the default installation
needs one resident model for these roles. The Python proposal worker defaults
to **G9v3-3B-Heretic-Abliterated** on `http://127.0.0.1:8712/v1` when its model
environment variables are unset; explicit deployments remain supported. Model selection
does not alter proposal validation, worker eligibility, approvals, or execution
permissions.

[The model manifest](../configs/model-manifest.yaml) records the Hugging Face
repository, immutable revision, exact Q4_K_M filename, size, and SHA-256 for each
preset. Download the chosen artifacts at those revisions, verify their hashes,
then serve the installed files with the following aliases. Substitute the actual
local file paths for `/path/to/models`. The commands use the documented
[llama.cpp server options](https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md):

```sh
llama-server \
  --model /path/to/models/Hermes-3-Llama-3.2-3B-abliterated.Q4_K_M.gguf \
  --alias Hermes-3-Llama-3.2-3B-abliterated \
  --host 127.0.0.1 --port 8711 --ctx-size 8192
```

When a Python proposal worker is needed, launch its model separately:

```sh
llama-server \
  --model /path/to/models/G9v3-3B-Heretic-Abliterated.Q4_K_M.gguf \
  --alias G9v3-3B-Heretic-Abliterated \
  --host 127.0.0.1 --port 8712 --ctx-size 8192
```

These commands are deployment examples; editing settings does not install model
weights or start an inference service. Register the worker with the same
`model_id` as its served alias and use
[its environment example](../workers/code-worker/.env.example). The project
worker keeps its separately configured 30B model and runtime setup.

`MONGARS_LLM_BASE_URL` selects the control-plane OpenAI-compatible endpoint.
`MONGARS_ORCHESTRATOR_MODEL` selects its chat/orchestrator alias.
`MONGARS_PLANNER_MODEL` and `MONGARS_EVALUATOR_MODEL` can override individual
goal roles with aliases served by that same endpoint. An unset, empty, or
whitespace-only role value inherits the orchestrator. Nonempty aliases are
trimmed and validated at startup; existing explicit aliases remain supported.
`MONGARS_SUMMARIZER_MODEL` and `MONGARS_SYNTHESIZER_MODEL` configure routing
metadata, while current result aggregation remains deterministic.

For the pinned Dolphin 3B presets used by the iOS app, including MLX, GGUF, and
the Core ML artifact's compatibility requirements, see
[the model kit](../docs/06-model-kit.md). Dolphin's uncensored fine-tuning is
distinct from abliteration. A published artifact and configured alias do not
establish inference quality, available memory, or device readiness.
