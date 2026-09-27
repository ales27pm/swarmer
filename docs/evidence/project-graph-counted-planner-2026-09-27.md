# Project graph and counted planner candidate

## Scope and baseline

This candidate starts at deployed commit `5b0807c9d8d9123a79fdcca05cab736c720af57f`.
It backports only the authenticated, read-only project graph endpoint, its strict
contracts/projection, public planner-summary audit fields, and the counted planner
wire format. The production capability set, worker contracts, database schema,
models and service configuration are unchanged. No `specialist_contracts` dependency
is introduced. The planner-provider delta changes only its system prompt.

The `/goals/{goal_id}/graph` projection reads one bounded SQLite snapshot. It preserves
hard/optional dependency semantics and original revision producer links; it exposes
file/check metadata without source text or runner output. Summaries remain attributed
planner/evaluator reports, separate from receipts. Legacy plans without persisted
rationale do not receive an invented one. Corrupt evidence fails closed with 503.

Planner generation first declares a node count 1–20, then emits exactly that many
ordered slots. References are limited to earlier slots; independent slots retain
empty dependency lists. Public contracts and historical chain/array decoding are
preserved. No model capabilities are added by this transport change.

## Local verification

The focused project-graph, counted-wire, planner-provider and research-query suite
passes 180 tests. It covers authenticated access, snapshot consistency, redaction,
exact revision provenance, malformed/cyclic/bounded graphs, cancellation, stale
planner replies, audit rollback with fenced model calls, schema bounds and legacy
wire compatibility. Ruff checks and changed-module formatting checks pass.

The runtime-recovery/project-runtime suite passes 26 tests and fails two existing
assertions about `model_call_count` (actual 4, expected 3). Both exact failures were
reproduced against a separate untouched export of 5b0807c:
- `test_project_question_reply_resumes_with_snapshot_and_history`, line 137;
- `test_manual_reply_grants_one_durable_dispatch_and_replay_grants_none`, line 216.
These failures are not repaired or silently excluded from this candidate report.

The separate integration suite for goal manager/API, planner dependency contracts,
worker argument schemas, planner failures and diagnostics passes another 90 tests.
Total: 296 passing tests and the two reproduced baseline failures.

No live model inference, database mutation, service restart or deployment is performed
by this packaging subtask. Live qualification and deployment are separate operations.
