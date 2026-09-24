# General continuation routing and bounded worker handoffs

## Problem

A user message on a goal with a saved coding project previously bypassed the
planner and always dispatched `code.build_project`. A request for research or
writing therefore reached a worker with neither that operation nor the needed
inputs. The existing no-progress guard correctly rejected empty edits but could
not repair the routing. The planner also required project work to be the only
node, preventing research followed by implementation.

## Change

- New user instructions go through the normal capability-aware planner. Internal
  iterations of an already selected project worker retain their direct path.
- Mixed plans can select independent workers or dependency chains. At most one
  project-mutating worker is allowed per plan. Legacy single-file generation
  retains its narrower input contract.
- The server builds bounded handoffs for project and writing workers from
  completed jobs belonging to the same goal and instruction revision. Job,
  task, skill and dependency identities are verified before dispatch. Summaries
  and research citations remain untrusted input, never execution authority.
- Partial project iterations transfer downstream dependencies to the successor
  atomically. An unrelated active worker cannot cause a downstream step to run
  against an unfinished project. Upstream evidence is retained for the next
  iteration without repeating the research.
- The writer retains the user's overall objective and latest guidance, with a
  separate bounded assignment for its particular plan step.
- Reply races, duplicate deliveries, failed planning and stale evidence preserve
  accepted files. Planner failures retain the pending instruction and use the
  existing retry cooldown. Manual-mode dispatch credit and actual model-call
  accounting are unchanged; routing a new reply consumes a planner call.

## Scope and resource limits

The existing scheduler enforces goal parallelism, fresh worker availability and
per-agent concurrency. This change enables mixed plans to use those slots; it
does not add a GPU/RAM admission controller or increase configured limits.
Structured specialists retain their specific validated input contracts. No
arbitrary tool or credential access is introduced. A search supplies titles,
URLs and snippets, not proof that complete web pages were read.

The project completion checks, no-progress pauses and explicit file-application
approval remain in force. Switching agents is not evidence that a model can
finish any requested project.

## Qualification

Benign CRM and writing fixtures cover code-to-research-to-code, code-to-writing,
failed research, replay, manual credit and partial-result dependency transfer.
Concurrency fixtures exercise two independent claims with a third waiting,
hard and optional downstream dependencies, and a reply arriving between the
initial read and transaction lock. Separate handoff tests alter authoritative
SQLite relationships and exercise Unicode/byte limits and untrusted snippets.

The broad server run passed 2,007 tests and skipped 9. It exposed 9 failures in
the then-current writer objective and interrupted application recovery paths.
Both were corrected; rerunning all five affected/continuation modules passed
33 tests, including the new malformed-plan recovery case. The final focused
handoff/continuation integration run passed 79 tests; the separate planner,
writer and legacy-contract run passed 291. The project worker suite passed
461 with 5 skips. Ruff, mypy and diff checks passed for the changed sources.
These counts overlap and must not be added together.

The production release backports only this change onto the existing runtime:
the repository also contains unrelated undeployed features. Live project files
and the schema were preserved. Deployment and real-model evidence follow.

## Live model qualification

On 2026-09-24 at 04:22–04:24 UTC, source `e654659` was exercised in a temporary
local database with a benign, seeded CRM snapshot. The actual Ubuntu planner
selected `research.query` followed by `writing.draft` for a new request for
Python sqlite3 documentation and a French explanation. It did not dispatch
another coding iteration. Actual SearXNG returned five results; the actual text
model returned a French draft with three source links. There was one planner
request (22.766 s), one search request and one writer request (62.599 s), with no
retry. Model-call accounting increased from 2 to 4.

The snapshot digest before and after was
`cca6c65bdb2937c2f7f9f072d000d05ae3c4a18874439254d429ad19ca2898ef`.
No production database was accessed, no file application or approval was
created, and the private SSH forwarding was stopped. The fixture was not a
model-generated CRM; this proves live routing, search and sourced drafting,
not full application completion or physical iPhone behavior.

Private receipt: `Library/Logs/SwarmerDeploy/agent-routing-20260924/live-model-qualification/run-bmogxf84/receipt.json`.
SHA-256: `726ae1327032df6be86a9c07d461c915915abf12722b3cc9537e3346f787d478`.

## Production deployment

Source commit `e654659c26dc4b030d65441636c02c0f920c0d99` was pushed to both
`origin/main` and `vibecode/main`. Reviewed narrow backports were deployed on
Ubuntu rather than shipping unrelated changes from the source tree.

| Component | Active immutable release | Independent verification (UTC) |
| --- | --- | --- |
| Project worker | `e9de98269e1f18413871139a446bd77442d5986f-44acc1e0deb8` | 2026-09-24 04:33:20 |
| Text worker | `e654659c26dc4b030d65441636c02c0f920c0d99-48a389352797` | 2026-09-24 04:36:41 |
| API | `ff2f31ee6f91cf68e84691a846a1ae4c6ab8c1e0-8eb0e92e5c21` | 2026-09-24 04:40:45 |

The final API verification saw six online workers with fresh authenticated
heartbeats. The 37 protected database fingerprints stayed identical through
all three cutovers, including 14 projects and 318 saved revisions. There were
no active jobs or goals at cutover. No project was cancelled or resumed, and no
database restoration was performed. Schema version 26, model configuration,
worker identities, credentials and runtime bindings remained unchanged.

A separate root check at 04:41:06 UTC verified the exact API release, the
installed handoff module hash and HTTP 200 with health `ok` (API 0.14.2).
The installed `worker_context.py` SHA-256 is
`a48839a43e65e6ad37b6abf58654c044231a530d595146846161d3cdb9bd86b8`.
Production readiness was checked without submitting a goal to the live database;
the real inference exercise above used its own temporary database. No physical
iPhone test or new TestFlight binary is claimed by this backend deployment.

Private final API receipt:
`Library/Logs/SwarmerDeploy/agent-routing-20260924/receipts/api/independent-verification.json`.
SHA-256: `e562e21220e1d3acb0f7b66f97f95bee31ac4b2f43581df42e92849445fc0521`.
