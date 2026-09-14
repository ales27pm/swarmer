# Planner capability grounding — 14 September 2026 UTC

A device report showed a worker node as dispatched while its independent synthesis
node was skipped for lack of evidence. A read-only runtime audit found that the
worker job was queued, unclaimed and had never attempted execution. Its
`code.generate_python` skill belonged to a legacy worker whose last heartbeat was
about 67 hours old. The active worker offered `code.build_project` instead.

The saved planner context contained only the project worker. The planner still
selected the legacy skill, and the server accepted it. Both nodes had no
dependencies, so the synthesis correctly ran immediately and found no input.
The reported two model credits represented one completed planner call and one
reserved worker credit; they did not prove two generations.

The audit did not start, cancel, replan or execute the reported task, and did not
enable the disabled legacy worker. Raw runtime snapshots remain private.

## Correction

- Agent cards now carry structured skills from fresh, protocol-compatible workers
  in online or busy state. Durable policy filters these skills before card and
  token budgeting. Additional narrative cards cannot supply capabilities.
- The planner's output schema permits only the skills in the cards actually
  presented. Its response is independently checked against the same snapshot.
  A worker arriving during inference cannot retroactively authorize a proposal.
- Initial plans, reviewed iPhone/manual plans, replans and evaluator additions
  recheck current capabilities inside the transaction that inserts their nodes.
  A disappearing worker or changed policy causes rejection before execution
  records are written.
- Evaluator output uses its saved capability snapshot as well. Unknown and empty
  capability lists retain distinct meanings; production insertion fails closed
  when its current policy snapshot is unavailable.
- Rejected supplied plans preserve the pending goal and graph. Model attempts
  remain accounted for and receive an explicit invalid-response state.

This is a server-only change with no schema migration. It does not modify the
already persisted malformed plan or establish physical-device execution.

The existing runtime reached the reported goal's original time limit at
03:15:50.556031 UTC. It marked the goal `budget_exhausted` and cancelled its
unclaimed job without any intervention from this investigation. The read-only
03:16:29 UTC snapshot found no active work, model calls, leases or pending outbox.

## Validation

The initial full server run had 1,168 passing tests, nine skips and two failures
in old fixtures: a manual worker plan without any registered worker, and provider
cards that carried skills only in prose. The fixtures now supply a fresh worker
and structured capabilities respectively; both affected suites pass (12 tests).
The final complete run passes: **1,173 passed, nine skipped** in 222.73 seconds.
Its log is retained at
`/private/tmp/swarmer-capability-grounding-pytest-final.log`.

One real, bounded planner inference used the modified provider with a benign
Python book-catalogue request and a single `code.build_project` capability. The
installed `swarmer-project-qwen2.5-coder:7b-32k` model accepted the schema and
returned HTTP 200 in 11.736 seconds. Its plan contained exactly one project
worker, no dependencies, and retained CRUD, title/author search, local persistence
and automated-test requirements in the worker objective. This proves planning
compatibility, not project implementation. The probe did not use GoalManager,
write the live database or create a job; its dedicated SSH tunnel was closed.

The private probe result is
`/private/tmp/swarmer-planner-capability-real-20260914/result.json`, SHA256
`93182317c42fdf75afbe0d2a3910a959c85565f1505d6b599a9322970d5824f5`.
It records the working-tree source hashes, with `79525cdc` as the parent commit.
The tested planner provider hash is
`361386cdfd56673fb56dbab7d86862a051cd42f38be0557c9e9366c964aedc32`;
the plan-validation hash is
`9762158dd2455e81fe316b627d7e070be6b5ced13622bf2594bc4953deca6687`.

Formatting, Ruff, strict typing of 61 modules, Bandit and the committed OpenAPI
contract check pass. No mobile source, dependency, model or worker configuration
changed.

Independent reviews covered GoalManager/plan-validation transaction and accounting
paths, then ContextBuilder/provider trust and schema boundaries. Neither review
identified a material defect. These were static reviews with test inspection,
not additional production or physical-device executions.
