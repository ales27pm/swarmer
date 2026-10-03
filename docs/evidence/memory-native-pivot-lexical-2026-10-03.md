# Qualified original and pivot lexical search — 3 October 2026

Two failing service regressions established the problem: a French word present
only in an accepted original was not searchable, and an exact code identifier or
path disappeared behind 600 newer unrelated memories. Search now scores the
current original and canonical views and returns one result per memory. Its
canonical content, original-language presentation, public fields and score scale
remain unchanged.

## Read contract

Scope, kind and sensitivity filters apply before text scoring. SQL ranks compact
candidates across the permitted collection; qualification proceeds in pages of
64 until the best 50 qualified lexical candidates are determined. Invalid
candidates do not consume that result budget. The previous 500-item recency
cutoff no longer limits lexical discovery.

A current head, revision, complete view set, view identities, content hashes,
source journal and normalization receipt qualify original text. Tombstones,
missing views and inconsistent heads cannot expose historical text. Selected
canonical memories with invalid normalization metadata still produce an explicit
qualification error. Mixed-language content and summaries follow the same
language rule as creation. Source views and any presentation provider are checked
again after presentation work.

Long queries use exact multi-pattern matching, including overlapping terms and
literal code paths. Repeated words or nested prefixes switch to substring checks
for every remaining term after bounded pattern-matching work. This changes the
algorithm, not the query length or score. Identical original/canonical text is
scored once.

## Validation

- The two initial regressions failed before implementation.
- A 41-file memory/strategy/goal-memory suite passed **814 tests in 122.16 s**
  before the final scorer optimization; source hashes were unchanged throughout
  that run.
- After that optimization, **131 focused tests passed in 19.98 s**, including
  public API retrieval, provenance damage, stale/edit/deleted sources,
  multi-page invalid candidates, mixed languages, embedding failure and presenter
  identity/signature changes.
- Independent differential coverage compared 1,500 deterministic cases with the
  existing substring formula. Unicode case folding, regex literals, overlaps,
  singleton terms, empty channels, large term sets and long texts agree.
- Independent review reproduced and then confirmed fixes for mixed-language
  originals and a presenter replacement during the added final database read.
- Ruff and formatting checks pass. Mypy has no new errors in changed files;
  the same two errors also occur at baseline `64d8c8c`: the existing media-model
  literal override and the unavailable optional `redis.asyncio` dependency.

Local end-to-end measurements used one accepted bilingual memory and 10,000
distinct unrelated memories, each with 3,935 content characters and real current
text views. Query translation was a deterministic fixture; no embedding provider
was called. Five final samples per case produced:

| Query | Median | Highest sample |
| --- | ---: | ---: |
| Native French term, 6 characters | 231.38 ms | 237.35 ms |
| Native match, 1,996 characters | 481.64 ms | 520.60 ms |
| No match, 1,999 characters | 513.98 ms | 607.40 ms |

The direct substring reference on the same database previously took 8,438.90 ms
and 8,621.59 ms for the two long cases, with identical results. Database bytes
were unchanged by the measured searches. Separate CPU cases exposed an initial
regression on repeated common terms and nested prefixes; the adaptive algorithm
reduced those cases to approximately 17 ms and 8 ms per 1,000 documents. A
diverse-initial-character 399-term case still required approximately 2.44 s per
10,000 documents. These are local fixture measurements, not production latency
guarantees or percentile estimates.

Private logs and benchmark scripts are under
`~/Library/Logs/SwarmerQualification/Memory/goal-model-admission-20261003/` in
`native-pivot-lexical-20261003/` and `fallback-schema30-20261003/`.

## Operational scope and remaining work

This source change has no migration, configuration or provider-call changes.
The staged Ubuntu candidate at `527a2380` and its recovery package remain
immutable and **do not contain this change**. No activation, iPhone installation
or new production qualification goal, task or image occurred in this work.

This remains a scoped substring scan, not FTS/BM25 or an indexed lexical engine.
There is still one existing vector channel, with its separate 500-item recent
pool, and the existing lexical/vector score mixture. Dual-view vectors, RRF,
symbolic retrieval, trusted lesson promotion and real translation/relevance
evaluation remain separate work.
