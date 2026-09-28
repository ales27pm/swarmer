# Project operation batches — 2026-09-28

The iPhone report showed repeated `model patch conflicts with a replacement or
deletion`, followed by incomplete output and the existing no-progress pause.
Read-only production metadata confirmed recent rejections rather than stale UI.
The affected project was paused; no user project was resumed for diagnosis.

## Change

Implementation commit: `984c906c8399989004545c55f69668b5ef43d927`.

The generic Python/Node generation grammar previously admitted mixed operation
families, including edits and patches whose paths collided. The parser correctly
rejected these batches, but the producer contract did not prevent them. Native
authoring already separated these families. All new model requests now select
one family: replacement, addressed patch, deletion, or explicit check. Local
response validation enforces this even if the provider ignores the grammar.
Existing persisted batch parsing remains compatible.

For projects with saved files, after the exact recorded conflict or
incomplete-response diagnostic, the next separately charged iteration allows
one operation and at most 800 source
characters, with bounded metadata. The accepted plan is restored by the worker
instead of being regenerated. The known no-progress pause wrapper and later
user replies preserve this recovery mode; unrelated later assistant responses
end it. Reads, deletions, checks, clarifications and native-ready validation
requests remain available.

No model replacement, increased token/time allowance, silent retries, partial
JSON acceptance, or user-file edits are part of this change. A smaller response
cannot guarantee that the model completes a project. The existing stall pause
and exact-revision validation remain in force.

## Local verification

- Full worker suite as the unprivileged operator: **490 passed, 5 skipped**.
  Skips are optional real-Docker checks, not successful executions.
- New rejection-recovery suite: **29 tests passed**. A baseline comparison of
  the initial 27 tests produced 22 failures and 5 passes before the fix.
- Ruff, worker mypy and `git diff --check`: passed.
- An initial root-account run hit 13 launcher tests' expected unprivileged-user
  boundary; rerunning as the actual operator passed the suite.
- A source-context budget regression introduced by longer instructions was
  caught and resolved without raising the prompt budget; its existing regression
  test passes in the final suite.

## Runtime qualification and deployment

Runtime candidate: `5b062420a650f64ae52aec74885afb8eb1bd7eba`, backported onto
the exact deployed predecessor `e9de98269e1f18413871139a446bd77442d5986f`.
An independent comparison confirmed the worker's added/removed lines match
the main-branch fix exactly. Unrelated main-branch context changes are excluded.
The candidate's full worker suite passed **433 tests, 5 skipped**; the predecessor
does not contain main's additional unrelated test modules. Deployment helpers
passed **88 tests** including admission and recovery paths.

Eight transported files were hash-verified. The staged immutable release is
`5b062420a650f64ae52aec74885afb8eb1bd7eba-37cb20ed06a6`; staging did not change
the active worker. Candidate worker SHA-256:
`779ed81d0086ff19dfad6994c248ef27ff1d404b1d566fe6e2a8c4cf5bd1fcf9`.

The real-model probe passed at **2026-09-28T01:34:28Z** in **71.781 seconds**:

- One call to the existing `swarmer-project-qwen3-coder-heretic:30b-32k-d2d985e`;
  183 generated tokens, normal terminal response, unchanged 2,000-token ceiling.
- Only the benign fixture's `src/calculator.py` changed. Immutable tests,
  scoped guidance and accepted plan were preserved.
- One isolated Docker validation: compileall passed and **2 pytest tests
  actually executed, 0 failures**. No additional inference call occurred.
- 29 idle-admission checks; all **37 protected fingerprints identical** before
  and after. No database writes or project resume.
- The first probe preflight stopped before inference because it expected the
  loopback model URL without `/v1`. The test script was corrected to match the
  unchanged configured URL; no earlier model call or retry was consumed.

Receipt SHA-256:
`8056ab5002332e2169e5683bd07e0b96e963af8eafad3bb68a188bf3bf570f13`.
This proves one bounded repair through the actual provider and check runtime;
it is not an end-to-end completion test of a user's larger project.

Production activation completed under the supervised rollout. Independent
verification at **2026-09-28T01:36:31Z** confirmed:

- Active immutable release `5b062420a650f64ae52aec74885afb8eb1bd7eba-37cb20ed06a6`.
- Exact changed worker bytes mounted in the running sandbox; seven other runtime
  source files preserved, with the same model, credentials and configuration.
- A fresh authenticated worker heartbeat and healthy control plane (`0.14.2`).
- All 37 protected data fingerprints unchanged, zero executable active work,
  and unchanged unrelated service processes. No database restore or user-project
  restart occurred.
- A separate root read-only SSH check confirmed the current process directory,
  expected worker SHA-256, active/running service and zero service restarts.

Reviewed baseline SHA-256:
`e47cbda14829346175df669cbb714578530fe8420330682361618db89430a685`.

The affected project remains paused with its saved files. Its next explicit
resume uses the corrected worker. Historical rejection messages remain in the
conversation; they are not rewritten. No new iOS binary is required.
