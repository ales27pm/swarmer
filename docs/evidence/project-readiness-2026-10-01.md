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

Independent Selenium checks on an exact copy of revision
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

The bootstrap correction is locally verified; deployment and the next real
qualification remain pending. The current looping qualification is being
stopped explicitly, with its evidence preserved, rather than counted as an
autonomous completion.
