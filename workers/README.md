# Worker context protocol

The current worker sources accept the bounded `symbolic_context` envelope and
advertise `context_protocols: ["symbolic-v1"]` in every job claim. This includes
file, research, code-review, code, text, project, personal, SQLite, Swift, and
media workers. Research and code-review have their own clients; the other
families use the shipped file-worker client, including `MediaClient` and Swift's
loaded protocol module. Ship each family's complete dependency closure.

After qualifying and installing these exact sources, the operator can register
the worker with `capacity.symbolic_context_version: 1`. The example agent cards
declare this value under `limits`, which the manifest validator maps to capacity
metadata. For families without an example card, use the same explicit capacity
in the authenticated registration request. Keep each family's existing skills,
limits, credentials, and runtime policy. These source declarations do not update
existing registrations or prove an older deployed binary supports the protocol.

The control plane must accept the claim protocol before starting this cohort.
Workers surface a rejected claim; they never retry by removing the handshake.
For rollback, hold admission and drain active jobs, then use a compatible API and
worker cohort together. Restore the cohort's corresponding registration metadata
only after verifying its binaries. Do not run new symbolic jobs on a legacy
consumer or mark an unverified old worker as supporting symbolic context.

Run the offline wire contract checks from the repository root:

```sh
server/.venv/bin/python -m pytest -q workers/test_symbolic_context_handshake.py
```

The checks load each worker's real client in an isolated interpreter, inspect
the serialized authenticated HTTP claim, and reject any socket connection.
