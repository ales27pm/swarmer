# Personal assistant implementation qualification — 2026-09-22

This records implementation and local qualification of the approved research plan.
It is not a claim that new workers, feature flags, CRM migrations or iPhone
embeddings are active in production. The separately built TestFlight client is
commit `7c51785b3b316d3ed88f29b5afc95854227335c2`, build `20260922181000`.

## Context and shared memory

`ProjectContextService` stores immutable, versioned snapshots derived from original
project messages and accepted revisions. User requirements retain their source IDs;
assistant proposals remain unverified; accepted edits and check receipts have
separate fields. Sources can be retrieved within their project. Changed roles,
messages or revisions invalidate the fingerprint. More than 10,000 source messages
requires archiving instead of silently dropping old requirements.

The planner and project worker accept the protected requirements. The optional
compactor summarizes assistant proposals only, keeps sources accessible, reserves
a model-call lease and a subsequent generation call, and accepts a summary only
while its source fingerprint and lease remain current. It starts at 75% of the
input budget. The default counter conservatively counts UTF-8 bytes; exact model
tokenizers remain to be integrated. Oversized protected state blocks dispatch.
Transport, timeout and malformed-summary failures preserve original sources.

An isolated synthetic proposal was sent to the real Ubuntu planner model
`swarmer-research-qwen35:9b-8k-6488c96fa5fa`. The initial request exhausted 1,024
output tokens without content. With `reasoning_effort=none`, the same proposal
returned a complete, source-validated summary in 2.35 seconds, using 227 input
and 91 output tokens. This is one transport/contract qualification, not a quality
or latency benchmark. The provider caps source JSON at 5,000 bytes to leave room
for instructions and output on the 8K model. Private raw receipts are stored in
`/tmp/mongars-real-compaction-probe.json` and
`/tmp/mongars-real-compaction-none-probe.json` for this session.

The shared retrieval service optionally combines lexical and vector rankings with
reciprocal rank fusion. Changing retrieval mode invalidates cached retrieval
receipts while preserving vector identity. Missing embeddings still produce an
explicit lexical fallback. iPhone E5 vectors are not silently mixed with Ubuntu
embeddinggemma vectors or substituted for its index.

The 50 deterministic FR/EN fixtures retain all 100 critical facts with structured
state, versus 50 for the disclosed recent-window baseline and 81 for observation
masking. Four oversized cases explicitly refuse dispatch; 46 fit. No foreign
project source or changed receipt projection was observed. The fixture comparison
does not measure LLM task completion, embedding quality, actual tokenizer counts
or iPhone performance. See `evals/context/README.md` for the full methodology.

## Tools and routing

The catalog includes SQLite, Swift, Python and quality roles plus typed specialist
arguments. New capability names do not count as live agents. Planner output must
provide the operation's actual arguments; dispatch revalidates them. Availability
still requires authenticated registration, policy and a healthy worker.

* SQLite: inspect, parameterized read-only query, create, transactional migration
  and backup, with integrity checks, hashes and stable migration receipts.
  Workspace isolation and a protected control-plane database path are required.
* Swift: fixed SwiftPM/Xcode build and test commands, source hashes, deadlines,
  cancellation and actual test receipts. Swift package compilation and one XCTest
  have been exercised locally; physical Xcode deployment is a separate check.
  This is a compiler/test worker, not yet a Swift source-editing pipeline.
* Python: immutable runtime tool pins and an isolated Ruff correctness gate before
  byte compilation. Generated Ruff configuration cannot weaken the gate. The
  updated Docker image still requires rebuild and deployment.
* Documents: actual bounded UTF-8 TXT/Markdown extraction with source hashes.
  Docling PDF/DOCX support is an optional profile whose dependencies, model
  artifacts and real corpus qualification remain pending.
* CRM: bounded HTTPS adapter for the existing 27PM CRM, with stable mutation keys.
  Its counterpart endpoint/migration is implemented in the separate `crm-v2`
  repository. It still requires deployment, service credentials and live readback.
  There is no outbound email or delete operation in this connector.

The worker READMEs describe their exact deployment contracts and limitations.
Compiler command allowlists and SQLite authorizers are not OS sandboxes.

## iPhone client

The client adds six agenda capabilities with native permission checks, explicit
approval, idempotency and readback. Older clients are protected by the server's
disabled-by-default agenda feature flag. Real Calendar/Reminders behavior remains
to be tested on the user's iPhone.

The experimental MLXEmbedders runtime uses pinned
`intfloat/multilingual-e5-small@614241f622f53c4eeff9890bdc4f31cfecc418b3`, explicit
query/passage prefixes, mean pooling and normalized 384-dimensional vectors.
Inputs are capped at eight texts and 512 tokens each. Models are preserved in the
durable Documents model store. Generation and embedding model loads are mutually
exclusive. The Settings panel provides explicit load, test and unload controls.

The generic iOS Debug build compiled successfully. The mobile release commit
passed typecheck, lint, 750 Jest tests, Expo Doctor's 20 checks, eight compiled
embedding validation cases and the ten archive-verifier regression tests.
The iPhone is away from the development network, so no physical E5 download,
inference, memory or energy measurement is claimed. TestFlight packaging,
submission and Apple processing are recorded separately.

The optional project-context request tolerates a 404 from an older backend while
preserving pairing-change checks and all other failures. The memory-server
diagnostic requires the new endpoint to be deployed.

## Activation order and remaining gates

Final local backend qualification: 1,696 server tests passed, nine skipped;
290 worker tests passed, five skipped; server and specialist strict type checks,
Ruff and Bandit passed. The separate CRM integration passed 116 tests plus its
TypeScript and lint checks. OpenAPI validation covers 61 paths, 67 operations,
502 references and seven JSON schemas. These are development checks, not live
deployment receipts.

All four flags default to false:
`MONGARS_PROJECT_CONTEXT_ENABLED`, `MONGARS_PROJECT_COMPACTION_ENABLED`,
`MONGARS_PROJECT_MEMORY_HYBRID_ENABLED`, `MONGARS_IPHONE_AGENDA_EXTENDED_ENABLED`.

Before activation: validate schema 24→25 on a database copy; deploy matching
API/project-worker contracts with active-job protection; register isolated new
workers only after their actual execution checks; deploy the CRM migration and
service endpoint; qualify iPhone agenda and E5 through the new TestFlight build.
The text-writing worker's older conversation-window path still needs the same
durable-context integration. Full-model FR/EN evaluation and updated runtime-image
qualification remain separate from the deterministic fixture suite.
