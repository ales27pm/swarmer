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

Model responses are limited to 1,500 tokens. Initial implementation writes one
small complete file; subsequent iterations can repair several paths. A generation
timeout preserves the previous source and real check receipts, then returns a
continuation for a new charged job. Network and request failures remain distinct
from a timed-out generation; no job retries its model request internally.

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

## Upgrade and recovery

Schema 22 adds project revisions, conversation linkage/messages, reply fencing,
and active-runtime pause accounting. Schema 23 adds private project memory and
idempotent retrieval receipts. Back up the stopped database, environment,
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
