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
qualification are separate gates; the direct probe proves neither.

## Guarded deployment

Implementation commit `93d776cdbbfa59130d9f46a7484cc930f729e5bd` was pushed.
The source validation comprises 42 server cases and 479 worker/launcher cases.
An overlapping subset of 16 server cases also passed on the exact packaged copy.
Only two API runtime files and two worker runtime files changed. Unrelated Core ML
work was excluded. The API wheel's 116 app files match its source bytes and its
dependency metadata matches the deployed predecessor.

Activated releases:

- API: `local-20261001-context64k-bca3d2ead680`.
- Worker: `project-context64000-cc6a742ecbfe73a9ebf7`.
- Existing project runtime image:
  `sha256:944801eb1120222a6b0ab29557d6d2cc10c8033c86b265a2c3f9b21cc5fa9fad`.

The sequential API-source, worker-source/settings and API-settings lanes each
retained a backup and configuration/source rollback. No database restoration
occurred. The final activation preserved all 52 protected data fingerprints.
A separate pre-qualification read confirmed six active services, healthy API,
the API's 64,000 budget and the worker process's 64,000/50,000 settings, with the
same 52 fingerprints and no active work. A separate agent also checked the six
services, five fresh worker heartbeats, source mounts and active settings; it
explicitly attributes the pre-QA fingerprint comparison to the root observer.
Later QA mutations are expected and must not be compared with this baseline as
though production remained idle.

The first worker staging attempt stopped before mutation because the old helper
inventoried Python while the dependency release also listed its browser JSON
profile. The revised helper checks the Python inventory and explicitly verifies
the JSON's hash and mount. The actual historical profile was additionally checked
as regular, not a symlink, and resolving to its exact expected path. Future reuse
should make that last path check an explicit helper guard as well.

Receipts: `api/cutover.json`, `api-env/activation.json` below the private root
above; worker intent and baseline are in the separately retained
`project-context64000-20261001T162200Z-v2` deployment kit.

## API project qualification

The new isolated goal is `goal_6fc92c9fa71d42b7ab478417486467ae`, created with
HTTP 201 and started with HTTP 200 through the previously approved QA server
client. This is not an iPhone execution. A manual one-node plan isolates the
constructor from the previously observed planner-shape failures; the requirement
set and existing normal correction loop remain unchanged. The first 9,825-byte
job advertises a 54,000-byte admission budget.

The real workflow crossed the former 14,000-byte barrier: jobs 2 through 9
were admitted with 16,208, 18,182, 19,321, 21,955, 22,759, 23,577, 23,599 and
23,637 serialized bytes. These transport sizes are not actual token counts.

The fourth iteration added exactly `flask==3.1.3` to `requirements.txt`; the
runner's installation succeeded in 1.902 seconds and the subsequent dependency
preflight reported ready. This is a real autonomous dependency correction. Its
scope is narrower than the requested scenario: the missing import was in the
extra `app.py`, not yet in a Selenium test. No test file had been produced at the
final ninth revision, and the latest actual pytest execution returned exit 5
with zero tests. The third, fifth, seventh and ninth jobs timed out and preserved
prior files and receipts. Their `completed` job status does not mean a successful
generation or a fresh execution of those checks. The sixth and eighth requested
focused reads of `app.py`.

The project paused itself at 17:05:51.954890 UTC as `waiting_permission` /
`needs_user`, after nine iterations and 18 model calls counted by the goal's
accounting. This is not an independently counted number of physical inference
requests. The recorded stop message exactly matches the fixed stalled-project
reason followed by the timeout diagnostic. The three unsuccessful iterations
since the last real change (5, 7, 9) reached the no-progress guard; intervening
reads did not reset it. The retained final revision is
`revision_0c22d12b608d413198437fa78cc4bfa2`, byte-identical to revision 4's files.
From first worker claim to pause, elapsed time was approximately 27 minutes
42 seconds.

A final scoped lineage read confirmed one goal, no descendant, no automatic
continuation pending, no queued or running jobs, and no unanswered question.
All nine jobs were recorded, and all artifacts remain retained. The observer
was then stopped without changing the goal. The constructor did not complete
the requested application and no Selenium test was executed. The passing
findings are increased admission, exact dependency correction and loop stopping;
the wider context does not establish project quality or full-window recall.

The requirement list and Flask diagnostics stayed present across these handoffs.
One preexisting redaction remains: `/usr/bin/chromium` and
`/usr/bin/chromedriver` become `<protected-path>` in the objective/capsule. Runtime
facts disclose executable-presence booleans but do not restore those paths. This
is distinct from the context limit and is not proven to cause the Flask issue.

## Model transport during the project trial

Existing Ollama samples confirm `context_length=64000` during each of the first
five jobs, with 25.314 GB model allocation and 6.559 GB GPU allocation reported.
This confirms the resident configuration, not consumption of 64,000 input tokens.

The timeout journal in job 3's execution interval reports 44,628 message bytes
plus 13,537 schema bytes. Headers arrived at 228.853 seconds and first content at
228.855 seconds; the wall limit cut off at 240.001 seconds after 43 chunks and
112 content bytes, without a terminal envelope. In job 5's interval, 45,487
message bytes plus a 4,784-byte schema produced no headers or content before
240.100 seconds. These journal entries have no job IDs: attribution uses the
exact recorded execution intervals. Chunks are not token counts.

Both entries record a 2,000-token output limit, but neither has `eval_count` or
`done_reason`. Success metrics are not persisted by the current worker. There is
therefore no evidence that the output-token limit was reached, and time before
headers cannot separate queueing, model loading and prompt processing. Narrow
numeric transport receipts keyed by goal/job/attempt are the next useful
instrumentation; no extra inference was run to fill those missing measurements.

Private evidence: `qa/transport-metrics-five-jobs.json` and its companion report.

## Follow-up findings from this qualification

These are recorded separately from the context change; no extra production fix
or manual edit of the generated application was performed during the trial.

- The no-tests guard accepts any edit, including `app.py`, without requiring a
  genuine test file. A successful dependency installation cannot satisfy the
  requirement for actual Selenium checks.
- The readiness gate unconditionally requires `README.md`, while this test's
  requested final file set contains only `index.html`, `tests/test_todo.py` and
  `requirements.txt`. Those two contracts need reconciliation.
- The accepted HTML uses an index from the filtered list to update the unfiltered
  task list. Static review identifies a wrong-task checkbox risk. A local browser
  reproduction was attempted on a byte-identical private copy, but the browser
  tool reported no available browser; no UI behavior is claimed as observed. The
  temporary localhost server was stopped and its closed port verified.

Private QA evidence is retained under `qa/`, including `REPORT.md`, bounded
`independent-observation-*.json`, `independent-artifacts-final.json`,
`independent-lineage-final.json`, and
`filter-checkbox-reproduction/receipt.json`. Recorded events and sampled metrics
are not exhaustive telemetry. Raw model reasoning and credentials are excluded
from this report.
