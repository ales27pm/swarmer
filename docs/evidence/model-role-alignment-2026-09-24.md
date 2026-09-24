# Ubuntu model role alignment — September 24, 2026

## Scope and preservation

The requested change is to preserve suitable installed abliterated generation
models and replace the standard Qwen3.5 9B used by the planner and research
evaluator. The existing 30B Heretic project worker/general evaluator, 7B
abliterated writer, Hermes aliases and G9v3 are retained. Embeddings are a separate
capability, not interchangeable with generation models.

The refusal-result correction was deployed separately; see
[its evidence](writing-declined-results-2026-09-24.md). Model selection does not
establish that every request will succeed or that a model will never decline.

## Download and import

Candidate: `mradermacher/Huihui-Qwen3.5-9B-abliterated-GGUF`, revision
`9f646d7eda193ddf2348134f3bff3d49eed7a2c6`, file
`Huihui-Qwen3.5-9B-abliterated.Q4_K_M.gguf`.

- Download: 5,627,045,248 bytes; SHA-256
  `ea1858ef4dc4b648b8dbb44612962a0333e945060dd0545ac0f28d7c4416e4b3`.
- Installed alias: `swarmer-research-qwen35-abliterated:9b-8k-9f646d7e`.
- Ollama manifest digest:
  `dea44495ce5b261fda71feeb08020d0b960f2f563869d1c70fb1bb0276a9ffdc`.
- Imported model blob SHA-256:
  `a7d5d655409a682e1f4af45d2b0ccc715c5a2773332c6f967672246bc3c420ed`.

Ollama rewrites the imported GGUF; its separately hashed blob is not byte-identical
to the Hugging Face download. The Qwen3.5 renderer and parser are explicit. All
15 previously installed aliases and their digests were unchanged by import.
The candidate is text-only; it does not include the old package's vision weights.

## Real qualification and failure retained

Three actual requests used the deployed planner/evaluator providers with private
fixtures, strict schemas, temperature zero and `reasoning_effort: none`.
No production job or database write was created, and no request was retried.

| Check | Result | Duration |
| --- | --- | --- |
| French family agenda plan | Contract passes; adds an unnecessary dependent synthesis | 19.913 s |
| Official Python sqlite3 research followed by writing | Correct `research.query` → `writing.draft` dependency | 7.967 s |
| Evaluate a complete sourced French explanation | Fails: calls the French draft English and proposes synthesis to translate it | 11.036 s |
| Evaluate absent evidence | Not executed after the failure | — |

The complete 348-character French draft and its source URL were present in the
actual evaluator payload; no truncation or missing context explains the verdict.
The proposed synthesis would also violate its documented deterministic role.
This is a model-quality failure, not a reason to weaken validation or label the
evaluation successful.

An initial local fixture identifier error consumed zero model calls. A separate
harness preference had prohibited all synthesis in the agenda plan; the original
response was revalidated without another model call because the server/user
contract permits dependent aggregation. The unnecessary synthesis remains a
quality reservation. All original receipts were retained.

Consolidated private qualification receipt SHA-256:
`363713b41b01d1552e5c0c7b6cdc23eabf2dff8fc13a6075207b90d7f4fcc46c`.

## Release status

The candidate is installed but is not qualified for the research evaluator role.
The active role configuration and old model storage remain unchanged.
A separate two-call qualification of the already-installed 30B Heretic passed
its positive control in 50.235 seconds. Its negative control returned `failed` in
13.647 seconds and correctly explained the absence of evidence, but omitted both
structured diagnostic lists and proposed no available research recovery. This
is not a false success; it still fails the original qualification criteria.
Its receipt SHA-256 is
`dbb69e2ce2bb46e2aee975ba9ef432acb55c9a80704e4916b93088fc1473c175`.

The existing 7B abliterated writer passed the same two criteria without changes:
`done` for the source-backed draft in 12.513 seconds, then `replan` with explicit
missing requirements and a valid research → writing graph for absent evidence in
7.314 seconds. Its explanations remain mostly English despite the French goal;
this editorial limitation is retained. No previous failure was reclassified.
Receipt SHA-256:
`13ccbcf07f44267fe014fa18b13ecbfd7132aed2b7228409ff1e0c512b4f27cc`.

An independent counterfactual control then supplied an official Python
`http.server` source with a fluent French SQLite draft incorrectly citing it.
The objective, capabilities and strict provider were unchanged. Before the single
call, the expected outcome was fixed as correction of the off-topic evidence with
new research and dependent writing. In 4.205 seconds, the 7B instead returned
`done` with empty diagnostics. It falsely accepted unsupported evidence, so its
first two successful controls do not qualify it for research evaluation.
The failed control receipt SHA-256 is
`9af91b375e3a74e50890ca4c142b5039f2856f91db1bc6aa3b96e12964525f2f`.

All eight actual calls are preserved in the consolidated comparison, SHA-256
`597b891a1f41b4262145f139706f61273d38eb689f5357c974477f0b3a7f57e7`.
No qualifying replacement evaluator was established. The prospective two-key
configuration rollout remains inactive, and no old alias or file was deleted.
All probe tunnels were closed. There was no production inference job, project
restart, configuration change or TestFlight release in this model qualification.

A separate read-only verification at 05:48:27 UTC found API health `ok`, the
unchanged `0a4c9e0…e8976a26d44d` source release and all 16 model aliases. The actual
API process still uses `swarmer-research-qwen35:9b-8k-6488c96fa5fa` for both planner
and research evaluator, with the existing 30B Heretic as general evaluator.
Therefore the requested all-abliterated active generation profile is not yet
achieved; the downloaded candidate has not silently replaced a working role.

These are bounded, synthetic source/writing controls passed through real
providers and actual model calls, not live search/writing end-to-end tests or a
general benchmark. They identify concrete failures and must not be weakened into
a success claim. The next qualification must retain an unseen-source relevance
control as well as complete and missing-evidence controls. The existing 30B and
7B keep their previous roles; these results concern the proposed research
evaluator assignment, not a universal verdict on their other uses.
