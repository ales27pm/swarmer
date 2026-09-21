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

- Worker suite as the ordinary Mac user: 267 passed, 5 Docker-dependent skipped.
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


## Physical install and compact-model canary

The tested source commit is `937958db1a2c10f1fd9f44aa9cef10761d868cdf`,
published to both `origin/main` and `vibecode/main`.

CoreDevice installed and then independently listed build `20260921001950` on
the paired iPhone 16 Pro. The temporary authenticated IPv6 application API
returned `app.status: succeeded` at 00:31:10.070 UTC, confirming the same native
and configured build, active app and retained pairing. No model was loaded and
no business action was submitted. The changed pause text was covered by tests;
it was not separately observed visually on the phone in this qualification.

A single benign synthetic Node repair used the committed candidate and configured
Ubuntu coding model after a read-only idle check. At 00:33:17 UTC it returned an
accepted, complete `package.json` in 88.955 seconds: 2,985 input tokens and 110
output tokens, within the 512-token cap. The prompt was 8,274 bytes; the schema
was 7,555 bytes. Model-reported load, prefill and generation times were 17.257,
43.874 and 27.740 seconds. The manifest had no dependencies and the expected
`node --check app.mjs` build command. The fixture's plan and existing files were
preserved. No user project source or generated application code was read, and
no project database mutation was performed by the canary. This demonstrates a
successful small repair under the time limit, not end-to-end user project success.

The final worker qualification additionally covers the first manual resume after
an exact timeout diagnostic: the latest assistant timeout followed only by user
replies keeps the 512-token recovery limit immediately. An ordinary assistant
response exits this mode. Consecutive-timeout accounting remains separate, so a
new user reply resets the pause counter. Five additional regression cases passed;
this predicate-only follow-up did not repeat the live model canary.

## Worker deployment

Final worker source commit `c4c6bf62168ad1dcc51195b49794725b993906db` was
published to both remotes and deployed at 00:43:09 UTC to the immutable release
`c4c6bf62168ad1dcc51195b49794725b993906db-c884b988cae6`.
Only `project_worker.py` and `runtime.py` changed in the eight-file runtime.

The reviewed operator helper passed 17 lifecycle/barrier tests and 7 self-checks.
A first read-only inspection stopped on unordered systemd dependency text; the
helper now canonicalizes only those dependency sets before comparison. A fresh
baseline confirmed unchanged database, protected configuration, sources and PIDs.
For cutover, the API cgroup briefly stopped accepting work while the final idle
fence was checked and the worker switched. A remote recovery timer protected
against operator disconnect; it was cleared after services were healthy. The API
process was not restarted. Its PID remained 541717, freezer state returned to
`running`, and local/HTTPS health plus authenticated worker heartbeat passed.

All protected database fingerprints matched after deployment. The goal remained
paused at revision 7, with two files, 16 model calls and no active job. No user
project was resumed automatically. Existing drop-ins, credentials and prior
release were preserved; the database was neither restored nor reset.

Private deployment receipt SHA256:
`5d66d54b5ea9c2dafc6d7c258dfd9939305789035738a5a678587e1d797b0aeb`.
A subsequent real user-project repair is still unverified.
