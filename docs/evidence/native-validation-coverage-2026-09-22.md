# Native validation coverage and tool availability

## Observed defect

A project containing native source could retain successful Python/npm receipts
and claim completion even though the project runtime supports only `python`,
`node` and `python_node`. Repeating those checks does not validate Swift or iOS.
The existing no-progress guard correctly pauses repeated unchanged iterations,
but its generic pause does not identify this missing validation capability.

This correction concerns the generic runtime and evidence model. It does not
change, resume, compile or execute the user's project. Test sources are small
benign fixtures. Historical project files, hashes and check receipts must remain
unchanged.

## Scope of the guard

Detect native requirements from snapshot paths ending in `.swift`, or containing
an `.xcodeproj` / `.xcworkspace` path component, ignoring case. Mentions in README
text do not change runtime classification. This covers native Swift/Xcode
snapshots; it is not a complete detector for every programming language.

- Existing native snapshots must stop before another model call or Python/npm
  execution, preserving their source and old receipts.
- A first native proposal may be retained as a draft, with no inherited check
  receipts attached to changed files and no claim of native validation.
- The server must pause for the missing capability without inventing a user
  question or file-write approval, and must not promote irrelevant checks into
  native readiness.
- Historical snapshots must remain readable. Read projections and new apply
  requests must not present an old native snapshot as validated by Python/npm.
- A file-write receipt must remain a file-write receipt, not become proof of a
  native build or test.

## Tool inventory measured during this change

Five workers had fresh production heartbeats: project development, web research,
writing, workspace reading and code review. Project build/test commands execute
inside the restricted Docker runner and appear in `agent_jobs` check receipts;
they do not create separate `ExecutionEngine.tool_calls` records. Therefore an
empty tool-call section alone does not establish that no checks ran.

The deployed API at the beginning of this change exposed 26 descriptive roles
and 79 capabilities: 10 worker, 6 iPhone and 63 planned. Main contained 30 roles
and 94 capabilities. No Swift, SQLite, CRM or document specialist worker was
registered in production, and its specialist capability whitelist was not yet
deployed. An old Python-generation worker labelled online had an approximately
11.7-day-old heartbeat and was not usable. Catalogue entries and static status
alone are not evidence of a working executor.

The Swift worker implementation exists locally. On the iMac, **16 tests passed**
in 25.40 seconds, including actual compilation and one XCTest on a small known
Swift package using the installed Apple toolchain. This proves local execution
of that fixture. It does not prove production registration, project routing,
an iOS application build or a physical iPhone test.

## Verification and rollout

Worker regressions fail before the guard and pass after it. Main: **340 passed,
5 skipped**; isolated production backport: **336 passed, 5 skipped**. Ruff and
strict mypy passed for both. Four pre-existing context tests account for the
count difference between main and the production backport.

Server main: **323 targeted tests passed**, including **21 new native-coverage
cases**. Independent review found no blocking issue and reran 167 tests. Ruff,
compileall and mypy passed. The production API backport passed **180 tests**,
Ruff and mypy. The first backport test invocation imported the editable main
package and produced 20 configuration-mismatch failures; rerunning with an
explicit backport `PYTHONPATH`, after confirming both package and service import
paths, passed all 180 tests. That first run is not counted as backport evidence.

Four benign guard probes passed locally and against the staged Ubuntu worker:
no model or check-runner call, unchanged source digest and preserved historical
receipts. These are deterministic runtime guard checks, not inference or native
compilation tests. The rollout helpers passed **75 fault-injection tests**.

The API candidate wheel contains 68 matching application files. A private-copy
database initialization check preserved schema 24 and all 57 tables, with no
live migration. No native executor was registered as part of this guard.

## Production rollout

Both supervised cutovers completed, followed by separate read-only verification:

- Worker commit `ac093d47729610456e59486fd350caae4b78c870`, runtime source SHA256
  `837a15e5fe86eb8f2f675f3a8186c9b05e8cd0de63f80db678d25a38d98aea7b`.
  Independently verified at `2026-09-23T01:17:39Z`.
- API commit `29d442462de164233f0637e72fba9d18fa645f51`, wheel SHA256
  `674c0582dc8a8d4cf3dd59670bcfad8c539cf53867f9be23eaf6efc6c8eb27ca`.
  Independently verified at `2026-09-23T01:19:11Z`.

The API and all five workers had fresh running processes and worker heartbeats.
The second cutover preserved the new project-worker binding, all worker source
inventories, credentials, model configuration and runtime environments. All
**35 protected history fingerprints**, including **278 project revisions**, were
unchanged. No user goal was resumed, no generated project code was executed and
no database was restored. Health checks passed. This was a backend rollout;
there was no new TestFlight upload or physical iPhone test.

Private baselines and receipts are stored under
`~/Library/Logs/SwarmerDeploy/native-validation-20260922/`, with remote evidence
in the corresponding `native-validation-worker-20260922` and
`native-validation-api-20260922` qualification directories. Cutover receipt hashes:

- Worker: `72fe013d528c882d58f6b1c3274b23ab8ce3ad0e216a3d9b471d51c2117135c8`.
- API: `6fc8480ff235b8d0e1b0e8ce6d277fc2e96e7d1732cc0a94a57d748c9a605d34`.

## Separate context-design discussion

Root and nested `AGENTS.md` files, scoped lazy reading, and revisioned agent notes
were discussed as a future context layer. They are not implemented or claimed
as validated by this change. Human instructions should remain distinguishable
from model-authored observations; neither should replace source/check evidence.

File creation, replacement and exact revision-bound patches already exist.
`focus_paths` selects source content for the next prompt, but the protocol still
transports the complete snapshot. A future scoped-guidance layer should resolve
root and ancestor `AGENTS.md` files from the accepted snapshot, exclude sibling
folders, retain the source revision and hash, and load applicable instructions
before accepting edits. Generated observations belong in separate notes with
references to their evidence. Actual network-lazy file ranges require a separate
protocol extension; selective prompt inclusion alone is not that feature.
