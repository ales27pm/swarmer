# Planner validation and recovery diagnostics — 2026-09-23

## Observed failure

The reported goal remained in planning with no persisted nodes. Read-only production
inspection confirmed repeated planner calls classified as `invalid_response`, a healthy
API, matching installed planner sources, and available worker capabilities. The previous
runtime retained neither a rejection audit record nor the underlying parser diagnostic.
The exact cause of those historical responses therefore cannot be reconstructed.
The user's goal and project files were not resumed, cancelled or modified by this work.

## Reproduced contract defect

The model generation schema accepted arbitrary `worker_arguments` objects for
non-specialist workers and synthesis, although the public validator rejected unsupported
payloads. Twenty-two planner/evaluator regression cases demonstrated this disagreement.
The narrow production release already constrained these payloads to null; this defect
in the main branch therefore does not establish the cause of the production screenshot.
The generation schema now constrains these server-derived payloads to null. Specialist
operations keep their bounded argument schemas; valid legacy public API requests remain
compatible. No invalid response is silently repaired or accepted.

## Diagnostic and recovery changes

- The JSON, proposal, dependency, capability and budget validators attach bounded,
  server-defined diagnostic codes. Raw model text and exception identifiers are not
  copied into the diagnostic, public reason or audit payload.
- A rejected planner call records one `goal.plan.rejected` hash-chained audit event in
  the same transaction as the failed call and recoverable goal state. Expired calls,
  terminal goals and superseded conversations cannot overwrite newer state.
- A later, separately charged planner call can receive a fixed corrective instruction
  for the latest rejected call in the same conversation. Successful, transport-failed,
  old-conversation and unknown diagnostics do not supply instructions. Existing cooldown,
  call accounting, capability validation and approval requirements are unchanged.
- The iOS goal screen retains the server diagnostic after refresh and reopening instead
  of hiding it whenever the generic invalid-plan notice is present.

## Verification

Initial diagnostic regressions failed before implementation. A separate regression also
demonstrated the stale-conversation overwrite before its explicit fence was added.
The focused server integration suite passed 79 tests. The generation-schema suite and
neighboring planner/evaluator contracts passed 195 tests. The mobile goal screen suite
passed 56 tests, including initial load, refresh and reopening without an implicit retry.
The integrated server suite passed **598 tests** in 155.89 seconds. Ruff, formatting,
five-module mypy and the diff checks passed. Independent review found no blocking issue.
The root reran the mobile suite: **56 tests passed**, including refresh/reopening.
Release evidence is recorded below when completed.

This corrects a demonstrated schema inconsistency and makes future rejections diagnosable;
it is not evidence that the unseen historical response had that precise defect, nor that
an arbitrary generated project will complete successfully.
