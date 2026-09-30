# Text draft worker

This worker claims only `writing.draft` jobs and returns the actual requested
draft, plan, instructions, or analysis. It uses the user's language, incorporates
conversation replies, and labels genuinely unknown details as assumptions or
open issues. It does not ask the user to supply the deliverable it was asked to
write. Generated text remains an untrusted proposal: validation checks the
envelope, explicit word bounds, distinct supplied citations and required source
domains. It does not establish factual accuracy or compliance with every instruction.

The exact job payload is:

```json
{"schema_version":"1.0","objective":"Rédige un plan pour une application CRM.","conversation":[{"role":"user","content":"Prévoir les contacts et le calendrier."}]}
```

The objective and each conversation message contain 1–4,000 characters. There
are at most 12 messages; roles are `user` or `assistant`. The entire payload,
serialized as compact UTF-8 JSON without ASCII escaping, is at most 32,000 bytes.
Extra fields, NULs, invalid Unicode, and blank strings are rejected. No model
endpoint, model ID, credentials, command, tools, or execution flags can come from
the job.

The optional `research_sources` field accepts at most five completed-job sources,
each with `content_trust: "untrusted"`, `worker_job_id`, `title`, `url`, and
`snippet`. Their compact UTF-8 JSON is limited to 8,000 bytes within the payload
budget. Titles contain at most 240 characters, snippets 700, and public HTTP(S)
URLs 1,000. These are quoted search snippets, not instructions or proof that a
full page was visited. Sources from `research.collect` may additionally carry strict
`evidence` with `kind: page_excerpt`, requested/final URLs, fetch timestamp, body,
extracted-content and excerpt SHA-256 digests, exact excerpt text (up to4,000characters),
and a truncation flag. The citation must match the final URL; the excerpt hash is checked.
When such passages are present, up to6sources/24,000bytes are accepted, still inside
the unchanged32,000-byte payload. The server selects whole sources under that budget
and reserves room for the latest user guidance. A read receipt is not semantic validation.

For a nonempty source list, the private model request replaces URLs with `S1`
through `S6` (up to5without page evidence), keeping the title, snippet, source hostname
and any actually read passage. URLs inside excerpts are omitted in the model projection;
the original evidence digests remain in the canonical job, not attached to transformed text. The objective and conversation remain verbatim,
including any user-authored URLs. The model cites each supported claim using a
supplied marker such as `[S1]` in the delivered text. These markers are the source
selection; the generation schema does not ask for a second `source_ids` list.
The worker independently checks every marker against the admitted sources.
Markers only in a summary do not count. A non-delivery must have no source
markers and never receives a generated bibliography.

The worker rejects all model-emitted HTTP(S) URLs in sourced text/summary, unknown
IDs, duplicate JSON fields, and missing or extra private fields. Repeated citations
to one source are allowed and produce just one reference, ordered by first citation.
It appends `[S1] <original URL>` references only for markers the model put in text.
Legacy responses carrying `source_ids` remain accepted only if that distinct list
exactly matches the markers; the list is removed from the canonical result.
The final text, including these exact URLs, must still fit
the unchanged byte limit and pass the existing citation guard. The server receives
the canonical delivered result below, whose shape is unchanged. Unsourced model
requests and historical canonical results retain their existing format. Selecting
a real source does not prove that its snippet supports a claim; factual relevance
still needs evaluation.

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

The optional `requirements` object has strict integer `min_words`/`max_words`
(1–100,000), `min_citations` (0–5), and up to five distinct lowercase public
`required_source_domains`. Nulls, unknown fields and reversed bounds are rejected.
When absent, explicit FR/EN constraints are extracted from the original objective
and user messages; later user word bounds replace earlier bounds. Assistant and
planner text cannot impose these constraints. This conservative parser covers
common numeric requests, not arbitrary natural-language semantics. The server
stores the extracted contract with the job and independently validates results.

For older callers that omit `requirements`, the worker includes the extracted
contract in the private model input as well as using it for output allocation and
acceptance. This projection does not mutate the original job or conversation.
Explicit requirements are preserved; a request without measurable constraints
keeps its existing input shape. The projected input, including these constraints,
must fit the 32,000-byte UTF-8 payload limit or fail before any model call.

For example, 150–200 words, two citations and domains `docs.python.org` and
`sqlite.org` require that length and two distinct exact supplied URLs covering
those domains. Subdomains match only on a dot boundary. Citation appendices,
source markers and URLs do not pad the word count. Links in a summary do not
satisfy citations required in the delivered document. Selecting an allowed URL
still does not prove relevance or that its page was read.

The model declares `delivered`, `declined`, `needs_clarification`, or
`insufficient_sources`. Delivery retains the four-field legacy result. Every
non-delivery contains its `outcome` and the worker-configured `model_id` (the
model cannot supply provenance). Clarification also requires a bounded specific
`question`. Non-deliveries are submitted as failed jobs with distinct fixed
codes; the server decides whether the goal waits for the user or stops. They do
not have to meet delivery word/citation requirements. No automatic retry occurs.
The generation schema has a separate closed branch for each outcome: only
`needs_clarification` admits and requires `question`. No branch generates a
redundant source selection. The decoder still independently checks the
response, citation use and the meaning of a clarification question.

Inference uses one native Ollama `/api/chat` request and an absolute wall budget
of 1–120 seconds. `think: false` requests final structured content for every
operator alias; reasoning support is not guessed from a model's name. No internal
reasoning stream is stored as the delivered draft. The output allowance adapts
to explicit word bounds with a
512-token JSON/summary reserve, between 1,024 and 8,192 tokens. With no explicit
length, 600 words size the allowance without imposing a word-count target. The
prompt permits requested tables and plans instead of forcing 100–140 words of
prose. The envelope remains capped at 24,000 UTF-8 text bytes; incomplete streams
and token-limit finishes are still rejected. A minimum above 1,800 words returns
`writing_budget_exceeded` before calling the model: splitting long documents into
stages belongs to goal orchestration, not hidden retries in this worker.

Deploy the server's additive requirements and non-delivery support before sending
new-contract jobs or activating this worker. Older persisted delivered results
retain their wire shape; no historical records are rewritten.

CPU inference remains the default (`MONGARS_TEXT_GPU_LAYERS=0`). Operators may
request 1–128 GPU layers through that environment variable, mapped to Ollama's
`num_gpu` option. Invalid values fail at startup before claiming a job. Jobs and
model output cannot change this setting. The integer bound is an input limit,
not a VRAM reservation or a guarantee that Ollama can offload the layers.
Qualify the requested document lengths, actual placement, memory and contention
with the other roles before enabling it; no deadline, retry or acceptance rule
changes with the setting. When roles share the same alias, placement and loading
can affect later planner/evaluator calls (see `docs/33-writing-drafts.md`).
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
