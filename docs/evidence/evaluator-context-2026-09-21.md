# Evaluator context truncation — 21 September 2026 (Montréal)

The post-TestFlight research failure was reproduced against the installed backend
without resuming or changing the user's goal. One real research job had completed;
two accepted recovery proposals added deterministic synthesis nodes, which reused
the same evidence. Three subsequent evaluator calls were rejected as invalid JSON.
The first two failures still displayed the previous accepted evaluation summary.

## Root cause and controlled comparison

The installed `qwen3.5:9b` profile was actually loaded with a **4,096-token context**.
A private replay of the last stored evaluation context used **3,925 prompt tokens**
and generated only **171 completion tokens** before stopping with
`finish_reason=length`. Its 405-character JSON was incomplete. This is independent
of whether a research conclusion is positive or negative.

A dedicated alias, `swarmer-research-qwen35:9b-8k-6488c96fa5fa`, reuses the installed
source with `num_ctx=8192` and `num_predict=2048`. The source digest, template,
weights metadata and original generation parameters were preserved. The alias's
parent metadata correctly names its source. No original model was replaced.
The [manifest](../../configs/model-manifest.yaml) records both model digests.

An otherwise identical private replay returned **435 completion tokens**, a complete
1,432-character response and `finish_reason=stop` in **18.970 seconds**. The actual
loaded context was **8,192**, with approximately 5.78 GB reported VRAM use.
The current provider and canonical graph validator accepted its `continue` decision
and proposed `research.query` worker. No new search was dispatched by this replay.
Both private model calls preserved the exact goal, node and model-call row hashes.
An initial attempt used the repository's default endpoint, which refused the
connection before inference; subsequent calls used the running process's endpoint.

This demonstrates complete, valid structured output for the observed context.
It does not establish that the requested information exists, that further research
will succeed, or that the complete user goal has finished. No synthesis semantics,
permission rules or graph validation were relaxed.

## Runtime change

`finish_reason=length` now produces the safe `invalid_response` / `truncated`
diagnostic before JSON parsing, including when the returned prefix happens to be
valid JSON. Only a bounded digest is retained in diagnostics.
The current failure summary explains the output/context limit immediately, instead
of showing an older accepted summary. Accepted history remains intact. Existing
phases, accounting, 60-second cooldown and pause after three invalid attempts remain
unchanged. The public API schema and database schema are unchanged.

The separate research profile is selected explicitly for the planner and research
evaluator. The writer and normal code evaluator retain their existing profiles.
See [configuration instructions](../34-web-research.md#contexte-du-planificateur-et-de-lévaluateur).

## Verification and baseline limits

- Focused evaluator, recovery and alias tests: **85 passed**, repeated on the final
  source after canonical import/format normalization; final run 6.85 seconds.
- Changed runtime/test files pass canonical Ruff lint and formatting.
- Strict mypy: **65 application sources passed**.
- Full backend suite: **1,527 passed, nine skipped, one failed**, 215.63 seconds.
  The failure in `test_project_plan_shape` passes public `required_skill` fields to
  the private grammar, which now requires `00_required_skill`. It reproduces on
  clean baseline `d82eb48`; changing only the fixture alias in memory satisfies
  that grammar. The unrelated test was not changed.
- Baseline-only checks also reproduce two existing test import-order errors,
  formatting in `app/main.py`, the missing writing-draft route in the checked-in
  OpenAPI description and a low-severity Bandit B101 in `result_aggregator.py`.
  The affected files are byte-identical to the baseline and the deployed source.
  These broader checks are not reported as passing.

Private receipts are under
`~/Library/Logs/SwarmerDeploy/evaluator-66fda533-20260921/`; raw contexts and model
responses remain outside Git.

| Receipt | SHA256 |
| --- | --- |
| `replay-comparison-proof.json` | `109f1577a7ff8f25329892eaa45af55d54476095e143bc47aef9b40d9eba15e5` |
| `current-endpoint-result.json` | `94260347fd77d5158377e887211e1df965ae981ed2deaaaa40decfae5bf2dc71` |
| `profile-8k-result.json` | `4d1dd793926545cbfa36b40424a4325e8923c464bb8b525b5c42f93deea46368` |
| `scoped-research-model-verified.json` | `c57df4199c6c579de276500a0dbb4471e69898d42ec21ac5b9a4591eaa52a48e` |
| `baseline-checks/baseline-proof.json` | `600481a6484a976e4a3f80a2f8a3d2aa2ac4b9af2abec4eeadc3cbf734d1d81e` |

## Deployment and independent runtime verification

The backend source `d17c1d3394ab8c78651f03ad03bd9ada582e310f` was activated on
21 September at 20:38 Montréal time (22 September 00:38 UTC). Its immutable wheel
SHA256 is `23b9e1cb348bb56860a7dfe8f666122f0ee8737fb0181928c757c74577bf9aea`.
All 66 packaged application files matched the staged source and installation.
Canonical initialization on a private database copy preserved schema version 24,
all 57 tables, `sqlite_sequence` and pairing records. The deployment helpers passed
34 tests before activation.

The original supervised command exited **1** after activation: the file, code
review and research workers automatically restarted when their API connection was
temporarily refused. Their process identities therefore violated the helper's
unchanged-PID assumption. The original `activated` journal and failure receipt were
preserved; neither was rewritten as successful. No second deployment was run and
no database was restored. The API, project worker and text worker had the planned
restart; the other three restarts are an explicit deviation from the rollout plan.

A separate read-only reconciliation at **00:41:43 UTC** confirmed the new API was
healthy and all five workers were online with heartbeats less than four seconds
old. Worker sources, configuration, credentials, identities and systemd bindings
were unchanged. Their journals showed the connection refusal and automatic
systemd recovery.

The coordinator then independently verified the running installation at
**00:43:13–00:43:14 UTC**. HTTP health returned 200, the two changed installed runtime
files matched the committed hashes, and the live process used the qualified 8K
alias. Parsed settings differed only in `planner_model` and
`research_evaluator_model`; the writer, code evaluator, timeouts and leases were
preserved. All six service bindings were unchanged. The verifier reports the three
automatic worker restarts explicitly and does not claim the original PID condition
passed.

Exact row hashes matched for all 18 protected collections of the reported goal
and all 19 collections of the other paused goal, including goal-level audit
events, contexts, evaluations, messages and existing project revisions. All eight
admission counters were zero. No user goal was resumed, no new search was submitted
and no additional inference was performed during deployment or post-deployment
verification. The existing **Réessayer l’évaluation** action remains available to
resume the paused evaluation. This backend change needs no new TestFlight build.

Additional private receipts under the same evidence directory:

| Receipt | SHA256 |
| --- | --- |
| `deploy/baseline.json` | `b24447a336c632aa6f4fffa504f3e4cf12ce1250db2d02ce933dce2ddbfd3d14` |
| `deploy/cutover.json` (original failure preserved) | `84ee5dd582397aeb62b42e398b97e1551a05b8645f096cf14eefcf5b6737d96a` |
| `deploy/post-activation-reconciliation.json` | `78af72efb5d9bfee36aa321c659e4d02988e1630f2a68b4e0ae9f3bc6c916b83` |
| `deploy/independent-post-cutover-result.json` | `c26bc09f15b1066056b369139d9687c108b65e06e170c3074576c3c86041967e` |
