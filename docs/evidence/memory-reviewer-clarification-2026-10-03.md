# Translation reviewer clarification trial — 3 October 2026

The private v3 comparison completed **70 of 70 planned inference calls**. Both
arms returned valid structured responses, but the clarified prompt still accepted
a translation that weakened a prerequisite. The predeclared integration gate
failed. No prompt, acceptance rule, model configuration or production memory was
promoted or changed by this experiment.

This follows the [quoted-issue comparison](memory-reviewer-comparison-2026-10-03.md).
It tests the next hypothesis stated there: clarify Boolean conditions, permission
versus factual uncertainty, and English technical spans within French prose,
while keeping the original strict `_Review` schema and acceptance gate.

## Frozen comparison

Both arms used the same model and settings as v2:
`swarmer-research-qwen35:9b-8k-6488c96fa5fa`, digest
`cc81d93f910b2a4fbb2b5cbd38faf70de1b39178871f866f2cd80f09f4172a3d`,
Ollama 0.32.3, temperature zero, 2,048 output tokens and independent conversation
inputs. The baseline is the unmodified review prompt from frozen `c527f5d`; the
candidate appends a compact clarification. Hash, language, five preservation
flags and clarification checks are unchanged. No generated flag is corrected
after the response.

The 23 v2 cases are now a known regression set, including v2's formerly reserved
cases. Twelve new cases were fixed before inference, with direct faithful
translations, equivalent paraphrases and deliberate changes. A pre-run review
replaced “invalid” with “not valid” in the archive paraphrase to avoid treating
an unknown checksum as proven invalid. The initial draft and exact difference
remain in private provenance. No text or expected label changed during the run.

Each case was evaluated once by each arm in counterbalanced order: 20 positive
and 15 negative observations per arm. These are synthetic examples reviewed by
assistants, not an external blinded benchmark. One observation per arm/case
does not establish stability, even at temperature zero.

## Results

| Lot / arm | Positive / negative | Correct accepts | Correct semantic rejects | False accepts | False rejects | Invalid or missing |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Known / baseline | 12 / 11 | 11 | 10 | 1 | 1 | 0 |
| Known / clarified | 12 / 11 | 12 | 10 | 1 | 0 | 0 |
| New reserved / baseline | 8 / 4 | 5 | 4 | 0 | 3 | 0 |
| New reserved / clarified | 8 / 4 | 6 | 4 | 0 | 2 | 0 |

The clarified arm accepts 18 of 20 faithful translations versus 16 of 20 for the
baseline. Both wrongly accept the same altered export condition. Four negative
cases per arm would already be blocked by the exact-literal or unchanged-English
gates; the false acceptance is among the remaining 11 reviewer-reachable
negative cases, not among those deterministic rejections.

Independent inspection of the failed text pairs and recorded verdicts confirms:

- **Both arms miss a weakened requirement:** the source requires signature and
  validation before export; the candidate allows signature or validation. Both
  set every preservation flag to true and require no clarification.
- **Both reject an equivalent prohibition:** permission to publish only if the
  checksum is valid and security review complete is expressed as a prohibition
  if either prerequisite is not satisfied. The semantic condition is preserved,
  but both reviewers reject meaning, uncertainty and no-added-facts checks.
- **Both reject an equivalent alternative:** “one or the other approval
  suffices” becomes “at least one of the approvals ... has been received.” Both
  incorrectly mark `no_added_facts=false`.
- The clarified arm accepts the previously rejected mixed French/English code
  case and the direct permission-to-permission transfer case in this run. Those
  two improvements do not repair the incorrect acceptance above or establish
  repeatability.

All 70 HTTP responses completed with status 200 and `stop`; none was counted as
semantically correct merely because transport succeeded. There were no retries
or unresolved HTTP outcomes, and the owned GPU marker was cleared at exit.

## Decision and remaining work

The frozen gate required complete valid observations, acceptance of all faithful
candidate cases and valid rejection of all reviewer-reachable altered cases.
The first condition passes; both semantic conditions fail. Do not promote the
clarification or relax the gate. Do not repair verdicts with cue-word counting:
the faithful negated and “at least one” examples show why lexical operators
alone cannot determine equivalence.

Prompt refinements have now failed to remove the known false acceptance. The
next investigation must reassess the reviewer mechanism or provider against the
retained counterexamples, rather than repeat the same prompt experiment or
discard difficult cases. New reserved cases will be needed to assess any change;
this corpus is now known. Full translation, ingestion, revision, deletion and
retrieval qualification still remains; no such phase ran in this reviewer-only
comparison. Infrastructure staging is separate from this failed quality gate.

## Reproducibility

Private remote run:
`/home/ales27pm/.local/state/swarmer-provider-qualification-c527f5d-20261003/reviewer-comparison-v3/runs/comparison-01`.

| Artifact | SHA-256 |
| --- | --- |
| Transferred archive | `fd97eed47beb7931a71483cc003eb619035a5c7fcec3304f4135b66eebed0270` |
| Comparison seal | `d86ede14aa50b525542359bbcde991cd74cf299098753b748b26aba5b9aa7b0f` |
| Predeclared plan | `df7302f7a575a7ba204a682660125459008e18488c6851192d5085b73491436b` |
| Observations | `e8d55c688bd7e7d7687b93d3d6390860faef71b7a9faa70d4356a7c3c2fe1cdb` |
| Scores | `fe2a5654e165a4d736d2d60804185135e4871c5584efb2c44ee75e3901f9109f` |
| Terminal receipt | `10f1afe2ebc7e42a878a1e0e3f7f1d0424d49471e66a0291fe8ab0f260024b6c` |

The runner passed 48 local tests with networking forbidden, followed by Ubuntu
verification and a 70-call simulation before the one live run. Simulation
validates harness behavior, not translation quality. The 1,800-second setting
is a ceiling, not an observed runtime or a reason to repeat the experiment.
