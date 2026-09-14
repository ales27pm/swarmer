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

## Verification

| Check | Result |
| --- | --- |
| Full mobile Jest suite | 508 passed, 36 suites |
| Mobile TypeScript, ESLint, installed dependencies | Passed |
| Expo Doctor | 20/20 passed |
| Full server pytest suite | 1,094 passed, 9 skipped |
| Worker suites | 413 passed, 5 skipped |
| Python formatting, Ruff, strict app typing | Passed; 61 application modules typed |
| Bandit application scan | No findings |
| OpenAPI agreement | 55 paths, 61 operations, 469 references, 7 JSON schemas |
| iOS archive verifier regressions | 10 passed |
| Native model-store harness | 15/15 passed |
| Core ML Dolphin support harness | Passed |

The final memory-specific rerun covers the literal SQL alternatives introduced
after the full server suite started. Existing optional integration skips remain
skips, not runtime evidence.

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
