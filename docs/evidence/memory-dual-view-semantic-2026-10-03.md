# Versioned native/pivot semantic retrieval — 2026-10-03

Status: locally validated source candidate. No deployment or device-runtime claim.

## Behavior and identity

Schema 31 adds empty `memory_view_embeddings` storage keyed by memory, revision, view and provider. Each vector carries source identity/hash, exact view hash, projection signature, item revision and dimensions. Canonical text and original evidence remain separate qualified views of one logical memory. The former `memory_embeddings` table remains an index-view-only compatibility projection; it is never used as proof of an original-language vector.

A scoped search prepares original and English query texts, deduplicates equal inputs, and makes one embedding request under the existing admission/execution contract. HTTP results are associated through their explicit indices, with strict batch count, dimensions, numeric and nonzero validation inside the receipted operation. A singleton response without an index remains unambiguous. A malformed batch cannot contribute a partially trusted stream.

The SQL reader applies scope, kind and sensitivity before candidate selection. It scans current qualified per-view vectors in pages of 64 without the old 500-recent-memory cutoff. It checks head, receipt, source, provider, view and revision bindings before retaining the top 50 of each semantic stream. Lexical retrieval continues to use the maximum overlap from original/pivot text.

Fusion uses three declared streams: lexical, original-query/original-view and English-query/canonical-view. Each memory gets at most one vote per stream and appears only once. Rankings use one-based RRF with k=60 and depth=50 per stream, scaled by 61/active-stream-count into 0..1. This is a truncated candidate fusion, not an exhaustive global RRF ranking. Equal query texts save an embedding input but do not collapse document roles: native and canonical views can differ. When the two views are identical, they still supply the two declared semantic-role votes; these are not two independent evidence sources. Public results expose `score_kind` and ranking provenance; lexical-only fallback retains overlap scores.

Presentation revalidates current source/view identity and provider/normalizer/presenter configuration after asynchronous work. No retrieval result can promote itself into policy.

## Write, rebuild and migration

New schema 31 writes create an indexing intention for every current view in the same domain transaction. Each worker call retains its own view/revision lease and global unknown-request fence. Editing or forgetting purges all affected view vectors atomically; pinning updates current item revision without inference. An explicit two-view index request captures one head revision and cannot continue onto a later revision after an awaited call.

Explicit backfill pages at most 100 logical memories and dispatches at most 100 missing views per provider request. Two-view pages may require two requests. Already-current qualified views are skipped. Counts for scanned/indexed/unchanged/failed/conflicted refer to logical memories; view and batch counts are reported separately. Failure/conflict holds the input cursor, and retry preserves successful views. An unknown remote outcome remains fenced across cancellation/restart; observation timeout is not proof of completion.

The 30→31 migration preserves all prior tables, rows, rowids, sequence values, old vector cache, and pending/claimed/obsolete intention ownership. It changes only the deduplication index expression and adds empty per-view storage. It enqueues nothing and calls no models. Startup on schema 31 validates the installed schema. Historical 29/30 migration contracts remain unchanged.

## Verification

Tests use disposable local databases and deterministic providers. They do not establish translation quality, embedding-model relevance, production latency or iPhone behavior.

The broad run completed with **4,094 passed, 16 failed and 11 skipped** in 913.26 seconds. Its imported source preceded the final numerical optimization. Twelve failures were historical-migration fixtures that downgraded `user_version` but left schema-31 objects in place; four were a connection test double that did not implement the async context-manager contract. The fixtures were corrected without relaxing runtime migration checks or their domain assertions. Each failing group passed its focused rerun. This broad run is not represented as an all-green run of the final revision.

**Final affected suite: 998 passed, no failures, one existing Starlette deprecation warning, in 185.75 seconds across 50 files.** The source manifest remained unchanged throughout the run and matched the files prepared for commit. This includes every previously failing test and the optimized numerical implementation. Commands run as the normal repository owner, using `python -I -B`, an explicit worktree import path and `pytest -p no:cacheprovider`. The runner records before/after SHA-256 manifests and asserts the loaded `state_service` path. The numerical suite covers 600 seeded vector pairs plus extreme/subnormal values, invalid types and dimensions, unchanged input arrays, and top-50 ordering. The focused numeric/search suite passed 137 tests before final formatting.

Ruff check and format verification passed for all 36 changed Python files; `git diff --check` passed. Mypy checked the 10 changed runtime modules and still reports two pre-existing imported-module errors: the model literal override in `media_contracts.py:57` and the unavailable `redis.asyncio` module/stub in `message_board.py:545`. The changed numerical modules pass focused mypy; no repository-wide clean type-check claim is made.

Private receipts are under `~/Library/Logs/SwarmerQualification/Memory/goal-model-admission-20261003/dual-view-semantic-20261003/` (`full-suite.json`, `full-suite.log`, final affected-suite manifest/log and type-check log). No private database or prompts are committed.

## Performance and limits

An alternating paired benchmark used one loaded source revision and the same unchanged private database: 10,001 qualified memories, 10,001 original views and **one** canonical view, with deterministic 768-dimensional vectors. This is not a benchmark of 10,001 bilingual memories. Three runs per mode gave:

| Measured operation | Reference p50 | Optimized p50 |
| --- | ---: | ---: |
| Complete local search | 5,747.48 ms | 2,922.13 ms |
| Semantic candidate scan | 5,394.48 ms | 2,647.98 ms |

The optimization validates vectors through C-backed iteration, normalizes each query once, and uses `math.hypot`/`math.sumprod`. Extreme and subnormal norms retain scaled normalization. Scope filters, evidence qualification and candidate depth are unchanged; no dependency was added. Complete public results, scores and ordering matched on this benchmark. Numerical comparison against the previous robust formula has an absolute tolerance of `5e-16`; tiny differences around floating-point ties or zero remain possible and are not called bitwise equivalence.

The scan is still O(rows × dimensions), and roughly 2.9 seconds at this size is above the proposed 250 ms target. This is a qualified exact-search baseline, not production latency qualification. Real embeddings, larger bilingual corpora and an indexed alternative still need measured evaluation. Receipts and unchanged source/database hashes are under the sibling `dual-view-semantic-optimized-20261003/` directory, including `proof-receipt.json` and `paired-math-results.json`.

Queue health is also not vector coverage: migration creates empty per-view storage without enqueuing model calls. A completed historical outbox does not prove the new views are indexed. Explicit backfill is supported. The subsequent `8fefb0a` change adds [scoped read-only coverage reporting](memory-index-coverage-2026-10-03.md); it does not establish provider readiness, retrieval quality or a globally consistent snapshot across pages.

## Deployment boundary

The existing immutable schema 30 staged kit and its fallback do not support schema 31. A new reviewed candidate and compatible recovery package are required before activation. Existing production qualification trials and images remain deleted; this change creates none.

## Primary references consulted

- [Ollama OpenAI-compatible endpoint](https://docs.ollama.com/api/openai-compatibility): the actual adapter uses `/v1/embeddings`; native `/api/embed` options are not injected into this request.
- [Qdrant hybrid query documentation](https://qdrant.tech/documentation/search/hybrid-queries/): rank fusion combines ranking streams without directly adding incomparable raw scores. Our explicit k60/one-based contract is a local design choice, not Qdrant's default.
