# Translation reviewer comparison — 3 October 2026

The actual private comparison completed all **92 planned inference calls**, with
no retries, missing observations or unresolved HTTP outcomes. Its owned GPU
reservation was cleared. Neither reviewer is qualified by this result, and the
experiment changed no production service, memory record or acceptance rule.

This follows the [failed full memory-provider trial](memory-provider-qualification-2026-10-03.md).
It isolates review: it does not translate, ingest, retrieve, or qualify the newer
local iPhone context endpoint or worker transport.

## Frozen inputs and limits

Both arms used the installed, pinned
`swarmer-research-qwen35:9b-8k-6488c96fa5fa` model, digest
`cc81d93f910b2a4fbb2b5cbd38faf70de1b39178871f866f2cd80f09f4172a3d`,
through Ollama 0.32.3, temperature zero, 2,048 output tokens, and independent
conversation inputs. The original strict review contract came from immutable
commit `c527f5d3be9f5c59b1c8ab5149e8f7c0bd8310c9`. The second arm kept every
decision flag and added typed issues with exact source/candidate quotations.
Invalid explanations could not turn negative flags into acceptance.

The predeclared corpus contained 10 reference cases, the one discovered bilingual
code case, and 12 reserved cases. Every case was evaluated twice by each arm in
counterbalanced order. Repetitions are dependent observations, not additional
independent examples. The reserved cases were visible to their author; this is
neither a blinded external benchmark nor evidence about training-data novelty.
The prompt, labels and corpus remained unchanged throughout the trial.

## Observed results

Counts below include both repetitions. Correct semantic rejection excludes a
malformed explanation, even when the invalid response safely blocks acceptance.

| Lot / arm | Planned positive / negative | Correct accepts | Correct semantic rejects | False accepts | False semantic rejects | Invalid |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Reference / original | 10 / 10 | 8 | 8 | 2 | 2 | 0 |
| Reference / quoted issues | 10 / 10 | 8 | 2 | 2 | 0 | 8 |
| Discovered / original | 2 / 0 | 0 | 0 | 0 | 2 | 0 |
| Discovered / quoted issues | 2 / 0 | 0 | 0 | 0 | 0 | 2 |
| Reserved / original | 12 / 12 | 12 | 12 | 0 | 0 | 0 |
| Reserved / quoted issues | 12 / 12 | 12 | 0 | 0 | 0 | 12 |

Each arm therefore leaves **4 of 24 faithful observations unaccepted**. Moving a
false rejection into the invalid category does not improve that outcome. The
quoted-issue arm has 22 invalid observations: 18 have missing required quotation
sides, and four have a quotation/occurrence mismatch as their first validation
error. Some contain further defects, including duplicate issues and confusing a
character position with the occurrence number. No raw model reasoning was saved.

Eight of each arm's 22 negative observations would already be stopped by the
existing exact-literal or unchanged-English gates. Both false accepts concern
the same remaining reference case, repeated twice; they are not two distinct
translation mistakes. They are reachable by the reviewer and cannot be dismissed
as already covered by those deterministic gates.

## Independent inspection of the synthetic text

The parent inspected the source/candidate pairs and recorded verdicts separately
from the runner's author labels:

- Both arms accept changing the export prerequisite from signature **and**
  validation to signature **or** validation. The source requires both; the
  candidate permits either. Every preservation flag nevertheless returns true.
  The quoted-issue arm returns an empty issue list.
- Both arms refuse the faithful translation of French permission into English
  “You may close the window after saving.” The original arm sets
  `uncertainty_preserved=false`, despite permission being preserved. In this case
  “may” denotes permission, not an added uncertain factual claim.
- The discovered code case preserves the French instruction's meaning, all code
  identifiers and the existing English sentence. The original arm again rejects
  it, while the issue arm produces an invalid explanation. This remains a known
  regression example, not a reserved success.
- Exact issue quotations do not prove a sound diagnosis. For example, the
  altered permission case includes a valid quotation pair identifying the
  permission-to-obligation change, but also an omission issue attached to
  `no_added_facts`. The quote locator cannot establish that such a category is
  semantically appropriate.

This inspection supports the specific diagnoses, not a universal error rate or
the correctness of every generated issue. The stored
`human_issue_correctness` field remains null; an assistant inspection is not a
human evaluation and does not silently rewrite the frozen scoring result.

## Decision and next experiment

Do not promote the quoted-issue schema or relax the production acceptance gate.
The next bounded hypothesis is a compact clarification of Boolean prerequisites,
permission versus factual uncertainty, and faithfully retained English technical
spans, using the existing strict response schema. The known corpus stays a
regression set. Any evaluation of a changed prompt must retain these results and
use a newly fixed reserved set, including valid paraphrases; counting occurrences
of “and”, “or”, or “may” is not a semantic validator.

End-to-end ingestion/retrieval and target-device qualification remain required.
Successful request parsing, a green transport suite, or the reserved result of
this small reviewer-only trial cannot substitute for those checks.

## Reproducibility receipts

Remote run: `/home/ales27pm/.local/state/swarmer-provider-qualification-c527f5d-20261003/reviewer-comparison-v2/runs/comparison-01`.

| Artifact | SHA-256 |
| --- | --- |
| Comparison seal | `66fe24aa6c7dab2aca520809fb20546698266f2fd3bf3e87cfa1f54fc8aafa39` |
| Predeclared plan | `8cc96768b974adc3f3afb72a092ec55f3aedb51242294951d3ed0797461dbea1` |
| Observations | `001dd26959adea9019a6651958e523f299b503d908e0221a5d337b4b7cdfda94` |
| Scores | `d9c69e6fff8e73f0ede3b5746e86c64bb35827110f2bb95cf77797e06fa63b4a` |
| Terminal receipt | `588a3ef085eb05f5624dff5f3867ed05d36bf9a6c55a1664d571de6cd0c8bd33` |

The receipt's `wall_seconds: 1800` is the configured trial ceiling, not measured
elapsed runtime. All 92 responses completed; HTTP 200 alone was not counted as a
correct review.
