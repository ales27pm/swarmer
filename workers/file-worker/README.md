# Read-only file worker

This sample worker implements the authenticated agent job protocol for
`workspace.list_dir` and `workspace.read_text`. Register it from an already
paired device, save the returned agent credential outside Git, then configure
the values shown in `.env.example`.

The worker resolves every path beneath `MONGARS_WORKER_ROOT`, rejects absolute,
escaping, secret-like and protected paths, caps text reads at 1 MB, and never
writes or starts processes. Server-side dispatch and policy authorization remain
authoritative; the worker's checks are defense in depth.

For each claimed job, the worker echoes the opaque `claim_token`, `lease_id`,
and `lease_generation` supplied by the control plane. It renews that lease in a
background heartbeat while local work or an optional iPhone capability request
is pending. A stale lease (`HTTP 409`) or an uncertain heartbeat suppresses the
terminal result so a replacement worker cannot be overwritten.

Jobs may explicitly include one brokered phone capability in their payload:

```json
{
  "path": ".",
  "capability_request": {
    "capability_name": "iphone.location.current",
    "arguments": {}
  }
}
```

The worker creates and polls that request through the authenticated control
plane, and includes the completed broker result under `capability_result`. It
has no direct route or credential for the iPhone. Denied, failed, expired, or
cancelled capabilities safely fail the worker job. This opt-in broker step does
not add filesystem-write or process-execution skills.

`MONGARS_JOB_HEARTBEAT_SECONDS` and `MONGARS_CAPABILITY_POLL_SECONDS` tune the
two intervals. The heartbeat interval must remain comfortably below the
control-plane lease duration.

Run one polling iteration:

```sh
python3 file_worker.py --once
```

Run continuously:

```sh
python3 file_worker.py
```
