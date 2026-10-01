# Project context window qualification — 2026-10-01

The previous API qualification stopped before its third worker job because the
server counted its serialized project envelope against 14,000 conservative
UTF-8 bytes. That was not evidence that the model exhausted its context window.

The installed model is `swarmer-project-qwen3-coder-heretic:30b-32k-d2d985e`,
Qwen3 MoE, 30.5B parameters, Q4_K_M. Its metadata declares a 262,144-token
architecture context. That declaration does not qualify that size on this host.

## Explicit experiment settings

All defaults remain unchanged. The three independent operator settings are:

| Layer | Setting | Default | Trial |
| --- | --- | ---: | ---: |
| API admission | `MONGARS_PROJECT_CONTEXT_BUDGET_TOKENS` | 24,000 | 64,000 |
| Actual worker prompt text | `MONGARS_PROJECT_PROMPT_MAX_BYTES` | 22,000 | 50,000 |
| Ollama window per request | `MONGARS_PROJECT_MODEL_CONTEXT_TOKENS` | 32,768 | 64,000 |

The API reserves 2,000 for output and 8,000 for overhead, giving the trial a
54,000-byte admission envelope. This is conservative byte accounting, not a
tokenizer measurement. Complete artifacts are still transported, while the worker
selects source for its final prompt separately. The trial enlarges both limits;
it does not remove the distinction between transport and model context.

The worker validates its prompt budget plus 2,000 output tokens and 1,024 framing
reserve against the requested window. Normal output remains capped at 2,000;
compact repair output remains 512. Timeouts, model identity, permissions, runtime
image and sandbox protections are unchanged. Invalid configurations fail before
runtime probing or model calls. The sandbox launcher explicitly forwards the two
new worker variables.

## Direct model comparison

Two sequential, bounded native Ollama calls used identical synthetic messages,
33,796 serialized UTF-8 bytes, with one unique value at the beginning and another
at the end. Both returned the exact two values. Each actually evaluated 11,974
input tokens and generated 31 output tokens. These calls neither created a
project nor changed production configuration.

| Observation | 32,768 window | 64,000 window |
| --- | ---: | ---: |
| Total wall seconds | 69.22 | 79.75 |
| Load seconds | 12.87 | 11.04 |
| Prompt evaluation seconds | 54.41 | 63.71 |
| Generation seconds | 1.86 | 4.92 |
| Peak model allocation reported by Ollama, decimal GB | 22.107 | 25.314 |
| GPU allocation reported by Ollama, decimal GB | 6.515 | 6.559 |
| Lowest sampled available host RAM, GiB | 15.08 | 13.93 |

`/api/ps` independently reported each requested context length during execution.
This verifies allocation and retrieval on this sample, not comprehension of a
full 64,000-token input. One call per setting is not a statistical performance
benchmark. The larger window was slower on this sample.

NVML could not measure GPU memory because `nvidia-smi` returned driver/library
version mismatch, library 595.91. GPU figures above are Ollama's reports, not
NVML measurements. Host swap was already almost full before either probe.

Private input script and sampled receipts are retained in
`~/Library/Logs/SwarmerDeploy/context64k-20261001/` as `context_probe.py`,
`probe-32768.json` and `probe-64000.json`. Deployment and end-to-end project
qualification are separate subsequent gates; the direct probe proves neither.
