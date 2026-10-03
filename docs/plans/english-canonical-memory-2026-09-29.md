# Multilingual memory: original, English pivot and symbolic identity

User requirement recorded on 29 September 2026 UTC: translate French input to English before storing reusable memory; prevent duplicate entries and meaning loss. This applies to the memory engine, not to the user's preferred conversation or interface language. It refines M02, M04, M09, M10 and M12 of the consolidated plan. The subsequent user specification supersedes an English-only representation: retain the original, treat English as a versioned operational view, and give concepts and claims identities independent of their labels. English is not the semantic authority.

## Storage boundary

A logical memory has one identity and may have several versioned views: immutable original text, an English pivot, and structured concepts/claims. Source conversations and imported documents retain their language, scope and retention rules. The first implementation indexes only the accepted English pivot and preserves its original in a source journal. Future native and pivot vectors are search projections under the same logical identity, not two independent memories. The original is never overwritten by its translation.

Code, symbol identities, paths, URLs, product names, commands, units and exact values remain literal. Their presence does not make an otherwise English record non-English. A source quotation needed as evidence stays in its source artifact, referenced by location/hash instead of silently translated and presented as a verbatim quote.

This is an explicit normalization policy, not a new trusted instruction extracted from a document. It cannot change the permissions or strength of evidence associated with the source.

## Write flow

1. Resolve the authorized source/project and read its exact version. Do not accept a source ID or a language claim solely from generated prose.
2. Protect literal spans and extract the relevant original statement. Preserve obligations, exclusions, uncertainty, conditions, dates, quantities and negation.
3. Produce English content through an explicitly configured local normalization provider. Already-English content follows the same validation path but must not be repeatedly paraphrased. Do not use a dictionary or absence of accents as proof that a sentence is English.
4. Validate the proposed translation against the source in a separate model request (not a claim of statistical independence). Check every protected span and structured meaning condition. A structural check is necessary but does not establish perfect semantic equivalence. Ambiguous or unverified translations remain pending/quarantined; they do not enter the active retrieval corpus.
5. Recheck the source hash, project scope, expected memory revision and normalization signature after the asynchronous call. A source changed while translating cannot publish an obsolete memory.
6. Commit the accepted English revision and its normalization receipt atomically. Index only an accepted revision under the matching embedding signature. The current implementation has receipts and guarded indexing; a transactional projection outbox for native/pivot indexes is still to implement.

No background retry loop, translation on a GET, or implicit model download. Unavailable translation, invalid output, source conflict and failed verification have distinct outcomes. None is treated as a successful empty memory. An unavailable provider must not silently store the French source in the English corpus.

## Provenance and signatures

Each normalized record records `canonical_language=en`, detected/declared source language with its origin, source ID/version/hash, normalization policy version, translator model and pinned revision when known, validator identity, validation status, canonical content hash and the superseded record revision. These describe the transformation; they do not promote an assistant claim into an observed fact.

The normalization signature is separate from the embedding signature. An embedding provider change must not retranslate source text. A translation-policy change creates a new candidate canonical revision and invalidates only the derived projection once that revision is accepted. Unknown model revisions remain unknown; an alias string alone does not attest to weights.

## Deduplication

- Retried processing of the same source/version and normalization signature is idempotent, including concurrent requests and process restart.
- Exact equal canonical statements can share a retrieval representative only within the same authorized project/namespace, kind and applicability conditions. All source occurrences remain attributable.
- Similar vectors are candidates for review, not authority to merge. Opposite constraints, changed dates, different API versions, uncertainty and different projects must not collapse into one record.
- A user correction creates a superseding revision. It does not become an extra active copy, and it does not erase the authorized history needed to explain the change.
- Older French entries are migrated through a resumable projection build with dry-run counts. Do not bulk overwrite them, mix both projections, or delete originals before parity and access checks.

## Retrieval and presentation

French questions remain accepted. Normalize the query once into English for the canonical lexical/semantic channels, while also preserving exact code identifiers and paths for exact lookup. The query transform has its own signature; a bounded cache is a future optimization, not implemented yet; it is not automatically saved as a new durable memory. If normalization is unavailable, expose the limitation and the actual fallback mode.

French memory queries from the app or the agent strategy path obtain a temporary French rendering of qualified English results, with source references and revision/content hashes retained. English queries retain English results. The French rendering is neither a second indexed memory nor new evidence. The app can reveal the stored English view. Direct episode and ContextBuilder reads remain legacy paths pending a separate migration.

## Acceptance cases

| Case | Required observation |
|---|---|
| French preference submitted twice | One active English memory revision, attributable source occurrences, no duplicate vectors. |
| Equivalent English input | Exact canonical duplicates collapse for retrieval only after scope/kind/applicability checks; semantic-only matches remain distinct until validated. |
| “Do not send automatically” | Negation and the automatic-action condition survive; a translation that authorizes sending is rejected. |
| Numeric and technical instruction | Version, amount, date, identifier, command and URL remain exact; quantities are not rounded or dates guessed. |
| Uncertain diagnosis | “Might be caused by” does not become “is caused by”; no upgrade to verified knowledge. |
| Correction during translation | Stale candidate cannot commit or be indexed. |
| Provider/validator unavailable | Explicit pending or failure state; no French content admitted to the English corpus, no fabricated English record. |
| Mixed French prose and code | English prose, literal code and identifiers, source reference retained. |
| Other project or changed sensitivity | No result or source excerpt crosses the boundary, including deduplication and graph expansion. |
| Interrupted backfill | Resume from the committed cursor; canonical records and active index remain consistent. |

## Integration order

The normalizer/validator contract and a narrow general-memory write path are implemented. The 3 October candidate now connects the strategy-hint seam at startup when canonical English memory is explicitly enabled. Each real normalization, review, presentation or embedding request uses goal-scoped admission, accounting and revision fencing; retrieval preserves one call for the planner and makes a provider failure or budget fallback explicit. Context receipts retain the selected source revisions and hashes. See the [integration evidence and rollout limits](../evidence/goal-memory-admission-2026-10-03.md).

This candidate is not an activation of canonical memory in production. Real-provider semantic qualification and a schema-compatible rollback release are still required. Then route the remaining project knowledge and episodes through the same service, and build the migration projection without activating it. Finally qualify multilingual retrieval and expose translation/validation status in the memory UI. The current signature and passive-inspection corrections are prerequisites; they do not, by themselves, translate existing memories.

## Local configuration and rollout boundary

The first integration targets `memory_items`, including explicitly scoped items
created through the memory write API. It does not automatically convert project
message indexes, episodes or existing French records. The default remains
`MONGARS_MEMORY_CANONICAL_LANGUAGE=legacy` until isolated qualification and a
scoped migration have passed. This compatibility setting is not a claim that
legacy records are English.

An operator can configure the isolated candidate with
`MONGARS_MEMORY_CANONICAL_LANGUAGE=en`,
`MONGARS_MEMORY_NORMALIZATION_BASE_URL`, `MONGARS_MEMORY_TRANSLATOR_MODEL` and
`MONGARS_MEMORY_REVIEWER_MODEL`. The endpoint must be credential-free loopback
HTTP(S), with no query or fragment; it is never derived from memory content.
Optional `MONGARS_MEMORY_TRANSLATOR_REVISION` and
`MONGARS_MEMORY_REVIEWER_REVISION` preserve known revisions. Missing revisions
remain unknown, and a configured string is not proof of the served weights.
`MONGARS_MEMORY_NORMALIZATION_TIMEOUT_SECONDS` bounds one operation to at most
60 seconds. No global planner model is silently substituted for either role.
`MONGARS_MEMORY_NORMALIZATION_REASONING_EFFORT=none` explicitly disables reasoning
when the configured compatible provider supports that value. Unset or blank
preserves the provider default; other values are rejected. This setting applies
to both normalization and French presentation, including their review requests,
and participates in their signatures. Each request includes the compact generation
schema in its system prompt as well as `response_format`; this versioned prompt
policy also changes the signature and counts toward the existing request byte
budget. Source content remains untrusted user data. Truncated responses remain
invalid and never trigger an automatic retry.

The API retains its successful memory-item response format. Unavailable
normalization returns 503, invalid or uncertain translation returns 422, and a
source/configuration conflict returns 409. `X-Mongars-Memory-Normalization`
identifies the category without echoing source text or model output. The app
receives a readable error; it must not treat a failed write as an empty success.
No model download, production flag change or historical backfill is implied by
these configuration additions.


## Symbolic and multilingual refinement

The [symbolic design and staged integration](multilingual-symbolic-memory-2026-09-29.md) is the current target. A concept identity is independent of language labels; a claim identity includes polarity, modality and applicability. Neither translation equality nor vector proximity proves claim equivalence. The current idempotent write path only unifies retries of the same exact source, attributes and normalization signature. Distinct French and English source observations can still produce distinct records; cross-source grouping is not implemented and must not discard provenance.
