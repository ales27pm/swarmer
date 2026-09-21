# Written plans: deployment qualification, 2026-09-21

Candidate source: `acaec196f6aa36a703dc4b12e728516cc7e9ceac`.
This change routes requests for plans and other written deliverables to the
bounded `writing.draft` worker. Code implementation remains a separate skill.
A generated plan is not evidence that an application was built or tested.

## Controlled stop

The user authorized the pending server deployment and controlled interruption.
At 03:24:25 UTC, the installed server's canonical `GoalManager.cancel_goal`
completed cancellation of `goal_5150854edc18444f966ee372ce2686d0`. Its root task
was cancelled. All twelve worker jobs had completed by the final cancellation
snapshot, and their existing results were preserved. No raw SQL edits, new
device credentials, database restore, or restart of previously failed goals
were used. The subsequent deployment gate found zero active goals, jobs,
model calls, nodes, tasks, or tools.

The first iPhone cancellation request failed at transport; database inspection
confirmed that the goal was still running. The controlled host operation used
the installed application services without starting another application
lifespan or reconciliation loop. The paired iPhone subsequently reconnected.

## Candidate and installation checks

The immutable API wheel matches 65 installed application files. Dependencies
pass `pip check`. The unchanged StateService preserves schema version 24 and
all 57 tables in a private snapshot initialization test. The deployment helper's
ten tests cover active-work rejection, writer exclusion, recovery and preserved
project-worker bindings. Its configured Linux service hardening was exercised
with a transient service.

The installed physical iPhone Debug build is `20260921012604`, bundle
`org.27pm.mongars`. The embedded native and configured build numbers agreed in
a successful `app.status` request at 03:24:29 UTC. The application API advertises
`goals.writing-draft`. This is a development installation, not a TestFlight upload.

## Live verification and deployment

The first disposable live case (native Swift CRM plan) completed with exactly
three model calls and zero tool calls or approvals. Planning took 12.051 s,
CPU writing 98.785 s, and evaluation 48.567 s. The writer emitted a terminal
stop after 467 tokens and produced 1,433 UTF-8 bytes. Full-text retrieval matched
SHA-256 `4028accbce9645668bde28d526e3f58e3e12658d3e56ed8f7fbed2d1594d0934`.

The second disposable case preserved the latest conversation features (clients,
soumissions, courriels, calendrier) in both the worker payload and actual text.
Planning took 76.236 s, writing 39.682 s (199 tokens, terminal stop), and evaluation
27.633 s. It completed with three charged calls, zero tools/approvals, and an
exact full-text retrieval match. Its 581-byte draft has SHA-256
`01d2fd67c13e3731628e498c259ebf2005f61426fea87ae6e0e4bfcc70c79069`.

Both cases used the existing configured 7B model, CPU writing, one attempt per
call, a 512-token output limit, and the unchanged 120-second writer wall limit.
The earlier 1,536-token candidate timed out at that limit; that failure is not
counted as a passing result. The final combined live receipt SHA-256 is
`ab0029608c34b268f34c9966843209d855cebbbe3f2ad7b17cec3c5dceb214db`.
These are short, high-level plans; the first is structured JSON rendered as
plain text. The tests establish routing, completion and guidance preservation,
not comprehensive design quality or software implementation.

The first supervised cutover stopped before activating the candidate: systemd
rejected stopping a frozen service. The recovery hook restored service health
on the previous release; no agent enrollment or database restore occurred.
The retry must thaw the API while retaining SQLite's writer reservation, then
stop both services before releasing that reservation.

The revised helper passed twelve tests locally, on Ubuntu and in independent
review. Its exact passive-task fingerprints reject changed/retried tasks.
Durable goal/job/tool admission and accepted results stay blocked across thaw
and stop. Transient chat/embedding requests can be interrupted by the explicitly
authorized service stop; this is not a claim of universally fenced inference.

The second supervised attempt activated the candidate and started the new
worker. Its final checker incorrectly used an old project-worker credential
file's identity, while the unchanged active worker environment specifies a
different existing identity. The forward recovery hook kept the new version
active. The original journal still truthfully says `activated`; a separate
read-only verification at 03:40:18 UTC confirms effective completion:

- API process working directory points at the candidate and exposes the new
  writing-draft route; health is OK.
- The active project worker and new text worker have fresh authenticated online
  heartbeats, confirmed against actual process/configured identities.
- Existing agent identities, configuration, units, project-worker source and
  bindings are preserved; schema remains 24. No database was restored.

The separate verification receipt SHA-256 is
`af785ce1f2d75e5f6164ab1615ad41463d9340f2a4a723eb694ee3a398d712cb`.
The reviewed helper SHA-256 is
`38c4e8ca890c3c332250f3990c1a83ff792935dbc01fdee8af685ab23eb85cd8`.

## Physical iPhone end-to-end test

The installed application API created and started the benign CRM writing goal
`goal_9568a2ce3c954940b472356cd083712d`. Live server state confirmed one
`writing.draft` node and the new worker processing its job. The fuller request
exposed a failure: the writer reached exactly 512 output tokens and failed
validation after about 100 seconds. No draft was accepted. Old worker logging
cannot establish the exact terminal reason, but Ollama's output count confirms
the ceiling was reached. The evaluator then exceeded its production transport
limit; the four-call test budget stopped the goal. The cancellation request
arrived after terminal budget exhaustion and did not restart it.

This physical failure is not counted as success. It also exposed a qualification
gap: the first fixture allowed 120 seconds for planner/evaluator calls while
production allows 60. The worker always used the correct 120-second wall cap.
A compact plain-text prompt and safe error classification are being qualified
against the exact physical-test objective and production provider limits.

## Follow-up fixes and agent visibility

The compact worker (`a71e233`) keeps the same 512-token/120-second bound.
Its first exact-input qualification exposed the planner's 60-second limit.
`0fb145a` adds configurable absolute provider deadlines bounded by their lease;
the prepared server configuration uses 120 seconds and a 180-second lease.
The unchanged global goal budgets still apply.

The next exact-input case produced a valid 725-byte draft (233 tokens, terminal
stop), but its evaluator continued instead of completing. That evaluation saw
only the short summary, not the accepted document. This result was a failed
end-to-end qualification; concurrent user work also prevents treating its
latencies as isolated benchmarks.

The follow-up evaluator projection now reads the strictly matched completed
writing job and includes a redacted excerpt plus job and text-hash provenance.
It honors both the configured per-node and total context limits. Normal goal
responses and task summaries do not gain the full document. The evaluator can
still reject or continue; a completed writing job never forces goal success.
The focused six-file regression set passed 77 tests, with Ruff and mypy clean.

The user's report of only two agents is confirmed: the active project and text
workers are real executors, while the catalog's roles do not represent additional
connected processes. Existing file and code-review workers are being prepared
separately. Research has no configured adapter and must not be represented as
available. Root tasks previously displayed only direct tool calls; the new
read-only task projection shows up to 20 child worker nodes with skill, agent,
status and safe summaries, and links to the goal's project-check evidence.
Worker results and project checks remain distinct from direct tool calls.

Task projection and screen validation passed 15 server tests and 27 mobile tests;
TypeScript, ESLint, targeted Ruff and mypy checks passed. The GET path uses a
read-only database transaction and reads no worker payloads or raw results.

The first c6edc9b disposable qualification stopped after a correct single-node
writing plan: planning took 102.993 seconds, but fixture-only agent heartbeats
had expired at 90 seconds. No writer or evaluator call ran. The failure receipt
is preserved (SHA-256 `9eeea9594bf2df757adb4db2c0e2f4eafe97672865958c788ef2f2a5577c1b93`).
The qualification helper now maintains authenticated heartbeats throughout its
bounded lifetime, matching production workers. Three fast tests reproduce the
103-second liveness failure, verify successful dispatch with heartbeats, check
cleanup, and reject invalid credentials. This changes the fixture, not the
candidate's worker admission rules.

The signed physical Debug build `20260921041035` was built from c6edc9b and
installed successfully. All 172 recorded mobile/native source files matched;
the provisioning profile includes the target iPhone. IPA SHA-256:
`1f8e7ee6a25a3e6de1167140fb3a7912b509df2b375a64328ac0a1bb62071ac2`.
Initial API health passed. This installation is separate from the pending
server cutover and does not establish end-to-end goal success.

With continuous fixture heartbeats, the real planner and writer passed:
79.865 seconds for planning, 42.293 seconds for writing, 244 output tokens with
a terminal stop, and an 806-byte/109-word French draft. Before evaluation, the
fresh admission check detected that the user had resumed goal522. No evaluator
request was sent. The run is incomplete, not a passing end-to-end result;
its receipt SHA-256 is `97352c703c077e2bfa9fa57a09c637b6ddabbf216388c575fa9ebd5b0fac503b`.

After the iPhone installation, its API console terminated. A fresh diagnostic
confirmed the saved pairing and Developer Mode, but an unavailable development
tunnel and unavailable DDI. One targeted Apple-service refresh retained the
pairing and did not recover a usable session. `app.status` and post-deployment
physical API tests therefore remain unverified at this point.

Goal522 naturally reached `budget_exhausted` at 04:27:58 UTC after 13 steps and
30 calls; no cancellation was needed. A separately labeled evaluator-only probe
then reused the preserved real draft, a fixed manual plan and canonical strict
worker-result submission. Its sole real evaluator call took 9.011 seconds and
returned `needs_user`, inventing a requirement to approve the written plan.
The actual objective and criteria request no such approval. The full accepted
text was confirmed in its bounded context. Receipt SHA-256:
`acb1a6384c99db1b9e8d6f9f7a77baa73f82072363efc3cf9dc4299eb4d91ef3`.
The pending follow-up clarifies the evaluator's distinction between untrusted
text instructions and valid evidence of a text deliverable; it does not bypass
real authorization requirements or rewrite model decisions as success.
