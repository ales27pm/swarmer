# Python proposal worker

This worker claims only `code.generate_python` jobs. It makes one request to an
operator-configured local model and returns a proposed `app.py`. It never writes
the generated source, imports it, executes it, installs dependencies, or starts an
application. AST validation checks syntax only; it does not prove correctness,
safety, or successful tests. The server retains artifact review, approval, and
execution authority.

The exact job payload is `{"objective":"A Python application objective"}`,
with a nonempty objective of at most 4,000 characters. No path, command, model,
URL, credentials, or execution flags can come from a job. The exact result is:

```json
{"schema_version":"1.0","path":"app.py","content":"Python source","summary":"Proposal description; not executed or tested."}
```

Source is bounded to 64,000 UTF-8 bytes; summary to 500 characters. Duplicate JSON
keys, malformed envelopes, truncated generations, wrong paths, invalid Unicode,
invalid Python syntax, and explicit imports outside Python's standard library
fail the job. Relative imports are rejected because the proposal is one file.
This static import check does not prove that dynamic runtime dependencies or
every execution path are valid. There is no inference retry or fallback.
Generated content remains an untrusted proposal even when validation succeeds.

The worker reuses `../file-worker/file_worker.py` for authenticated claims,
opaque lease proof, background renewal, and result fencing. Deploy both sibling
directories together. A failed or uncertain heartbeat suppresses the proposal
result. One polling process handles at most one job at a time; register with
`max_concurrency: 1` and run one process for its credential.

Register through authenticated `POST /agents/register` using a paired device
and skill `code.generate_python`. Save the returned one-time worker credential
outside Git, configure the variables in `.env.example`, and run:

```sh
python3 code_worker.py --once
python3 code_worker.py
```

For a managed Ubuntu release, use `launch_sandboxed.py` instead. The release root
must contain `release.json` with a `worker_sources` mapping from the two sibling
source paths to their SHA-256 digests. The launcher verifies these hashes,
mounts only those sources and `/usr` read-only, provides a private temporary
directory and process namespace, and preserves loopback networking for the
model and control plane. It forwards only the dedicated worker variables;
credentials never appear in its command arguments. Local operator enrollment
and the approval workflow are described in
[the deployment guide](../../docs/29-python-code-proposals.md).

Model URLs must be numeric loopback HTTP(S), or `localhost` (normalized to
`127.0.0.1` without DNS); only the `/v1` base path is allowed. Model calls disable
proxies and redirects and never contain the worker credential. Use a local model
with cloud forwarding disabled at the model service. The worker rejects the
standard `:cloud` model suffix; a loopback address cannot independently prove
that an operator's model service performs all inference locally.

The control plane must support the new skill and proposal evidence contract
before registration. Its normal worker policy, lease generation, and review
approval checks remain authoritative. Returning a proposal does not mean the
user's application has been applied or executed.

Checks from this directory:

```sh
../../server/.venv/bin/ruff format --config ../../server/pyproject.toml --check .
../../server/.venv/bin/ruff check --config ../../server/pyproject.toml .
../../server/.venv/bin/mypy --strict code_worker.py launch_sandboxed.py
../../server/.venv/bin/pytest -q
../../server/.venv/bin/bandit code_worker.py launch_sandboxed.py
```
