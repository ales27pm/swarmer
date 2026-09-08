# Read-only file worker

This sample worker implements the authenticated agent job protocol for
`workspace.list_dir` and `workspace.read_text`. Register it from an already
paired device, save the returned agent credential outside Git, then configure
the values shown in `.env.example`.

The worker resolves every path beneath `MONGARS_WORKER_ROOT`, rejects absolute,
escaping, secret-like and protected paths, caps text reads at 1 MB, and never
writes or starts processes. Server-side dispatch and policy authorization remain
authoritative; the worker's checks are defense in depth.

Run one polling iteration:

```sh
python3 file_worker.py --once
```

Run continuously:

```sh
python3 file_worker.py
```
