# Writing model input contract — 2026-09-30 UTC

Status: locally implemented and tested; no production deployment.

## Reproduced gap

The writer derives measurable constraints from the objective and user messages
when a caller omits `requirements`. It already used those constraints to allocate
the response budget and validate delivery, but did not include their structured
form in the model input. The original objective was still present as prose.

The preceding isolated source-coverage trial omitted that field. The normal
server preparation path supplies it. This finding therefore does not establish
the cause of every live short draft or `insufficient_sources` outcome.

## Change

`_model_input` now materializes the same derived requirements in its private
projection. Explicit server requirements, including an empty object, remain
authoritative. The canonical job, user conversation, source evidence and citation
mapping are not rewritten. Historical requests with no measurable constraints
keep their existing shape.

The final projection must remain within the existing 32,000-byte UTF-8 payload
budget. A projection that exceeds it fails before connection or inference.
Acceptance checks, non-delivery classification and the one-call limit remain
unchanged.

## Local evidence

- Three new regressions failed before the implementation: missing structured
  requirements with/without sources and overflow after materializing constraints.
- **194 text-worker tests passed**, including explicit-contract precedence,
  ignored assistant instructions, input immutability, historical compatibility,
  cancellation and one-call behavior.
- **106 server tests passed** across writing requirements, completion, file
  requirements and research-to-writer handoff. A dependency deprecation warning
  remains; no test failed.
- Ruff lint/format with `server/pyproject.toml`, strict mypy on the writer and
  `git diff --check` passed.

This is not another full-repository suite run. Initial tests caught an empty
`requirements` object being added to historical unconstrained requests; that
compatibility regression was corrected before the final passing run.

## Controlled live qualification

The replay uses the exact payload captured in `source-coverage-20260929-2350`,
with SHA-256 `4630cfb31ffaa1ef6213c22757fb1f3a878b5a83f55ab21586c763193f2a3f73`.
It retains the same three dated official page excerpts and configured Qwen 7B
writer. It performs no new searches, creates no production jobs and uses a
private copy of the candidate worker with recorded source hashes.

Read-only SQLite admission checks gate inference and run during generation.
Production work appearing during the trial aborts the trial's own connection.
No user task, service, model alias or production record is changed.

Local receipts are under
`/Users/ales27pm/Library/Logs/SwarmerQualification/writing-model-contract-qy8cqow9/`.
The live outcome and independent validation are recorded separately there.
Neither a valid envelope nor a terminal server status establishes semantic
compliance with the requested comparison or a successful iPhone workflow.

The replay made **one real model call** and ended in `wall_timeout` after
120.046 seconds. No completed model response or draft was accepted. Independent
server-side validation of the captured request confirms the same derived contract:
150–200 words, two citations, `docs.python.org` and `sqlite.org`. All three source
projections were present. This proves transmission, not successful drafting or an
improvement/regression in model quality. There was no automatic retry.

The production goal linked to the three jobs previously preventing admission,
`goal_59b6c58e74b84f7c9f103e635214540c`, was already terminal when the user authorized
closing it if still active. Its recorded status was `budget_exhausted`, completed
at 2026-09-30 01:23:17 UTC, with reason `goal project iteration budget exhausted`.
All three inspected jobs were completed. No goal status was rewritten and the
terminal budget state was not reclassified as verified project success.
