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
  It returned HTTP 200 in 4.559 s. No further model replays were run.
- No replay modified a goal, job, approval, conversation, or live model binding.

Deployment, recovery of the existing paused goal, IPA validation, upload, and
physical-device behavior require their own evidence; passing tests alone does
not establish those outcomes.
