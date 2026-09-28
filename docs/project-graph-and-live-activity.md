# Project structure and visible decisions

The authenticated, read-only `GET /goals/{goal_id}/graph` endpoint supplies the
project screen. It reads one consistent SQLite snapshot and returns `private,
no-store`. The endpoint never advances a goal or calls a model.

## What the graph means

- Nodes are persisted plan steps, with their current status, assigned agent,
  capability and expected result.
- Solid arrows are required dependencies; dashed arrows are optional inputs.
  The service checks that stored edges agree with node dependencies and form a DAG.
- Steps at the same level have no dependency on one another. This does not prove
  simultaneous execution: actual concurrency still depends on available resources.
- Selecting a step opens its details. Its operations shortcut filters the activity
  timeline by that step. Plan, Activity, Results and Exchanges are separate tabs.

The payload is bounded to the current goal (at most twenty nodes), the latest linked
project revision, five recent planning explanations and five recent evaluations.
It explicitly reports when older explanations or evaluations exist.

## Public explanations, distinct from evidence

Accepted initial plans and replans store the planner's existing public rationale in
the same transaction as plan acceptance. The record includes the source, model call,
model identifier when known, conversation revision, timestamp and affected nodes.
Stale or rejected proposals do not leave an accepted explanation behind.

The UI shows why the plan was proposed and lets the user expand its recorded source.
Evaluation summaries show what the evaluator reports as missing or invalid. These
are model reports, not proof that an operation happened. No hidden reasoning stream
is exposed, and no explanation is reconstructed for older records that lack one.

Files and checks are attached only to the exact durable project revision that produced
them, with hashes and producer IDs. A check attached to a revision does not establish
that it is fresh for every current file. Criterion-to-evidence mapping is not yet
recorded, so the graph reports `not_mapped` instead of inventing completion coverage.
The graph cannot, by itself, guarantee absence of requirement drift.

## Live operation view

The timeline follows recorded server events and refreshes even when detailed rows
are collapsed. The summary exposes unfinished operations from the last received
snapshot. Agent, model, tool, file/check information and durations are shown when
the server recorded them. Internal unrecorded computation and token-by-token output
are not synthesized as activity.

Connection changes invalidate old responses. Cached observations carry a stale
label and observation date; mutations stay disabled until the current connection
has authoritative data. The project conversation keeps the user's draft.

## Mobile verification

The mobile suite passed all 958 tests across 58 suites, with typecheck and lint passing.
Component-level web QA used an isolated harness with fictional data and outbound
application mutations disabled: graph selection, detail dismissal, step-filtered
activity, chat history/draft preservation, grouped settings and authorization effects
were exercised at phone and wide widths. These checks are not a physical-iPhone
or TestFlight qualification.
