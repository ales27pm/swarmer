# Text draft worker

This worker claims only `writing.draft` jobs and returns the actual requested
draft, plan, instructions, or analysis. It uses the user's language, incorporates
conversation replies, and labels genuinely unknown details as assumptions or
open issues. It does not ask the user to supply the deliverable it was asked to
write. Generated text remains an untrusted proposal: validation checks the
envelope and limits, not factual accuracy or compliance with every instruction.

The exact job payload is:

```json
{"schema_version":"1.0","objective":"Rédige un plan pour une application CRM.","conversation":[{"role":"user","content":"Prévoir les contacts et le calendrier."}]}
```

The objective and each conversation message contain 1–4,000 characters. There
are at most 12 messages; roles are `user` or `assistant`. The entire payload,
serialized as compact UTF-8 JSON without ASCII escaping, is at most 32,000 bytes.
Extra fields, NULs, invalid Unicode, and blank strings are rejected. No URL,
model, credentials, command, tools, or execution flags can come from the job.

The exact result is:

```json
{"schema_version":"1.0","content_trust":"untrusted","text":"Plan proposé…","summary":"Plan proposé pour révision ; aucune exécution effectuée."}
```

Text is limited to 24,000 UTF-8 bytes; summary to 1,200 characters. Both must be
nonempty, valid Unicode without NULs. Strict JSON parsing rejects duplicate
fields and non-finite values. The stream must terminate with `done: true` and
`done_reason: "stop"`; token-limit stops, malformed output, tool calls, truncated
JSON, and incomplete streams fail without publishing a draft. No response is
repaired or retried.

Failed generation logs only fixed reason codes, such as `token_limit`,
`wall_timeout`, `invalid_json`, or `transport_error`. Generated text, model error
bodies, user inputs, credentials, and exception messages are never logged.
The control-plane failure response remains fixed and contains no partial draft.

Inference uses one native Ollama `/api/chat` request, at most 512 output tokens,
and an absolute wall budget of 1–120 seconds. The prompt requests a complete
100–140-word plain-prose draft (5 concise steps for a plan and one short line for
assumptions, dependencies, and limits) with a summary within 80 characters,
including JSON overhead within the token budget. The result contract retains its
larger byte and character bounds; the generation budget does not relax validation.
CPU inference (`num_gpu: 0`) limits the writer's GPU use. When roles share the
same alias, Ollama may reuse its CPU placement for later planner/evaluator calls;
their deadlines must account for CPU latency (see `docs/33-writing-drafts.md`).
The worker sends no download or unload requests. Set an existing small local model alias through
`MONGARS_TEXT_MODEL_ID`; there is deliberately no invented default alias.

Model URLs use numeric loopback HTTP(S), or `localhost` normalized to
`127.0.0.1`. Optional `/v1` is accepted as a base path for consistency with the
sibling validator and mapped to Ollama's native endpoint. Model requests use
`http.client`, which does not use environment proxies or follow redirects. They
never contain the agent credential. Cloud aliases are rejected, but the operator
must also disable cloud forwarding in the configured model service.

The worker reuses `../code-worker/code_worker.py` for URL/model validation and
`../file-worker/file_worker.py` for authenticated claims, opaque lease proofs,
heartbeats, and result submission. Deploy those three source files together.
The lease is checked before, during, and after generation and synchronously
renewed before submission. Losing the lease closes the connection and suppresses
the result. A supervised transport enforces the wall budget even if headers or
a slowly arriving body would keep resetting a socket inactivity timeout. A
transport that has not finished closing prevents another model request.

Register using `writing.draft`, `max_concurrency: 1`, and the configured model ID.
Keep the returned credential outside Git. Configure `.env.example`, then run:

```sh
python3 -B text_worker.py --once
python3 -B text_worker.py
```

Run the worker as an unprivileged account. It never writes generated files,
executes code or tools, invokes a shell, installs packages, or sends external
messages. The server retains project, policy, and approval authority. Logs use
fixed failure messages and never print prompts, drafts, credentials, or response
bodies. The source declarations are not an OS network/filesystem sandbox; apply
deployment isolation separately if required.

Local checks:

```sh
../../server/.venv/bin/pytest -q .
../../server/.venv/bin/ruff check --config ../../server/pyproject.toml .
../../server/.venv/bin/mypy --strict text_worker.py
```
