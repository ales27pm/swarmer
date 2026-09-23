# Project coding and continued conversations (API 0.14)

`code.build_project` develops a versioned, multi-file Python, Node, or combined
project. It replaces the single-file proposal as the preferred software worker
when that capability is online. The legacy `code.generate_python` endpoints and
existing proposals remain readable and approvable.

## User workflow

1. Create and start a software goal. Its planner preserves the requested
   functionality, interface, language, and acceptance criteria.
2. The project worker asks a concrete question when a missing choice materially
   changes the application. Answer in the goal's conversation. Human waiting
   pauses the execution clock; it does not reset step or model-call counters.
3. Each iteration receives the latest conversation and cumulative source
   snapshot. The worker proposes file additions, replacements, or deletions,
   resolves declared dependencies, and runs actual build/test checks in a
   disposable runtime. Failed checks feed the next iteration. File inspection
   can request another bounded context without discarding omitted files.
4. Inspect the plan, files, check diagnostics, and run instructions in the
   project panel. Empty tests or a successful syntax check alone cannot make a
   project ready. Check results describe the isolated runtime, not deployment.
5. Prepare and approve one write for the reviewed revision. The approval binds
   every file to a digest and exact revision. Publication creates a new directory
   at `generated/<project_id>/revisions/<revision_id>` in the configured workspace.
   It never overwrites an existing revision. Start the application using its
   README instructions; publication does not start a public service.
6. Reply with the next change. A running goal incorporates the instruction at
   the next safe iteration. A completed or exhausted goal creates a linked run
   over the same project/conversation, retaining the old run and its counters.
   Continuing a legacy Python proposal retains its `app.py` as starting source.

Replies never approve writes. If a prepared approval is outstanding, new
instructions wait until that operation is resolved. A manual profile permits
one dispatch per explicit reply/start; background iterations remain bounded by
the selected autonomy profile.

## Durable API and privacy

- `GET /goals/{id}/messages` returns the recent chronological conversation,
  active run, and authoritative pending question ID.
- `POST /goals/{id}/messages` accepts a message, client message ID, and optional
  question ID. Retries reuse the accepted message; stale question answers fail.
- `GET /goals/{id}/project` is an authenticated, explicit, `no-store` source read.
- `POST /goals/{id}/project/apply` binds the reviewed revision ID and SHA-256 to
  the existing one-use approval and execution gateway.

Raw source and runner output stay out of bootstrap replicas, notifications,
generic tool previews, episodes, and planner training exports. Goal messages are
private and persisted before any model request. Incoming replies fence stale
planner/evaluator decisions and prevent an older project step from superseding
new instructions. A model completion claim alone never authorizes a write.

## Worker runtime and limits

Each job makes exactly one model call, charged atomically at dispatch. A repair
or inspection uses another node/job and another call. Generation jobs have one
attempt and cannot be automatically redistributed. Existing user-selected step,
runtime, and model-call limits remain authoritative.

Model responses are limited to 2,000 tokens. Each iteration can write one
complete file and use small patches or deletions to repair several paths. Ollama
responses are consumed as bounded NDJSON streams. Code-changing batches retain a
240-second model wall limit so the established operation lease still reserves time
for isolated dependency, build and test checks. A generation timeout preserves the
previous source and real check receipts, then returns a continuation for one new
charged job. A second consecutive timeout pauses instead of charging a third call.
Network and request failures remain distinct from a timed-out generation; no job
retries its model request internally. When source and tests already pass, no newer
user request is pending, and README.md is the sole remaining readiness gate,
generation is constrained to that single documentation file with a 700-token
output budget. Its README content is capped at 1,800 characters and the total
response at 700 tokens. A dependency-free Python project may use a 420-second
model wall, reserving 150 seconds for its two real isolated checks; projects with
dependency manifests or mixed runtimes keep the 240-second wall and larger check
reserve. Prior receipts are not reused for a new project digest.

An evaluator transport or response failure waits at least 60 seconds before an
automatic retry of unchanged state. Three invalid or rejected responses for the
same conversation and worker state pause evaluation and its runtime clock. Use
the app's retry action or send new instructions to recover; the original budgets
and call history remain intact. Invalid evaluation context pauses immediately
without making a provider request. Diagnostics contain fixed categories and
stages, not model response text.

The local Ollama provider requests a 32,768-token context and retains the model
for ten minutes. A stable prompt prefix allows Ollama to reuse its in-memory KV
cache across compatible iterations. Model eviction or restart discards that
cache; it is not the durable record of a conversation.

Project semantic memory is a separate SQLite projection of bounded, redacted
conversation and project-decision summaries. Retrieval is scoped to the linked
project and contributes up to four historical hints; current source, check
receipts, and recent user instructions take precedence. Raw project files and
test output are not indexed. Content hashes, embedding model revision, provider,
prefixes, and vector dimensions prevent reuse of incompatible vectors. Restarted
processes can reuse persisted query receipts and compatible embeddings.

The iPhone MLX planner, Ubuntu planner, evaluator, and project workers retrieve
from this same project-scoped projection. Embeddings and vector storage stay on
Ubuntu; the phone receives bounded historical excerpts and generates its plan
locally. This does not enable the separate general or episodic embedding stores.
The local-plan screen identifies the embedding model, actual retrieval mode,
and number of received excerpts. A new goal without a linked project returns
an explicit `no_linked_project` result with no items.

For an existing terminal project, enter the next instruction in its conversation
and choose **Planifier la suite sur l’iPhone**. The message request sets
`planning_mode: "iphone_local"`; the new linked goal waits in
`awaiting_local_plan` with no automatic dispatch credit. Its memory and recent
conversation are available to the local model before generation. Ordinary
messages keep their existing automatic behavior, while additional messages to
an already waiting local continuation preserve that choice. A server restart
does not start it. After leaving the screen, use **Reprendre le plan sur l’iPhone**
to return to the pending local plan. Starting that continuation requires its
reviewed iPhone plan and current memory fingerprint.

`POST /goals/{goal_id}/memory-context` requires device authentication and the
expected goal update timestamp. The server derives the project and search query
from authoritative goal/conversation records. Planning and evaluation retrievals
have durable receipts, separate from worker-node receipts. The response binds
the provider, conversation revision, project revision, and selected content to
a fingerprint. The phone includes this fingerprint when starting its plan;
the server checks it before starting and again in the plan-write transaction.
Changed context rejects the plan without creating nodes or marking it started.
Successful receipt validation is recorded with the accepted plan.

Historical excerpts cannot override the objective, current user answers or
execution evidence. Planner hints use at most 2,400 characters. The evaluator
includes hints only in space left after its current goal, conversation and
worker evidence. Neither local inference nor a retrieved historical summary
is evidence that a worker executed or a check passed.

An uncached retrieval reserves one model-call credit before its bounded embedding
request, leaving a credit for generation. Cached retrieval does not charge again.
Missing embeddings, exhausted retrieval budget, or provider failure use explicitly
labelled lexical retrieval. They do not stop coding or claim semantic results.
Configure `MONGARS_PROJECT_EMBEDDING_BASE_URL`, `MONGARS_PROJECT_EMBEDDING_MODEL`,
and `MONGARS_PROJECT_EMBEDDING_MODEL_REVISION` in the server environment. This
metered project provider is separate from the optional general-memory provider.
EmbeddingGemma can run on CPU to preserve the coder's GPU residency;
its query/document prefixes follow the
[Google retrieval guidance](https://ai.google.dev/gemma/docs/embeddinggemma/inference-embeddinggemma-with-sentence-transformers).

### CPU embedding setup

With Ollama running on the backend host, pull `embeddinggemma:300m`. The validated
upstream manifest is
`85462619ee721b466c5927d109d4cb765861907d5417b9109caebc4e614679f1`.
Check its full digest with Ollama's [`GET /api/tags`](https://docs.ollama.com/api/tags)
before creating an alias; stop if the mutable upstream tag has changed.

Create a separate CPU alias through [`POST /api/create`](https://docs.ollama.com/api/create)
using this JSON body. These parameters belong to the embedding alias and leave
the coding model's GPU configuration unchanged:

```json
{
  "from": "embeddinggemma:300m",
  "model": "swarmer-embeddinggemma:300m-cpu-85462619ee72",
  "parameters": {"num_gpu": 0, "num_ctx": 2048},
  "stream": false
}
```

Read the alias's own full digest from `/api/tags` and configure the dedicated
project provider in the private server environment. The validated alias digest
is shown below; use the digest of the alias actually installed, not the upstream
tag's digest. Changing this identity invalidates incompatible stored vectors.

```dotenv
MONGARS_PROJECT_EMBEDDING_BASE_URL=http://127.0.0.1:11434/v1
MONGARS_PROJECT_EMBEDDING_MODEL=swarmer-embeddinggemma:300m-cpu-85462619ee72
MONGARS_PROJECT_EMBEDDING_MODEL_REVISION=a3a329bf4947e5a7acfc3044a9cbfc0ab0001f75c070d2804361bf370b1009ec
MONGARS_PROJECT_MEMORY_QUERY_PREFIX="task: search result | query: "
MONGARS_PROJECT_MEMORY_DOCUMENT_PREFIX="title: none | text: "
```

The prefix trailing spaces are intentional. These settings enable metered
project retrieval; they do not require configuring the separate general-memory
`EMBEDDING_*` provider. With no project embedding provider, coding remains usable
with explicitly labelled lexical memory.

### Source and execution limits

Snapshots are bounded to 80 text files, 64,000 UTF-8 bytes per file, and 1,000,000
bytes total. Paths are canonical relative names; credential files, traversal,
case collisions, and file/directory collisions are rejected. Context selection
is bounded; unselected files remain in the durable snapshot. The configured
local model determines coding quality. The runtime does not claim support for
unavailable platforms, external services, native toolchains, or public deployment.

The operator builds a Docker image from pinned Python 3.12 and Node 22 images,
then configures its immutable local image ID. Only sanitized, exact dependency
version declarations enter a separate dependency-install stage. Its proxy
permits the public Python/npm registries; install lifecycle scripts and arbitrary
package URLs are rejected. Generated code runs afterward with no network, no
host home/workspace, no worker credential, no Docker socket, read-only inputs,
and resource/time/output limits. Test receipts require actual nonempty tests.

See `workers/project-worker/README.md` for runtime setup and configuration.
Enroll locally under the actual Unix operator identity with:

```sh
python -m app.worker_admin --kind project \
  --db /private/path/mongars.db --permissions /private/path/permissions.yaml \
  --credential-file /private/project-worker/registration.json \
  --model swarmer-project-qwen3-coder:30b-32k-06c1097e
```

Keep the registration and dedicated environment private and outside Git. The
worker launcher verifies its shipped sources against the release manifest. Its
scratch directory must be operator-owned and mounted at the same absolute path
visible to the Docker daemon; a namespace-private `/tmp` is not a Docker bind
source on the host.

### Root and nested AGENTS.md guidance

The project worker resolves `AGENTS.md` from the accepted file snapshot. For
`src/data/store.py`, guidance applies in order from `AGENTS.md`,
`src/AGENTS.md`, then `src/data/AGENTS.md`. Nearest-directory guidance refines
ancestor guidance; sibling folders do not apply. Explicit user instructions and
runtime constraints take precedence. Guidance is versioned project content,
not execution permission or proof that a check succeeded.

The prompt pins complete applicable guidance with its path, scope, content hash
and base revision. Required instructions that exceed the context budget produce
an explicit failure rather than silent truncation. Other source files are read
selectively through `focus_paths`; the transport still carries the complete
bounded snapshot. This is selective prompt loading, not network range retrieval.

Before a create, edit, patch or deletion, all applicable guidance from the old
revision must have appeared in the final prompt. Otherwise the worker preserves
the snapshot and returns a focused read for the missing instructions. The next
iteration remains metered. Worker-owned `guidance_reads` receipts are validated
again on the server and exposed in the project preview. A stale or fabricated
source hash cannot authorize accepting a changed snapshot.

Agents can create and maintain Markdown files, including `AGENTS.md`, through
normal revision-bound edits. An instruction edit governs later iterations; it
does not change the rules for the current mutation. Model-authored observations
must remain distinguishable from user requirements and verified check evidence.
No host filesystem is searched for project instructions, and existing user
projects are not modified merely by enabling this feature.

### On-demand reads and native source transfer

After final prompt compaction, files whose complete content is already visible
are removed from the model's read-operation schema. Omitted files and partial
fragments remain readable. The worker also rejects a redundant read locally if
the model disregards that schema. A rejection changes no source or check receipt
and consumes no hidden retry. Existing progress limits still apply; this does
not guarantee completion of an arbitrary project.

Native compilation has a separate, device-authenticated transfer API:

| Route | Purpose |
| --- | --- |
| `POST /goals/{id}/project/swift-validation` | Approve the exact latest revision, digest, worker, operation and target; enqueue once |
| `GET /goals/{id}/project/swift-validation` | Read revision-bound status and validated successful receipt |
| `POST /goals/{id}/project/swift-validation/{validation_id}/cancel` | Revoke execution and cancel its task/job; preserve project files |
| `POST /agents/{agent_id}/jobs/{job_id}/project-source` | Fetch the approved source with the current authenticated job lease |

The request requires `execution_consent: true` (a JSON boolean) and an
idempotency key. Approval is valid for 15 minutes, one job and one worker; a
conversation change, newer source revision, cancellation or expired lease
prevents execution/results from using that approval. It is not a grant to run
future revisions or to save generated files in the user's workspace.

The iMac worker must explicitly enable `--project-staging-root` with a private
0700 directory outside its credential directory. It retrieves the snapshot,
checks both canonical project and filesystem hashes, stages all bounded text
files in a fresh directory, and executes the selected SwiftPM/Xcode command.
Source approval is rechecked before each command, during execution and before
returning a receipt. Reserved runtime directories, symlinks and path collisions
are rejected. The existing fixed-workspace mode cannot execute these jobs.

Compilation executes code under the opted-in Mac account; staging is not a
sandbox. The UI therefore asks for review and approval of this exact revision.
The unsigned compiler lane has a 120-second budget and requires dependencies
already available to the configured toolchain. See the Swift worker README for
destination configuration and evidence limitations. A native test receipt stays
separate from Python/npm project checks and never automatically completes a goal.

## Upgrade and recovery

Schema 22 adds project revisions, conversation linkage/messages, reply fencing,
and active-runtime pause accounting. Schema 23 adds private project memory and
idempotent retrieval receipts. Schema 24 adds `goal_memory_queries` for shared
planner/evaluator retrieval receipts and preserves existing pairing records.
Schema 25 adds durable context/compaction storage. Schema 26 adds the native
validation grants without changing stored project snapshots or pairing records.
The schema-23 server cannot reopen schema 24: after accepting this migration,
recover forward with a compatible server; do not perform a code-only rollback.
Back up the stopped database, environment,
policy, and release link before cutover; rehearse migration and foreign-key/
integrity checks against a copy. An older worker-policy epoch continues to deny
the new skill until explicit reload. Enable only the project worker after its
authenticated heartbeat and isolated runtime checks pass.

Source revisions survive restarts. An interrupted application preparation can
resume its existing child task; approved writes are not replayed automatically.
Incomplete publication remains private staging, and atomic exclusive rename
exposes only the complete revision. A crash after publication but before its
durable receipt is an uncertain outcome, not an automatically retried write.
Rolling back to schema-21 code requires its corresponding stopped DB snapshot.
