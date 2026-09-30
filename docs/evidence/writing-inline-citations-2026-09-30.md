# Writing citations: one model-authored reference per claim

Status: implemented and checked locally; production unchanged. This continues
the [runtime comparison](writing-runtime-comparison-2026-09-30.md).

## Reproduced failure

The writer required the model to name each source twice: in a bracketed marker
beside the claim and in a separate `source_ids` list. Qwen 2.5 Coder 7B cited S3
without selecting it; the new outcome schema produced exactly the same final
object as the preceding flat schema (18.074 s, still rejected). The installed
Qwen3.5 9B abliterated candidate selected S2 without citing it (41.787 s,
233 words, also rejected). It also invented a recommendation threshold of a few
thousand consultations per day. Neither trial qualified a production role.

## Change and preserved boundaries

Current generation supplies only markers in the text. The worker resolves each
known ID once, in first-citation order, to the exact admitted URL. It does not
insert a marker, choose an unused source, repair an unknown ID or add evidence
to a non-delivery. Summary-only references cannot satisfy a citation requirement.
The closed outcome schemas no longer generate a redundant selection array.

The decoder retains compatibility with older private responses carrying that
array, including its exact marker/list equality and uniqueness checks. Public
delivered results remain `schema_version`, `content_trust`, `text`, `summary`.
The server independently validates those canonical URLs and measurable user
requirements. Word limits, required domains, byte limits, timeouts, cancellation,
single-call behavior and the separation of non-delivery outcomes are unchanged.

Five new regression cases failed before implementation; eleven invalid-output
controls already passed. After the change, **254 writer tests** pass in 6.62 s,
including the outcome, legacy selection and new marker-only cases. A targeted
server integration run passes **164 tests** in 16.32 s, including a new check that
the worker's canonical output is accepted by the server and a substituted URL
is independently rejected. Ruff, formatting, strict worker mypy, targeted Bandit
and `git diff --check` pass. These are not a full-server, deployment or iPhone run.

## Real replay: quantitative pass, semantic failure

The installed candidate is
`swarmer-research-qwen35-abliterated:9b-8k-9f646d7e`, Ollama digest
`dea44495ce5b261fda71feeb08020d0b960f2f563869d1c70fb1bb0276a9ffdc`.
This checks the writer use case; its earlier research-evaluator failure remains
recorded in [the role assessment](model-role-alignment-2026-09-24.md).

The unchanged captured payload has SHA-256
`4630cfb31ffaa1ef6213c22757fb1f3a878b5a83f55ab21586c763193f2a3f73`.
It requests 150–200 French words comparing SQLite/JSON persistence, searches,
updates and limits, followed by a recommendation, with at least two official
citations spanning Python and SQLite. It includes three actual page excerpts.

One replay of the changed worker used temperature 0, `think: false`, 1,312 output
tokens and 32 requested GPU layers. It completed in **28.323 s**, with first
content at 8.437 s and 361 generated tokens. Its **184-word** French note carries
three exact official citations, recommends SQLite, and passes both the worker
and an independent invocation of the server's canonical validator. The alias
kept its 8,192-token context; the host had reported 5,339,516,763 bytes in VRAM
for this model during the preceding trial. Placement and timings are observations,
not concurrency or latency guarantees.

Reading the note against the supplied passages still finds substantive defects:

- The source's website-traffic example below 100K hits/day becomes a criterion
  for choosing storage for a local single-user CRM. That scope transfer is not
  supported by the passage.
- Searches are called instantaneous without evidence for that guarantee.
- The `json.load` behavior is generalized into an unavoidable limitation of the
  entire JSON format.

Therefore `response_validated: true` and `delivered: true` mean that a complete
document passed the mechanical contract. The separate review records
`semantic_conformity: not_accepted` and `all_requirements_proven: false`.
This is not a successful CRM acceptance test or a qualified replacement writer.
No automatic retry was added. No role, production record, service or model file
was changed. Each trial was admitted against live production activity using
read-only SQLite and registered no production job.

Next: qualify evidence interpretation, including a control where a source is
official but its section applies to a different use case. Preserve these negative
examples when evaluating the complete research → writing → evaluation flow.

## Receipts

Local directories under `~/Library/Logs/SwarmerQualification/`:

- `writing-qwen-outcome-ckx49s5k/`: unchanged faulty 7B response.
- `writing-qwen35-outcome-sm5rpo6k/`: 9B selection mismatch and semantic review.
- `writing-inline-citations-6h8f9k4o/`: before snapshot, patch and local checks.
- `writing-qwen35-inline-sssdfwt4/`: immutable candidate sources, exact payload,
  stream timing receipt, final content, canonical draft and independent review.
