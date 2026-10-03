# Actual local FR/EN provider qualification — 3 October 2026

Follow-up: the [92-call reviewer comparison](memory-reviewer-comparison-2026-10-03.md)
completed with both false acceptance and false rejection. Adding exact quoted
issues did not improve faithful-case acceptance and is not promoted. The original
failed trial below remains unchanged evidence.

## Scope and source identity

This trial ran the memory implementation from commit
`c527f5d3be9f5c59b1c8ab5149e8f7c0bd8310c9`, in a separate SQLite database on
Ubuntu. Its 136 application files matched the frozen API wheel. It did not
deploy a service, modify a production project, or qualify the newer schema-32
local iPhone selection endpoint.

The reviewed runner archive was
`fa2b3f450be4aa4db0821310c10b8dee11e1e4a0624d5fc3fff668b086fa465b`;
the independent seal was
`77a63c0a48f78da2b32ee508a622b06a4c23e33b53d88ced0e69a03dad9b199b`.
The transferred archive and seal were checked before execution. CPython 3.12.13,
Ollama 0.32.3 and the installed Python dependency versions matched their pins.

The actual providers were:

| Role | Alias | Digest |
| --- | --- | --- |
| Translation and review | `swarmer-research-qwen35:9b-8k-6488c96fa5fa` | `cc81d93f910b2a4fbb2b5cbd38faf70de1b39178871f866f2cd80f09f4172a3d` |
| Embeddings | `swarmer-embeddinggemma:300m-cpu-85462619ee72` | `a3a329bf4947e5a7acfc3044a9cbfc0ab0001f75c070d2804361bf370b1009ec` |

The translator and reviewer use the same model. A successful internal review
would not be independent semantic evidence.

## Executed checks and outcome

- The runner's 43 local tests passed. The Ubuntu simulation completed all ten
  events and fourteen queries, with 90 simulated POST calls and sockets forbidden.
  Its semantic quality metrics were deliberately null.
- The first real trial stopped after **43.90 seconds**, **14 inference POSTs** and
  **three accepted seed events**. All 14 HTTP responses completed with status 200;
  generation responses reported `stop`.
- Two scoped copies of a French report constraint were translated with its
  negation, approval condition and `rapport.csv` literal intact. An English cache
  hypothesis retained its uncertainty and exact `30 ms` value.
- The next source combined French prose and an English sentence, with protected
  `src/CachePolicy.swift`, `CRM_KEEP_30D` and `crm_keep_30d` literals. The candidate
  visible in the review request preserved these values and the stated meaning.
  The normalization receipt nevertheless recorded `uncertain`, after the review
  call. The runner did not retain that call's structured review fields, so the
  first run alone does not establish which check rejected it.

No search query or revision/deletion phase was reached. Recall, MRR and the
comparison of retrieval variants therefore remain **unqualified**. The failed
case is retained; it is not omitted from the planned corpus or counted as success.
The diagnostic below captures a new structured verdict without persisting model
reasoning or weakening the acceptance checks.

## Explicit one-call diagnostic

A separate sealed diagnostic repeated only review request 14 once, with the same
model, prompt, schema and settings. Its 13 local tests passed before execution.
The single real response completed, and the GPU reservation was cleared with no
unknown request. Its parsed verdict was:

| Check | Result |
| --- | --- |
| Source/candidate hashes | Match |
| Languages | French to English |
| Literal, negation and uncertainty preservation | All true |
| Meaning preserved | False |
| No added facts | False |
| Clarification required | False |

Manual comparison of this synthetic pair finds no added fact or meaning change:
the French sentence is translated, the existing English sentence is retained,
and all three code literals are unchanged. This new observation demonstrates a
false rejection on this pair. It does not recover the lost verdict from the
first run, establish a general error rate, or authorize bypassing a future
negative review. A reviewer correction needs both faithful translations and
deliberately altered counterexamples, including held-out cases.

## Isolation and remaining limits

The trial used the shared GPU reservation and checked for production model work
before every inference request. Every HTTP outcome was known at exit and its
owned reservation marker was cleared. A subsequent read confirmed an empty lock
file and a healthy production API at version 0.14.2. The disposable database had
zero tasks, goals, coding projects, agent jobs and production model-call receipts.

The fixed corpus has eight synthetic memories. The planned concept-assisted
comparison uses human-preannotated concepts; it cannot qualify concept extraction.
Three variants reconstruct rankings from the same captured vectors, rather than
making independent provider calls. These limits remain even if a later run
completes. Original private receipts and hashes are retained separately from the
repository; generated text and raw provider reasoning are not published here.
