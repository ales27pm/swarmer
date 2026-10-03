# Memory reviewer: bounded thinking diagnostic — 3 October 2026

The four real requests completed, but **neither mode met the two-case diagnostic**.
With thinking disabled, the reviewer repeated one false acceptance and one false
rejection. With the model default, both responses reported 2,048 completion
tokens and `finish_reason: length`, so neither verdict was validated. No
production setting or memory was changed.

## Controlled change and runtime

This follows the [failed 70-call clarification comparison](memory-reviewer-clarification-2026-10-03.md).
It uses two retained failures, not new held-out examples. Expected labels and
request order were frozen before inference. The original baseline `REVIEW_PROMPT`,
strict `_Review` schema, literal checks, decision gates, model, temperature zero,
60-second request limit and `max_tokens=2048` request setting were unchanged. The only
request-field change was explicit `reasoning_effort: "none"` versus omission.
The existing provider supports both configurations and gives them distinct
normalization signatures; no application source change was needed.

The source was the sealed installed `c527f5d` package. Its
`memory_normalization.py` is byte-identical to the staged runtime candidate
`243e913c3126a1cb15d290fbbac36e6f5024b7fe` (SHA-256
`78e8a1e21329107eeb4b5dcaf4f6606494d4c65abeca9cf731a1ee2820d67ef0`).
This does not qualify other code differences between those releases.

The model remained `swarmer-research-qwen35:9b-8k-6488c96fa5fa`, digest
`cc81d93f910b2a4fbb2b5cbd38faf70de1b39178871f866f2cd80f09f4172a3d`,
on Ollama 0.32.3. Read-only model metadata advertised thinking support.
The [versioned OpenAI adapter](https://github.com/ollama/ollama/blob/v0.32.3/openai/openai.go#L591-L617)
maps `none` to disabled thinking; omission leaves the default to the
[chat handler](https://github.com/ollama/ollama/blob/v0.32.3/server/routes.go#L2450-L2466).
The actual default-mode responses contained a reasoning field. No calibrated
low/medium/high compute-level claim follows from this result.

## Real results

Each row is one observation. Durations are captured HTTP durations, not an
estimate of future latency.

| Order | Retained case | Mode | Finish | Completion tokens | Seconds | Outcome |
| --- | --- | --- | --- | ---: | ---: | --- |
| 1 | Required signature **and** validation weakened to **or** | `none` | `stop` | 212 | 9.33 | False acceptance |
| 2 | Same altered pair | model default | `length` | 2,048 | 37.83 | Rejected, no validated verdict |
| 3 | Faithful negated prerequisites for publishing an archive | model default | `length` | 2,048 | 38.61 | Rejected, no validated verdict |
| 4 | Same faithful pair | `none` | `stop` | 219 | 4.58 | False rejection |

All four responses were HTTP 200 and completely received. There were no retries,
missing observations or unknown HTTP outcomes. The trial released its owned GPU
marker after completion. The production database was used only for admission
reads; no memory, goal, task, capability or model configuration was written.

The two default-mode failures are **not correct semantic rejections**. Both
responses were rejected at the `finish_reason=length` gate before final-content
validation; the completeness of their final JSON was not established. Conversely, the two
`stop` responses were valid JSON but gave the wrong semantic decisions. This
separates protocol success, completion-budget failure and semantic correctness.

The reported token counts do not establish the number of internal generation
phases or a universal aggregate cap across them. The [versioned adapter](https://github.com/ollama/ollama/blob/v0.32.3/openai/openai.go#L552-L554)
maps `max_tokens` to `num_predict`, but structured output may involve a
[separate constrained phase](https://github.com/ollama/ollama/blob/v0.32.3/server/routes.go#L2719-L2740)
using the same options; this trial did not trace that branch.

Only synthetic requests, parsed final verdicts, hashes, timings, usage counts and
aggregate reasoning-field metadata were retained. Raw reasoning text was not
saved or published. The local 58-test suite and Ubuntu four-call simulation
checked the harness; simulated scores were null.

## Reproduction pins and limits

| Artifact | SHA-256 |
| --- | --- |
| Transferred eight-file archive | `078b4b244b3e8770fba277a4cf07d8a04684c313cbc1e16cd69663a21f87df3e` |
| Diagnostic seal | `58977fa1a3b61c38d92cde7515b1ee36d4ce1afba558fdbceefcb1b7ba5ecc46` |
| Predeclared four-request plan | `95d4ca8feae550b8c3d5168818b80589a06f6413b22cf6dee2524875fa0d9e25` |
| Terminal receipt | `32b0201388997505c331bc908d248c991f5cabb3b22f666a535b273012c56ae3` |
| Observations | `28c36c1bf91e5faabe3c0dff34c3ed04bd784f08df41101d283677d64dea61de` |
| Descriptive scores | `c7df8554919bf2230574213bdf4a1713e1312fcf9052c1b4320271c925cc16ab` |
| Verified copied readback | `6053bf40c2b911e97caf902badbc28f31cfc6f9866ca99b51e1724f59a7e932c` |

The retained private evidence is under
`SwarmerQualification/Memory/goal-model-admission-20261003/symbolic-integration-20261003T200412Z/provider-qualification/reviewer-thinking-diagnostic-20261003`.
The live run is `runs/diagnostic-01` in the corresponding Ubuntu qualification
directory. The copied manifest, four exact request bodies and scores were checked
again after transfer; root also read the terminal result directly from Ubuntu.

This proves failure of this budgeted configuration on two known cases, not that
thinking can never help, nor that a larger budget would fix semantic accuracy.
There is one observation per case/mode and no stability or general-quality
estimate. Any larger-budget or different-model trial needs its own fixed limits
and complete-response checks. The current result authorizes no promotion,
automatic flag correction, gate relaxation or production retry.
