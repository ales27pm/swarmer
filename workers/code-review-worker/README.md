# monGARS code-review worker

This standalone worker offers four narrowly scoped, read-only skills:

- `code_review.git_status`
- `code_review.git_diff`
- `code_review.git_show`
- `code_review.static_analysis`

It preserves the v0.9 claim, lease-generation, heartbeat, fencing, and terminal
result protocol. Every result is marked `content_trust: untrusted` because
repository contents, commit messages, filenames, and analyzer diagnostics can
all be attacker-controlled.

## Policy boundary

Jobs never provide a command, executable, URL, environment, or working
directory. Git and Ruff executable paths are operator configuration. The worker
constructs fixed argument arrays and never invokes a shell. Git prompts, pagers,
hooks, fsmonitor, optional index locks, global/system configuration, external
diffs, text conversion, and lazy object fetching are disabled. Ruff runs with
`--isolated`, `--no-cache`, and `--no-fix` against explicitly selected regular
Python files.

Subprocesses receive a minimal environment that excludes the agent credential.
Git pathspecs are always literal, replacement objects are disabled, and `HEAD`
is resolved to one full object ID before a multi-command show or staged diff.
The index and selected worktree files are descriptor-checked and copied into a
private snapshot before diffing; Ruff receives only private descriptor-copied
files. A bounded, descriptor-relative pre/post manifest joins sequential file
copies into one generation and rejects same-inode edits or path swaps during
capture. Repository-root and `.git` directory identities are revalidated around
every command. Commit reads use a full immutable object ID, and the complete
Git metadata/object manifest is fenced around live content-addressed object
reads.

All commands have hard time and output limits. Protected credential-like paths,
symbolic links, hard-linked review files, external object stores, grafts, and
repository-local `filter` or `include` configuration are rejected or omitted.
This intentionally excludes linked worktrees and repositories that require a
local Git filter such as LFS. Snapshot files and the index are limited to 16
MiB each. A hostile process with concurrent write access can still race Git's
own object/configuration reads and restore state between manifest observations,
so production deployments must still mount `MONGARS_REVIEW_ROOT` read-only at
the operating-system/container boundary. The worker detects observed concurrent
mutation and never returns a known mixed snapshot; it is not a substitute for
an OS-enforced immutable source mount against a malicious same-UID writer.

## Job contracts

`code_review.git_status` accepts only `{}` and returns bounded structured
porcelain entries.

`code_review.git_diff` accepts:

```json
{"staged": false, "paths": ["src/module.py"], "context_lines": 3}
```

All fields are optional. An empty `paths` array discovers changed files, filters
protected paths, and reviews at most 100 files.

`code_review.git_show` accepts `revision` plus optional `paths` and
`context_lines`. Revision is exactly `HEAD` or a full 40/64-character hexadecimal
object ID; revision expressions and option-like values are rejected.

```json
{"revision": "HEAD", "paths": ["src/module.py"], "context_lines": 3}
```

`code_review.static_analysis` requires 1–50 explicit repository-relative,
regular, non-symlink `.py` files:

```json
{"paths": ["src/module.py", "tests/test_module.py"]}
```

## Configuration and run

Copy `.env.example` into a secret-management mechanism; do not commit populated
values. Register the agent through `POST /agents/register` with the four skills,
then place the returned one-time credential in the process environment.

```sh
python3 code_review_worker.py --once
```

The server must expose the existing authenticated agent heartbeat, claim, job
heartbeat, and job result endpoints. It must add all four skill identifiers to
dispatch validation and deterministic payload validation. None of these skills
should be added to automatic lease retry: a review can be recomputed by an
operator-created new job, while an expired generation remains fenced.

## Local verification

From this directory, using the repository server virtual environment:

```sh
../../server/.venv/bin/ruff format --check .
../../server/.venv/bin/ruff check .
../../server/.venv/bin/mypy --strict code_review_worker.py
../../server/.venv/bin/pytest -q
../../server/.venv/bin/bandit code_review_worker.py
```
