# Shared project memory qualification — 13 September 2026

This change connects the iPhone initial-plan prompt and the Ubuntu planner and
evaluator to the existing project-worker memory projection. Embeddings and
vectors remain on Ubuntu. It does not activate general or episodic embeddings.
The [earlier runtime audit](semantic-memory-2026-09-13.md) remains the record of
the deployed system before this change.

## Behavior verified in code and tests

- Device-authenticated memory retrieval derives its project and query from the
  goal. It cannot accept another project ID or arbitrary query from the client.
- A goal without a linked project returns an explicit empty result, without
  creating a project, node, query receipt or model call.
- Linked-project retrieval uses the same projection, embedding provider and
  ranking as project workers. Cached requests do not spend another credit;
  failed requests have an explicit lexical result and preserve generation credit.
- The receipt binds the goal, provider, project revision, conversation and
  selected source content. Latest replies travel separately from historical
  excerpts. Current instructions and execution evidence retain priority.
- The iPhone verifies its pairing session and receipt at opening, generation
  and start. A stale or uncertain request never silently falls back to Ubuntu
  planning or drops its receipt. Local output remains limited to 512 tokens.
- The server validates an iPhone receipt before starting and again in the
  plan transaction. Tests cover a changed reply between checks and a different
  server instance accepting a plan first; neither case advances the stale plan.
- Historical hints cannot displace existing planner/evaluator evidence.
  An in-flight embedding lookup defers evaluation without a manual-retry pause.
- A terminal goal with a linked project accepts an explicit `iphone_local`
  continuation. The new goal inherits its project and conversation, remains
  unstarted, and has no pending automatic dispatch credit. Restart reconciliation
  leaves it untouched. Additional messages invalidate an earlier memory receipt
  without changing the selected planning mode. Starting it requires the reviewed
  local plan and its current memory receipt.
- The continuation test creates its project through ordinary worker dispatch,
  records a user decision through the message service, terminates that run, and
  starts its linked continuation with a local plan. Both the local planning
  context and the subsequent worker payload contain the same historical source.
  Duplicate messages retain their original planning mode across phase changes;
  stale or missing-project continuation requests leave relevant state unchanged.

## Verification

| Check | Result |
| --- | --- |
| Full mobile Jest suite | 527 passed, 36 suites |
| Mobile TypeScript, ESLint, installed dependencies | Passed |
| Expo Doctor | 20/20 passed |
| Full server pytest suite | 1,106 passed, 1 timing-sensitive failure, 9 skipped |
| Isolated notification suite, unchanged tests | 10 passed, including the failing case |
| Worker suites | 413 passed, 5 skipped |
| Python formatting, Ruff, strict app typing | Passed; 61 application modules typed |
| Bandit application scan | No findings |
| OpenAPI agreement | 55 paths, 61 operations, 469 references, 7 JSON schemas |
| iOS archive verifier regressions | 10 passed |
| Native model-store harness | 15/15 passed |
| Core ML Dolphin support harness | Passed |

The complete server run failed the notification test's 50-millisecond deadline
while Xcode was compiling. Its event was already set in the timeout traceback.
The unchanged notification suite then passed all ten tests in isolation.
The relevant broadcast, database relay and authentication functions are identical
to the deployed release. This is consistent with host contention; the full run
is still recorded as a failure rather than relabelled green. No test timeout was
relaxed. Existing optional integration skips remain skips, not runtime evidence.

## Real embedding provider qualification

A private migrated copy used the actual Ubuntu model
`swarmer-embeddinggemma:300m-cpu-85462619ee72`, digest
`a3a329bf4947e5a7acfc3044a9cbfc0ab0001f75c070d2804361bf370b1009ec`.
One HTTP call produced two 768-dimensional vectors in 1.256 seconds and returned
two project-isolated semantic items. A second retrieval used the cache with no
HTTP call, budget credit or database change. No live database was opened by this
qualification and no job or generation was launched.

The copied goal was already started and therefore correctly rejected local
initial planning. The reachable continuation test above covers eligibility
separately with a deterministic embedding fixture. The qualification result
JSON is retained; the post-test SQLite copy was not retained with its WAL and
cannot be used for independent offline reproduction of the final database.

## Database migration rehearsal

A coherent, read-only snapshot of the live schema-23 database was captured at
2026-09-14 00:02:09 UTC. The candidate's canonical `StateService.initialize`
ran on a private copy. All data and SQL objects in the 56 existing tables were
preserved, including pairing records. Only the empty `goal_memory_queries`
table and its indexes were added. Integrity and foreign-key checks passed.

- StateService SHA-256: `c88cde6a7fd975acb2665e03763728a2a55c93a0811fd50378d4ce3ff6367b47`
- Resulting schema SHA-256: `0607c6c42871d3913a504b7e5a26e7b1dc177211f50236be59bfe18d5e8063fa`

The old schema-23 server cannot open schema 24. After accepting migration24,
recovery must preserve the new database and use compatible server code.

## Release boundary

These checks qualify source and a migration rehearsal. They do not demonstrate
deployment, a new signed build, installation, real Dolphin generation with
shared memory, or a completed CRM on the physical iPhone. The iPhone was
unavailable at the last development-device discovery during this qualification.
Record those outcomes separately after execution.
