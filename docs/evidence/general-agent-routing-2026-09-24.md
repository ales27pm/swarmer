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

The deployed immutable release and real-model qualification are recorded after
activation. Production must backport only this change onto the existing
runtime: the repository also contains unrelated undeployed features. The live
project files and schema are not migration targets.
