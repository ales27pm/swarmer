# Scoped memory-index coverage — 3 October 2026

This change adds an explicit inspection of current memory vectors. It does not deploy schema 31, backfill an index, run a model, or recreate any deleted production qualification task or image.

## Behavior

`GET /memory/index-coverage?scope=general&limit=50` requires the existing paired-device owner authority. `scope` is required, exact and nonblank (at most 100 characters); `kind` is optional, `sensitivity` defaults to `normal`, and `limit` is bounded to 1–100. Selection is applied before pagination. Pairing already grants owner access to this control plane's memories; choosing a scope is a filter, not a new permission or an agent's cross-project grant.

One read-only SQLite transaction qualifies the current source, head, view set and canonical receipt using the same qualification as retrieval. It then inspects only those current views and their exact provider-space rows. Other-provider existence is checked without loading historical vectors. The service opens SQLite with `mode=ro` and `query_only`; it cannot create a missing database or repair anything. The normal authentication layer may update the paired device's last-seen timestamp, as it does for other owner API calls.

The response contains IDs, roles and counts, never memory text, summaries, vector values, source receipts, credentials or provider URLs. Runtime response models reject extra fields. Successes and authentication, validation, conflict and unavailable errors all use `Cache-Control: no-store` through a route-specific handler. Existing routes are unaffected.

## Interpretation

Per qualified current view, reasons have this precedence:

1. No configured provider: `configuration_missing`.
2. No exact current-view/provider row: `embedding_configuration_mismatch` if another space has a row for this same current view, otherwise `missing_vector`. Historical rows and the old canonical cache never satisfy coverage.
3. An exact row with mismatched source, text hash, pipeline or item revision: `stale_binding`.
4. Stored width differs from the configured provider's declared width: `embedding_configuration_mismatch`.
5. Invalid numeric payload or disagreement between payload length and its stored width: `invalid_vector`.
6. All those checks pass: `current`.

Unqualified memories remain visible as `unqualified_source`, with no purported current views; they prevent a page from being reported covered. The page reports its expected and covered view counts separately. A changed provider instance or configuration during the awaited database read returns `409 memory_index_configuration_changed`; unavailable storage returns `503 memory_index_unavailable`. Neither becomes an empty successful result.

`counts_scope=page` and `consistency=page_snapshot` are deliberate. `after_id` pages do not share a transaction, so concatenating pages cannot prove an atomic global snapshot. `covers_entire_selection` is true only when the initial page exhausts the selected scope/kind/sensitivity. `provider_readiness=configured_not_probed` never means the provider has answered a request. When the provider does not declare dimensions, `dimension_check=stored_vector_only` exposes that the check uses the recorded vector's own width; it does not establish the current remote model's actual width or immutable identity.

The frequent `/memory/status` route keeps its inexpensive outbox counters and now explicitly labels them `outbox_activity_not_vector_coverage`, with a link to the inspection path. Coverage is not automatically scanned on every health request.

## Verification

The integrated run passed **203 tests in 80.56 seconds** across 11 files, including public OpenAPI parity, pairing, health, current search/backfill and schema migration. Review then found deeply nested corrupt JSON that could escape normal error classification. A targeted fix classifies malformed vector nesting as `invalid_vector` and malformed source-metadata nesting as sanitized 503. The final coverage/API suite passed **65 tests in 13.04 seconds** on unchanged source hashes. Both runs had only the existing Starlette deprecation warning. Ruff check/format passed for all seven changed Python files; focused mypy passed both new runtime modules. The standalone OpenAPI verifier passed: 90 paths, 98 operations, 677 references and seven JSON schemas. The four bounded response models are compared exactly with the public contract. Tests use temporary local databases and deterministic providers that fail if inference is attempted. They cover a cold migrated index with completed legacy outbox rows, current/missing/foreign/historical/corrupt projections, invalid receipts, filtering and pagination, provider-change races, read-only enforcement and coherent concurrent-writer snapshots. HTTP tests additionally cover pairing, non-caching, private-data exclusion, and the public OpenAPI contract. These tests do not prove live provider readiness, multilingual relevance, production latency or iPhone behavior.

Private local receipts are under `~/Library/Logs/SwarmerQualification/Memory/goal-model-admission-20261003/index-coverage-20261003/`. Source and OpenAPI before/after hashes accompany the integrated run.
