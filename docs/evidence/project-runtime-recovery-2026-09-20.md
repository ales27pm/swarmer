# Project runtime recovery — 20 September 2026

## Observed failure

Read-only observation of the Ubuntu control plane and project worker found two
consecutive model calls reaching the 240-second wall limit. Neither response
produced an accepted edit. The worker preserved the previous file and validation
receipts, then paused the goal rather than charging another automatic call.

The last actual Node checks failed because `package.json` was absent and
`node --test` discovered no tests. A completed worker job and a newly persisted
revision did not imply a successful build or newly executed checks.

The iOS conversation rendered this technical pause as “Une précision est
demandée” and “Répondre à la question”, even though the runtime diagnostic did
not ask a substantive question.

## Performance evidence

Ollama service timings, read on 21 September UTC during the same Montreal-evening
session:

| Call completed (UTC) | Prompt work | Output work |
| --- | --- | --- |
| 23:44:55 | 1,717 tokens, 25.47 s | 103 tokens, 21.44 s; 4.80 tokens/s |
| 23:48:09 | 609 new tokens, 13.70 s; 2,056-token input | 439 tokens, 111.93 s; 3.92 tokens/s |
| 23:52:13 | 7,163-token input | Aborted at the 240 s wall; no terminal throughput receipt |
| 23:56:15 | 7,267-token input | Aborted at the 240 s wall; no terminal throughput receipt |

The selected coding model was
`swarmer-project-qwen3-coder-heretic:30b-32k-d2d985e`, with a 32,768-token context.
Ollama reported about 22.08 GB resident model allocation, including 6.26 GB in VRAM.
The host had an RTX 2070 with 8 GB VRAM and about 31 GiB system RAM. A later
snapshot showed 27 GiB RAM used and the 8 GiB swap allocation fully used.
These resource observations suggest pressure; they do not separately establish
how much of either timed-out call was spent loading, prefilling, or decoding.

At the measured output rate, a 2,000-token response alone would exceed the
240-second model budget. A smaller response and less repeated repair context
are justified; extending the timeout or accepting truncated JSON is not evidence
of a successful repair.

## Qualification boundary

The diagnostic observations above used read-only backend access. They did not
submit project messages or execute generated project code. Source qualification,
deployment, installation and later physical-app results are separate evidence.
No successful end-to-end repair of the user's project is claimed here.

## Implemented recovery

After a timed-out repair with failed check receipts, the worker now requests one
complete small edit, one addressed patch, or one focused source read. The response
is bounded to 512 tokens and 800 source characters. The worker retains the plan,
files and normal validation contract. The 240-second wall limit, single call per
job, and pause after consecutive timeouts remain unchanged. Recovery targets a
10 KB prompt while preserving valid user input under the existing hard limit.
Timeout logging records numeric transport timings and sizes without source text.

The mobile UI distinguishes the exact runtime-owned repeated-timeout diagnostic
from a real clarification question. Existing pending-message IDs, idempotent
reply handling and workspace approval behavior remain intact.

## Dependency cache failure

The user's next reply produced `package.json` at 00:13:10 UTC. Dependency
installation then failed with `ENOSPC` (exit 228) after 50.845 seconds. The host had
about 282 GB available; npm was writing its cache to the runner's 256 MiB `/tmp`
tmpfs. The runner now explicitly places npm's cache in the existing private,
per-job dependency scratch directory, which is deleted with the job's scratch.
No shared cache, new network destination, or install-script execution is added.

An isolated Docker probe used the existing pinned runtime image, read-only root,
no network, the same 256 MiB `/tmp`, and disposable dependency scratch. One-MiB
cache entries failed after 256 entries under `/tmp` with errno 28. The private
dependency location accepted all 270 entries; npm reported the intended cache
path. The probe exited successfully and removed its temporary data. This proves
the storage-path correction, not successful installation of the user's project.

## Verification before publication

- Worker suite as the ordinary Mac user: 262 passed, 5 Docker-dependent skipped.
- Four affected mobile suites: 121 passed.
- Worker Ruff and mypy, mobile TypeScript and targeted ESLint passed.
- Added regression coverage preserves missing-manifest priority, strict small
  repair acceptance/rejection, Unicode user input, cache cleanup after failure,
  real clarification wording, pending-message IDs, and retry idempotency.
- Debug iPhone build `20260921001950`: `xcodebuild` exited 0.
- All 163 captured mobile source hashes remained unchanged during compilation.
- Strict code-signature/profile/device checks and all 7 Mach-O dependency checks
  passed. The embedded JavaScript bundle is present. Installation and live API
  behavior require separate receipts; compilation alone does not establish them.

Private build receipts and the signed Debug IPA are retained under
`~/Library/Developer/Xcode/SwarmerAPIQualifications/20260921001950`.
