# Saved answers and empty synthesis regression

The reported CRM goal saved the user's requested features correctly, but the
evaluator input omitted the conversation. Both evaluations therefore returned
the same requirements question. The exact persisted evaluator-context hashes
matched the model-call input digests. The post-reply planner saw the answer but
also saw the stale evaluator claim that requirements were missing.

The release carries bounded, redacted conversation and its revision into the
evaluator's persisted input. The latest question/answer stays whole or evaluation
fails closed on the configured context budget. Later answers take precedence
over historical assistant claims. Generation schemas expose the existing
status/question/node constraints enforced by the authoritative parser.

Empty deterministic synthesis is skipped without consuming a completed-work
step. Old placeholder summaries cannot become evidence through chained
synthesis. New plan, replan and evaluator nodes inherit the current conversation
revision. Replanning resumes the active runtime clock after a user wait.
Historical questions remain stored but are pending only while the current goal
actually needs a user answer.

The mobile conversation also had an independently reproduced refresh race.
A parent refresh during submission could invalidate its reload and leave the
controls disabled. Deferred refresh now reconciles after submission while
preserving pairing fences, uncertain-send warnings and the same explicit retry
identifier. No automatic message retry is introduced.

## Verification

- Regression tests first reproduced the missing evaluator revision/conversation,
  empty synthesis consuming two steps, and disabled reply controls.
- Database integration tests verify the exact saved answer, persisted JSON and
  input digest; node revisions; skipped/blocked empty synthesis; legacy placeholder
  rejection; runtime pause accounting; and pending-question visibility.
- Mobile validation: 389 tests in 32 suites, TypeScript, lint and Expo Doctor
  21/21 passed. Native store/harness and archive dependency checks passed.
- A first real replay against the configured Qwen 2.5 Coder 7B model still
  repeated the question with an invalid `continue` decision. After refining the
  instructions and grammar, both the original three-message history and current
  four-message history returned valid `continue` decisions with one
  `code.build_project` node preserving Python and all supplied features.
  Responses used HTTP 200 and took 4.803 s and 4.030 s. These are bounded incident
  replays, not evidence of actual project execution or general model quality.
- One additional Python inventory replay preserved SKU identifiers, stock levels
  and CSV import, selected `code.build_project`, and introduced no CRM features.
  It returned HTTP 200 in 4.559 s. No further evaluator replays were run.
- No replay modified a goal, job, approval, conversation, or live model binding.

Deployment, recovery of the existing paused goal, IPA validation, upload, and
physical-device behavior require their own evidence; passing tests alone does
not establish those outcomes.

## Deployment and submission outcome

The server was deployed at `f5854de3175c6df58968af6b3faaefb4ca19e409` after
1,072 server tests and 413 worker tests passed (9 and 5 explicit skips).
Ruff, mypy, Bandit and the committed OpenAPI checks passed. Health, installed
source identity, fresh worker heartbeat and unchanged protected tables were
verified. Schema 23 and explicit model bindings were preserved.

One audited operator replan reused the existing saved reply. A separate real
planner rehearsal still produced empty synthesis; production skipped that
proposal and normal evaluation dispatched the project worker. The repeated
question is no longer pending, and historical messages were preserved. The
first worker attempt timed out; bounded continuation subsequently produced files
and check receipts. Project implementation was still being repaired at the last
observation; resumption is not a completion claim. No file approval was supplied.

iOS build `20260913000200` is committed but **not uploaded**. EAS refused cloud
creation after the monthly iOS quota was exhausted. Local signing was repaired
with an isolated temporary keychain, but ExpoModulesJSI failed to compile under
the installed Xcode 26.3. Expo SDK 57 requires Xcode 26.4+; those toolchains
require macOS Tahoe 26.2+, while this host runs macOS 15.7.9. No IPA was produced,
temporary signing credentials were removed, and the original keychain search
list was restored. At that checkpoint, App Store Connect listed `20260912232600` as valid and
in internal beta testing. That SDK 57 submission remained blocked; no
subscription purchase or OS upgrade was performed.

References: [Expo SDK requirements](https://docs.expo.dev/versions/latest/),
[Apple toolchain requirements](https://developer.apple.com/xcode/system-requirements/).

## Follow-up evidence

A read-only snapshot at 2026-09-13 00:49:17 UTC found the recovered CRM goal
`budget_exhausted` at 30/30 model calls and 14/20 steps. Twelve project-worker
jobs were recorded: eleven completed and the last cancelled. Revision 11
retained `requirements.txt` and `tests/test_clients.py`; saved install and
compile receipts passed, but pytest failed with exit 5. No apply task or file
approval exists. The duplicate question is not pending, and the preserved
history contains normal progress messages. The fix restored execution; the
CRM application did not finish successfully. No further runtime mutation was
performed. Local and HTTPS health remained 200 at source `f5854de`.

The user then requested an Expo downgrade for the installed Xcode. SDK 55
compiled and exported successfully with Xcode 26.3, preserving this mobile
conversation fix. Build `20260913004000` supersedes the unuploaded SDK 57
release attempt. Its source, IPA verification and submission status are in
[the SDK 55 release evidence](expo55-xcode263-2026-09-12.md).
