# Context projection evaluation (50 FR/EN fixtures)

This suite measures deterministic preservation, source access and context construction. It is **not a model-quality benchmark** and cannot establish semantic retrieval accuracy, reasoning improvement, real-world task success, iPhone energy consumption or background execution.

`scenarios.json` contains 25 concrete situations in both French and English: old requirements, explicit corrections, assistant assertions unsupported by receipts, conflicting tool instructions, private sources of another project, restart/replay and deliberately oversized protected state. The filler is repeated tool observations representing long transcripts. The source text and expectations are reviewable; no benchmark score is hidden in a model-generated label.

Run from the repository root:

```sh
server/.venv/bin/python scripts/evaluate_personal_context.py --output /tmp/context-evaluation.json
PYTHONPATH=server server/.venv/bin/python -m pytest -q server/tests/test_context_evaluation.py
```

The script opens only disposable temporary SQLite databases. It exercises the actual `ProjectContextService.refresh/source/prompt_state` APIs, repeated snapshot replay, reconstructed service instances and project-isolated source lookup. The test suite additionally runs a projection against the full application database schema. No production state, device, model or external API is contacted.

Four **reference projections** are compared:

1. `current_reduction`: contiguous recent conversation window with an 8000-character per-message tail limit. This is an explicit reference baseline, not an exact replay of every production worker path.
2. `observation_masking`: replaces older tool bodies with source references, preserves the latest observation, then fits whole messages into the budget.
3. `structured_summary`: actual source-backed structured project state. This is deterministic extraction, not generative summarization. Protected state that exceeds the budget causes a non-dispatchable result, never silent removal.
4. `summary_hybrid`: same protected state plus up to four source-ranked results. Without an embedding provider this harness uses a disclosed lexical fallback; it does **not** pretend to evaluate a hybrid embedding implementation.

Tokens are estimates (`ceil(serialized UTF-8 bytes / 4)`), not model-tokenizer counts. Latency is measured locally for snapshot creation and projections, separately from any explicitly requested model plugin. `critical_preserved` includes protected payloads whose dispatch was refused; `dispatchable_critical_*` metrics report only contexts that fit. An intentionally refused oversized context is not a successful completed task.

Every variant reports receipt-projection equality and foreign-source leakage. These are projection invariants, not proof that an LLM would avoid hallucination. Actual no-duplicate external effects are covered by the separate CRM27PM integration tests (atomic idempotency reservation, concurrent duplicate keys, replay after later edits, receipt rollback), not by this suite.

## Optional real-model experiment

The CLI refuses model execution unless both `--model-plugin package.module:function` and `--allow-model-calls` are explicitly supplied. The plugin must be trusted installed local code; it receives `{scenario_id,language,query,mode,context}` and returns `{answer,claimed_verified_source_ids?}`. Configure provider credentials outside fixtures and output artifacts. A plugin could perform network calls and must only be enabled for an authorized experiment.

The harness records actual callback latency and unsupported receipt IDs. It does not automatically assign answer-quality grades: independent review or a separately calibrated judge is still required. Do not report plugin test doubles as actual model calls or improvements. No model plugin is enabled in ordinary tests; the one plugin-contract unit test uses an explicitly labeled deterministic test callback.

Before promoting a context strategy, extend the experiment with actual tokenizer counts, real configured embedding retrieval, repeated real-model samples, FR/EN independent answer grading, and device-specific latency/RAM/energy measurements. Preserve raw sources and runtime/version identifiers with results.
