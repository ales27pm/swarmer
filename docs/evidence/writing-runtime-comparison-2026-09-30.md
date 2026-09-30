# Writing runtime comparison — 2026-09-30 UTC

Status: candidate worker changed locally; production services and aliases unchanged.

## Observable runtime gap

The writer hardcoded `num_gpu: 0`. With the captured CRM payload, the configured
Qwen 7B CPU call reached the 120-second wall deadline. A comparison using the
already installed Hermes 3B also timed out: first content arrived at 39.289 s;
527 nonempty content events (not an authoritative token count) arrived before
cancellation at 120.089 s. Neither trial delivered a complete response.

The candidate now accepts operator-only `MONGARS_TEXT_GPU_LAYERS` (integer
0–128), defaulting to CPU. It is sent as the existing Ollama `num_gpu` option.
Invalid configuration fails before claiming a job. A job cannot supply placement
options. The model-call deadline, output budget, lease checks, cancellation and
acceptance rules are unchanged. This setting is not a memory reservation and
requires qualification with the host's other model roles.

The option and terminal timing fields were checked against the
[Ollama v0.32.3 API types](https://github.com/ollama/ollama/blob/v0.32.3/api/types.go),
matching the installed runtime. The worker still uses its native streamed API.

## Local regression evidence

Before implementation, 19 configuration/transport regressions failed and the
two job-override rejection cases already passed. After implementation, all
**215 text-worker tests passed** in 3.75 s for GPU configuration. Ruff lint/format with the server's
configuration, strict mypy on the writer, targeted Bandit and `git diff --check`
passed. This is not a new full-server or iPhone qualification.

The written-deliverable documentation was also brought into agreement with the
current contracts; its old fixed 512-token/100–140-word description was obsolete.

## Controlled comparisons

Each trial uses the same captured objective and three official page excerpts,
SHA-256 `4630cfb31ffaa1ef6213c22757fb1f3a878b5a83f55ab21586c763193f2a3f73`.
It runs in a private candidate directory, gates against production activity in
read-only SQLite and creates no production job. There are no new searches or
automatic retries. Generation is temperature 0, with 1,312 output tokens and
the same 120-second wall limit. Model weights were already installed.

| Model / requested placement | First content | Total elapsed | Accepted deliverable |
|---|---:|---:|---|
| Qwen 2.5 Coder abliterated 7B / CPU | not recorded | 120.046 s | no; wall timeout |
| Hermes 3 abliterated 3B / CPU | 39.289 s | 120.089 s | no; wall timeout |
| Hermes 3 abliterated 3B / 32 GPU layers | 3.808 s | 8.880 s | no; unsupported citation |
| Qwen 2.5 Coder abliterated 7B / 32 GPU layers | 7.872 s | 16.673 s | no; unselected source reference |
| G9v3 Heretic abliterated 3B / 32 GPU layers, thinking unspecified | no final content | 76.028 s | no; output token limit |
| G9v3 Heretic abliterated 3B / 32 GPU layers, thinking disabled | 0.351 s | 10.134 s | no; invalid non-delivery fields |
| Qwen3 Coder Heretic 30B / 16 GPU layers, thinking disabled | 55.529 s | 120.128 s | no; wall timeout |
| G9v3 / 32 GPU layers, thinking disabled, outcome-specific schema | 5.388 s | 18.183 s | no; structurally valid insufficient-sources response |

These are single controlled trials, not a model benchmark or latency guarantee.
The CPU runs are censored at the deadline, so their complete generation time is
unknown. GPU placement was observed through Ollama's loaded-model response:
5,872,403,086 bytes for Hermes and 6,421,584,280 for Qwen were reported as VRAM,
with 32,768-token contexts. Placement is not inferred solely from the request.

Independent review found additional defects hidden by the first rejection:

- Hermes produced 293 words, damaged escape sequences, an invented SQLite URL,
  and unsupported claims about compression and size limits.
- Qwen produced 323 words and referenced S3 without selecting it. Its statement
  that SQLite requires a separate server contradicts the supplied source. Its
  conclusion does not give the requested clear recommendation.

The validators were not weakened to accept these responses. Faster generation
has not yet established compliant writing, semantic accuracy, or the complete
research-to-document iPhone flow.

## Alias-independent thinking option

G9v3 advertises `thinking` in Ollama's model metadata. The writer only sent
`think: false` when the alias contained `qwen3`; its alias therefore left the
runtime's default enabled. The trial ended at 1,312 generated tokens with
`done_reason: length`, without a nonempty final-content event. No internal
reasoning text was retained or displayed. It used partial GPU placement
(1,481,585,459 of 2,437,152,767 bytes reported as VRAM), at an 8,192-token context;
the runtime reported 4,281 prompt tokens.

The candidate now sends `think: false` for every writer alias. The installed
[Ollama chat handler](https://github.com/ollama/ollama/blob/v0.32.3/server/routes.go)
permits false for models without thinking support, so no model-name heuristic or
additional discovery request is needed. The deadline and output allowance stay
unchanged.

An isolated copy of the preceding candidate fails three alias regressions while
the existing Qwen3 case passes. The first attempted before-run overlapped the
edit and did not establish failure; its log is retained separately. The isolated
reproduction removes that ambiguity. The final complete writer suite passes
**219 tests** in 4.90 s after the correction.

A controlled replay with only that candidate change completed its final stream
in 10.134 s, at 219 generated tokens. Its `insufficient_sources` response wrongly
treats the test identifier E2E 2809-A as a missing tool and adds `question`, which
is only valid for `needs_clarification`. The worker rejects it. The 42-word text
does not supply the requested note. Prompt counts (4,283) and a warm model load
are recorded; the first-token difference is not attributed solely to thinking
because runtime caching also differed. This trial proves final-content
production under the limit, not successful task completion.

The installed 30B model was then tested with 16 GPU layers. Ollama reported
22,110,200,133 loaded bytes, including 7,298,487,418 in VRAM, with a 32,768-token
context. Only 161 nonempty content events (668 bytes) arrived before the
120.128-second deadline. No complete JSON or draft was accepted; no terminal
token counts were available. This trial does not qualify that placement as a
replacement either.

## Outcome-specific generation contract

The failed G9 response exposed a separate structural defect: the flat generation
schema admitted `question` for every outcome and nonempty citation selections
for non-deliveries, while the existing decoder rejected both. Eleven regression
cases reproduced those disagreements; eight valid-shape cases already passed.

The candidate now generates from four closed object branches with disjoint
outcomes. Only clarification requires and admits `question`; sourced
non-deliveries require an empty source selection. The existing decoder,
word-count, URL, evidence-marker and clarification checks remain in force.
Delivered public results keep their historical four-field contract.

All **238 text-worker tests pass** in 6.33 s. Ruff, format checks, strict mypy,
targeted Bandit and `git diff --check` pass. These are local worker checks, not a
new complete server run or a deployed integration test.

One guarded G9 replay using the new schema and the unchanged fixed payload
completed in 18.183 s, with 175 generated tokens and `done_reason: stop`.
Ollama accepted the union schema and the worker accepted the resulting
`insufficient_sources` shape without a question or citation selection.
Independent reading of its 42-word text still finds the same mistake: it treats
the E2E test identifier as a tool absent from the evidence and says official
links are unavailable despite the admitted official sources. It does not deliver
the comparison. `response_validated: true` therefore accompanies
`delivered: false` in the receipt; structural validity is not task success.

No tested configuration is qualified for this CRM writing scenario. The next
investigation concerns evidence interpretation and deliverable quality, with
the original requirements and failed receipts preserved. No validator was
weakened and no automatic retry was added.

## Host observation and receipts

`nvidia-smi` could not initialize NVML: userspace library 595.91 differs from the
loaded kernel driver 595.84. Nevertheless, the already running Ollama service
successfully placed these trial models in VRAM. This observation does not prove
that GPU discovery would survive a service restart. No driver package, service
or host was restarted or changed.

Receipts, exact model outputs (untrusted), source manifests, independent reviews
and candidate patches are retained in these local directories:

- `~/Library/Logs/SwarmerQualification/writing-gpu-option-rdg57h5i/`
- `~/Library/Logs/SwarmerQualification/writing-hermes-comparison-9k1m3m1f/`
- `~/Library/Logs/SwarmerQualification/writing-hermes-gpu-tjmv4f7_/`
- `~/Library/Logs/SwarmerQualification/writing-qwen-gpu-egpc8vde/`
- `~/Library/Logs/SwarmerQualification/writing-g9-gpu-m9xyyc0l/`
- `~/Library/Logs/SwarmerQualification/writing-g9-no-thinking-7t52z56b/`
- `~/Library/Logs/SwarmerQualification/writing-30b-gpu-g2oi120z/`
- `~/Library/Logs/SwarmerQualification/writing-outcome-schema-li218p0z/`
- `~/Library/Logs/SwarmerQualification/writing-outcome-grammar-s_6060b3/`

No production model was replaced or deleted, and the default CPU setting remains.
