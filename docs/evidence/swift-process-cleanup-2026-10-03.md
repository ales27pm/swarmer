# Swift worker process-group cleanup — 3 October 2026

The full server suite exposed a pre-existing macOS failure in
`test_process_timeout_and_output_limit`: cleanup raised `PermissionError`, hiding
the intended output-byte-limit error. Both the worker and its test were unchanged
from `e4d52d1` when it failed.

A deterministic native check under UID 501 reproduced the cause: the child had
terminated and was a zombie, but had not been reaped. Both group signals returned
EPERM; waiting for the child then returned exit code 0. Apple's
[XNU group-signal implementation](https://github.com/apple-oss-distributions/xnu/blob/main/bsd/kern/kern_sig.c)
excludes zombies and can return EPERM when no signalable member remains.

The fix handles EPERM only by polling/reaping a finished leader and attempting
the same group signal once more. A disappeared group is accepted; a live child
or persistent permission refusal remains an error. Remaining descendants are
still targeted by the process-group shutdown. The output pipe is closed even
when shutdown fails.

Verification:

- Six deterministic cases cover zombie reaping, live and persistent refusals,
  surviving descendants, the original output-limit diagnostic and pipe closure.
- All 24 `tests/test_swift_worker.py` tests passed as `ales27pm` in 23.68 seconds,
  including the small real SwiftPM project.
- A separate native zombie check succeeded with the changed `_stop`, reaping
  exit code 0; Ruff, formatting and `git diff --check` passed.

A final review added the case where the leader exits between the TERM deadline
and KILL escalation. The shared signal helper also accepts ESRCH at that point,
while keeping both waits bounded and permission refusals explicit. After this
delta, all 24 non-compilation Swift cases passed in 0.32 seconds. The real SwiftPM
case passed in the preceding full Swift run; no native runtime deployment is
inferred from either result.

This is a local worker correction, separate from the memory integration. These
results do not establish a production Swift-worker deployment.
