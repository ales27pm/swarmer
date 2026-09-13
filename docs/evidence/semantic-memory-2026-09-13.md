# Semantic memory: live verification

Read-only inspection captured on 2026-09-13 at 23:32–23:35 UTC. No model was
loaded, no new embedding was generated, and no memory, task or configuration
was changed for this audit. Evidence reflects the running Ubuntu service,
cross-checked against matching local source hashes.

## Result

Semantic vector memory is configured and demonstrably used by the project
worker. General memory and completed-episode vector retrieval are not enabled.
The local iPhone initial planner receives no memory context.

| Path | Live evidence | Outcome |
| --- | --- | --- |
| Project memory | 26/26 stored items have 768-dimensional vectors; 13 embedding queries completed | Active on Ubuntu |
| Worker use | 13 claimed `code.build_project` jobs contain nonempty `memory.mode=semantic`; 12 completed, 1 cancelled | Retrieval delivered to real jobs |
| General memory | General embedding URL/model unset; 0 memory items, 0 memory embeddings | Not configured |
| Completed episodes | 5 episodes, 0 episode embeddings | No semantic episode retrieval |
| iPhone MLX initial plan | Prompt includes goal, budgets and active agents only; native module has no embedding operation | No local semantic memory |

## Actual embedding model

The project embedding provider uses the installed Ollama model
`swarmer-embeddinggemma:300m-cpu-85462619ee72`, based on
`embeddinggemma:300m`. Its installed digest exactly matches the configured
revision:
`a3a329bf4947e5a7acfc3044a9cbfc0ab0001f75c070d2804361bf370b1009ec`.
The GGUF BF16 model is 621,875,929 bytes and returns 768-dimensional vectors.
It runs on the Ubuntu CPU, not through the iPhone's Dolphin MLX runtime.

No Ollama model was resident at the inspection time. This is separate from
being installed or having completed previous embedding requests. Thirteen
successful embedding requests and matching Ollama HTTP 200 records occur
between September 13 00:19:35 and 01:17:10 UTC, which is September 12 evening
in Québec. The latest associated completed job finished at 01:17:54 UTC.

## Retrieval reaches the worker prompt

Each of the thirteen jobs joins to a completed embedding query with the same
768-dimensional provider identity. Twelve payloads contain four memory items;
one contains a single item. A pure reconstruction using the active worker's
exact prompt-building functions confirms that all thirteen retain their
semantic memory hints after context trimming. The combined system/context
sizes are 7,490–13,635 bytes, below the 22,000-byte cutoff.

This verifies storage, successful query embeddings, retrieval delivery and
prompt construction for real jobs. No raw outgoing model HTTP bodies were
retained; the audit does not prove how much the model's answers depended on
those hints. No inference was launched to manufacture historical usage evidence.

The project chain is implemented in
[`project_memory.py`](../../server/app/services/project_memory.py),
[`goal_project.py`](../../server/app/services/goal_project.py) and
[`project_worker.py`](../../workers/project-worker/project_worker.py).
The 26 items span two projects: sixteen conversation summaries and ten plan
summaries. Vectors are stored in SQLite and ranked by cosine similarity.

## Why the model is not visible in the app

The settings screen lists local generation presets and server generation
roles; it has no embedding-model panel or live embedding status. The Memory
screen labels its search as lexical. That matches the currently unconfigured
general embedding provider, but the label is static even though the server
can support hybrid search when separately configured.

General memory selection in the planner still uses pinned/date/lexical
selection. Semantic completed-episode retrieval is a separate optional path
and has no vectors in the live database. The project store indexes selected
conversation and plan summaries; it is not a general index of source files
and test output. E5/BGE names in declarative YAML files do not load those models
into the running service.

The status backend label and episode-retrieval counter alone are not proof of
embedding usage. A useful app status would separately expose the provider,
model/revision, device/server location, indexed count, last successful query,
retrieval mode and fallback reason for each memory path.

## Retained evidence

Private local evidence is under
`/private/tmp/swarmer-semantic-memory-audit-20260913/`:

- `snapshot.json`: effective configuration, model metadata, aggregate tables
  and request evidence, without memory contents.
- `worker-usage.json`: semantic payload counts and completed-query joins.
- `prompt-inclusion.json`: pure prompt reconstruction and trimming checks.

The six checked server source hashes match the current repository exactly:
main, embedding service, project memory, episode memory, context builder and
state service. The separate iPhone storage/planning build and its pending
physical validation are recorded in
[`iphone-mlx-crm-2026-09-13.md`](iphone-mlx-crm-2026-09-13.md).
