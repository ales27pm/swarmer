# Project worker readiness follow-up — 2026-10-01

The prior 64,000-token server-API qualification stopped without a test file. It
preserved requirements but repeatedly timed out, and its browser executable
references were redacted. This follow-up keeps the configured context, GPU,
timeouts, runtime image and permissions unchanged.

## Changes

- Exact pytest/Node no-tests receipts constrain the next mutation to a test file
  discoverable by that runner, or allow a necessary source read. A filename is
  not proof of a passing test. Other failed compilation/dependency checks take
  precedence and remain repairable.
- Test creation omits unusable patch addresses and duplicate previews while
  retaining actual source and user requirements. Concise instruction wording
  preserves the existing near-limit focused-source regression.
- Completion accepts setup in nonempty `run_instructions` without requiring an
  extra README. Successful executed checks and actual worker test-count gates
  remain; an explicitly requested README is still a project requirement.
- Context redaction preserves only the exact public references
  `/usr/bin/chromium` and `/usr/bin/chromedriver`, after secret redaction. It
  does not assert availability or grant permission. Cached capsules refresh
  through projection version 3; historical source records are not rewritten.
- One content-free transport receipt is logged per actual model attempt, with
  authenticated job attribution, request sizes, time to headers/content, elapsed
  time and observed terminal Ollama counters. Missing measurements remain null.
  Transport success is separate from acceptance of the generated project step.
  Model text, reasoning, credentials and private paths are not logged in receipts.

## Local evidence

- Full project-worker suite under the ordinary operator account: **715 passed,
  5 skipped**. No Docker/device execution is claimed for skipped tests.
- Affected server context/publication suites: **107 passed**; new inline setup
  contract suite: **9 passed**.
- Ruff and mypy pass for changed source and tests; `git diff --check` passes.
- Regression evidence includes rejection of unrelated edits after zero tests,
  preserving repairs for other failures, unchanged source/read boundaries,
  content-free success/error/timeout receipts, and cached redaction refresh.
- Private release adapter qualification: **65 passed**; exact regular Git file
  mode checks: **7 passed**. Package and deployment evidence follow separately.

## Runtime status

Implementation commit: `bb9627c9541e2f65524b87f3bc564e814b6d9ff5`, pushed to origin/main.

- API release: `local-20261001-project-readiness-3dda7323a8a6`.
- Project worker: `project-readiness-5f142af0108783009974`.
- Wheel: `8f3b3c25` prefix; exact wheel/source comparison covers 116 application
  files and unchanged dependency metadata. All 37 new server tests also pass
  against the immutable candidate source.
- API cutover completed at 19:19 UTC. Worker independent verification at
  19:21:36 UTC confirms six active services, five fresh worker heartbeats,
  11 host sources / 10 mounted sources (including the transport module),
  unchanged configuration/image/profile and 52 exactly preserved fingerprints.
- API baseline: `feea5d1d74e7fdd507c662e78253f3bb4edf6dfe974656c7be62d4322f6ad5ee`.
- Worker baseline: `a94fe3077bb3c5ffe845e8bdf248e53decd7efa454bf5e6f74bec49c24a75108`.
- Independent worker receipt SHA-256:
  `a73321264afe5717a985051c1260f41b3df200f204a6f13797796d2f26804b72`.

The new goal `goal_3be0b8317daf4814a32536c8003bce25` (TODO-READINESS-0101)
was created with HTTP 201 and started with HTTP 200. Its objective and criteria
match the prior qualification except for the marker; maximum parallelism is 1.
It uses the Ubuntu server API and a supplied manual initial plan: this does not
establish iPhone connectivity or autonomous planner success.

## Observed qualification and bootstrap correction

The first generated revision contains only `index.html`. Five successive
revisions retain the same snapshot; intervening source reads do not count as
new test executions. The worker's pytest receipt has exit 5 with no tests. Its
compile receipt has exit 1 because the trusted harness finds no Python source,
before any linting or compilation. This combination incorrectly selected
general repair and duplicated HTML patch context instead of requiring a test.

The first actual model response took 183.946 seconds: 11.868 seconds loading,
57.016 prefill, and 114.987 generation, with 4,833 input / 1,280 output tokens.
Two later attempts reached the unchanged 240-second wall limit, with prompts
of 39,273 and 40,750 bytes. No terminal token counters were received for those
attempts. Configuring 64,000 tokens did not resolve this workflow failure and
these measurements do not demonstrate use of the whole context window.

Independent Playwright/Chromium checks on an exact copy of revision
`revision_c7ba4ef8b1cd4e438ffaa8ac6204a4b1` ran in the existing immutable Ubuntu
browser image. Five assertions pass, four fail: the failures expose two defects
and their persistence after reload. Toggling Bravo in the filtered active list
changes Alpha instead; a title `<b>marker</b>` is interpreted as HTML. This is
independent browser evidence, not tests generated or run by the project worker.
The isolated container was removed; no project contents were changed.

The follow-up worker change recognizes only the exact missing-source build
receipt alongside pytest's exact no-tests receipt, using the complete accepted
file inventory. Any Python file or `.py` directory, dependency failure, other
command/exit code or failed collection retains ordinary repair priority. Failed
receipts remain failed; real build/test execution still determines readiness.
The unsupported claim that dependency installation already succeeded was also
removed from the test-creation instruction.

- Full worker suite after the correction: **726 passed, 5 skipped**.
- Eleven focused bootstrap cases pass, including a Python file omitted from
  model context and a directory ending in `.py`.
- Same-input local comparison: messages **33,155 → 20,449 bytes**; schema
  **9,685 → 1,257 bytes**. Original requirements, source, research evidence,
  dependency context and both failed receipts are preserved exactly.
- Ruff, mypy and `git diff --check` pass.

The looping qualification was explicitly cancelled through the public API at
19:40:08 UTC; a separate GET confirmed cancellation. Five completed jobs and the
sixth cancelled job remain recorded. Read-only follow-up confirms no active
job/node, descendant or pending automatic continuation. This is an interrupted
qualification, not an autonomous completion.

Bootstrap implementation commit `27b4705ea1fec8e29b1ad068771f93c3959e8fc8`
is pushed and deployed as worker `project-bootstrap-beb1785dbd38c4208414`.
Only `project_worker.py` changes in the runtime. Independent verification at
19:47:14 UTC confirms six active services, five fresh worker heartbeats,
11 exact host sources / 10 mounted sources, unchanged runtime configuration,
and 52 strictly identical protected fingerprints. The API release is unchanged.

- Fresh deployment baseline:
  `f83fc370ce7b72aff63b28e3bdeb529176eebc2606a11d268e181163bf498257`.
- Independent verification receipt SHA-256:
  `4254300221425e38f9612177bec7610f7619274112610e7c8e4b176ee5c80f83`.

The subsequent qualification `goal_db24e06625744158a194adadb79e4ed3`
(TODO-BOOTSTRAP-0101) was created with HTTP 201. It has the same objective,
criteria, limits and manual initial plan, except for its unique marker; no
corrected application or independently authored test is injected into it.
It was started with HTTP 200 and reached the targeted bootstrap branch:

- First model call: **185.702 s**, 20,494 message bytes, 4,829 input / 1,092
  output tokens; it produced only `index.html` (3,271 bytes).
- Second model call: **236.632 s**, 26,284 message bytes / 1,507 schema bytes,
  6,589 input / 987 output tokens. It produced `tests/test_todo.py` rather than
  repeatedly patching the HTML. The targeted creation behavior is now observed
  through the real server API, not just the local regression fixture.
- Real dependency preflight identified missing Flask and provided its pinned
  catalogue recipe to the next job. It executed **zero tests**. This proves
  detection and transmission, not successful autonomous dependency repair.
- The generated test includes explicit `--no-sandbox` despite the objective,
  does not set the requested browser/driver paths, and has fixture/lifecycle
  defects. It is not an acceptable test suite.
- Independent Playwright/Chromium checks on the new HTML again find the same
  two UI defects: five assertions pass and four fail (including persistence).

The qualification was cancelled through the public API at 19:57:57 UTC before
further execution. Read-only verification at 19:59:33 confirms two completed
jobs, the third cancelled, no descendant or pending continuation, and no later
revision/check. The third model call was cancelled after 213.818 s without any
content; its request had grown to 48,474 message bytes / 14,194 schema bytes.
No worker-launched browser is evidenced in the recorded operations. This is
not exhaustive process telemetry; the independent browser run is separate.

The test confirms the bootstrap correction and exposes a further adherence
gap: the browser Docker profile does not inspect Chromium options in generated
tests. A scoped static preflight now rejects recognized
explicit sandbox-disabling calls before installation/execution and returns a
repair diagnostic. It must not be described as enforcement against dynamically
constructed arguments or arbitrary generated code.

## Explicit browser configuration preflight

The worker now checks recognized Python Selenium and Playwright calls before
any install/build/test subprocess when the browser profile is enabled. It
resolves supported imports/aliases and unambiguous bindings, rather than
matching an argument name anywhere in the source. A rejection preserves the
proposed files and becomes a failed `swarmer project-checks` receipt with
`browser_sandbox_disabled`, path and line, and zero executed tests. The normal
repair loop receives this diagnostic; source is not silently rewritten.

This remains a scoped adherence check. Classes, ambiguous/rebound names,
dynamic arguments and unsupported forms are not established as safe. Container
isolation stays unchanged; neither an ignored expression nor passing this
preflight proves that an arbitrary browser process is sandboxed.

Local validation: **747 passed, 5 skipped** across the worker suite; **21**
targeted policy tests cover actual known call forms, false positives, aliases,
shadowing, bounded failure handling, no subprocess on rejection, and propagation
to a failed project check. Ruff, mypy and `git diff --check` pass.

Implementation commit `fe08512318b3f0cf933eb921dfabdfb850e4e03b` is pushed and
deployed as worker `project-browser-policy-d59be8bf3cf4286d441d`. Only
`runtime.py` changes against the bootstrap predecessor. Independent verification
at 20:16:09 UTC confirms 11 host sources / 10 mounted sources, six active
services, five fresh heartbeats, unchanged API/schema/runtime configuration,
and 52 strictly identical protected fingerprints. No database restoration or
new model invocation was needed for this deployment.

- Fresh baseline:
  `b27aa3a8f5f5f95b718eb8185b5a44857ba84d287d11bebb8f4428ff1309ddd7`.
- Independent verification receipt SHA-256:
  `5821ddb21d7839c4562e52a480847107dcb5678146de35a261e96abb2a92d081`.

The single static probe passed at 20:17:26 UTC against the deployed runtime
SHA-256 `4954e8e191521ef610c2ce9c5544836d3824bc1c0052941cbe1c5e3c168ea71c`.
The exact generated revision `5b4b539f` test was rejected at
`tests/test_todo.py:40` with `browser_sandbox_disabled`. No project code,
browser, child process or model was executed, and no backend record changed.
This is deployed-code validation against a real artifact, not a new end-to-end
repair or browser run.

No complete application success, autonomous dependency repair or iPhone runtime
proof is claimed by these deployments. The remaining qualification must correct
the test configuration/lifecycle, repair the declared Flask dependency, and
then expose/fix the two observed application defects through actual checks.
The 64,000-token setting remains active, but the observed inputs do not fill
that window and therefore do not establish full-window performance or quality.
