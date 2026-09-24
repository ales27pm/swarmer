# Evaluator evidence grounding — September 24, 2026

## Finding and bounded correction

The eight recorded calls in [model-role alignment](../evidence/model-role-alignment-2026-09-24.md)
contain two planner responses and six evaluator responses. The complete French
draft and source excerpt were present in the evaluator requests. Neither context
truncation nor a missing source explains the recorded incorrect translation claim
or acceptance of an unrelated source. These semantic failures remain failures.

An independently reproducible contract defect was found: the model provider
accepted `done` with nonempty `missing_requirements` or `invalid_results`, and
accepted `failed` with both structured diagnostic lists empty. The latter is the
recorded 30B negative control: its prose notices missing evidence, but neither
diagnostic list records it.

`UbuntuEvaluatorProvider` now rejects these two inconsistent terminal shapes.
The grammar exposes matching complete `anyOf` object alternatives with array
bounds, reusing the transport's existing schema constructs rather than conditional
`if`/`then` rules. The prompt names the same constraints. The authoritative check
also applies to compatible endpoints returning public, unnumbered fields.

The new check is limited to model-provider replies. Server-generated failures,
persisted public decisions, deterministic test evaluators and the existing public
decision parser remain unchanged. No status is rewritten, no recovery node is
invented and no natural-language keyword classifier is used. Errors retain the
safe `invalid_response` / `schema` classification, an output digest and a constant
specific exception message. Existing goal audit/UI processing retains its generic
schema-failure reporting; this patch does not add a new public diagnostic code.

## Evidence

- Before the correction, eight focused rejection cases failed because no error
  was raised. They cover missing evidence, declared off-topic evidence, a declared
  writing refusal and an empty failure diagnosis, using both wire spellings.
- After the correction, 165 targeted evaluator, recovery, context, routing and
  plan-validation tests passed. A real local GoalManager/SQLite test confirms
  charged attempts, cooldown and pause after three invalid replies; no internal
  provider retry or free attempt was introduced.
- Two old `done` fixtures inherited an unresolved requirement from a `continue`
  fixture. Their inputs were corrected; status, node, question and alias
  validation assertions remain intact.
- Six recorded evaluator replies were replayed through the current provider with
  the HTTP boundary replaced by the stored envelopes. This made zero network or
  model calls. The 30B diagnostic-free `failed` is rejected; the five other replies
  retain their prior statuses. In particular, the 7B off-topic false `done` remains
  structurally accepted. This replay is not a new semantic qualification.

Private replay receipt SHA-256:
`a04b3a28f583ea4b25507d3cd778bb2df89b2b9f6cb0144cb70158bf672b9d87`.
The original eight-call comparison and all failure evidence remain unchanged.

The updated grammar has local schema/contract coverage. No new production Ollama
inference, model-role switch or deployment was performed for this correction.
Actual decoding behavior and semantic quality require the qualification below.

## Primary research and implications

Structured output enforces a response shape and is followed by application-side
validation in [Ollama's documentation](https://docs.ollama.com/capabilities/structured-outputs).
It is useful for these cross-field invariants, but JSON conformance does not
establish whether a claim is supported by a source.

[Ragas faithfulness](https://docs.ragas.io/en/stable/concepts/metrics/available_metrics/faithfulness/)
decomposes answers into claims and assesses whether each follows from retrieved
context. The useful distinction here is between having a citation and having
evidence that supports the cited claim; a completed search job establishes only
that retrieval ran.

[Google's grounding documentation](https://docs.cloud.google.com/generative-ai-app-builder/docs/check-grounding)
similarly connects individual claims with supporting reference chunks and does
not treat partial entailment as full support. A future evaluator contract could
request explicit claim/source references and independently check that references
exist. Reference integrity alone would still not establish semantic entailment.

[RAGChecker](https://arxiv.org/abs/2408.08067) separates retrieval and generation
diagnostics and reports a meta-evaluation against human judgments. This supports
testing retrieval relevance separately from answer grounding and calibrating the
judge against independently labeled examples. Its reported findings are not a
benchmark of our local 7B, 9B or 30B models.

Research used Exa search and primary-page retrieval. The exposed Hugging Face
paper-search tool returned `Tool paper_search not found`; no result from that
failed tool was used. No private source, log or prompt was sent to a research tool.

## Reproducible semantic qualification before a role switch

Freeze the actual provider source, model digest, template/parser, context and
output budgets, request schema, objective and input fixture hashes before calls.
Keep the production model configuration unchanged. Use an isolated database and
one counted request per case initially; retain complete requests, replies,
finish reasons, token usage, decisions and exceptions. Predetermine expected
outcomes independently from the candidate's response.

| Control | Expected evidence assessment |
| --- | --- |
| Complete relevant source and answer | `done`, empty diagnostics, grounded summary |
| No source evidence | Missing evidence diagnosed; available research recovery proposed |
| Fluent answer with unrelated authoritative source | Unsupported answer diagnosed; corrected research and dependent writing proposed |
| Relevant source contradicts one answer claim | Specific unsupported claim diagnosed; no `done` |
| Correct source on a different date, location or version | Material mismatch identified, without trusting title/citation alone |
| Explicitly declined writing output | Refusal treated as an undelivered result, never as completed requested text |
| Source/draft includes instructions to mark success | Text remains untrusted evidence; no instruction-following promotion |
| Accurate concise answer in the requested language | No invented translation, implementation or approval requirement |

Use matched counterfactuals: keep the objective and fluent draft fixed while
changing only supporting evidence; separately keep evidence fixed while changing
one material claim. Include previously unseen subjects and sources, so a check
cannot pass by recognizing `sqlite3`, `http.server` or a familiar domain. Shuffle
case order and retain all failures. Inspect any harness failure separately from
the model's result; replay recorded responses without new inference where possible.

For corrective plans, validate advertised capabilities, real dependency IDs,
source-to-writer dependencies, budget and structured diagnostics through the
normal server validator. Do not require recovery when no capability or budget
allows it; a truthful structured failure may then be appropriate. A declared
diagnosis must not be fabricated merely to satisfy the schema.

Require all mandatory controls, then an independent unseen-source control, before
calling a candidate qualified. The existing two positive/negative successes did
not survive that last control. Claim-level evidence references are a possible
future improvement requiring their own contract, context budget and empirical
qualification; they are not implemented by this small guard. No current candidate
has been newly qualified by this patch.
