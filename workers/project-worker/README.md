# Project worker

`code.build_project` performs one local model call per leased job. The control
plane reserves that call before dispatch, persists the full resulting project,
and dispatches a new job for each edit/repair. The worker never retries model
calls or runs an opaque multi-call coding CLI. Clarifications return a question
without running code. User replies and previous files arrive in the next payload.

The model returns incremental replacements, exact text patches and deletions.
Each call changes at most three paths. Up to eight patches may replace unique
nonempty spans in the current base, with 8KB limits on each old/new string.
Every generation writes at most one small complete file. Short patches and
deletions retain the three-path allowance; later iterations build remaining modules.
Messages use one sentence, with a concise milestone plan and brief run instructions.
Ambiguous, overlapping or conflicting changes reject the entire batch; matching
never normalizes whitespace or guesses the intended source. The worker validates
paths and bounds, verifies the base snapshot hash, preserves every unmentioned file, and
returns the cumulative snapshot. A maximum of 80 files, 64KB each and 1MB total applies.
The model sees a bounded selection of complete source files or labelled fragments
plus the full file manifest; omitted files remain in the result. Source blocks
preserve exact whitespace. The generation grammar separates reading from changing
files and offers only unique spans from the final visible source selection
(up to 8 files, 24 choices per file and 12KB of source choices). The prompt displays
their real line coordinates and revision-bound span IDs, with at most 8KB of
address metadata inside the overall prompt limit. The model selects an ID and
writes new code; the worker resolves the exact old source from that request.
Each offered ID also has a raw `PATCH_TARGET` preview delimiting precisely what
the replacement covers. Those previews count toward the 22KB prompt limit.
Exact project traceback lines take priority; a duplicated line expands only to
the shortest available unique visible context within the 8KB patch limit.
When that old span ends with CRLF, LF or CR and a nonempty replacement omits its
terminal newline, the editor appends the original separator to preserve the next
unselected line. Empty deletions, explicit replacement newlines and unterminated
EOF spans are unchanged. Resulting sizes still undergo strict validation.
Unknown, stale or incorrectly targeted IDs fail closed. CRLF, LF, CR, Unicode and
partial boundary lines retain their original source representation. Local
validation still checks every proposed patch and cumulative snapshot.
An exact quoted Python function name in a diagnostic may select its existing
AST function for context. A whole-function span is offered only when unique,
within the byte limit, and wholly visible; decorators remain outside that span.
Ambiguous duplicate function names do not select an automatic target. Source line
coordinates count physical CRLF, LF and CR, without treating Unicode separators
inside strings as Python source lines.

## Operator setup

Install the official `qwen3-coder:30b-a3b-q4_K_M` model, then run `./setup_model.sh`
to verify its pinned upstream digest and create the dedicated local alias
`swarmer-project-qwen3-coder:30b-32k-06c1097e`. The script requires upstream digest
`06c1097efce0431c2045fe7b2e5108366e43bee1b4603a7aded8f21689e90bca`
and prints the alias's own digest for release evidence. It does not run inference.
Then run `./setup_runtime.sh` on the Linux
Docker host. It builds a combined
Python 3.12/Node 22 runtime from the two immutable upstream digests in Dockerfile,
with pinned pytest and Ruff. Copy the printed **image ID** into
`MONGARS_PROJECT_RUNTIME_IMAGE`. Mutable tags are rejected by the worker.
The worker itself requires only Python 3.12+ and the fixed sibling
`code-worker`/`file-worker` protocol files. Run `python3 project_worker.py` under
an unprivileged operator account with Docker access and the environment variables
documented in `.env.example`. Registration uses the normal operator enrollment
flow with `agent-card.json`; do not send its credential to a model or container.
Use `launch_sandboxed.py` for the operator service: set
`MONGARS_PROJECT_SCRATCH_DIR` to a dedicated real operator-owned 0700 directory
named `swarmer-project-worker-<uid>` (for example
`/var/tmp/swarmer-project-worker-1000`). The launcher preserves that exact absolute
path for Docker bind mounts, hides host home, and makes worker source read-only.

The provider uses native Ollama `/api/chat` on the validated loopback origin,
with `num_ctx=32768`, `num_predict=2000`, and `keep_alive=10m`. Its stable system
prefix supports Ollama's existing KV reuse; there is no invented cache-hit API or
cloud fallback. Total prompt text is bounded to 22KB of UTF8, reserving output and
message framing within 32K tokens. Numeric prompt/evaluation/load timings are
available on the generator for diagnosis; token counts alone are not cache-hit
proof. CPU-only semantic embedding retrieval is supplied by the control plane as
up to 4 bounded historical hints; recent user instructions and source/checks take
precedence. The worker never performs a hidden second model/embedding call.
The candidate Qwen3-Coder profile uses non-thinking inference with temperature 0.7,
top_p 0.8, top_k 20 and repetition penalty 1.05, following the
[official model card](https://huggingface.co/Qwen/Qwen3-Coder-30B-A3B-Instruct).
The project-only request timeout defaults to 240 seconds and remains configurable
between 30 and 240 seconds, within the 600-second worker operation limit. Model
selection is a candidate configuration; passing unit checks does not establish
project quality. Release acceptance requires actual independent application tests.

`focus_paths` requests a separately charged read iteration for omitted files.
Complete focused files are prioritized; oversized files are explicitly labelled
fragments and never treated as safe full-file replacements. Exact patches can
change visible spans while preserving the rest of the file. Error context remains
visible as a fragment when the final prompt budget cannot hold a complete file.
Existing projects retain recent user decisions and the latest assistant progress,
compacting earlier assistant repetition and successful command logs first. The
optional address catalog shrinks before complete source is demoted to fragments.
Candidates are shared across files in rounds, prioritizing focused and relevant
source. A focused read rotates the candidate region even when the full file fits.
Repeated focus remains
subject to the goal budget. Invalid model edits are rejected without altering the
snapshot; a safe diagnostic guides the next job, preserving previous real checks.
A model timeout also returns an unchanged snapshot with a fixed diagnostic asking
for a smaller complete batch. The next attempt is a new, separately charged job;
there is no retry inside the timed-out job and no fabricated check receipt.
Connection failures, rejected HTTP configuration and service unavailability are
distinct fixed transport categories and remain failed jobs. Logs omit raw error
bodies and endpoints. Goal and worker time/call budgets remain unchanged.

Docker access is operator authority. The generated project never receives the
Docker socket, host home, control-plane environment, agent credential, model
endpoint, or production workspace. Scratch directories are private and temporary;
only source snapshots and bounded runner receipts return to the control plane.

## Checks and dependencies

Python projects provide pytest-compatible tests and may declare exact
`name==version` public PyPI dependencies in requirements.txt. Only wheels are
accepted; source builds, URL/git/local dependencies and custom indexes are denied.
Node projects provide exact package versions, an npm `build` script and tests for
Node's built-in `node --test` runner. Both profiles run for `python_node`.
Dependency installation uses sanitized manifests, `npm --ignore-scripts`, and a
disposable internal Docker network. A credential-free proxy allows CONNECT only
to public IPs for pypi.org, files.pythonhosted.org and registry.npmjs.org. No project
source is mounted into the dependency-install container. Package lifecycle
scripts, non-registry sources and network-dependent tests are unsupported.

Each build/test runs in a fresh container with **network=none**, a read-only root,
read-only input source/dependencies, a private tmpfs working copy, no capabilities,
no privilege escalation, and CPU/memory/process/file/output/time bounds. Check
commands are fixed profiles; model requests cannot supply a shell or host command.
Exact `python -m pip install -r requirements.txt`, `pip install -r requirements.txt`,
`npm install` and `npm ci` requests select the already automatic sanitized
dependency stage. Extra arguments, alternate files and registry overrides are rejected.
Cancellation/lease loss removes active containers and suppresses stale results.

The runtime records actual exit codes and nonempty test execution counts. Ready
projects require all checks passing, a README, a plan and run instructions.
Generated tests remain project-authored evidence: passing checks do not prove
security, complete requirements, deployment, or a running production service.
Failed checks return to the next iteration. Application into the control plane's
managed workspace still requires the user's reviewed revision approval.

## Verification

Run `server/.venv/bin/pytest -q workers/project-worker` from the repository root.
To include real Docker tests, set `MONGARS_PROJECT_TEST_IMAGE` to the built image
ID. Those tests use disposable projects/networks/containers and no production
database, service, or goal. The runtime image build and live local-model acceptance
are separate evidence from these unit checks.
