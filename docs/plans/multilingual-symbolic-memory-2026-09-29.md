# Multilingual symbolic memory — implementation target

This records the user's latest refinement of M02/M04/M09/M10/M12. It supersedes
English-only representation, while preserving the bounded English-pivot work.
The target is one logical memory with original evidence, versioned textual views,
and language-independent concepts/claims. It does not authorize a production
migration, a model download, or a switch of database by itself.

## Identity and authority

- Preserve original bytes and provenance. Source hashes are exact UTF-8 hashes;
  label normalization must not change code, identifiers, paths, case or literals.
- Keep English translation as a derived view with model/policy signature,
  review status and source revision. Replacing a translation must not replace
  the observation that justified it.
- Concepts have scoped stable IDs, with multilingual preferred/alternative labels.
  The declared locale wins over detection. Detecting French cannot infer Canada.
- Claims include typed subject/predicate/object, polarity, modality, conditions,
  units, versions and applicability. “After” does not automatically mean “only
  after”; “may” does not mean “must”. Evidence strength and freshness are separate.
- One retrieval result may represent several views of the same revision. Equal
  translations are only candidates for grouping distinct observations. Preserve
  all attributable observations, including corrections and contradictions.

The conceptual separation follows the W3C [SKOS reference](https://www.w3.org/TR/skos-reference/).
Language tags follow [BCP 47 / RFC 5646](https://www.rfc-editor.org/info/rfc5646/).
[Unicode normalization](https://www.unicode.org/reports/tr15/) is applied only to
separate label/search forms; it is not evidence that two source artifacts match.
These standards do not establish the quality of our translation or retrieval.

## Current implementation boundary

The opt-in `en` memory-item path preserves originals in `memory_source_journal`,
publishes reviewed English text with normalization receipts, and checks source
and configuration revisions across asynchronous model calls. The default stays
`legacy`. Scoped French search normalizes the query and can return a temporary
French presentation of qualified results; that presentation is checked against
the English source revision and hashes and is never written as another memory.
The mobile memory screen can reveal the stored English text.

For an original already reviewed as French, the current candidate instead returns
the exact source journal fields after verifying their accepted receipt, scope,
hashes and current canonical revision. It labels this as preserved source text.
The temporary translation path remains for English sources and mixed-language
items; a mixed batch does not retranslate its eligible French originals. This
avoids a second translation changing an obligation through an ambiguous pivot.

The 3 October candidate wires strategy retrieval to this path when canonical
English memory is explicitly enabled. Actual model calls use goal-scoped
admission, budgets and revision fencing; candidates remain limited to
normal-sensitivity general/global items and the exact linked project. Legacy
episodes and direct ContextBuilder reads are not silently reclassified or
translated. The [integration evidence](../evidence/goal-memory-admission-2026-10-03.md)
distinguishes passing local checks from real-provider qualification and the
schema/client rollout still required before production activation.

The conceptual module supplies deterministic contracts. Schema 30 connects those
contracts to scoped SQL concepts, multilingual labels, proposals, exact
original-source bindings and explicit relations, exposed through paired API
routes. The next local candidate adds explicit catalog-selected retrieval,
source-qualified agent/worker context and mobile inspection; its
[integration evidence](../evidence/memory-symbolic-retrieval-2026-10-03.md)
tracks local checks and outstanding combined/device qualification. It does not
promote proposals to validated lessons.

The next schema-32 slice connects these complete cards to the iPhone's local
goal planner and tool-proposal flow, with server-issued expiring selection
receipts and transactional source revalidation at acceptance. Its
[local-inference evidence](../evidence/memory-local-inference-2026-10-03.md)
separates mobile tests from server, recovery and physical-device qualification.

The locally qualified schema-31 candidate adds current original/pivot vectors,
scoped lexical/semantic rank fusion and bounded explicit backfill. The
[semantic retrieval evidence](../evidence/memory-dual-view-semantic-2026-10-03.md)
records the exact candidate depths, revision fences and remaining performance
limits. The [coverage endpoint](../evidence/memory-index-coverage-2026-10-03.md)
separately reports current, missing, mismatched and invalid vectors; completed
outbox work is not treated as proof of coverage. These are implemented local
behaviors, not a claim of production activation or measured FR/EN relevance.
Concept-assisted retrieval is being integrated locally; production qualification,
trusted promotion, dependency freshness and chronological quality evaluation
remain outstanding. There is no automatic
cross-language merge or startup backfill.

## Integration sequence

Schema 29 established the SQL portion of step 2: immutable original and canonical
view revisions, a current head/tombstone, and transactional index intents. Schema
31 extends projection to every qualified current view, while retaining the old
index-view-only cache for compatibility. Writes and explicit backfill consume
these intents; there is no autonomous startup drain. An interrupted request is
not retried merely because its lease expired. The
[earlier implementation evidence](../evidence/memory-versioned-projections-2026-10-03.md)
remains historical; the schema-31 receipts supersede its single-vector boundary.
The new schema needs its own qualified candidate, compatible recovery binary and
rollout guard; raising an older kit's version ceiling is insufficient.

1. Finish qualification of the pivot write/search/display path, including the
   actual local provider and fixed model-call budgets. Keep rejected attempts as
   rejected; a structurally valid response is not proof of faithful translation.
2. Add versioned textual projections to the existing SQL authority. Key them by
   logical memory ID, source revision, view role, language and pipeline signature.
   Add a transactional outbox and idempotent projector; retain retryable lag,
   mismatch and unavailable states explicitly. Never return an empty list for
   all of those conditions.
3. Persist scoped concepts, labels and claims, with evidence links, unvalidated
   proposals and explicit supersession. Deny implicit cross-project promotion.
   Trusted constraints continue through the trusted configuration path.
4. Add lexical original/pivot channels and exact symbol/path lookup. Evaluate
   multilingual native embeddings before adding another model. Keep native and
   pivot vector signatures distinct, fuse rankings with RRF, group by memory ID
   and revision, and revalidate SQL scope, sensitivity and source before return.
5. Measure phase-aware retrieval, dependency freshness and optional graph
   expansion against the same coding model, tasks and total execution budget.
   Adopt Qdrant only if this measured retrieval workload earns the extra service.

The next SQL integration must keep proposal identity separate from the claim
fingerprint: equal claims from two observations must retain both evidence links.
Bind each proposal to an original view's memory ID, revision, field, exact field
bytes hash and document hash. The conceptual module's `source_bytes_sha256` hashes
raw bytes, whereas a text head's source hash covers the content/summary document;
these are different keys. Likewise, do not silently map legacy `global` or free
scope strings to the conceptual contract's `general` or `project:*` scopes.
Public proposal writes remain unvalidated and cannot request curated status.
Supersession is explicit; forgetting a memory also erases proposals derived from
that source and their source/concept/relation links atomically. Standalone concept
definitions remain separate records, including definitions shared by other
proposals. The schema-30 SQL/API integration binds only current normal-sensitivity
original views in the exact declared scope. It never promotes a proposal or
labels a source as a verified user statement. Agent retrieval now has a locally
tested source-qualified integration; the evidence-gated validation/promotion
path requested by the user remains to be implemented. See the
[symbolic registry evidence](../evidence/memory-symbolic-registry-2026-10-03.md).

## Corrections required before adopting the supplied PoC

- Do not casefold an entire coding memory for identity: `Cache` and `cache`, or
  case-sensitive paths, can refer to different artifacts.
- Canonical-hash equality alone cannot merge records. Namespace, kind, project,
  applicability, polarity, modality, source occurrence and evidence must survive.
- Unknown language is `und`, not a successful English translation. A failed
  translation remains unavailable/unqualified.
- Never use tokenizer truncation to silently discard the end of a requirement.
  Reject or explicitly segment inputs with preserved boundaries.
- Protected-token replacement must verify that tokens were neither omitted,
  duplicated, mutated nor swapped; a plain string replacement is insufficient.
- Apply scope and sensitivity before every lexical/vector/concept candidate
  limit and again after asynchronous processing, including graph expansion.
- An in-memory Qdrant instance is not durable storage. Writing an outbox row and
  immediately calling upsert is not a durable projector without acknowledgement,
  replay, revisions, deletion and failure handling.
- Do not load a floating model revision or remote custom code implicitly.

## Acceptance evidence

Use chronological, scoped fixtures containing equivalent FR/EN requests,
negations, uncertainty, changed conditions, exact symbols, code-switching,
contradictions, stale snapshots, source deletion during indexing, and hostile
source instructions. Include same text in different projects, and source edits
while translating or presenting. Compare original-only, pivot-only, dual-view
and concept-assisted retrieval before selecting a production strategy.

False merges are more costly than remaining duplicates. Do not auto-merge from
cosine thresholds, and do not advertise the user's proposed recall/latency
targets as measured outcomes. The pasted research citation IDs and chart paths
are not recoverable sources here; numerical research claims require a separate
primary-source verification before reuse in product documentation.
