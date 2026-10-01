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

At source freeze, these changes are not yet deployed. No new qualification has
started. The previous failed qualification and its receipts remain intact.
The planned new trial uses the Ubuntu server API and a manual initial plan;
it will not establish iPhone connectivity or autonomous planner success.
